"""선택한 Open Problem → 연구 주제 후보 생성 (2턴).

- 출처 정보는 LLM 출력이 아니라 all.json에서 가져온다.
- 선행 연구 검색 전이므로 생성 결과는 중복 검증 전 후보다.

실행 (확인용): python -m src.topic --problem-id "id" --lab "연구실 이름" --interests "관심 분야" [--extra "추가 정보"]
"""
import argparse
import json
import sys
import time

from pydantic import BaseModel

from src import config, llm
from src.recommend import load_index


class Topic(BaseModel):
    title: str
    research_question: str
    approach: str
    contribution: str
    feasibility: str


class TopicSet(BaseModel):
    intro: str
    topics: list[Topic]


def generate_topics(lab_name: str, interests: str, extra: str, problem_id: str) -> dict:
    """status: ok | no_topics."""
    t0 = time.monotonic()
    by_id, _, _ = load_index()
    p = by_id[problem_id]
    user = f"연구실 이름: {lab_name}\n관심 분야: {interests}\n"
    if extra.strip():
        user += f"추가 정보: {extra.strip()}\n"
    user += (f"\n선택한 Open Problem:\n- title: {p['title']}\n- summary: {p['summary']}\n"
             f"- keywords: {', '.join(p['keywords'])}\n- quote: {p['quote']}\n- paper: {p['paper_title']}")
    messages = [{"role": "system", "content": (config.PROMPT_DIR / "topic_generate.md").read_text(encoding="utf-8")},
                {"role": "user", "content": user}]
    answer = llm.chat_json(messages, TopicSet, temperature=0.4)
    topics = [t.model_dump() for t in answer.topics[:config.TOPIC_COUNT]]
    return {
        "status": "ok" if topics else "no_topics",
        "problem_id": problem_id,
        "problem_title": p["title"],
        "intro": answer.intro,
        "topics": topics,
        "trace": {"returned_topics": len(answer.topics), "elapsed_sec": round(time.monotonic() - t0, 1)},
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--problem-id", required=True)
    ap.add_argument("--lab", required=True)
    ap.add_argument("--interests", required=True)
    ap.add_argument("--extra", default="", help="사용자가 추가로 알리고 싶은 정보")
    args = ap.parse_args()
    print(json.dumps(generate_topics(args.lab, args.interests, args.extra, args.problem_id),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
