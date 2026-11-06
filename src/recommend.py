"""사용자 정보 → Open Problem 추천 (① 둘러보기).

흐름: 질의 작성(LLM) → 임베딩 검색 → 선택(LLM) → problem_id 검증 → 답변 생성(LLM)
- 화면에 보여 줄 title, summary, quote, 출처는 LLM 출력이 아니라 all.json과 config에서 가져온다.
- 답변 생성 LLM에는 검증을 통과한 항목만 넘긴다.
- profile = {lab_name, lab_topics, interests}. interests는 비어 있을 수 있다 (API 명세 2.1).
- rejections = [{target: "problem", target_id, reason}]. 거절한 문제는 후보에서 빼고, 이유는 질의와 선택에 반영한다 (C-6).
- exclude = [problem_id]. 이미 보여 준 문제다. 후보에서 빼기만 하고 프롬프트에는 넣지 않는다 (이슈 #21).

실행 (확인용): python -m src.recommend --lab "연구실 이름" --topics "연구실 연구 주제" [--interests "관심사"]
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


def _noop(*args, **kwargs):
    pass


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


def profile_text(profile: dict) -> str:
    interests = profile.get("interests", "").strip() or "(not given; use the lab research topics)"
    return (f"Lab name: {profile['lab_name']}\nLab research topics: {profile['lab_topics']}\n"
            f"User's interests: {interests}")


def rejection_text(rejections: list[dict]) -> str:
    """거절한 문제와 이유. 없으면 빈 문자열."""
    if not rejections:
        return ""
    by_id, _, _ = load_index()
    lines = [f"- {by_id[r['target_id']]['title'] if r['target_id'] in by_id else r['target_id']}: "
             f"{r.get('reason') or '(no reason given)'}" for r in rejections]
    return "\n\nProblems the user rejected earlier (reason after the colon):\n" + "\n".join(lines)


def write_query(profile: dict, rejections: list[dict] = ()) -> SearchQuery:
    messages = [
        {"role": "system", "content": _prompt("recommend_query")},
        {"role": "user", "content": profile_text(profile) + rejection_text(list(rejections))},
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


def select(profile: dict, query: SearchQuery, hits: list[tuple[str, float]], rejections: list[dict] = ()) -> list[str]:
    by_id, _, _ = load_index()
    lines = []
    for pid, _ in hits:  # 번호를 붙이면 번호를 섞은 가짜 id를 만드는 경우가 있어 id만 쓴다
        p = by_id[pid]
        lines.append(f"- problem_id: {pid}\n  title: {p['title']}\n  summary: {p['summary']}\n"
                     f"  keywords: {', '.join(p['keywords'])}\n  paper: {p['paper_title']}")
    user = (f"{profile_text(profile)}\nResearch description: {query.research_description}"
            f"{rejection_text(list(rejections))}\n\nCandidates:\n" + "\n".join(lines))
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


def generate_answer(profile: dict, problem_ids: list[str]) -> Answer:
    by_id, _, _ = load_index()
    blocks = []
    for pid in problem_ids:
        p = by_id[pid]
        blocks.append(f"- problem_id: {pid}\n  title: {p['title']}\n  summary: {p['summary']}\n"
                      f"  keywords: {', '.join(p['keywords'])}\n  quote: {p['quote']}\n  paper: {p['paper_title']}")
    user = f"연구실 이름: {profile['lab_name']}\n연구실 연구 주제: {profile['lab_topics']}\n"
    if profile.get("interests", "").strip():
        user += f"사용자 관심사: {profile['interests'].strip()}\n"
    user += "\nOpen Problems:\n" + "\n".join(blocks)
    messages = [{"role": "system", "content": _prompt("recommend_answer")}, {"role": "user", "content": user}]
    return llm.chat_json(messages, Answer, temperature=0.3)


def problem_card(pid: str) -> dict | None:
    """all.json의 Open Problem 하나를 화면·검색용 카드로. 없으면 None."""
    by_id, _, _ = load_index()
    p = by_id.get(pid)
    if p is None:
        return None
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
    }


def recommend(profile: dict, rejections: list[dict] | None = None, progress=_noop, exclude=()) -> dict:
    """1턴 추천. status: ok | no_match. progress(message, step=...)는 단계마다 불린다."""
    t0 = time.monotonic()
    rejections = [r for r in rejections or [] if r.get("target", "problem") == "problem" and r.get("target_id")]
    rejected = {r["target_id"] for r in rejections}
    excluded = set(exclude) - rejected
    skip = rejected | excluded
    progress("검색어 작성 중", step="query")
    query = write_query(profile, rejections)
    progress("관련 Open Problem 검색 중", step="search")
    hits = [h for h in search(query, config.RETRIEVE_K + len(skip)) if h[0] not in skip][:config.RETRIEVE_K]
    trace = {"query": query.model_dump(), "rejected": sorted(rejected), "excluded": sorted(excluded),
             "candidates": [{"problem_id": i, "score": round(s, 4)} for i, s in hits]}
    # 다시 추천한 문제는 앞 번호에 이어 매긴다 (1~5 다음 6~10). 화면과 대화 컨텍스트가 같은 번호를 쓴다 (이슈 #23)
    result = {"status": "no_match", "intro": "", "note": "", "recommendations": [], "first_number": len(set(exclude)) + 1,
              "trace": trace}

    if not hits or hits[0][1] < config.MIN_SCORE:
        result["note"] = ("관련된 Open Problem은 앞에서 모두 보여 드렸습니다." if excluded
                          else "입력하신 연구 주제·관심사와 관련된 Open Problem을 찾지 못했습니다.")
        trace["stop_reason"] = f"최고 유사도 {hits[0][1]:.3f} < {config.MIN_SCORE}" if hits else "후보 없음"
        trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
        return result

    progress("연구실에 맞는 문제 고르는 중", step="select")
    selected = select(profile, query, hits, rejections)
    valid, invalid = verify_ids(selected, hits)
    trace.update({"selected": selected, "invalid_ids": invalid})
    if not valid:
        result["note"] = "검색된 후보 중 연구 주제·관심사와 직접 관련된 Open Problem이 없었습니다."
        trace["stop_reason"] = "선택 결과 없음"
        trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
        return result

    progress("추천 이유 작성 중", step="answer")
    answer = generate_answer(profile, valid)
    texts = {r.problem_id.strip(): r for r in answer.recommendations if r.problem_id.strip() in valid}
    scores = dict(hits)
    cards = []
    for pid in valid:
        text = texts.get(pid)
        cards.append({**problem_card(pid), "score": round(scores[pid], 4),
                      "reason": text.reason if text else "", "connection": text.connection if text else ""})
    result.update({"status": "ok", "intro": answer.intro, "note": answer.note, "recommendations": cards})
    trace["answer_missing_ids"] = [pid for pid in valid if pid not in texts]
    trace["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return result


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", required=True)
    ap.add_argument("--topics", required=True, help="연구실 연구 주제")
    ap.add_argument("--interests", default="", help="사용자 관심사 (선택)")
    ap.add_argument("--json", action="store_true", help="결과 전체를 JSON으로 출력")
    args = ap.parse_args()
    res = recommend({"lab_name": args.lab, "lab_topics": args.topics, "interests": args.interests})
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
