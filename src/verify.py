"""④ 검증 (설계서 4장, J-1~J-8): ③에서 고른 후보를 기존 논문이 이미 했는지 확인한다.

V1 검색어 3개 → V2 S2 검색 + ③ 참고문헌 → V3 임베딩 상위 15편 → V4 초록 카드 → V5 요소별 비교, J-4 순서로 최근접 3편
→ V6 본문 카드 (Studio B. 안 되면 초록 카드 유지) → V7 본문 카드로 다시 비교 → V8 판정 (코드, J-5) → V9 비평.
- 비교의 quote가 카드 필드의 인용에 없거나 카드 필드가 비면 그 요소는 unknown (J-1, J-3).
- V7은 본문 카드를 얻은 논문만 다시 비교한다. 초록 카드 그대로면 입력이 같으므로 V5 결과를 쓴다.
- 호출 수는 코드가 센다 (J-7). 단계에 들어가기 전에 상한을 넘을지 보고, 넘으면 멈춰 status "budget"으로 한 데까지 돌려준다.
  Solar는 논리 호출 수(chat_json 1회 = 1)이고 캐시된 초록 카드는 세지 않는다. S2 검색은 캐시 여부와 상관없이 1회로 센다.
- progress는 메인 스레드에서만 부르고 그 예외(서버의 취소)는 잡지 않는다.
"""
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import numpy as np
from pydantic import BaseModel, Field

from src import calllog, cards, config, llm, references, studio
from src.textnorm import contains

N_QUERIES = 3   # J-6 시작값
NARROW = 15     # J-6 시작값
NEAREST = 3     # J-6 본문 읽기 시작값
LIMITS = {"solar": 50, "s2": 15, "studio": 3}  # J-7. 임베딩은 세기만 한다
WORKERS = 4
EMBED_CHARS = 4_000
CANDIDATE_FIELDS = ("title", "summary", "gap", "approach", "evaluation")
ELEMENTS = ("problem", "method", "assumptions", "evaluation")
CORE = ("problem", "method")  # 판정에 쓰는 요소 (J-5)
MATCHES = ("same", "partial", "different")
STATUSES = ("defined", "undecided", "not_applicable")
REQUIREMENTS = ("problem_scope", "difference", "mechanism", "method_definition",
                "validation_design", "alternative_explanations", "failure_feasibility")
REVIEW_FIELDS = ("unsupported", "vague", "already_done", "suggestion")
FLAG_ABSTRACT = "초록 수준"
FLAG_UNKNOWN = "확인 못 한 요소 있음"
STRIP_QUOTES = " \t\n\"'“”‘’"


class Queries(BaseModel):
    queries: list[str] = Field(min_length=N_QUERIES, max_length=N_QUERIES)


class Element(BaseModel):
    match: str  # enum 대신 str로 받고 코드가 값을 확인한다
    reason: str
    quote: str


class Comparison(BaseModel):
    problem: Element
    method: Element
    assumptions: Element
    evaluation: Element


class Requirement(BaseModel):
    status: str
    note: str


class Requirements(BaseModel):
    problem_scope: Requirement
    difference: Requirement
    mechanism: Requirement
    method_definition: Requirement
    validation_design: Requirement
    alternative_explanations: Requirement
    failure_feasibility: Requirement


class Critique(BaseModel):
    requirements: Requirements
    unsupported: str
    vague: str
    already_done: str
    suggestion: str


class BudgetExceeded(Exception):
    def __init__(self, scope, used, limit, need):
        super().__init__(f"{scope} 호출 예산 초과: {used}회 사용, {need}회 더 필요, 상한 {limit}회")
        self.info = {"scope": scope, "used": used, "limit": limit, "need": need}


class Budget:
    """요청 하나의 호출 수 (J-7). 메인 스레드에서만 쓴다."""

    def __init__(self, limits=None):
        self.limits = {**LIMITS, **(limits or {})}
        self.used = {"solar": 0, "s2": 0, "studio": 0, "embed": 0}

    def take(self, kind, n=1):
        limit = self.limits.get(kind)
        if limit is not None and self.used[kind] + n > limit:
            raise BudgetExceeded(kind, self.used[kind], limit, n)
        self.used[kind] += n


def _s(v) -> str:
    if isinstance(v, str):
        return v.strip()
    return json.dumps(v, ensure_ascii=False) if v else ""


def candidate_text(topic) -> str:
    return "\n".join(f"{k}: {_s(topic.get(k))}" for k in CANDIDATE_FIELDS if _s(topic.get(k)))


