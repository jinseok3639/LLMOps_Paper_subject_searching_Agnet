"""추천 이후 후속 대화.

도구 연결 없이, 1턴 추천 결과(검증된 Open Problems + 검색 후보)와 연구실 프로필만 컨텍스트로 쓴다.
"""
from src import config, llm
from src.recommend import load_index

MAX_HISTORY_MESSAGES = 12  # 컨텍스트에 넣는 최근 대화 메시지 수 (user+assistant)


def build_context(profile: dict, result: dict) -> str:
    lines = ["[컨텍스트]", "## 연구실 프로필",
             f"- 연구실 이름: {profile['lab_name']}", f"- 관심 분야: {profile['interests']}"]
    if profile.get("extra"):
        lines.append(f"- 추가 정보: {profile['extra']}")

    lines.append("\n## 추천한 Open Problems")
    if not result["recommendations"]:
        lines.append("(없음: 관련 항목을 찾지 못함)")
    for i, r in enumerate(result["recommendations"], 1):
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
    shown = {r["problem_id"] for r in result["recommendations"]}
    others = [c["problem_id"] for c in result["trace"]["candidates"] if c["problem_id"] not in shown]
    lines.append("\n## 검색 후보 (추천에 넣지 않음, 관련도 순)")
    for pid in others:
        p = by_id[pid]
        lines.append(f"- {p['title']} [{pid}] ({config.PROPOSALS[p['paper_id']]['title']}, p.{p['page']}): {p['summary']}")
    return "\n".join(lines)


def reply_stream(profile: dict, result: dict, history: list[dict], user_msg: str):
    """후속 질문에 대한 답변 스트림. history: [{"role": "user"|"assistant", "content": str}] (이번 질문 제외)."""
    system = (config.PROMPT_DIR / "chat_followup.md").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": system + "\n\n" + build_context(profile, result)}]
    messages += history[-MAX_HISTORY_MESSAGES:]
    messages.append({"role": "user", "content": user_msg})
    return llm.chat_stream(messages)
