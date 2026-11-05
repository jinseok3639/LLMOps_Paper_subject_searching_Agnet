"""추천 이후 후속 대화.

도구 연결 없이, picks가 가리키는 저장된 결과(① 추천, ③ 후보)와 연구실 프로필만 컨텍스트로 쓴다.
picks = {recommend_run_id, problem_id, research_run_id, topic_id} (API 명세 2.2). 없거나 만료된 결과는 빼고 답한다.
"""
import json

from src import config, llm, runs
from src.recommend import load_index

MAX_HISTORY_MESSAGES = 12  # 컨텍스트에 넣는 최근 대화 메시지 수 (user+assistant)


def build_context(profile: dict, recommend_run: dict | None = None, research_run: dict | None = None,
                  picks: dict | None = None) -> str:
    picks = picks or {}
    lines = ["[컨텍스트]", "## 연구실 프로필",
             f"- 연구실 이름: {profile['lab_name']}", f"- 연구실 연구 주제: {profile['lab_topics']}"]
    if profile.get("interests"):
        lines.append(f"- 사용자 관심사: {profile['interests']}")

    recs = recommend_run["recommendations"] if recommend_run else []
    lines.append("\n## 추천한 Open Problems")
    if not recs:
        lines.append("(없음: 관련 항목을 찾지 못함)")
    for i, r in enumerate(recs, 1):
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
        topic = next((t for t in research_run["topics"] if t["id"] == picks.get("topic_id")), None)
        if topic:
            lines.append(f"\n## 사용자가 다듬기로 고른 후보\n- {topic['title']} [{topic['id']}]")
    return "\n".join(lines)


def reply_stream(profile: dict, picks: dict, history: list[dict], user_msg: str):
    """후속 질문에 대한 답변 스트림. history: [{"role": "user"|"assistant", "content": str}] (이번 질문 제외)."""
    picks = picks or {}
    context = build_context(profile, runs.load(picks.get("recommend_run_id")),
                            runs.load(picks.get("research_run_id")), picks)
    system = (config.PROMPT_DIR / "chat_followup.md").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": system + "\n\n" + context}]
    messages += history[-MAX_HISTORY_MESSAGES:]
    messages.append({"role": "user", "content": user_msg})
    return llm.chat_stream(messages)
