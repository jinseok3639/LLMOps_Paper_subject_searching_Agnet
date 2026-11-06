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

from src import llm, references, verify  # noqa: E402
import requests  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from src.clients import semantic_scholar as s2  # noqa: E402

SAMPLES_DIR = HERE / "samples"
CACHE_DIR = HERE / "cache"
RUNS_DIR = HERE / "runs"
SUMMARY = HERE / "summary.md"
PROMPTS = HERE / "prompts"
CS = "Computer Science"
FEEDBACK_TITLES = 10  # rewrite·seed 선택이 보는 검색어별 상위 제목 수
N_SEEDS = 5           # 확장에 쓰는 seed 논문 수
REFS_PER_SEED = 20    # graph: seed마다 참고문헌을 인용 수 순으로 몇 편 쓸지 (검색어 하나의 결과 수와 같게)
N_RECS = 100          # recommend: 추천 받을 논문 수 (검색어 5개 × 20편과 같게)
REC_URL = "https://api.semanticscholar.org/recommendations/v1/papers"
LIMIT = references.SEARCH_LIMIT  # 앱과 같은 검색어당 결과 수
FIELDS = "title,year,publicationDate"
MODES = ("references", "verify")


# ---------- 전략: (topic, find) -> 검색어 목록 또는 (검색어 목록, trace) ----------
# find(query)는 그 모드의 검색 조건(날짜, 분야)으로 S2를 검색한다 (캐시). 결과를 보고 검색어를 고치는 전략이 쓴다.

def references_problem(topic) -> dict:
    """③ 입력 근사: 샘플에는 Open Problem 카드가 없으므로 gap을 선택한 문제로 넣는다 (방법은 넣지 않는다)."""
    return {"summary": topic["gap"]}


def references_input(topic) -> str:
    return references.researcher_context({}, [], references_problem(topic))


def verify_input(topic) -> str:
    return f"Research candidate:\n{verify.candidate_text(topic)}"


def current_references(topic, find=None, before=None):
    return references.write_queries(references_input(topic))


def current_verify(topic, find=None, before=None):
    return verify.write_queries(topic)[1]


def clean(queries, min_words=2, max_words=6):
    """min_words 미만은 버리고 max_words 넘으면 자른다. 중복 제거."""
    return list(dict.fromkeys(" ".join(q.split()[:max_words]) for q in queries if len(q.split()) >= min_words))


def _prompted(prompt, user, schema):
    system = (PROMPTS / f"{prompt}.md").read_text("utf-8")
    out = clean(llm.chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}], schema).queries)
    if not out:
        raise ValueError(f"{prompt}: 쓸 수 있는 검색어가 없습니다")
    return out


def facet_references(topic, find=None, before=None):
    return _prompted("facet_references", references_input(topic), references.Queries)


def facet_verify(topic, find=None, before=None):
    return _prompted("facet_verify", verify_input(topic), verify.Queries)


def rewrite(base, user, schema):
    """base 전략의 검색어로 1차 검색 → 상위 제목을 보여 주고 검색어를 다시 쓴다. 채점은 다시 쓴 검색어로만 한다."""
    def strategy(topic, find, before=None):
        first = base(topic, find, before)
        rounds = [find(q) for q in first]
        listing = "\n\n".join(f"Query {i}: {s['query']}\n" + "\n".join(f"- {r['title']}" for r in s["results"][:FEEDBACK_TITLES])
                                for i, s in enumerate(rounds, 1))
        queries = _prompted("rewrite", f"Research description:\n{user(topic)}\n\n"
                                       f"First-round queries and top titles:\n{listing}", schema)
        return queries, {"first_queries": first}
    return strategy


class Seeds(BaseModel):
    ids: list[str]


def pick_seeds(user, rounds):
    """검색어별 상위 제목에서 Solar가 seed 논문을 고른다 (Solar 1회). [(paperId, title)]"""
    pool = list({r["paperId"]: r for s in rounds for r in s["results"][:FEEDBACK_TITLES]}.values())
    ids = {f"p{i}": r for i, r in enumerate(pool, 1)}
    listing = "\n".join(f"[{k}] {r['title']} ({r.get('year') or '?'})" for k, r in ids.items())
    system = (PROMPTS / "seed_select.md").read_text("utf-8")
    out = llm.chat_json([{"role": "system", "content": system},
                         {"role": "user", "content": f"Research description:\n{user}\n\nPapers:\n{listing}"}], Seeds)
    picked = [ids[k.strip().strip("[]")] for k in out.ids if k.strip().strip("[]") in ids]
    return [(r["paperId"], r["title"]) for r in list({r["paperId"]: r for r in picked}.values())[:N_SEEDS]]