def _card_text(card, fields=ELEMENTS) -> str:
    return "\n".join(f"{f}: {card[f]['text']}\n  quote: {card[f]['quote']}" if card[f]["quote"] else f"{f}: (empty)"
                     for f in fields)


def _chat(name, user, schema):
    system = (config.PROMPT_DIR / f"{name}.md").read_text("utf-8")
    return llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], schema)


def _parallel(fn, items, on_done, workers=WORKERS):
    """fn을 스레드로 돌리고 하나 끝날 때마다 메인 스레드에서 on_done(n, total)을 부른다.
    on_done(progress)이 취소 예외를 던지면 시작 안 한 작업은 버리고, 돌고 있는 것(최대 workers개)만 기다린다.
    verify가 끝난 뒤에 남아 도는 스레드가 없게 하기 위해서다. 작업자는 calllog.bind로 호출 로그 문맥을 물려받는다."""
    out = [None] * len(items)
    if not items:
        return out
    ex = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    try:
        run = calllog.bind(fn)
        futures = {ex.submit(run, x): i for i, x in enumerate(items)}
        for n, f in enumerate(as_completed(futures), 1):
            out[futures[f]] = f.result()
            on_done(n, len(items))
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return out


def _cached(folder, paper, suffix) -> bool:
    return (folder / f"{cards._key(paper)}{suffix}").exists()


def _paper(row, source, **extra) -> dict:
    return {"paper_id": (row.get("paper_id") or row.get("paperId") or "").strip(), "title": row.get("title") or "",
            "abstract": row.get("abstract") or "", "year": row.get("year"), "url": row.get("url") or "",
            "source": source, **extra}


def _merge(papers) -> list[dict]:
    """초록 없는 논문과 중복(paper_id, 제목)을 뺀다. 먼저 온 것(③ 참고문헌)을 남긴다."""
    out, ids, titles = [], set(), set()
    for p in papers:
        title = re.sub(r"\W+", " ", p["title"].lower()).strip()
        if not p["paper_id"] or not p["abstract"].strip() or p["paper_id"] in ids or (title and title in titles):
            continue
        ids.add(p["paper_id"])
        titles.add(title)
        out.append(p)
    return out


