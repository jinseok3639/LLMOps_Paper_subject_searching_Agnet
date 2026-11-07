"""최종 측정의 Codex 판정을 작은 묶음으로 나눠, 하나씩 돌리며 사용량을 확인할 수 있게 한다.

  python eval/baseline_comparison/run_final_judging.py            묶음 목록과 진행 상황
  python eval/baseline_comparison/run_final_judging.py <묶음>     그 묶음만 판정하고 집계

묶음은 위에서부터 중요한 순서다. 1~6만 하면 주 가설(B > A)의 답이 나온다.
끝난 판정은 건너뛰므로 같은 묶음을 다시 실행하면 이어서 한다. Codex 사용량은 https://chatgpt.com/settings/usage 에서 본다.
"""
import argparse
import shutil
import subprocess
import sys

import common

HERE = common.HERE
PY = sys.executable
SAMPLES = ("t006", "t007", "t008", "t009", "t010")
FINAL = common.RUNS / "final"

# (이름, 설명, 판정 대상 run, 필터). 필터는 judge.py 옵션과 같다.
BATCHES = [
    ("dev4-t003", "B 방법론 결정용: dev_v4 수정본 점수 t003", "dev_v4/stage", {"kinds": "score", "stages": "refined", "samples": "t003"}),
    ("dev4-t004", "B 방법론 결정용: dev_v4 수정본 점수 t004", "dev_v4/stage", {"kinds": "score", "stages": "refined", "samples": "t004"}),
    ("dev4-t005", "B 방법론 결정용: dev_v4 수정본 점수 t005", "dev_v4/stage", {"kinds": "score", "stages": "refined", "samples": "t005"}),
    ("prep", "B 방법론 결정(v3/v4), B 결과 복사, Codex 사전 점검(호출 2), 블라인드 포장", None, None),
    ("ab-pair", "주 지표 1: A-B 세트 쌍대 비교 (두 순서)", "final", {"kinds": "pairwise", "pairs": "A-B"}),
    ("a-score", "주 지표 2: A 후보 점수", "final", {"kinds": "score", "conditions": "A"}),
    ("b-score", "주 지표 2: B 후보 점수", "final", {"kinds": "score", "conditions": "B"}),
    ("c-score", "기준점: C(Opus) 후보 점수 → 공백 메움 비율", "final", {"kinds": "score", "conditions": "C"}),
    ("d-score", "기준점: D(Sonnet) 후보 점수", "final", {"kinds": "score", "conditions": "D"}),
    ("bc-pair", "기준점: B-C 쌍대 비교", "final", {"kinds": "pairwise", "pairs": "B-C"}),
    ("bd-pair", "기준점: B-D 쌍대 비교", "final", {"kinds": "pairwise", "pairs": "B-D"}),
    ("acd-pair", "선택: A-C, A-D 쌍대 비교 (모델 차이)", "final", {"kinds": "pairwise", "pairs": "A-C,A-D"}),
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


def choose():
    """사전 규칙 (2026-11-07, 확인용 결과를 보기 전에 정함):
    v4는 토너먼트 투표 기준만 바꾼 개선이므로, 그 목적 지표인 개발용 샘플의 평균 선별 손실(이상적 3개 − 최종 3개)로 정한다.
    v4의 선별 손실이 v3(dev_v3)보다 작으면 v4, 같거나 크면 v3. 선별 손실은 수정본 점수만으로 계산한다."""
    loss = {}
    for v in ("v3", "v4"):
        rep = common.read_json(common.RUNS / f"dev_{v}" / "stage" / "stage_report.json")
        vals = [r["selection_loss"] for r in rep.values() if r["selection_loss"] is not None]
        if len(vals) != 3:
            raise SystemExit(f"dev_{v}의 선별 손실이 샘플 3개 모두 계산되지 않았습니다. dev4-t003~t005 묶음을 먼저 끝내세요: {vals}")
        loss[v] = sum(vals) / len(vals)
    return ("v4" if loss["v4"] < loss["v3"] else "v3"), loss


def prep():
    run_script("dev_v4 단계별 집계", ["stage_eval.py", "report", "--run", "dev_v4", "--ref-run", "dev_v2"])
    decision_path = FINAL / "b_method.json"
    if decision_path.exists():
        decision = common.read_json(decision_path)
        print(f"\n=== B 방법론: 이미 정함 → {decision['method']}")
    else:
        pick, loss = choose()
        decision = {"method": pick, "rule": choose.__doc__.strip(), "selection_loss_dev": loss}
        common.write_json(decision_path, decision)
        print(f"\n=== B 방법론 결정: {pick} (개발용 평균 선별 손실 v3 {loss['v3']:.2f}, v4 {loss['v4']:.2f})")
    src = common.RUNS / f"final_B_{decision['method']}" / "gen"
    for s in SAMPLES:
        for suffix in ("B.json", "B_trace.json"):
            if not (FINAL / "gen" / f"{s}_{suffix}").exists():
                shutil.copy2(src / f"{s}_{suffix}", FINAL / "gen" / f"{s}_{suffix}")
    print(f"=== B 생성 결과: {src}")
    if not (FINAL / "key.json").exists():
        run_script("Codex 사전 점검", ["check.py", "--run", "final", "--part", "judge"])
        run_script("블라인드 포장", ["pack.py", "--run", "final"])


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
        if run.startswith("dev_v4"):
            run_script("dev_v4 단계별 집계", ["stage_eval.py", "report", "--run", "dev_v4", "--ref-run", "dev_v2"])
        else:
            run_script("집계", ["aggregate.py", "--run", "final"])
    print()
    status()


if __name__ == "__main__":
    main()
