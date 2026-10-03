"""사용자 정보 → Open Problem 추천 (P3).

흐름: 질의 작성(LLM) → 임베딩 검색 → 선택(LLM) → problem_id 검증 → 답변 생성(LLM)
- 화면에 보여 줄 title, summary, quote, 출처는 LLM 출력이 아니라 all.json과 config에서 가져온다.
- 답변 생성 LLM에는 검증을 통과한 항목만 넘긴다.

실행 (확인용): python -m src.recommend --lab "연구실 이름" --interests "관심 분야" [--extra "추가 정보"]
"""
import argparse
import json
import sys
import time
from functools import lru_cache

import numpy as np
from pydantic import BaseModel

from src import config, llm


class SearchQuery(BaseModel):
    research_description: str
    topics: list[str]


class Selection(BaseModel):
    problem_ids: list[str]


class RecommendationText(BaseModel):
    problem_id: str
    reason: str
    connection: str


class Answer(BaseModel):
    intro: str
    recommendations: list[RecommendationText]
    note: str


def _prompt(name: str) -> str:
    return (config.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_index() -> tuple[dict[str, dict], list[str], np.ndarray]:
    """(problem_id -> 항목, 임베딩 행 순서의 id 목록, 임베딩 행렬)."""
    problems = json.loads(config.OPEN_PROBLEMS_PATH.read_text(encoding="utf-8"))["problems"]
    by_id = {p["problem_id"]: p for p in problems}
    meta = json.loads(config.EMBEDDINGS_META_PATH.read_text(encoding="utf-8"))
    vecs = np.load(config.EMBEDDINGS_PATH)
    if set(meta["ids"]) != set(by_id) or len(meta["ids"]) != vecs.shape[0]:
        raise RuntimeError("임베딩이 all.json과 맞지 않습니다. python -m src.embed 를 실행하세요")
    return by_id, meta["ids"], vecs


def profile_text(lab_name: str, interests: str, extra: str = "") -> str:
    text = f"Lab name: {lab_name}\nResearch interests: {interests}"
    if extra.strip():
        text += f"\nAdditional information from the user: {extra.strip()}"
    return text


def write_query(lab_name: str, interests: str, extra: str = "") -> SearchQuery:
    messages = [
        {"role": "system", "content": _prompt("recommend_query")},
        {"role": "user", "content": profile_text(lab_name, interests, extra)},
    ]
    return llm.chat_json(messages, SearchQuery)


def search(query: SearchQuery, k: int = config.RETRIEVE_K) -> list[tuple[str, float]]:
    """임베딩 코사인 유사도 상위 k개 (problem_id, score)."""
    _, ids, vecs = load_index()
    text = f"{query.research_description} Topics: {', '.join(query.topics)}"
    q = np.array(llm.embed([text], config.EMBED_QUERY_MODEL)[0], dtype=np.float32)
    scores = vecs @ (q / np.linalg.norm(q))
    order = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in order]


def select(lab_name: str, interests: str, extra: str, query: SearchQuery,
           hits: list[tuple[str, float]]) -> list[str]:
    by_id, _, _ = load_index()
    lines = []
    for pid, _ in hits:  # 번호를 붙이면 번호를 섞은 가짜 id를 만드는 경우가 있어 id만 쓴다
        p = by_id[pid]
        lines.append(f"- problem_id: {pid}\n  title: {p['title']}\n  summary: {p['summary']}\n"
                     f"  keywords: {', '.join(p['keywords'])}\n  paper: {p['paper_title']}")
    user = (f"{profile_text(lab_name, interests, extra)}\nResearch description: {query.research_description}\n\n"
            f"Candidates:\n" + "\n".join(lines))
    messages = [{"role": "system", "content": _prompt("recommend_select")}, {"role": "user", "content": user}]
    return llm.chat_json(messages, Selection).problem_ids


def verify_ids(selected: list[str], hits: list[tuple[str, float]]) -> tuple[list[str], list[str]]:
    """선택된 id 중 검색 후보에 실제로 있는 것만 남긴다 (중복 제거, 최대 개수 제한). (통과, 탈락)."""
    candidates = {pid for pid, _ in hits}
    valid, invalid = [], []
    for pid in selected:
        pid = pid.strip()
        if pid in candidates and pid not in valid:
            valid.append(pid)
        elif pid not in candidates:
            invalid.append(pid)
    return valid[:config.MAX_RECOMMENDATIONS], invalid


