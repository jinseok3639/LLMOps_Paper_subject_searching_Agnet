"""⑤ 확정: 저장된 ①·③·④ 결과로 최종 주제 카드를 조립한다 (설계서 5장). LLM은 부르지 않는다.

카드 항목 순서: 주제(topic), 출발 문제(problem), 연구 설계(design), 가장 가까운 연구(nearest), 판정(verdict),
빈틈(gaps), 비평(critique), 조사 범위(scope). source는 카드를 만든 선택 (화면이 카드가 낡았는지 본다).
④ 결과는 키가 빠져 있어도 되고, 아예 없으면 판정을 "검증 안 함"으로 둔다. to_markdown(card)은 내보내기용 (R-2).
"""
from datetime import date

from src import revise

ELEMENTS = (("problem", "문제"), ("method", "방법"), ("assumptions", "가정"), ("evaluation", "평가"))
MATCH = {"same": "같음", "partial": "부분", "different": "다름", "unknown": "확인 못 함"}
REQUIREMENTS = (("problem_scope", "문제와 범위"), ("difference", "기존 연구와의 차이"), ("mechanism", "핵심 메커니즘"),
                ("method_definition", "방법 정의"), ("validation_design", "검증 설계"),
                ("alternative_explanations", "대안 설명 배제"), ("failure_feasibility", "실패 조건과 실행 가능성"))
STATUS = {"defined": "명시됨", "undecided": "미정", "not_applicable": "해당 없음"}
REVIEW = (("unsupported", "근거 없는 주장"), ("vague", "검증을 막는 빈 정의·절차"), ("already_done", "이미 한 부분"),
          ("suggestion", "가장 중요한 개선"))
DESIGN = (("gap", "연구 공백 (gap)"), ("approach", "방법 (approach)"), ("evaluation", "평가 (evaluation)"))
VERDICT = {"solved": "이미 한 연구가 있음 (solved)", "partial": "일부 겹침 (partial)",
           "not_addressed": "같은 문제·방법을 한 논문을 찾지 못함 (not_addressed)",
           "not_verified": "검증 안 함", "none": "판정 없음"}
FLAG_NOTE = {"초록 수준": "본문을 읽지 못해 초록 카드로 비교한 논문이 있습니다. "
                       "초록에 없는 방법·평가 세부는 비교하지 못했으니 원문 링크에서 확인하세요.",
             "확인 못 한 요소 있음": "카드에 없거나 인용 대조에 실패해 판단하지 못한 문제·방법 요소가 있습니다."}
LEVEL = {"fulltext": "본문", "abstract": "초록"}
# ④ fulltext_status → 본문을 못 읽은 이유 (static/modules/verify.js UNREAD와 같게 맞춘다)
UNREAD = {"no_pdf: s2_failed": "논문 정보 조회 실패", "no_pdf: no_url": "공개된 PDF 없음",
          "no_pdf: not_pdf": "PDF 대신 웹페이지가 옴", "no_pdf: download_failed": "PDF 내려받기 실패",
          "no_pdf": "PDF를 받지 못함", "studio_unavailable": "본문 분석 설정 없음", "studio_error": "본문 분석 실패"}


def unread_reason(status) -> str:
    """본문을 못 읽은 이유. 읽었거나 상태가 없으면 빈 문자열."""
    if not status or status == "ok":
        return ""
    return UNREAD.get(status) or UNREAD.get(status.split(":")[0], "")
SCOPE = ("queries", "search_returned", "searched", "from_references", "narrowed", "fulltext_read", "date")


def _day(run: dict) -> str:
    return (run.get("created_at") or "")[:10] or date.today().isoformat()


def _element(e) -> dict:
    e = e or {}
    return {"match": e.get("match") or "unknown", "reason": e.get("reason") or "", "quote": e.get("quote") or "",
            "page": e.get("page"), "level": e.get("level")}


def _nearest(n: dict) -> dict:
    comp = n.get("comparison") or {}
    return {"id": n.get("id") or "", "paper_id": n.get("paper_id") or "", "title": n.get("title") or "",
            "year": n.get("year"), "url": n.get("url") or "",
            "level": n.get("level") or (n.get("card") or {}).get("level") or "abstract",
            "fulltext_status": n.get("fulltext_status"), "unread_reason": unread_reason(n.get("fulltext_status")),
            "comparison": {k: _element(comp.get(k)) for k, _ in ELEMENTS}}


