"""2단계: 생성 결과를 블라인드 판정 폴더로 만든다.

작업 폴더(jobs/j001 ...)에는 그 판정에 필요한 파일만 둔다. 어느 조건인지는 jobs 밖의 key.json에만 남긴다.
예: python eval/baseline_comparison/pack.py --run pilot
"""
import argparse
import random
import shutil

import common
from src.multi_agent import corpus_text

PROMPT = {"score": "judge_score.md", "novelty": "judge_novelty.md", "pairwise": "judge_pairwise.md"}


def load_sets(run):
    """{(sample, cond): 섞은 후보 목록}. 실패한 조건은 뺀다."""
    sets, skipped = {}, []
    for path in sorted((run / "gen").glob("t*_?.json")):
        rec = common.read_json(path)
        key = (rec["sample"], rec["condition"])
        if rec.get("failed") or len(rec.get("candidates", [])) != common.N_CANDIDATES:
            skipped.append(key)
            continue
        order = list(range(common.N_CANDIDATES))
        random.Random(f"order-{rec['sample']}-{rec['condition']}").shuffle(order)
        sets[key] = [(i, common.normalize(rec["candidates"][i])) for i in order]
    return sets, skipped


def plan_jobs(sets, kinds=("score", "novelty", "pairwise"), pairs=None):
    """kinds, pairs로 만들 작업을 줄일 수 있다. pairs는 "A-B" 형식의 목록, None이면 common.PAIRS 전부."""
    jobs = []
    for (sample, cond), cands in sets.items():
        for i, cand in cands:
            for kind in [k for k in ("score", "novelty") if k in kinds]:
                jobs.append({"kind": kind, "sample": sample, "condition": cond, "candidate": i,
                             "files": {"candidate.json": cand}})
    for sample in sorted({s for s, _ in sets}):
        for p, q in common.PAIRS:
            if "pairwise" not in kinds or (pairs is not None and f"{p}-{q}" not in pairs):
                continue
            if (sample, p) not in sets or (sample, q) not in sets:
                continue
            for x, y in ((p, q), (q, p)):
                files, ids = {}, {}
                for side, cond in (("x", x), ("y", y)):
                    named = []
                    for n, (i, cand) in enumerate(sets[(sample, cond)], 1):
                        named.append({"id": f"{side}{n}", **cand})
                        ids[f"{side}{n}"] = i
                    files[f"set_{side}.json"] = named
                jobs.append({"kind": "pairwise", "sample": sample, "pair": f"{p}-{q}", "X": x, "Y": y, "ids": ids, "files": files})
    return jobs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--kinds", default="score,novelty,pairwise", help="만들 작업 종류")
    ap.add_argument("--pairs", default="", help="쌍대 비교 쌍만 (예: A-B,A-E). 비우면 전부")
    ap.add_argument("--force", action="store_true", help="기존 jobs와 key.json을 지우고 다시 만든다 (판정 결과도 지워진다)")
    args = ap.parse_args()

    run = common.run_dir(args.run)
    jobs_dir, key_path = run / "jobs", run / "key.json"
    if key_path.exists():
        if not args.force:
            raise SystemExit("이미 포장했습니다. 판정 결과를 지우고 다시 만들려면 --force")
        shutil.rmtree(jobs_dir, ignore_errors=True)

    sets, skipped = load_sets(run)
    jobs = plan_jobs(sets, args.kinds.split(","), set(args.pairs.split(",")) if args.pairs else None)
    random.Random(f"jobs-{args.run}").shuffle(jobs)  # 작업 번호로 조건을 짐작하지 못하게 섞는다
    refs_text = {s: corpus_text(common.refs(s)) for s in {j["sample"] for j in jobs}}
    key = {}
    for n, job in enumerate(jobs, 1):
        jid = f"j{n:03d}"
        folder = jobs_dir / jid
        folder.mkdir(parents=True)
        (folder / "instructions.md").write_text((common.PROMPTS / PROMPT[job["kind"]]).read_text("utf-8"), "utf-8")
        (folder / "references.txt").write_text(refs_text[job["sample"]], "utf-8")
        common.write_json(folder / "schema.json", common.judge_schema(job["kind"]))
        for name, data in job.pop("files").items():
            common.write_json(folder / name, data)
        key[jid] = job
    common.write_json(key_path, key)
    counts = {k: sum(j["kind"] == k for j in jobs) for k in PROMPT}
    print(f"작업 {len(jobs)}개 {counts}, 빠진 세트 {skipped}")


if __name__ == "__main__":
    main()
