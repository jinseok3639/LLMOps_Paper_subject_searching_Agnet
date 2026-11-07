"""3단계: 판정 작업을 Codex CLI로 하나씩 돌린다.

판정 데이터는 프롬프트에 직접 넣고, 실험 폴더 밖의 빈 임시 폴더에서 돌린다. Codex가 key.json이나 생성 결과를 볼 일이 없게 하기 위해서다.
result.json이 있는 작업은 건너뛰므로, 끊기면 그대로 다시 실행한다. 사용량 한도에 걸리면 멈춘다.
예: python eval/baseline_comparison/judge.py --run pilot --kinds score,pairwise --limit 5
"""
import argparse
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import common

LIMIT_HIT = re.compile(r"usage limit|rate limit|quota|429|too many requests", re.I)
DATA_FILES = ("references.txt", "candidate.json", "set_x.json", "set_y.json")


def build_prompt(folder):
    """지시문 뒤에 판정 데이터를 붙인다. Codex가 파일을 읽지 않아도 되게 한다."""
    parts = [(folder / "instructions.md").read_text("utf-8"), "\n# Input"]
    for name in DATA_FILES:
        if (folder / name).exists():
            parts.append(f'<file name="{name}">\n{(folder / name).read_text("utf-8")}\n</file>')
    return "\n\n".join(parts)


def codex_args(kind, folder):
    args = [common.codex_bin()]
    if kind == "novelty":
        args.append("--search")
    return args + ["exec", "-C", str(folder), "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                   "-s", "read-only", "-m", common.JUDGE_MODEL, "-c", f'model_reasoning_effort="{common.JUDGE_EFFORT}"',
                   "--output-schema", str(folder / "schema.json"), "-o", str(folder / "result.json"), "-"]


def run_codex(kind, job_dir, timeout):
    """반환: (결과 dict 또는 None, 로그 텍스트)."""
    with tempfile.TemporaryDirectory(prefix="bc_judge_") as tmp:
        work = Path(tmp) / "job"
        shutil.copytree(job_dir, work, ignore=shutil.ignore_patterns("result*.json", "codex*.log"))
        prompt = build_prompt(work)
        proc = subprocess.run(codex_args(kind, work), input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=work, timeout=timeout)
        log = f"$ exit {proc.returncode}\n--- stdout\n{proc.stdout[-20000:]}\n--- stderr\n{proc.stderr[-20000:]}"
        out = work / "result.json"
        if proc.returncode != 0 or not out.exists():
            return None, log
        try:
            return json.loads(out.read_text("utf-8")), log
        except json.JSONDecodeError:
            return None, log


class LimitHit(Exception):
    pass


def judge_one(jid, job, job_dir, timeout, stop):
    """작업 하나를 판정한다. 성공하면 True. 사용량 한도면 LimitHit."""
    kind = job["kind"]
    meta = {"model": common.JUDGE_MODEL, "effort": common.JUDGE_EFFORT, "attempts": []}
    result = None
    for attempt in range(2):  # 스키마에 맞지 않으면 한 번 더
        if stop.is_set():
            return None
        t0 = time.time()
        result, log = run_codex(kind, job_dir, timeout)
        (job_dir / f"codex_{attempt + 1}.log").write_text(log, "utf-8")
        ok = result is not None and common.valid_result(kind, result)
        meta["attempts"].append({"ok": ok, "seconds": round(time.time() - t0, 1)})
        if ok:
            break
        if LIMIT_HIT.search(log.split("--- stderr", 1)[-1]):
            common.write_json(job_dir / "result_meta.json", meta)
            raise LimitHit(f"{jid}: 사용량 한도로 보입니다. 로그: {job_dir / f'codex_{attempt + 1}.log'}")
    common.write_json(job_dir / "result_meta.json", meta)
    if result is not None and common.valid_result(kind, result):
        common.write_json(job_dir / "result.json", result)
        return True
    common.write_json(job_dir / "result_failed.json", {"kind": kind, "result": result})
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--kinds", default="score,pairwise,novelty")
    ap.add_argument("--jobs", default="", help="예: j001,j002 (비우면 전부)")
    ap.add_argument("--conditions", default="", help="점수·새로움 작업을 이 조건만 (예: A,B)")
    ap.add_argument("--pairs", default="", help="쌍대 비교를 이 쌍만 (예: A-B,B-C)")
    ap.add_argument("--stages", default="", help="단계별 판정을 이 단계만 (예: refined)")
    ap.add_argument("--samples", default="", help="이 샘플만 (예: t003)")
    ap.add_argument("--limit", type=int, default=0, help="이번 실행에서 돌릴 최대 작업 수")
    ap.add_argument("--workers", type=int, default=3, help="동시에 돌릴 Codex 수. 작업끼리는 서로 영향이 없다")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    run = common.run_dir(args.run)
    key = common.read_json(run / "key.json")
    kinds = args.kinds.split(",")
    wanted = set(args.jobs.split(",")) if args.jobs else None
    conds = set(args.conditions.split(",")) if args.conditions else None
    pairs = set(args.pairs.split(",")) if args.pairs else None
    stages = set(args.stages.split(",")) if args.stages else None
    samples = set(args.samples.split(",")) if args.samples else None

    def selected(job):
        if conds and "condition" in job and job["condition"] not in conds:
            return False
        if pairs and job["kind"] == "pairwise" and job["pair"] not in pairs:
            return False
        if samples and job["sample"] not in samples:
            return False
        return not (stages and "stage" in job and job["stage"] not in stages)

    todo = [j for j in sorted(key) if key[j]["kind"] in kinds and (wanted is None or j in wanted) and selected(key[j])
            and not (run / "jobs" / j / "result.json").exists()]
    if args.limit:
        todo = todo[:args.limit]
    print(f"남은 작업 {len(todo)}개, 동시 {args.workers}개", flush=True)
    stop, done, failed, limit = threading.Event(), [], [], []
    with ThreadPoolExecutor(args.workers) as ex:
        futures = {ex.submit(judge_one, j, key[j], run / "jobs" / j, args.timeout, stop): j for j in todo}
        for fut in as_completed(futures):
            jid = futures[fut]
            try:
                ok = fut.result()
            except LimitHit as e:
                stop.set()
                limit.append(str(e))
                continue
            except Exception as e:  # noqa: BLE001  한 작업의 예외로 전체를 멈추지 않는다
                ok = False
                (run / "jobs" / jid / "judge_error.txt").write_text(f"{type(e).__name__}: {e}", "utf-8")
            if ok is None:
                continue
            (done if ok else failed).append(jid)
            print(f"[{len(done) + len(failed)}/{len(todo)}] {jid} {key[jid]['kind']} {'완료' if ok else '실패'}", flush=True)
    print(f"완료 {len(done)}, 실패 {len(failed)} {sorted(failed)}", flush=True)
    if limit:
        raise SystemExit("사용량 한도로 멈췄습니다. 나중에 다시 실행하면 이어서 합니다. " + limit[0])


if __name__ == "__main__":
    main()
