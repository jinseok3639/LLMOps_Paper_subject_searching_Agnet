"""추천 이후 후속 대화.

picks가 가리키는 저장된 결과(① 추천, ③ 후보, ④ 검증)와 연구실 프로필을 컨텍스트로 쓴다.
컨텍스트에 없는 논문은 답변 전에 찾아 질문 뒤에 붙인다 (이슈 #36): 저장된 ③④ 논문은 get_reference, 새 논문은 search_papers.
답변이 끝나면 논문 링크가 컨텍스트, 저장된 결과, 찾은 논문 안에 있는지 검사한다.
picks = {recommend_run_id, problem_id, research_run_id, topic_id, revision_run_id, verify_run_id} (API 명세 2.2). 없거나 만료된 결과는 빼고 답한다.
대화 기록을 넣는 규칙과 압축은 src.conversation에 있다.
"""
import json
import re

from pydantic import BaseModel

from src import calllog, config, conversation, llm, revise, runs
from src.agents.tools import chat_toolbox
from src.recommend import load_index


def build_context(profile: dict, recommend_run: dict | None = None, research_run: dict | None = None,
                  picks: dict | None = None, verify_run: dict | None = None, revision_run: dict | None = None) -> str:
    """system 컨텍스트. 잘 안 바뀌는 것부터 둔다 (prefix 캐시): 프로필 → ① → ③ → ④ → 현재 선택."""
    picks = picks or {}
    lines = ["[컨텍스트]", "## 연구실 프로필",
             f"- 연구실 이름: {profile['lab_name']}", f"- 연구실 연구 주제: {profile['lab_topics']}"]
    if profile.get("interests"):
        lines.append(f"- 사용자 관심사: {profile['interests']}")

    recs = recommend_run["recommendations"] if recommend_run else []
    lines.append("\n## 추천한 Open Problems")
    if not recs:
        lines.append("(없음: 관련 항목을 찾지 못함)")
    for i, r in enumerate(recs, (recommend_run or {}).get("first_number", 1)):  # 화면 번호와 맞춘다
        p = r["paper"]
        lines += [
            f"{i}. {r['title']} [{r['problem_id']}]",
            f"   출처: {p['title']} ({p['year']}), {r['section_title']}, p.{r['page']}, {p['url']}",
            f"   요약: {r['summary']}",
            f"   키워드: {', '.join(r['keywords'])}",
            f"   원문 인용: \"{r['quote']}\"",
            f"   추천 이유: {r['reason']}",
        ]

    by_id, _, _ = load_index()
    if recommend_run:
        shown = {r["problem_id"] for r in recs}
        others = [c["problem_id"] for c in recommend_run["trace"]["candidates"]
                  if c["problem_id"] not in shown and c["problem_id"] in by_id]
        lines.append("\n## 검색 후보 (추천에 넣지 않음, 관련도 순)")
        for pid in others:  # 다시 추천은 버튼·채팅 행동이 하므로 제목과 출처만 둔다
            p = by_id[pid]
            lines.append(f"- {p['title']} [{pid}] ({config.PROPOSALS[p['paper_id']]['title']}, p.{p['page']})")

    if research_run:
        lines.append("\n## 연구 주제 후보와 검색 참고문헌 (중복 판정 미실행)")
        lines.append(json.dumps({"topics": research_run["topics"], "references": reference_view(research_run)},
                                ensure_ascii=False))
    if verify_run:
        lines += ["", verify_view(verify_run)]

    # 선택은 가장 자주 바뀌므로 맨 끝에 둔다. 앞부분(지시문~결과)은 선택이 바뀌어도 캐시가 남는다.
    lines.append("\n## 현재 선택")
    pid = picks.get("problem_id")
    lines.append(f"- 사용자가 고른 문제: {by_id[pid]['title']} [{pid}]" if pid in by_id else "- 사용자가 고른 문제: (없음)")
    try:  # 고친 후보가 있으면 지금 버전을 보인다 (이슈 #12). 다른 후보의 수정안이면 원본만
        topic, version = revise.resolve(research_run or {}, picks.get("topic_id") or "", revision_run)
    except ValueError:
        topic, version = revise.resolve(research_run or {}, picks.get("topic_id") or "")
    if topic:
        lines.append(f"- 사용자가 다듬기로 고른 후보: {topic['title']} [{topic['id']}]")
        if version > 1:
            lines.append(f"- 지금 버전: v{version} (사용자가 고친 내용을 적용함. 위 topics의 원본보다 이것을 따른다)")
            lines.append("  " + json.dumps({k: topic.get(k) or "" for k in revise.FIELDS}, ensure_ascii=False))
    return "\n".join(lines)