def generate_answer(lab_name: str, interests: str, extra: str, problem_ids: list[str]) -> Answer:
    by_id, _, _ = load_index()
    blocks = []
    for pid in problem_ids:
        p = by_id[pid]
        blocks.append(f"- problem_id: {pid}\n  title: {p['title']}\n  summary: {p['summary']}\n"
                      f"  keywords: {', '.join(p['keywords'])}\n  quote: {p['quote']}\n  paper: {p['paper_title']}")
    user = f"연구실 이름: {lab_name}\n관심 분야: {interests}\n"
    if extra.strip():
        user += f"추가 정보: {extra.strip()}\n"
    user += "\nOpen Problems:\n" + "\n".join(blocks)
    messages = [{"role": "system", "content": _prompt("recommend_answer")}, {"role": "user", "content": user}]
    return llm.chat_json(messages, Answer, temperature=0.3)


def _card(pid: str, score: float, text: RecommendationText | None) -> dict:
    by_id, _, _ = load_index()
    p = by_id[pid]
    paper = config.PROPOSALS[p["paper_id"]]
    return {
        "problem_id": pid,
        "title": p["title"],
        "summary": p["summary"],
        "keywords": p["keywords"],
        "quote": p["quote"],
        "page": p["page"],
        "section_title": p["section_title"],
        "parent_section": p["parent_section"],
        "paper": {"paper_id": p["paper_id"], "title": paper["title"], "year": paper["year"], "url": paper["url"]},
        "score": round(score, 4),
        "reason": text.reason if text else "",
        "connection": text.connection if text else "",
    }


def recommend(lab_name: str, interests: str, extra: str = "") -> dict:
    """1턴 추천. extra = 사용자가 추가로 알리고 싶은 정보 (선택). status: ok | no_match."""
    t0 = time.monotonic()
    query = write_query(lab_name, interests, extra)
    hits = search(query)
    trace = {"query": query.model_dump(), "candidates": [{"problem_id": i, "score": round(s, 4)} for i, s in hits]}
    result = {"status": "no_match", "intro": "", "note": "", "recommendations": [], "trace": trace}

    if not hits or hits[0][1] < config.MIN_SCORE:
        result["note"] = "입력하신 관심 분야와 관련된 Open Problem을 찾지 못했습니다."
        trace["stop_reason"] = f"최고 유사도 {hits[0][1]:.3f} < {config.MIN_SCORE}" if hits else "후보 없음"
        trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
        return result

    selected = select(lab_name, interests, extra, query, hits)
    valid, invalid = verify_ids(selected, hits)
    trace.update({"selected": selected, "invalid_ids": invalid})
    if not valid:
        result["note"] = "검색된 후보 중 관심 분야와 직접 관련된 Open Problem이 없었습니다."
        trace["stop_reason"] = "선택 결과 없음"
        trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
        return result

    answer = generate_answer(lab_name, interests, extra, valid)
    texts = {r.problem_id.strip(): r for r in answer.recommendations if r.problem_id.strip() in valid}
    scores = dict(hits)
    result.update({
        "status": "ok",
        "intro": answer.intro,
        "note": answer.note,
        "recommendations": [_card(pid, scores[pid], texts.get(pid)) for pid in valid],
    })
    trace["answer_missing_ids"] = [pid for pid in valid if pid not in texts]
    trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return result


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", required=True)
    ap.add_argument("--interests", required=True)
    ap.add_argument("--extra", default="", help="사용자가 추가로 알리고 싶은 정보")
    ap.add_argument("--json", action="store_true", help="결과 전체를 JSON으로 출력")
    args = ap.parse_args()
    res = recommend(args.lab, args.interests, args.extra)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    tr = res["trace"]
    print(f"[질의] {tr['query']['research_description']}\n       topics: {', '.join(tr['query']['topics'])}")
    print(f"[검색] 상위 5: " + ", ".join(f"{c['problem_id']}({c['score']:.3f})" for c in tr["candidates"][:5]))
    print(f"[선택] {tr.get('selected')}  탈락 id: {tr.get('invalid_ids')}")
    print(f"[상태] {res['status']}  {tr.get('stop_reason', '')}  ({tr['elapsed_sec']}초)\n")
    if res["intro"]:
        print(res["intro"] + "\n")
    for i, r in enumerate(res["recommendations"], 1):
        print(f"{i}. {r['title']}  [{r['problem_id']}, score {r['score']}]")
        print(f"   출처: {r['paper']['title']} ({r['paper']['year']}), p.{r['page']}")
        print(f"   연결: {r['connection']}")
        print(f"   이유: {r['reason']}\n")
    if res["note"]:
        print(f"참고: {res['note']}")


if __name__ == "__main__":
    main()