def _verdict(v: dict, nearest: list[dict]) -> dict:
    if not v:
        return {"verdict": "not_verified", "label": VERDICT["not_verified"], "basis": [], "flags": [],
                "read_level": "", "status": None}
    code, by_id = v.get("verdict") or "none", {n["paper_id"]: n for n in nearest}
    basis = [{"id": by_id.get(b, {}).get("id", ""), "paper_id": b, "title": by_id.get(b, {}).get("title", "")}
             for b in v.get("verdict_basis") or []]
    full = sum(n["level"] == "fulltext" for n in nearest)
    read = f"가장 가까운 {len(nearest)}편 중 본문 {full}편, 초록 {len(nearest) - full}편" if nearest else ""
    return {"verdict": code, "label": VERDICT.get(code, code), "basis": basis, "flags": list(v.get("flags") or []),
            "read_level": read, "status": v.get("status")}


def _gaps(v: dict, nearest: list[dict]) -> list[dict]:
    """최근접 논문이 스스로 밝힌 한계의 인용. limitations가 없으면 카드에서 꺼낸다."""
    rows = v.get("limitations")
    if rows is None:
        rows = [{"paper_id": n.get("paper_id"), **((n.get("card") or {}).get("limitations") or {})}
                for n in v.get("nearest") or []]
    by_id, out = {n["paper_id"]: n for n in nearest}, []
    for r in rows:
        if not (r.get("quote") or "").strip():
            continue
        n = by_id.get(r.get("paper_id"), {})
        out.append({"id": r.get("id") or n.get("id", ""), "paper_id": r.get("paper_id") or "",
                    "title": r.get("title") or n.get("title", ""), "text": r.get("text") or "",
                    "quote": r["quote"].strip(), "page": r.get("page"), "level": r.get("level") or n.get("level")})
    return out


def _critique(c) -> dict | None:
    if not c:
        return None
    req = c.get("requirements") or {}
    return {"requirements": {k: {"status": (req.get(k) or {}).get("status") or "", "note": (req.get(k) or {}).get("note") or ""}
                             for k, _ in REQUIREMENTS},
            **{k: c.get(k) or "" for k, _ in REVIEW}}


def _scope(research_run: dict, v: dict) -> dict:
    """④가 있으면 ④의 조사 범위 (③ 참고문헌 포함), 없으면 ③ 참고문헌 검색 범위."""
    refs = research_run.get("references") or []
    if v.get("scope"):
        out = {k: v["scope"].get(k) for k in SCOPE}
        out.update(stage="verify", date=out["date"] or _day(v))
    else:
        trace = research_run.get("trace") or []
        searched = [t for t in trace if t.get("query")]
        filt = next((t for t in trace if t.get("step") == "relevance_filter"), {})
        queries = [t["query"] for t in searched] or list(dict.fromkeys(r["query"] for r in refs if r.get("query")))
        out = {"queries": queries, "search_returned": sum(t.get("returned") or 0 for t in searched) if searched else None,
               "searched": filt.get("candidates"), "from_references": None, "narrowed": None, "fulltext_read": 0,
               "date": _day(research_run), "stage": "research"}
    out["queries"] = list(out["queries"] or [])
    out["fulltext_read"] = out["fulltext_read"] or 0
    out["references"] = len(refs)
    return out