def _rows(rows, before):
    """확장 결과 정리. before가 있으면 그 날짜 이후 논문을 뺀다 (발표일이 없으면 연도로)."""
    out = []
    for r in rows:
        if not r or not r.get("paperId"):
            continue
        when = r.get("publicationDate") or (f"{r['year']}-01-01" if r.get("year") else None)
        if before and (not when or when > before):
            continue
        out.append({"paperId": r["paperId"], "title": r.get("title"), "year": r.get("year")})
    return out


def seed_references(seed_id, before):
    """seed의 참고문헌 중 인용 수 상위 REFS_PER_SEED편 (S2 1회, 캐시)."""
    data, cached = _cached_get(f"/paper/{seed_id}/references",
                               {"fields": "title,year,publicationDate,citationCount", "limit": 1000})
    rows = sorted((d.get("citedPaper") or {} for d in data.get("data") or []),
                  key=lambda r: r.get("citationCount") or 0, reverse=True)
    return _rows(rows, before)[:REFS_PER_SEED], cached


def recommend_papers(seed_ids, before):
    """S2 Recommendations (SPECTER 임베딩 이웃). seed를 positive로 넣는다 (S2 1회, 캐시)."""
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha256(json.dumps(["recs", sorted(seed_ids), N_RECS]).encode()).hexdigest()
    file = CACHE_DIR / f"{key}.json"
    cached = file.exists()
    if cached:
        data = json.loads(file.read_text("utf-8"))
    else:
        data = _post(REC_URL, {"positivePaperIds": seed_ids, "negativePaperIds": []},
                     {"fields": "title,year,publicationDate", "limit": N_RECS})
        file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return _rows(data.get("recommendedPapers") or [], before), cached


def recommend_for_paper(seed_id, before):
    """seed 한 편의 이웃을 CS 전체(from=all-cs)에서 추천받아 날짜 거른 뒤 상위 REFS_PER_SEED편 (S2 1회, 캐시).
    여러 편을 넣는 POST 추천은 최근 논문만 돌려줘서(2026년 98/100) ③에는 이것을 쓴다."""
    params = {"fields": "title,year,publicationDate", "limit": 100, "from": "all-cs"}
    CACHE_DIR.mkdir(exist_ok=True)
    file = CACHE_DIR / (hashlib.sha256(json.dumps(["forpaper", seed_id, params], sort_keys=True).encode()).hexdigest() + ".json")
    cached = file.exists()
    if cached:
        data = json.loads(file.read_text("utf-8"))
    else:
        data = _post(f"{REC_URL}/forpaper/{seed_id}", None, params, method="GET")
        file.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return _rows(data.get("recommendedPapers") or [], before)[:REFS_PER_SEED], cached


def _post(url, body, params, tries=6, method="POST"):
    """S2 Recommendations 호출 (graph/v1 밖이라 s2.get을 못 쓴다). 앱 클라이언트와 같은 대기열을 지난다.
    실험용이라 여기 둔다 (채택하면 clients로 옮긴다)."""
    key = s2._api_key()
    headers = {"x-api-key": key} if key else {}
    wait, status = s2.BACKOFF, None
    for _ in range(tries):
        s2._queue.pass_through(s2.INTERVAL_KEY if key else s2.INTERVAL_PUBLIC)
        try:
            resp = requests.request(method, url, json=body, params=params, headers=headers, timeout=s2.TIMEOUT)
        except requests.RequestException:
            status = None
        else:
            if resp.status_code == 200:
                return resp.json()
            status = resp.status_code
            if status != 429 and status < 500:
                raise s2.S2Error(f"S2 {status} {url}: {resp.text[:300]}", status)
        s2._queue.pause(wait)
        wait = min(wait * 2, s2.MAX_BACKOFF)
    raise s2.S2Error(f"S2 재시도 초과 {url}", status)


def expand(base, user, how):
    """base 전략의 검색 결과는 그대로 두고, Solar가 고른 seed로 논문을 더한다.
    how: graph (seed의 참고문헌) | recommend (seed를 positive로 넣은 추천)."""
    def strategy(topic, find, before=None):
        queries = base(topic, find, before)
        seeds = pick_seeds(user(topic), [find(q) for q in queries])
        extra = []
        if seeds and how == "graph":
            for sid, title in seeds:
                rows, cached = seed_references(sid, before)
                extra.append({"query": f"references of: {title}", "retried_query": None, "cached": cached, "results": rows})
        elif seeds and how == "recommend_each":
            for sid, title in seeds:
                rows, cached = recommend_for_paper(sid, before)
                extra.append({"query": f"recommendations for: {title}", "retried_query": None, "cached": cached, "results": rows})
        elif seeds:
            rows, cached = recommend_papers([sid for sid, _ in seeds], before)
            extra.append({"query": "recommendations from seeds", "retried_query": None, "cached": cached, "results": rows})
        return {"queries": queries, "trace": {"seeds": seeds}, "extra": extra}
    return strategy


