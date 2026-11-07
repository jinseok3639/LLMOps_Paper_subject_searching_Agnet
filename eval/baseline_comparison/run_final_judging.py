"""최종 측정의 Codex 판정을 작은 묶음으로 나눠, 하나씩 돌리며 사용량을 확인할 수 있게 한다.

  python eval/baseline_comparison/run_final_judging.py            묶음 목록과 진행 상황
  python eval/baseline_comparison/run_final_judging.py <묶음>     그 묶음만 판정하고 집계

조건: A Solar 단일, B 워크플로우 v3(주 결과), E 워크플로우 v4(추가 결과), C Opus, D Sonnet.
주 결과를 v3로 정한 것은 판정 전(2026-11-07)이다. v3와 v4 중 결과가 좋은 쪽을 골라 주 결과로 바꾸지 않는다.
묶음은 위에서부터 중요한 순서다. prep 뒤 b3-score까지 하면 주 가설(B > A)의 답이 나온다.
끝난 판정은 건너뛰므로 같은 묶음을 다시 실행하면 이어서 한다. Codex 사용량은 https://chatgpt.com/settings/usage 에서 본다.
"""
import argparse
import subprocess
import sys

import common

HERE = common.HERE
PY = sys.executable
FINAL = common.RUNS / "final"

# (이름, 설명, 판정 대상 run, 필터). 필터는 judge.py 옵션과 같다.
BATCHES = [
    ("prep", "Codex 사전 점검(호출 2)과 블라인드 포장", None, None),
    ("ab3-pair", "주 지표 1: A-B(v3) 세트 쌍대 비교 (두 순서)", "final", {"kinds": "pairwise", "pairs": "A-B"}),
    ("a-score", "주 지표 2: A 후보 점수", "final", {"kinds": "score", "conditions": "A"}),
    ("b3-score", "주 지표 2: B(v3) 후보 점수", "final", {"kinds": "score", "conditions": "B"}),
    ("ae4-pair", "추가 결과: A-E(v4) 세트 쌍대 비교", "final", {"kinds": "pairwise", "pairs": "A-E"}),
    ("e4-score", "추가 결과: E(v4) 후보 점수", "final", {"kinds": "score", "conditions": "E"}),
    ("c-score", "기준점: C(Opus) 후보 점수 → 공백 메움 비율", "final", {"kinds": "score", "conditions": "C"}),
    ("d-score", "기준점: D(Sonnet) 후보 점수", "final", {"kinds": "score", "conditions": "D"}),
]


def run_script(title, args):
    print(f"\n=== {title}", flush=True)
    proc = subprocess.run([PY, str(HERE / args[0]), *args[1:]], cwd=common.ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"'{title}'이 멈췄습니다(종료 코드 {proc.returncode}). Codex 사용량 한도라면 한도가 풀린 뒤 같은 묶음을 다시 실행합니다.")


def matches(job, f):
    if job["kind"] not in f["kinds"].split(","):
        return False
    for field, opt in (("condition", "conditions"), ("pair", "pairs"), ("stage", "stages"), ("sample", "samples")):
        if opt in f and field in job and job[field] not in f[opt].split(","):
            return False
    return True


def counts(run, f):
    key_path = common.RUNS / run / "key.json"
    if not key_path.exists():
        return None, None
    key = common.read_json(key_path)
    jobs = [j for j, job in key.items() if matches(job, f)]
    done = sum((common.RUNS / run / "jobs" / j / "result.json").exists() for j in jobs)
    return len(jobs), done


def prep():
    """B(v3), E(v4) 생성 결과는 runs/final/gen에 이미 놓았다(NOTE.md). 여기서는 Codex 점검과 포장만 한다."""
    if (FINAL / "key.json").exists():
        print("이미 포장했습니다.")
        return
    run_script("Codex 사전 점검", ["check.py", "--run", "final", "--part", "judge"])
    run_script("블라인드 포장", ["pack.py", "--run", "final", "--kinds", "score,pairwise", "--pairs", "A-B,A-E"])


def status():
    print("묶음 (위에서부터 중요한 순서, [완료/전체])\n")
    for i, (name, desc, run, f) in enumerate(BATCHES, 1):
        if run is None:
            mark = "완료" if (FINAL / "key.json").exists() else "대기"
        else:
            total, done = counts(run, f)
            mark = "포장 전" if total is None else f"{done}/{total}"
        print(f"{i:>2}. {name:<10} [{mark:>7}]  {desc}")
    print("\n실행: python eval/baseline_comparison/run_final_judging.py <묶음 이름>")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("batch", nargs="?", help="묶음 이름. 비우면 목록과 진행 상황")
    ap.add_argument("--workers", default="3")
    args = ap.parse_args()
    if not args.batch:
        status()
        return
    found = {b[0]: b for b in BATCHES}
    if args.batch not in found:
        raise SystemExit(f"없는 묶음: {args.batch}. 가능한 것: {', '.join(found)}")
    name, desc, run, f = found[args.batch]
    if run is None:
        prep()
    else:
        total, done = counts(run, f)
        if total is None:
            raise SystemExit("아직 포장하지 않았습니다. prep 묶음을 먼저 실행하세요.")
        print(f"{name}: {desc} — 남은 판정 {total - done}개")
        opts = []
        for k, v in f.items():
            opts += [f"--{k}", v]
        run_script(f"판정 {name}", ["judge.py", "--run", run, "--workers", args.workers, *opts])
        run_script("집계", ["aggregate.py", "--run", "final"])
    print()
    status()


if __name__ == "__main__":
    main()
