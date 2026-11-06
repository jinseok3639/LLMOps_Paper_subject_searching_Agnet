"""추천 이후 후속 대화.

도구 연결 없이, picks가 가리키는 저장된 결과(① 추천, ③ 후보, ④ 검증)와 연구실 프로필만 컨텍스트로 쓴다.
picks = {recommend_run_id, problem_id, research_run_id, topic_id, revision_run_id, verify_run_id} (API 명세 2.2). 없거나 만료된 결과는 빼고 답한다.
대화 기록을 넣는 규칙과 압축은 src.conversation에 있다.
"""
import json

from pydantic import BaseModel

from src import config, conversation, llm, revise, runs
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
        # 화면용 한국어(ko)는 영어 필드와 내용이 같아 입력만 늘리므로 뺀다 (이슈 #39)
        topics = [{k: v for k, v in t.items() if k != "ko"} for t in research_run["topics"]]
        lines.append(json.dumps({"topics": topics, "references": reference_view(research_run)},
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


INTENT_HISTORY_MESSAGES = 4  # 판별에 넣는 최근 대화. "그걸로 하자"처럼 앞 말을 가리키는 경우를 위해서다
INTENT_HISTORY_CHARS = 200  # 메시지마다 앞부분만 넣는다. 긴 답변이 판별을 흐리지 않게


STAGE_HINTS = {  # 판별에 넣는 현재 단계 설명. 다음에 무엇을 할지 유추하게 한다
    "browse": "① 둘러보기: 추천한 Open Problem 목록을 보는 중. 다음은 문제 하나를 고르는 것이다 (다시 추천도 가능).",
    "explore": "② 문제 파고들기: 문제를 골랐다. 사용자가 조건(GPU, 기간 등)을 알려 줄 수 있고, 다음은 참고문헌을 검색해 연구 주제 후보를 만드는 것이다.",
    "refine": "③ 주제 다듬기: 연구 주제 후보를 보는 중. 후보를 고치거나 검증할 수 있다.",
    "verify": "④ 검증: 고른 후보의 검증 결과를 보는 중. 결과를 보고 후보를 고치거나, 다시 검증하거나, 확정할 수 있다.",
    "final": "⑤ 확정: 최종 주제를 정리했다.",
}


def decide_action(history: list[dict], user_msg: str, actions: list[dict], stage: str = "") -> dict | None:
    """채팅이 actions 중 하나를 요청하면 그 항목을, 질문이거나 애매하면 None을 돌려준다.
    LLM이 고른 name과 target이 목록에 그대로 있을 때만 행동으로 본다."""
    listing = "\n".join(f"- action: {a['name']} | target: {a['target'] or '(none)'} | {a['label']}" for a in actions)
    convo, _ = conversation.chat_messages(history)
    recent = [{"role": m["role"], "content": m["content"][:INTENT_HISTORY_CHARS]} for m in convo[-INTENT_HISTORY_MESSAGES:]]
    user = (f"현재 단계: {STAGE_HINTS.get(stage, '알 수 없음')}\n\n"
            f"최근 대화 (메시지마다 앞부분만):\n{json.dumps(recent, ensure_ascii=False)}\n\n"
            f"할 수 있는 행동:\n{listing}\n\n사용자 메시지:\n{user_msg}")
    messages = [{"role": "system", "content": (config.PROMPT_DIR / "chat_intent.md").read_text(encoding="utf-8")},
                {"role": "user", "content": user}]
    got = llm.chat_json(messages, Intent)
    name, target = got.action.strip(), got.target.strip()
    if target == "(none)":
        target = ""
    return next((a for a in actions if a["name"] == name and (a["target"] or "") == target), None)


def reply_stream(profile: dict, picks: dict, history: list[dict], user_msg: str, memo: dict | None = None,
                 usage: dict | None = None):
    """후속 질문에 대한 답변 스트림. history: common.history(meta=True) (이번 질문 제외), memo: 세션의 압축 결과.
    순서: [지시문 + 컨텍스트 + 이전 대화 요약] [대화 (카드는 한 줄)] [이번 질문]. 대화는 자르지 않고 뒤에 붙이기만 해서
    턴마다 앞부분이 캐시된다. 길어지면 화면이 압축을 요청한다 (src.conversation). usage에는 입력 토큰 수가 채워진다."""
    picks = picks or {}
    verify_run = runs.load(picks.get("verify_run_id"))
    if verify_run and verify_run.get("topic_id") != picks.get("topic_id"):
        verify_run = None  # 다른 후보의 검증 결과는 넣지 않는다
    context = build_context(profile, runs.load(picks.get("recommend_run_id")),
                            runs.load(picks.get("research_run_id")), picks, verify_run, runs.load(picks.get("revision_run_id")))
    system = (config.PROMPT_DIR / "chat_followup.md").read_text(encoding="utf-8")
    convo, summary = conversation.chat_messages(history, memo)
    if summary:
        context += "\n\n## 이전 대화 요약 (오래된 대화를 줄인 것)\n" + summary
    messages = [{"role": "system", "content": system + "\n\n" + context}, *convo, {"role": "user", "content": user_msg}]
    return llm.chat_stream(messages, usage=usage)