def reference_view(research_run: dict) -> list[dict]:
    """③ 참고문헌. 후보가 인용한 논문만 초록을 넣고, 나머지는 id·제목·연도만 넣는다. PDF 조회 필드는 뺀다."""
    cited = {i for t in research_run.get("topics") or [] for i in t.get("support") or []}
    return [{"id": r.get("id"), "title": r.get("title"), "year": r.get("year"),
             **({"abstract": r.get("abstract")} if r.get("id") in cited and r.get("abstract") else {})}
            for r in research_run.get("references") or []]


MATCH_KO = {"same": "같음", "partial": "부분", "different": "다름", "unknown": "확인 못 함"}


def verify_view(v: dict) -> str:
    """④ 검증 결과 요약: 판정, 가장 가까운 논문의 요소별 비교(인용 포함), 저자가 밝힌 한계, 비평."""
    ver = f", v{v['topic_version']} 기준" if (v.get("topic_version") or 1) > 1 else ""
    lines = [f"## ④ 검증 결과 (후보 [{v.get('topic_id')}] {v.get('topic_title') or ''}{ver})",
             f"- 판정: {v.get('verdict') or '없음'} (근거 논문: {', '.join(v.get('verdict_basis') or []) or '없음'})"]
    if v.get("status") == "budget":
        lines.append("- 호출 예산 초과로 중간에 멈춘 결과다")
    if v.get("flags"):
        lines.append(f"- 주의: {', '.join(v['flags'])}")
    sc = v.get("scope") or {}
    if sc:
        lines.append(f"- 조사 범위: 검색어 {' / '.join(sc.get('queries') or [])}, 검색한 논문 {sc.get('searched')}편, "
                     f"요소별 비교 {sc.get('narrowed')}편, 본문까지 읽은 논문 {sc.get('fulltext_read')}편, {sc.get('date')}")
    for n in v.get("nearest") or []:
        lines.append(f"- [{n.get('id')}] {n.get('title')} ({n.get('year') or '연도 모름'}), 읽은 수준: {n.get('level')}")
        for k, e in (n.get("comparison") or {}).items():
            quote = f' / 논문 인용: "{e["quote"]}"' if e.get("quote") else ""
            lines.append(f"  - {k}: {MATCH_KO.get(e.get('match'), e.get('match'))}. {e.get('reason') or ''}{quote}")
    for g in v.get("limitations") or []:
        lines.append(f"- [{g.get('id')}] 저자가 밝힌 한계: \"{g.get('quote')}\"")
    c = v.get("critique")
    if c:
        req = c.get("requirements") or {}
        lines.append("- 비평 7요건: " + ", ".join(f"{k}={r.get('status')}" for k, r in req.items()))
        lines += [f"- 비평 {k}: {c[k]}" for k in ("unsupported", "vague", "already_done", "suggestion") if c.get(k)]
    return "\n".join(lines)


class Intent(BaseModel):
    action: str
    target: str = ""
    # 참고문헌 밖의 논문을 찾는 질문이면 S2 검색어 (이슈 #36). Solar는 답을 먼저 쓰고 Tool을 맨 끝에 부르는 일이 많아서
    # 검색할지는 여기서 미리 정하고, 답변 호출 전에 검색해 결과를 질문 뒤에 붙인다
    search_query: str = ""
    search_year: str = ""


