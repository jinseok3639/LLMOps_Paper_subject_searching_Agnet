"""5단계: 사람 검증용 후보를 조건별로 골라 블라인드 채점지를 만든다.

human/sheet.md에서 후보를 읽고 human/scores.json에 항목 1~5 점수(정수)를 채운다. key.json은 열지 않는다.
채운 뒤 aggregate.py를 다시 돌리면 일치율이 나온다.
예: python eval/baseline_comparison/human_sample.py --run pilot --per-condition 3
"""
import argparse
import json
import random

import common


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--per-condition", type=int, default=3)
    args = ap.parse_args()

    run = common.run_dir(args.run)
    out = run / "human" / "scores.json"
    if out.exists():
        raise SystemExit(f"이미 있습니다: {out}")
    key = common.read_json(run / "key.json")
    rng = random.Random(f"human-{args.run}")
    picked = []
    for cond in common.CONDITIONS:
        jobs = sorted(j for j, job in key.items() if job["kind"] == "score" and job["condition"] == cond)
        picked += rng.sample(jobs, min(args.per_condition, len(jobs)))
    picked.sort()  # 작업 번호 순서는 이미 섞여 있다

    lines = ["# 사람 검증 채점지", "", "`rubric.md`의 항목 1~5로 채점합니다. 참고문헌은 각 작업 폴더의 `references.txt`에 있습니다.",
             "점수는 `scores.json`에 정수로 적습니다. `key.json`은 채점이 끝날 때까지 열지 않습니다.", ""]
    for jid in picked:
        cand = common.read_json(run / "jobs" / jid / "candidate.json")
        lines += [f"## {jid} ({key[jid]['sample']})", "", f"참고문헌: `jobs/{jid}/references.txt`", "", "```json",
                  json.dumps(cand, ensure_ascii=False, indent=1), "```", ""]
    (run / "human").mkdir(exist_ok=True)
    (run / "human" / "sheet.md").write_text("\n".join(lines), "utf-8")
    common.write_json(out, [{"job": j, **{c: None for c in common.CRITERIA}, "note": ""} for j in picked])
    print(f"후보 {len(picked)}개: {run / 'human' / 'sheet.md'}")


if __name__ == "__main__":
    main()
