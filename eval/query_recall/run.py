"""검색어 재현율 평가: 정답을 아는 샘플로 검색어 전략을 채점한다.

샘플(samples/t00x.json)은 실제 논문 T 한 편을 거꾸로 풀어 둔 것이다.
- topic: T의 연구 주제 (gap, approach, evaluation)
- published_at: T의 발표일
- gold: T가 인용한 논문. grade가 core인 것이 "꼭 찾아야 했던" 논문
- source_paper: T의 arXiv ID

두 가지를 잰다.
- references (③ 참고문헌 검색): topic의 gap을 "선택한 문제"로 넣어 검색어를 만들고, T 발표일 이전 논문만 검색한다.
  검색 결과 합집합에 core 논문이 몇 편 들었는지(재현율)를 본다. 앱에서는 프로필·대화도 들어가므로 근사다.
- verify (④ 검증 검색): topic 전체를 후보로 넣어 검색어를 만들고, 날짜 제한 없이 검색한다.
  topic이 T에서 나왔으므로 정답은 T 자신이다. T가 결과에 나오는지, 몇 위인지를 본다.

검색어를 만드는 쪽에는 topic만 넘긴다. gold와 source_paper는 채점에만 쓴다 (정답 누출 방지).
S2 검색은 앱과 같게 검색어당 20편, 분야 필터 없음, 결과가 0건이면 앞 두 단어로 한 번 더 검색한다.
S2 응답은 cache/에 저장해 같은 검색어를 다시 부르지 않는다. 검색어 생성(Solar)은 캐시하지 않는다.

실행 (앱 폴더에서):
    python eval/query_recall/run.py --strategy current --mode references,verify --repeat 1
결과: runs/{시각}_{전략}.json (검색어, 맞힌·놓친 논문), summary.md (한 줄씩 누적)

새 전략은 STRATEGIES에 {"references": topic -> 검색어 목록, "verify": topic -> 검색어 목록}을 추가한다.
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from src import references, verify  # noqa: E402
from src.clients import semantic_scholar as s2  # noqa: E402

SAMPLES_DIR = HERE / "samples"
CACHE_DIR = HERE / "cache"
RUNS_DIR = HERE / "runs"
SUMMARY = HERE / "summary.md"
LIMIT = references.SEARCH_LIMIT  # 앱과 같은 검색어당 결과 수
FIELDS = "title,year,publicationDate"
MODES = ("references", "verify")


# ---------- 전략: topic -> 검색어 목록 ----------

def references_problem(topic) -> dict:
    """③ 입력 근사: 샘플에는 Open Problem 카드가 없으므로 gap을 선택한 문제로 넣는다 (방법은 넣지 않는다)."""
    return {"summary": topic["gap"]}


def current_references(topic):
    return references.write_queries(references.researcher_context({}, [], references_problem(topic)))


def current_verify(topic):
    return verify.write_queries(topic)[1]


STRATEGIES = {
    "current": {"references": current_references, "verify": current_verify},
}


# ---------- S2 (캐시) ----------

def _cached_get(path, params):
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
    file = CACHE_DIR / f"{key}.json"
    if file.exists():
        return json.loads(file.read_text("utf-8")), True
    data = s2.get(path, params)
    file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data, False


def search(query, before=None):
    """앱과 같은 검색 (검색어당 LIMIT편, 분야 필터 없음). before가 있으면 그 날짜 이전 논문만."""
    params = {"query": query, "fields": FIELDS, "limit": LIMIT}
    if before:
        params["publicationDateOrYear"] = f":{before}"
    data, cached = _cached_get("/paper/search", params)
    return [r for r in data.get("data") or [] if r.get("paperId")], cached


def search_like_app(query, before=None):
    """결과가 0건이면 앞 두 단어로 한 번 더 (references._search를 쓰는 앱 흐름과 같다)."""
    rows, cached = search(query, before)
    used = query
    if not rows:
        used = " ".join(query.split()[:2])
        rows, c2 = search(used, before)
        cached = cached and c2
    return {"query": query, "retried_query": used if used != query else None, "cached": cached,
            "results": [{"paperId": r["paperId"], "title": r.get("title"), "year": r.get("year")} for r in rows]}


def source_paper_id(arxiv_id):
    data, _ = _cached_get(f"/paper/ARXIV:{arxiv_id}", {"fields": "paperId,title"})
    return data["paperId"]


# ---------- 채점 (API 없이 계산) ----------

def score_references(searches, gold):
    """검색 결과 합집합에 core 논문이 몇 편 들었는지. paper_id가 없는 gold는 채점에서 뺀다."""
    found = {r["paperId"] for s in searches for r in s["results"]}
    core = {g["paper_id"]: g["title"] for g in gold if g["grade"] == "core" and g.get("paper_id")}
    every = {g["paper_id"] for g in gold if g.get("paper_id")}
    hit = sorted(core[i] for i in core.keys() & found)
    return {"core_total": len(core), "core_hit": len(hit), "core_recall": round(len(hit) / len(core), 4) if core else None,
            "gold_total": len(every), "gold_hit": len(every & found), "pooled": len(found),
            "hit_titles": hit, "missed_titles": sorted(core[i] for i in core.keys() - found),
            "per_query_core_hit": [len(core.keys() & {r["paperId"] for r in s["results"]}) for s in searches]}


def score_verify(searches, target_id):
    """원 논문이 어느 검색어에서 몇 위로 나왔는지. best_rank는 검색어별 순위 중 가장 높은 것 (1부터)."""
    ranks = [next((i for i, r in enumerate(s["results"], 1) if r["paperId"] == target_id), None) for s in searches]
    found = [r for r in ranks if r]
    return {"found": bool(found), "best_rank": min(found) if found else None, "per_query_rank": ranks}


# ---------- 실행 ----------

def load_samples(ids=None):
    samples = [json.loads(p.read_text("utf-8")) for p in sorted(SAMPLES_DIR.glob("t*.json"))]
    return [s for s in samples if not ids or s["sample_id"] in ids]


def run_one(strategy, mode, sample):
    topic = sample["topic"]
    make = STRATEGIES[strategy][mode]
    try:
        queries = make(topic)
    except Exception as e:  # 검색어 생성 실패도 결과로 남긴다 (점수 0)
        return {"error": f"{type(e).__name__}: {str(e)[:300]}", "queries": []}
    if mode == "references":
        before = (date.fromisoformat(sample["published_at"]) - timedelta(days=1)).isoformat()
        searches = [search_like_app(q, before) for q in queries]
        return {"queries": queries, "before": before, "score": score_references(searches, sample["gold"]),
                "searches": searches}
    target = source_paper_id(sample["source_paper"])
    searches = [search_like_app(q) for q in queries]
    return {"queries": queries, "target_paper_id": target, "score": score_verify(searches, target), "searches": searches}


def summarize(results, mode):
    rows = [r for r in results if r["mode"] == mode]
    if not rows:
        return None
    if mode == "references":
        hit = sum(r.get("score", {}).get("core_hit", 0) for r in rows)
        total = sum(r.get("score", {}).get("core_total", 0) or 0 for r in rows)
        return f"core {hit}/{total} ({hit / total:.0%})" if total else "core 0/0"
    found = [r for r in rows if r.get("score", {}).get("found")]
    ranks = sorted(r["score"]["best_rank"] for r in found)
    return f"원 논문 {len(found)}/{len(rows)}, 순위 {ranks}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strategy", default="current", help="쉼표로 여러 개: " + ", ".join(STRATEGIES))
    ap.add_argument("--mode", default="references,verify", help="references, verify 중 쉼표로")
    ap.add_argument("--repeat", type=int, default=1, help="샘플마다 검색어 생성을 몇 번 반복할지")
    ap.add_argument("--samples", default="", help="예: t001,t004 (비우면 전부)")
    ap.add_argument("--note", default="", help="summary.md에 남길 메모")
    args = ap.parse_args()
    modes = [m for m in args.mode.split(",") if m]
    unknown = [m for m in modes if m not in MODES] + [s for s in args.strategy.split(",") if s not in STRATEGIES]
    if unknown:
        ap.error(f"모르는 값: {unknown}")
    samples = load_samples(set(filter(None, args.samples.split(","))))
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    for strategy in args.strategy.split(","):
        results = []
        for sample in samples:
            for mode in modes:
                for rep in range(1, args.repeat + 1):
                    r = {"sample_id": sample["sample_id"], "mode": mode, "repeat": rep,
                         **run_one(strategy, mode, sample)}
                    results.append(r)
                    sc = r.get("score") or {}
                    shown = (f"core {sc.get('core_hit')}/{sc.get('core_total')}" if mode == "references"
                             else f"found={sc.get('found')} rank={sc.get('best_rank')}") if sc else r.get("error")
                    print(f"[{strategy}] {sample['sample_id']} {mode} #{rep}: {shown} | {r['queries']}")
        out = RUNS_DIR / f"{stamp}_{strategy}.json"
        out.write_text(json.dumps({"strategy": strategy, "modes": modes, "repeat": args.repeat, "limit": LIMIT,
                                   "note": args.note, "results": results}, ensure_ascii=False, indent=1), "utf-8")
        line = (f"| {stamp} | {strategy} | {len(samples)}×{args.repeat} | "
                f"{summarize(results, 'references') or '-'} | {summarize(results, 'verify') or '-'} | "
                f"[{out.name}](runs/{out.name}) | {args.note} |")
        if not SUMMARY.exists():
            SUMMARY.write_text("# 검색어 재현율 평가 기록\n\n"
                               "③ references: T 발표일 이전 검색, 검색어별 20편 합집합에 든 core 논문 수. "
                               "④ verify: 날짜 제한 없이 검색했을 때 원 논문 T가 나온 샘플 수와 검색어별 최고 순위.\n\n"
                               "| 시각 | 전략 | 샘플×반복 | ③ references | ④ verify | 결과 | 메모 |\n"
                               "|---|---|---|---|---|---|---|\n", "utf-8")
        with SUMMARY.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)


if __name__ == "__main__":
    main()