def search_plan(got: Intent) -> dict | None:
    """판별 결과의 검색 계획. search_papers 입력 형식. 검색이 필요 없으면 None."""
    query = got.search_query.strip()
    if not query:
        return None
    year = got.search_year.strip()
    return {"query": query, **({"year": year} if re.fullmatch(r"\d{4}(-\d{4})?-?|-\d{4}", year) else {})}


def focus_text(picks: dict | None) -> str:
    """판별에 넣는 지금 고른 문제와 후보 제목. "이 주제 관련 논문"의 검색어를 만들 때 쓴다."""
    picks = picks or {}
    by_id, _, _ = load_index()
    lines = []
    pid = picks.get("problem_id")
    if pid in by_id:
        lines.append(f"고른 문제: {by_id[pid]['title']}")
    research_run = runs.load(picks.get("research_run_id"))
    try:
        topic, _ = revise.resolve(research_run or {}, picks.get("topic_id") or "", runs.load(picks.get("revision_run_id")))
    except ValueError:
        topic, _ = revise.resolve(research_run or {}, picks.get("topic_id") or "")
    if topic:
        lines.append(f"고른 후보: {topic['title']}")
    return "\n".join(lines) or "(없음)"


INTENT_HISTORY_MESSAGES = 4  # 판별에 넣는 최근 대화. "그걸로 하자"처럼 앞 말을 가리키는 경우를 위해서다
INTENT_HISTORY_CHARS = 200  # 메시지마다 앞부분만 넣는다. 긴 답변이 판별을 흐리지 않게


STAGE_HINTS = {  # 판별에 넣는 현재 단계 설명. 다음에 무엇을 할지 유추하게 한다
    "browse": "① 둘러보기: 추천한 Open Problem 목록을 보는 중. 다음은 문제 하나를 고르는 것이다 (다시 추천도 가능).",
    "explore": "② 문제 파고들기: 문제를 골랐다. 사용자가 조건(GPU, 기간 등)을 알려 줄 수 있고, 다음은 참고문헌을 검색해 연구 주제 후보를 만드는 것이다.",
    "refine": "③ 주제 다듬기: 연구 주제 후보를 보는 중. 후보를 고치거나 검증할 수 있다.",
    "verify": "④ 검증: 고른 후보의 검증 결과를 보는 중. 결과를 보고 후보를 고치거나, 다시 검증하거나, 확정할 수 있다.",
    "final": "⑤ 확정: 최종 주제를 정리했다.",
}


def decide_action(history: list[dict], user_msg: str, actions: list[dict], stage: str = "",
                  picks: dict | None = None, plan: dict | None = None) -> dict | None:
    """채팅이 actions 중 하나를 요청하면 그 항목을, 질문이거나 애매하면 None을 돌려준다.
    LLM이 고른 name과 target이 목록에 그대로 있을 때만 행동으로 본다.
    plan을 주면 답변 전에 할 검색을 plan["search"]에 채운다 (search_papers 입력, 없으면 None)."""
    listing = "\n".join(f"- action: {a['name']} | target: {a['target'] or '(none)'} | {a['label']}" for a in actions)
    convo, _ = conversation.chat_messages(history)
    recent = [{"role": m["role"], "content": m["content"][:INTENT_HISTORY_CHARS]} for m in convo[-INTENT_HISTORY_MESSAGES:]]
    user = (f"현재 단계: {STAGE_HINTS.get(stage, '알 수 없음')}\n\n"
            f"지금 고른 것:\n{focus_text(picks)}\n\n"
            f"최근 대화 (메시지마다 앞부분만):\n{json.dumps(recent, ensure_ascii=False)}\n\n"
            f"할 수 있는 행동:\n{listing or '(없음)'}\n\n사용자 메시지:\n{user_msg}")
    messages = [{"role": "system", "content": (config.PROMPT_DIR / "chat_intent.md").read_text(encoding="utf-8")},
                {"role": "user", "content": user}]
    got = llm.chat_json(messages, Intent)
    if plan is not None:
        plan["search"] = search_plan(got)
    name, target = got.action.strip(), got.target.strip()
    if target == "(none)":
        target = ""
    return next((a for a in actions if a["name"] == name and (a["target"] or "") == target), None)