def _narrow(topic, papers, budget) -> list[dict]:
    """V3: 후보와 코사인 유사도 상위 NARROW편. 비용 필터일 뿐 판정에 쓰지 않는다."""
    if len(papers) <= NARROW:
        return [{**p, "similarity": None} for p in papers]
    budget.take("embed", 1 + -(-len(papers) // 50))
    q = np.array(llm.embed([candidate_text(topic)[:EMBED_CHARS]], config.EMBED_QUERY_MODEL)[0])
    d = np.array(llm.embed([f"{p['title']}. {p['abstract']}"[:EMBED_CHARS] for p in papers], config.EMBED_PASSAGE_MODEL))
    sims = d @ q / (np.linalg.norm(d, axis=1) * np.linalg.norm(q) + 1e-12)
    return [{**papers[i], "similarity": round(float(sims[i]), 4)} for i in np.argsort(-sims, kind="stable")[:NARROW]]


def _abstract_card(paper):
    try:
        return cards.abstract_card(paper)
    except llm.LLMError:  # 형식 재시도까지 실패한 논문은 비교에서 뺀다
        return None


def check(card, out) -> dict:
    """Solar 비교 결과 → 요소별 {match, reason, quote, page, level, check} (J-1, J-3).
    카드 필드가 비었거나 quote가 카드 필드의 인용에 없거나 match 값이 틀리면 unknown. out이 None이면 호출 실패."""
    result = {}
    for f in ELEMENTS:
        field, e = card[f], getattr(out, f) if out else None
        quote = e.quote.strip(STRIP_QUOTES) if e else ""
        el = {"match": e.match.strip().lower() if e else "unknown", "reason": e.reason.strip() if e else "",
              "quote": quote, "page": field.get("page"), "level": card["level"], "check": "ok"}
        if not field["quote"].strip():
            el.update(match="unknown", reason="논문 카드에 이 요소가 없습니다.", quote="", page=None, check="empty_card")
        elif e is None:
            el.update(reason="비교 호출이 실패했습니다.", quote="", check="call_failed")
        elif el["match"] not in MATCHES:
            el.update(match="unknown", quote="", check="invalid_match")
        elif not (contains(field["quote"], quote) or contains(field["quote"], quote.rstrip("."))):
            el.update(match="unknown", quote="", model_match=el["match"], check="quote_failed")
        result[f] = el
    return result


def _compare(topic, card) -> dict:
    """V5, V7 한 편 (Solar 1회)."""
    user = f"Research candidate:\n{candidate_text(topic)}\n\nPaper card: {card['title']}\n{_card_text(card)}"
    try:
        out = _chat("verify_compare", user, Comparison)
    except llm.LLMError:
        out = None
    return check(card, out)


def rank_key(p) -> tuple:
    """J-4: 문제·방법 same 수, 문제·방법 partial 수, 가정·평가 same 수, 최신 연도 (큰 쪽이 가깝다)."""
    m = [p["comparison"][f]["match"] for f in ELEMENTS]
    return m[:2].count("same"), m[:2].count("partial"), m[2:].count("same"), p.get("year") or 0


def _nearest(papers) -> list[dict]:
    """J-4 순서로 앞의 NEAREST편. 같으면 유사도 순서(입력 순서)를 지킨다."""
    top = sorted(papers, key=rank_key, reverse=True)[:NEAREST]
    for i, p in enumerate(top, 1):
        p["id"] = f"N{i}"
    return top


def judge(nearest) -> dict:
    """V8 판정 (J-5). 한 논문이 문제·방법 모두 same일 때만 solved. 여러 논문을 합쳐 채운 조건은 solved가 아니다."""
    core = [(p, tuple(p["comparison"][f]["match"] for f in CORE)) for p in nearest]
    solved = [p for p, m in core if m == ("same", "same")]
    partial = [p for p, m in core if "same" in m or m == ("partial", "partial")]
    verdict, basis = ("solved", solved) if solved else ("partial", partial) if partial else ("not_addressed", [])
    flags = []
    if any(p["card"]["level"] == "abstract" for p in nearest):
        flags.append(FLAG_ABSTRACT)
    if any("unknown" in m for _, m in core):
        flags.append(FLAG_UNKNOWN)
    return {"verdict": verdict, "basis": [p["paper_id"] for p in basis], "flags": flags}


def _studio_ready() -> bool:
    try:
        studio.agent_id()
        return True
    except studio.StudioUnavailable:
        return False


def _fulltext(paper, should_cancel):
    """V6 한 편 → (본문 카드 또는 None, 상태). None이면 초록 카드를 그대로 쓴다. 취소는 그대로 올린다."""
    try:
        card = cards.fulltext_card(paper, should_cancel=should_cancel)
    except studio.StudioCancelled:
        raise
    except studio.StudioUnavailable:
        return None, "studio_unavailable"
    except studio.StudioError as e:
        return None, f"studio_error: {str(e)[:200]}"
    return (card, "ok") if card else (None, "no_pdf")


def _critique(profile, problem, topic, nearest) -> dict:
    """V9 (Solar 1회). 참고문헌은 최근접 논문의 카드만 넣는다 (4.5)."""
    refs = "\n\n".join(f"[{p['id']}] {p['title']} ({p.get('year') or 'year unknown'}; read: {p['card']['level']})\n"
                       f"{_card_text(p['card'], cards.FIELDS)}" for p in nearest)
    context = json.dumps({"profile": profile, "selected_problem": (problem or {}).get("title")}, ensure_ascii=False)
    out = _chat("verify_critique", f"Researcher context:\n{context}\n\nResearch candidate:\n{candidate_text(topic)}"
                                   f"\n\nNearest papers (research cards):\n{refs}", Critique)
    req = {}
    for k in REQUIREMENTS:
        r = getattr(out.requirements, k)
        status = re.sub(r"[\s-]+", "_", r.status.strip().lower())
        req[k] = {"status": status if status in STATUSES else "undecided", "note": r.note.strip()}
    return {"requirements": req, **{k: getattr(out, k).strip() for k in REVIEW_FIELDS}}


def _run(s, budget, profile, problem, topic, refs, emit, should_cancel):
    """V1~V9. 결과는 s에 쌓는다 (예산에서 멈춰도 한 데까지 남도록)."""
    emit("queries", "검증 검색어 작성")
    budget.take("solar")
    plan = _chat("verify_queries", f"Research candidate:\n{candidate_text(topic)}", Queries)
    s["queries_raw"] = plan.queries
    # Solar가 7단어를 자주 쓴다. 버리지 않고 6단어로 자른다 (J-6)
    s["queries"] = list(dict.fromkeys(" ".join(q.split()[:6]) for q in plan.queries if len(q.split()) >= 3))
    if not s["queries"]:
        raise ValueError("검증 검색어 형식이 맞지 않습니다. 다시 시도해 주세요.")

    found = []
    for i, q in enumerate(s["queries"], 1):
        emit("search", f"논문 검색: {q}", current=i, total=len(s["queries"]))
        budget.take("s2")
        rows, used = references._search(q), q
        if not rows:  # ③과 같이 앞 두 단어로 한 번 더
            used = " ".join(q.split()[:2])
            budget.take("s2")
            rows = references._search(used)
        s["search"].append({"query": q, "retried_query": used if used != q else None, "returned": len(rows),
                            "missing_abstract": sum(not r.get("abstract") for r in rows)})
        found += [_paper(r, "search", query=used) for r in rows]
    s["papers"] = _merge([_paper(r, "references", ref_id=r.get("id")) for r in refs] + found)
    if not s["papers"]:
        raise ValueError("비교할 논문을 찾지 못했습니다. 참고문헌과 검색 결과에 초록이 있는 논문이 없습니다.")

    emit("narrow", f"후보와 가까운 논문 고르기 ({len(s['papers'])}편 중 {min(NARROW, len(s['papers']))}편)")
    s["narrowed"] = _narrow(topic, s["papers"], budget)

    todo = s["narrowed"]
    budget.take("solar", sum(not _cached(cards.ABSTRACT_CARD_DIR, p, ".json") for p in todo))
    emit("abstract_cards", f"초록 카드 작성 (0/{len(todo)})", current=0, total=len(todo))
    made = _parallel(_abstract_card, todo,
                     lambda n, t: emit("abstract_cards", f"초록 카드 작성 ({n}/{t})", current=n, total=t))
    s["card_failed"] = [p["paper_id"] for p, c in zip(todo, made) if c is None]
    papers = [{**p, "card": c} for p, c in zip(todo, made) if c]
    if not papers:
        raise llm.LLMError("초록 카드를 하나도 만들지 못했습니다")

    budget.take("solar", len(papers))
    emit("nearest", f"초록 카드와 요소별 비교 (0/{len(papers)})", current=0, total=len(papers))
    comps = _parallel(lambda p: _compare(topic, p["card"]), papers,
                      lambda n, t: emit("nearest", f"초록 카드와 요소별 비교 ({n}/{t})", current=n, total=t))
    for p, c in zip(papers, comps):
        p["comparison"] = c
        s["checks"].update(e["check"] for e in c.values())
    s["compared"] = [{"paper_id": p["paper_id"], "title": p["title"], "year": p["year"], "source": p["source"],
                      "similarity": p["similarity"], "rank": list(rank_key(p)),
                      **{f: p["comparison"][f]["match"] for f in ELEMENTS}}
                     for p in sorted(papers, key=rank_key, reverse=True)]
    nearest = s["nearest"] = _nearest(papers)

    ready = _studio_ready()
    jobs = [p for p in nearest if ready or _cached(cards.STUDIO_CARD_DIR, p, ".card.json")]
    new = sum(not _cached(cards.STUDIO_CARD_DIR, p, ".card.json") for p in jobs)
    budget.take("studio", new)
    budget.take("s2", new)  # fetch_pdf의 PDF 주소 조회
    emit("fulltext", f"본문 분석 중 (0/{len(jobs)})" if jobs else "Studio 에이전트가 없어 초록 카드로 비교합니다",
         current=0, total=len(jobs))
    done = _parallel(lambda p: _fulltext(p, should_cancel), jobs,
                     lambda n, t: emit("fulltext", f"본문 분석 중 ({n}/{t})", current=n, total=t), workers=NEAREST)
    results = {p["paper_id"]: r for p, r in zip(jobs, done)}
    upgraded = []
    for p in nearest:
        card, p["fulltext_status"] = results.get(p["paper_id"], (None, "studio_unavailable"))
        if card:
            p["abstract_comparison"], p["card"] = p["comparison"], card
            upgraded.append(p)

    budget.take("solar", len(upgraded))
    emit("compare", f"본문 카드로 다시 비교 (0/{len(upgraded)})" if upgraded else "본문 카드가 없어 초록 카드 비교를 씁니다",
         current=0, total=len(upgraded))
    comps = _parallel(lambda p: _compare(topic, p["card"]), upgraded,
                      lambda n, t: emit("compare", f"본문 카드로 다시 비교 ({n}/{t})", current=n, total=t))
    for p, c in zip(upgraded, comps):
        p["comparison"] = c
        s["checks"].update(e["check"] for e in c.values())
    nearest = s["nearest"] = _nearest(nearest)  # 본문 비교로 순서가 바뀔 수 있다

    s.update(judge(nearest))

    budget.take("solar")
    emit("critique", "비평 작성")
    try:
        s["critique"] = _critique(profile, problem, topic, nearest)
    except llm.LLMError as e:  # 판정은 이미 나왔으므로 비평만 비운다
        s["critique_error"] = str(e)[:300]


def _out(p) -> dict:
    """⑤에 넘길 최근접 논문 한 편."""
    return {"id": p["id"], "paper_id": p["paper_id"], "title": p["title"], "year": p["year"], "url": p["url"],
            "source": p["source"], "ref_id": p.get("ref_id"), "similarity": p["similarity"],
            "level": p["card"]["level"], "fulltext_status": p.get("fulltext_status"),
            "comparison": p["comparison"], "card": p["card"]}


def verify(profile, problem, topic, refs, progress=lambda message, **kw: None, should_cancel=None, limits=None) -> dict:
    """③ 후보 하나를 검증한다 (④).

    topic: ③ topics[] 하나 {id, title, summary, gap, approach, evaluation, support}. refs: ③ references[].
    progress(message, step=..., current=..., total=...): step은 queries, search, narrow, abstract_cards, nearest,
    fulltext, compare, critique. should_cancel: Studio 상태 확인을 멈출지 (cards.fulltext_card로 넘김).
    반환 (JSON으로 바꿀 수 있음): status "ok" | "budget", verdict, verdict_basis, flags, nearest, limitations, critique,
    scope, budget, trace. budget이면 그때까지 한 것만 있고 verdict나 critique가 None일 수 있다.
    예외: progress가 던진 것(취소), studio.StudioCancelled, llm.LLMError (V1, 또는 V4 전부 실패), S2 검색 실패,
          ValueError (검색어 형식, 비교할 논문 없음).
    """
    budget, t0, marks = Budget(limits), time.monotonic(), []
    s = {"queries": [], "search": [], "papers": [], "narrowed": [], "compared": [], "nearest": [],
         "card_failed": [], "checks": Counter()}

    def emit(step, message, **kw):
        if not marks or marks[-1][0] != step:
            marks.append((step, time.monotonic()))
        progress(message, step=step, **kw)

    try:
        _run(s, budget, profile, problem, topic, refs, emit, should_cancel)
        status = "ok"
    except BudgetExceeded as e:
        status, s["budget"] = "budget", e.info
    end = time.monotonic()
    nearest = [_out(p) for p in s["nearest"]]
    return {
        "status": status, "topic_id": topic.get("id"), "topic_title": topic.get("title"),
        "problem_id": (problem or {}).get("problem_id"),
        "verdict": s.get("verdict"), "verdict_basis": s.get("basis", []), "flags": s.get("flags", []),
        "nearest": nearest,
        "limitations": [{"id": p["id"], "paper_id": p["paper_id"], "title": p["title"], "level": p["level"],
                         **p["card"]["limitations"]} for p in nearest if p["card"]["limitations"]["quote"]],
        "critique": s.get("critique"),
        "scope": {"queries": s["queries"], "search_returned": sum(t["returned"] for t in s["search"]),
                  "searched": len(s["papers"]), "from_references": sum(p["source"] == "references" for p in s["papers"]),
                  "narrowed": len(s["narrowed"]), "fulltext_read": sum(p["level"] == "fulltext" for p in nearest),
                  "date": date.today().isoformat()},
        "budget": s.get("budget"),
        "trace": {"queries_raw": s.get("queries_raw"), "search": s["search"], "card_failed": s["card_failed"],
                  "compared": s["compared"],
                  "checks": dict(s["checks"]),
                  "fulltext": [{"paper_id": p["paper_id"], "status": p.get("fulltext_status")} for p in s["nearest"]],
                  "recompare": [{"paper_id": p["paper_id"],
                                 **{f: [p["abstract_comparison"][f]["match"], p["comparison"][f]["match"]] for f in ELEMENTS}}
                                for p in s["nearest"] if "abstract_comparison" in p],
                  "critique_error": s.get("critique_error"), "calls": budget.used, "limits": budget.limits,
                  "steps_sec": {step: round((marks[i + 1][1] if i + 1 < len(marks) else end) - t, 1)
                                for i, (step, t) in enumerate(marks)},
                  "elapsed_sec": round(end - t0, 1)},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