STRATEGIES = {
    "current": {"references": current_references, "verify": current_verify},
    # 1. 검색어 하나에 개념 1~2개, 문제·기법·대상을 나누고 분야 표준 용어로
    "facet": {"references": facet_references, "verify": facet_verify},
    # 2. 검색어는 current 그대로, S2 분야 필터만 Computer Science
    "cs_filter": {"references": current_references, "verify": current_verify, "fos": CS},
    # 3. current 검색어로 1차 검색 → 결과 제목을 보고 다시 쓰기 (Solar 2회, S2 2배)
    "rewrite": {"references": rewrite(current_references, references_input, references.Queries),
                "verify": rewrite(current_verify, verify_input, verify.Queries)},
    # 4. facet 검색 + Solar가 고른 seed 5편의 참고문헌 (인용 수 상위 20편씩).
    #    ④는 뺀다: 참고문헌은 seed보다 옛 논문이라 더 새로운 원 논문이 나올 수 없다
    "graph": {"references": expand(facet_references, references_input, "graph")},
    # 5. facet 검색 + S2 Recommendations.
    #    ③: seed마다 CS 전체에서 이웃 20편 (옛 논문까지). ④: seed 5편을 함께 넣은 최근 논문 추천 100편
    "recommend": {"references": expand(facet_references, references_input, "recommend_each"),
                  "verify": expand(facet_verify, verify_input, "recommend")},
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


def search(query, before=None, fos=None):
    """앱과 같은 검색 (검색어당 LIMIT편, 분야 필터 없음). before가 있으면 그 날짜 이전 논문만, fos가 있으면 그 분야만."""
    params = {"query": query, "fields": FIELDS, "limit": LIMIT}
    if before:
        params["publicationDateOrYear"] = f":{before}"
    if fos:
        params["fieldsOfStudy"] = fos
    data, cached = _cached_get("/paper/search", params)
    return [r for r in data.get("data") or [] if r.get("paperId")], cached


def search_like_app(query, before=None, fos=None):
    """결과가 0건이면 앞 두 단어로 한 번 더 (references._search를 쓰는 앱 흐름과 같다)."""
    rows, cached = search(query, before, fos)
    used = query
    if not rows:
        used = " ".join(query.split()[:2])
        rows, c2 = search(used, before, fos)
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
    topic, spec = sample["topic"], STRATEGIES[strategy]
    before = (date.fromisoformat(sample["published_at"]) - timedelta(days=1)).isoformat() if mode == "references" else None
    fos = spec.get("fos")

    def find(q):
        return search_like_app(q, before, fos)
    if mode not in spec:
        return {"skipped": f"{strategy}는 {mode}를 지원하지 않습니다", "queries": []}
    try:
        made = spec[mode](topic, find, before)
    except Exception as e:  # 검색어 생성 실패도 결과로 남긴다 (점수 0)
        return {"error": f"{type(e).__name__}: {str(e)[:300]}", "queries": []}
    if isinstance(made, dict):  # 확장 전략: 검색어 결과 + 확장 결과(extra)
        queries, trace, extra = made["queries"], made.get("trace"), made.get("extra") or []
    else:
        queries, trace = made if isinstance(made, tuple) else (made, None)
        extra = []
    searches = [find(q) for q in queries]
    out = {"queries": queries, "fos": fos, **({"trace": trace} if trace else {})}
    if mode == "references":
        only = {"score_search_only": score_references(searches, sample["gold"])} if extra else {}
        return {**out, "before": before, "score": score_references(searches + extra, sample["gold"]), **only,
                "searches": searches, "extra": extra}
    target = source_paper_id(sample["source_paper"])
    only = {"score_search_only": score_verify(searches, target)} if extra else {}
    return {**out, "target_paper_id": target, "score": score_verify(searches + extra, target), **only,
            "searches": searches, "extra": extra}


def summarize(results, mode):
    rows = [r for r in results if r["mode"] == mode and "skipped" not in r]
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
                             else f"found={sc.get('found')} rank={sc.get('best_rank')}") if sc else (r.get("error") or r.get("skipped"))
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