TOOL_PROGRESS = {"get_reference": "저장된 참고문헌 확인 중", "search_papers": "논문 검색 중"}


def reply_stream(profile: dict, picks: dict, history: list[dict], user_msg: str, memo: dict | None = None,
                 usage: dict | None = None, on_tool=None, search: dict | None = None):
    """후속 질문에 대한 답변 스트림. history: common.history(meta=True) (이번 질문 제외), memo: 세션의 압축 결과.
    순서: [지시문 + 컨텍스트 + 이전 대화 요약] [대화 (카드는 한 줄)] [이번 질문 + 미리 찾은 논문]. 대화는 자르지 않고 뒤에 붙이기만
    해서 턴마다 앞부분이 캐시된다. 길어지면 화면이 압축을 요청한다 (src.conversation). usage에는 입력 토큰 수가 채워진다.

    문헌 (이슈 #36): 답변 호출 전에 논문을 찾아 이번 질문 뒤에 붙인다. 질문이 가리키는 저장된 ③④ 논문(prefetch)과,
    판별(decide_action)이 정한 S2 검색(search, presearch)이다. 답변 호출에는 Tool을 주지 않는다: Solar는 답을 먼저 쓰고
    Tool을 맨 끝에 부르거나, 사용자에게 함수를 부르라고 안내하는 일이 잦았다. 찾은 논문은 대화 기록에 남지 않는다.
    on_tool(name, arguments): 진행 표시용. 끝나면 답변의 논문 링크를 검사한다 (unverified_links)."""
    picks = picks or {}
    verify_run = runs.load(picks.get("verify_run_id"))
    if verify_run and verify_run.get("topic_id") != picks.get("topic_id"):
        verify_run = None  # 다른 후보의 검증 결과는 넣지 않는다
    research_run = runs.load(picks.get("research_run_id"))
    context = build_context(profile, runs.load(picks.get("recommend_run_id")),
                            research_run, picks, verify_run, runs.load(picks.get("revision_run_id")))
    system = (config.PROMPT_DIR / "chat_followup.md").read_text(encoding="utf-8")
    convo, summary = conversation.chat_messages(history, memo)
    if summary:
        context += "\n\n## 이전 대화 요약 (오래된 대화를 줄인 것)\n" + summary
    box = chat_toolbox(research_run, verify_run)
    fetched = prefetch(user_msg, box, research_run, verify_run, on_tool) + presearch(search, box, on_tool)
    messages = [{"role": "system", "content": system + "\n\n" + context}, *convo,
                {"role": "user", "content": user_msg + fetched}]
    parts = []
    for piece in llm.chat_stream(messages, usage=usage):
        parts.append(piece)
        yield piece
    known = "\n".join([context, *(m["content"] for m in convo if m["role"] == "user"),
                        json.dumps([(research_run or {}).get("references"), (verify_run or {}).get("nearest"),
                                    list(box.seen_papers.values())], ensure_ascii=False, default=str)])
    bad = unverified_links("".join(parts), known)
    if bad:
        calllog.record("check", "chat_citations", input=user_msg, output=bad, latency_ms=0, status="flagged")
        yield ("\n\n※ 확인되지 않은 링크: " + ", ".join(bad)
               + " (저장된 자료나 검색 결과에 없는 논문입니다. 직접 확인해 주세요.)")


def presearch(search: dict | None, box, on_tool=None) -> str:
    """판별이 정한 검색(search_papers 입력)을 실행하고, 질문 뒤에 붙일 글을 돌려준다."""
    if not search:
        return ""
    if on_tool:
        on_tool("search_papers", json.dumps(search, ensure_ascii=False))
    got = box.call("search_papers", search)
    head = f"\n\n[새로 검색한 논문 (Semantic Scholar, 데이터). 검색어: {search['query']}"
    if search.get("year"):
        head += f", 연도: {search['year']}"
    if got.get("error"):
        return head + f"]\n검색 실패: {got['error']}"
    return head + "]\n" + json.dumps(got.get("papers") or [], ensure_ascii=False)


