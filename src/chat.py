"""추천 이후 후속 대화.

도구 연결 없이, picks가 가리키는 저장된 결과(① 추천, ③ 후보)와 연구실 프로필만 컨텍스트로 쓴다.
picks = {recommend_run_id, problem_id, research_run_id, topic_id, revision_run_id} (API 명세 2.2).
없거나 만료된 결과는 빼고 답한다.
"""
import json

from pydantic import BaseModel

from src import config, llm, revise, runs
from src.recommend import load_index

MAX_HISTORY_MESSAGES = 12  # 컨텍스트에 넣는 최근 대화 메시지 수 (user+assistant)


def build_context(profile: dict, recommend_run: dict | None = None, research_run: dict | None = None,
                  picks: dict | None = None, revision_run: dict | None = None) -> str:
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
        for pid in others:
            p = by_id[pid]
            lines.append(f"- {p['title']} [{pid}] ({config.PROPOSALS[p['paper_id']]['title']}, p.{p['page']}): {p['summary']}")

    pid = picks.get("problem_id")
    if pid in by_id:
        lines.append(f"\n## 사용자가 고른 문제\n- {by_id[pid]['title']} [{pid}]")

    if research_run:
        lines.append("\n## 검색 참고문헌과 후보 (중복 판정 미실행)")
        lines.append(json.dumps({"topics": research_run["topics"], "references": research_run["references"]},
                                ensure_ascii=False))
        try:  # 고친 후보가 있으면 지금 버전을 보인다 (이슈 #12). 다른 후보의 수정안이면 원본만
            topic, version = revise.resolve(research_run, picks.get("topic_id") or "", revision_run)
        except ValueError:
            topic, version = revise.resolve(research_run, picks.get("topic_id") or "")
        if topic:
            lines.append(f"\n## 사용자가 다듬기로 고른 후보\n- {topic['title']} [{topic['id']}]")
            if version > 1:
                lines.append(f"- 지금 버전: v{version} (사용자가 고친 내용을 적용함. 위 topics의 원본보다 이것을 따른다)")
                lines.append(json.dumps({k: topic.get(k) or "" for k in revise.FIELDS}, ensure_ascii=False))
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
    recent = [{"role": m["role"], "content": m["content"][:INTENT_HISTORY_CHARS]} for m in history[-INTENT_HISTORY_MESSAGES:]]
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


def reply_stream(profile: dict, picks: dict, history: list[dict], user_msg: str):
    """후속 질문에 대한 답변 스트림. history: [{"role": "user"|"assistant", "content": str}] (이번 질문 제외)."""
    picks = picks or {}
    context = build_context(profile, runs.load(picks.get("recommend_run_id")),
                            runs.load(picks.get("research_run_id")), picks, runs.load(picks.get("revision_run_id")))
    system = (config.PROMPT_DIR / "chat_followup.md").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": system + "\n\n" + context}]
    messages += history[-MAX_HISTORY_MESSAGES:]
    messages.append({"role": "user", "content": user_msg})
    return llm.chat_stream(messages)