def build_card(recommend_run: dict | None, problem: dict | None, research_run: dict, topic_id: str,
               verify_run: dict | None = None, revision_run: dict | None = None) -> dict:
    """problem은 문제 카드 (recommend.problem_card). ① 추천 카드에 같은 문제가 있으면 그 값(추천 이유 포함)을 쓴다.
    recommend_run, verify_run, revision_run은 None이어도 된다. 후보가 없으면 KeyError.
    revision_run이 있으면 고친 후보(이슈 #12)로 카드를 만든다. 판정은 verify_run이 검증한 버전 그대로 두고 버전만 적는다."""
    topic, version = revise.resolve(research_run, topic_id, revision_run)
    if topic is None:
        raise KeyError(topic_id)
    v = verify_run or {}
    pid = (problem or {}).get("problem_id") or research_run.get("problem_id") or ""
    rec = next((r for r in (recommend_run or {}).get("recommendations") or [] if r.get("problem_id") == pid), {})
    p = {**(problem or {}), **rec}
    paper = p.get("paper") or {}
    refs = {r.get("id"): r for r in research_run.get("references") or []}
    nearest = [_nearest(n) for n in v.get("nearest") or []]
    ko = topic.get("ko") or {}  # 카드는 사용자가 읽는 글이라 한국어가 있으면 한국어를 쓴다 (이슈 #39)
    return {
        "topic": {"id": topic_id, "title": topic.get("title") or "", "summary": ko.get("summary") or topic.get("summary") or "",
                  "version": version},
        "problem": {"problem_id": pid, "title": p.get("title") or research_run.get("problem_title") or "",
                    "summary": p.get("summary_ko") or p.get("summary") or "", "reason": p.get("reason") or "",
                    "section_title": p.get("section_title") or "", "page": p.get("page"), "quote": p.get("quote") or "",
                    "paper": {k: paper.get(k) for k in ("paper_id", "title", "year", "url")}},
        "design": {**{k: ko.get(k) or topic.get(k) or "" for k, _ in DESIGN},
                   "support": [{"id": i, **{k: refs.get(i, {}).get(k) for k in ("title", "year", "url")}}
                               for i in topic.get("support") or []]},
        "nearest": nearest,
        "verdict": {**_verdict(v, nearest), "topic_version": (v.get("topic_version") or 1) if v else None},
        "gaps": _gaps(v, nearest),
        "critique": _critique(v.get("critique")),
        "scope": _scope(research_run, v),
        "source": {"recommend_run_id": (recommend_run or {}).get("run_id"), "problem_id": pid,
                   "research_run_id": research_run.get("run_id"), "topic_id": topic_id,
                   "revision_run_id": (revision_run or {}).get("run_id"), "verify_run_id": v.get("run_id")},
    }


# ---------- 마크다운 ----------
def _one(t) -> str:
    """한 줄로 (목록·표·인용 안에 넣을 때)."""
    return " ".join(str(t if t is not None else "").split())


def _cell(t) -> str:
    return _one(t).replace("|", "\\|")


def _page(p) -> str:
    return f" (p.{p})" if p not in (None, "") else ""


def _link(title, url) -> str:
    title = _one(title) or "(제목 없음)"
    return f"[{title}](<{url}>)" if str(url or "").startswith(("http://", "https://")) else title


def _year(y) -> str:
    return f" ({y})" if y else ""