# ref-001는 처럼 한글이 바로 붙으므로 \b 대신 앞뒤 글자를 직접 본다
PAPER_ID = re.compile(r"(?<![\w-])(ref-\d{1,3}|N\d{1,2})(?![\d-])", re.IGNORECASE)
WORD = re.compile(r"[a-z0-9]+")
TITLE_WORDS = 3  # 질문에 제목의 연속된 영어 단어가 이만큼 있으면 그 논문을 가리킨다고 본다
MAX_PREFETCH = 3


def _title_hit(title: str, msg_words: str) -> bool:
    words = [w for w in WORD.findall((title or "").lower()) if len(w) > 1]
    return any(" ".join(words[i:i + TITLE_WORDS]) in msg_words for i in range(len(words) - TITLE_WORDS + 1))


def prefetch(user_msg: str, box, research_run: dict | None, verify_run: dict | None = None, on_tool=None) -> str:
    """질문이 저장된 ③ 참고문헌(ref-NNN)이나 ④ 가까운 논문(N1)을 id나 제목으로 가리키면 get_reference로 읽어,
    질문 뒤에 붙일 글을 돌려준다. 초록이 이미 컨텍스트에 있는 참고문헌(후보가 인용한 것)은 뺀다."""
    cited = {i for t in (research_run or {}).get("topics") or [] for i in t.get("support") or []}
    ids = []
    for m in PAPER_ID.finditer(user_msg):
        pid = m.group(1).upper()
        ids.append("ref-" + pid[4:].zfill(3) if pid.startswith("REF-") else pid)
    msg_words = " ".join(w for w in WORD.findall(user_msg.lower()) if len(w) > 1)
    for p in [*((research_run or {}).get("references") or []), *((verify_run or {}).get("nearest") or [])]:
        if _title_hit(p.get("title"), msg_words):
            ids.append(p.get("id"))
    ids = [i for i in dict.fromkeys(ids) if i not in cited][:MAX_PREFETCH]
    papers = []
    for pid in ids:
        if on_tool:
            on_tool("get_reference", json.dumps({"id": pid}))
        papers += box.call("get_reference", {"id": pid}).get("papers", [])
    if not papers:
        return ""
    return "\n\n[질문에 나온 논문 (저장된 결과에서 읽음, 데이터)]\n" + json.dumps(papers, ensure_ascii=False)


# 논문 링크와 arXiv 번호. 그 밖의 링크(코드 저장소 등)는 검사하지 않는다
LINK = re.compile(r"https?://(?:www\.)?(?:arxiv\.org|semanticscholar\.org|api\.semanticscholar\.org|(?:dx\.)?doi\.org)/[^\s)\]>\"'<]+",
                  re.IGNORECASE)
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
S2_ID = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
ARXIV_MENTION = re.compile(r"arxiv\s*:?\s*(\d{4}\.\d{4,5})", re.IGNORECASE)


def _paper_key(link: str) -> str | None:
    """링크에서 논문 식별자 (arXiv 번호, S2 paperId, DOI). 못 찾으면 None."""
    low = link.lower().rstrip(".,;:")
    if "arxiv.org" in low:
        m = ARXIV_ID.search(low)
        return m.group(1) if m else None
    if "semanticscholar.org" in low:
        m = S2_ID.search(low)
        return m.group(0) if m else None
    if "doi.org/" in low:
        return low.split("doi.org/", 1)[1] or None
    return None


def unverified_links(answer: str, known: str) -> list[str]:
    """답변의 논문 링크·arXiv 번호 중 known(컨텍스트, 대화, 저장된 결과, Tool 결과) 어디에도 식별자가 없는 것.
    식별자를 못 뽑는 링크(검색 페이지 등)도 지어낸 것으로 본다."""
    known, bad = known.lower(), []
    for m in LINK.finditer(answer):
        key = _paper_key(m.group(0))
        if not key or key not in known:
            bad.append(m.group(0).rstrip(".,;:"))
    for m in ARXIV_MENTION.finditer(answer):
        if m.group(1) not in known and not any(m.group(1) in b for b in bad):
            bad.append("arXiv:" + m.group(1))
    return list(dict.fromkeys(bad))