def to_markdown(card: dict) -> str:
    t, p, d, v, sc = card["topic"], card["problem"], card["design"], card["verdict"], card["scope"]
    paper = p.get("paper") or {}
    out = [f"# {_one(t['title'])}", "", t["summary"], "", "## 출발 문제", "", f"**{_one(p['title'])}**", "",
           f"- 출처: {_link(paper.get('title'), paper.get('url'))}{_year(paper.get('year'))}"
           + (f" · {_one(p['section_title'])}" if p["section_title"] else "") + _page(p["page"])]
    if p["reason"]:
        out.append(f"- 추천 이유: {_one(p['reason'])}")
    if p["quote"]:
        out += ["", f"> {_one(p['quote'])}"]

    out += ["", f"## 연구 설계 (v{t['version']}, ③에서 고침)" if t.get("version", 1) > 1 else "## 연구 설계", ""]
    for k, label in DESIGN:
        if d[k]:
            out += [f"**{label}:** {d[k].strip()}", ""]
    if d["support"]:
        out += ["근거 참고문헌:", ""] + [f"- [{r['id']}] {_link(r['title'] or r['id'], r['url'])}{_year(r['year'])}"
                                    for r in d["support"]] + [""]

    out += ["## 가장 가까운 연구", ""]
    if not card["nearest"]:
        out += ["검증을 하지 않아 비교한 논문이 없습니다." if v["verdict"] == "not_verified" else "비교한 논문이 없습니다.", ""]
    for n in card["nearest"]:
        unread = f" (본문 못 읽음: {n['unread_reason']})" if n["level"] == "abstract" and n["unread_reason"] else ""
        out += [f"### [{n['id']}] {_link(n['title'], n['url'])}{_year(n['year'])} · {LEVEL.get(n['level'], n['level'])} 수준{unread}", "",
                "| 요소 | 일치 | 이유 | 논문 근거 |", "|---|---|---|---|"]
        for k, label in ELEMENTS:
            e = n["comparison"][k]
            quote = f'"{_one(e["quote"])}"{_page(e["page"])}' if e["quote"] else ""
            out.append(f"| {label} | {MATCH.get(e['match'], e['match'])} | {_cell(e['reason'])} | {_cell(quote)} |")
        out.append("")

    out += ["## 판정", "", f"**{v['label']}**" + (f" · 읽은 수준: {v['read_level']}" if v["read_level"] else ""), ""]
    if v["basis"]:
        out.append("- 근거 논문: " + ", ".join(f"[{b['id']}] {_one(b['title'])}" if b["id"] else b["paper_id"]
                                           for b in v["basis"]))
    out += [f"- {f}" + (f": {FLAG_NOTE[f]}" if f in FLAG_NOTE else "") for f in v["flags"]]
    if v["status"] == "budget":
        out.append("- 검증이 호출 예산 초과로 중간에 멈췄습니다. 그때까지 한 결과만 있습니다.")
    if v["verdict"] == "not_verified":
        out.append("- ④ 검증을 하지 않았습니다. 같은 연구가 이미 있는지 확인되지 않았습니다.")
    elif v.get("topic_version") and v["topic_version"] != t.get("version", 1):
        out.append(f"- 이 판정은 v{v['topic_version']} 후보 기준입니다. 연구 설계는 검증 뒤 고친 v{t['version']}이고, "
                   "고친 내용은 다시 검증하지 않았습니다.")
    out.append("")

    out += ["## 빈틈 (가장 가까운 논문이 밝힌 한계)", ""]
    out += [f"- [{g['id']}] {_one(g['title'])}: \"{_one(g['quote'])}\"{_page(g['page'])}" for g in card["gaps"]] \
        or ["가장 가까운 논문의 카드에 저자가 밝힌 한계가 없습니다." if card["nearest"] else "검증 결과가 없어 비어 있습니다."]
    out.append("")

    out += ["## 비평", ""]
    c = card["critique"]
    if c:
        out += ["| 요건 | 상태 | 메모 |", "|---|---|---|"]
        out += [f"| {label} | {STATUS.get(c['requirements'][k]['status'], c['requirements'][k]['status'])} "
                f"| {_cell(c['requirements'][k]['note'])} |" for k, label in REQUIREMENTS]
        out += ["", "명시됨(defined)은 명시 여부일 뿐 타당성 보장이 아닙니다. 7요건은 AI 사례 5개로 만든 잠정 기준(v0)입니다.", ""]
        out += [f"- **{label}:** {_one(c[k])}" for k, label in REVIEW if c[k]]
    else:
        out.append("비평이 없습니다." if v["verdict"] != "not_verified" else "검증을 하지 않아 비평이 없습니다.")
    out.append("")

    num = lambda x: "?" if x is None else x
    out += ["## 조사 범위", "", f"- 검색어: {' / '.join(_one(q) for q in sc['queries']) or '없음'}"]
    if sc["stage"] == "verify":
        out.append(f"- 검색한 논문: {num(sc['searched'])}편 (③ 참고문헌 {num(sc['from_references'])}편 포함), "
                   f"요소별 비교 {num(sc['narrowed'])}편, 본문까지 읽은 논문 {sc['fulltext_read']}편")
    else:
        out.append(f"- ③ 참고문헌 검색: 초록 있는 논문 {num(sc['searched'])}편 중 {sc['references']}편 사용. "
                   "④ 검증을 하지 않아 본문까지 읽은 논문은 없습니다.")
    out += [f"- 조사 날짜: {sc['date']}", ""]
    return "\n".join(out)
