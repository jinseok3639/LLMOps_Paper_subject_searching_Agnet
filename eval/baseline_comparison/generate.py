"""1단계: 네 조건(A Solar 단일 호출, B Solar + 워크플로우, C Claude Opus 단일 호출, D Claude Sonnet 단일 호출)으로 후보 3개씩 만든다.

이미 결과 파일이 있는 샘플과 조건은 건너뛴다. 실패도 결과로 남기고 다시 돌리지 않는다 (--retry-failed로만 다시).
예: python eval/baseline_comparison/generate.py --run pilot --conditions A,C
"""
import argparse
import json
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import common
from src import calllog
from src import multi_agent as ma
from src.llm import chat_json

METHOD_DIR = common.ROOT / "src" / "generation_method"
B_METHOD = METHOD_DIR  # --method로 바꾼다


def format_method():
    """problems()가 쓰는 설정(summary 단어 수, 재시도 횟수)을 B와 같은 method.json에서 읽는다."""
    return ma.Method(METHOD_DIR, context="")


def check(method, proposals, valid):
    """multi_agent.propose의 검사와 같다."""
    n = common.N_CANDIDATES
    out = [] if len(proposals.hypotheses) == n else [f"returned {len(proposals.hypotheses)} hypotheses instead of {n}"]
    return out + [f"hypothesis {i}: {p}" for i, h in enumerate(proposals.hypotheses, 1) for p in ma.problems(method, h, valid)]


class FormatFailed(ValueError):
    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


def with_format_retry(call, user, valid):
    """B의 제안 단계(multi_agent.propose)와 같은 규칙으로 최대 max_format_tries번 요청한다.
    검사를 통과한 번호의 가설은 유지하고, 실패한 번호만 다시 요청한다(2026-11-07, B의 형식 실패 수정과 맞춤).
    끝까지 3개가 채워지지 않으면 실패로 기록한다. B는 후보가 12개라 빠진 가설을 건너뛸 수 있지만 단일 호출은 그럴 수 없다."""
    method = format_method()
    n = common.N_CANDIDATES
    attempts, prompt, kept, issues = [], user, {}, []
    for _ in range(method.config["max_format_tries"]):
        proposals, raw = call(prompt)
        issues = []
        if proposals is None:
            issues = ["unparseable response"]
        else:
            for k in range(1, n + 1):
                if k in kept:
                    continue
                if k > len(proposals.hypotheses):
                    issues.append(f"hypothesis {k}: missing (return exactly {n} hypotheses)")
                    continue
                found = ma.problems(method, proposals.hypotheses[k - 1], valid)
                if found:
                    issues.append(f"hypothesis {k}: " + "; ".join(found))
                else:
                    kept[k] = proposals.hypotheses[k - 1]
        attempts.append({"issues": issues, "kept": sorted(kept), "raw": raw})
        if len(kept) == n:
            return ma.Proposals(hypotheses=[kept[k] for k in range(1, n + 1)]), attempts
        prompt = user + ma.RETRY_NOTE + "; ".join(issues)
    raise FormatFailed("format validation failed: " + "; ".join(issues), attempts)


def run_a(sample, refs):
    system = common.single_call_prompt()
    user = f"References:\n{ma.corpus_text(refs)}"

    def call(prompt):
        result = chat_json([{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                           ma.Proposals, temperature=common.SOLAR_TEMPERATURE)
        return result, result.model_dump()

    proposals, attempts = with_format_retry(call, user, {r["id"] for r in refs})
    return [h.model_dump() for h in proposals.hypotheses], {"attempts": attempts, "calls": len(attempts)}


def claude_args(system_file, cond="C"):
    model, effort = common.CLAUDE[cond]
    return [common.claude_bin(), "-p", "--model", model, "--effort", effort,
            "--system-prompt-file", str(system_file), "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(common.proposals_schema())]


def call_claude(system, user, timeout=1800, cond="C"):
    """빈 임시 폴더에서 Claude CLI를 한 번 부른다. 반환: (CLI의 JSON 출력, 구조화된 답)."""
    with tempfile.TemporaryDirectory(prefix="bc_claude_") as tmp:
        system_file = Path(tmp) / "system.md"
        system_file.write_text(system, "utf-8")
        work = Path(tmp) / "work"
        work.mkdir()
        proc = subprocess.run(claude_args(system_file, cond), input=user, capture_output=True, text=True,
                              encoding="utf-8", cwd=work, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude 종료 코드 {proc.returncode}: {proc.stderr[-2000:] or proc.stdout[-2000:]}")
    out = json.loads(proc.stdout)
    answer = out.get("structured_output")
    if answer is None:
        text = (out.get("result") or "").strip().removeprefix("```json").removesuffix("```").strip()
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            answer = None
    return out, answer


def run_claude(cond, sample, refs):
    system = common.single_call_prompt()
    user = f"References:\n{ma.corpus_text(refs)}"
    usage = []

    def call(prompt):
        out, answer = call_claude(system, prompt, cond=cond)
        usage.append({k: out.get(k) for k in ("usage", "total_cost_usd", "duration_ms", "num_turns", "modelUsage")})
        try:
            return ma.Proposals.model_validate(answer), out
        except Exception:  # noqa: BLE001  형식이 틀리면 재시도 대상
            return None, out

    proposals, attempts = with_format_retry(call, user, {r["id"] for r in refs})
    model, effort = common.CLAUDE[cond]
    return [h.model_dump() for h in proposals.hypotheses], {"attempts": attempts, "calls": len(attempts), "usage": usage,
                                                            "model": model, "effort": effort}


def run_b(sample, refs, run):
    method = ma.Method(B_METHOD, context="", progress=ma.log)
    generated = ma.run_sample(method, sample, refs, common.B_SEED)
    common.write_json(run / "gen" / f"{sample}_B_trace.json", generated)
    by_id = {c["id"]: c for c in generated["candidates"]}
    chosen = [{f: by_id[cid]["refined"][f] for f in common.FIELDS} for cid in generated["chosen"]]
    dropped = {a: i for a, i in generated["propose_problems"].items() if i}
    return chosen, {"calls": method.calls, "chosen_ids": generated["chosen"], "method": str(B_METHOD.relative_to(common.ROOT)),
                    "method_version": method.config.get("version"), "dropped_proposals": dropped,
                    "refine_failed": [c["id"] for c in generated["candidates"] if c.get("refine_failed")]}


RUNNERS = {"A": run_a, "B": run_b, "C": lambda s, r: run_claude("C", s, r), "D": lambda s, r: run_claude("D", s, r)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True, help="runs/ 아래 폴더 이름")
    ap.add_argument("--samples", required=True, help="예: t003,t004,t005. 확인용 샘플을 실수로 돌리지 않도록 반드시 적는다")
    ap.add_argument("--conditions", default="A,C,B")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--method", default="src/generation_method", help="B가 쓸 방법론 폴더 (저장소 루트 기준)")
    args = ap.parse_args()
    global B_METHOD
    B_METHOD = common.ROOT / args.method

    run = common.run_dir(args.run)
    calllog.ROOT = run / "calllog"  # 앱의 DB/logs와 섞지 않는다
    ma.LOG_FILE = run / "progress.log"
    for sample in common.samples(args.samples):
        refs = common.refs(sample)
        for cond in args.conditions.split(","):
            out = run / "gen" / f"{sample}_{cond}.json"
            if out.exists() and not (args.retry_failed and common.read_json(out).get("failed")):
                ma.log(f"{sample} {cond} 건너뜀 (결과 있음)")
                continue
            ma.log(f"{sample} {cond} 시작 (참고문헌 {len(refs)}편)")
            t0 = time.time()
            record = {"sample": sample, "condition": cond, "n_refs": len(refs)}
            try:
                fn = RUNNERS[cond]
                candidates, info = fn(sample, refs, run) if cond == "B" else fn(sample, refs)
                record.update(failed=False, candidates=candidates, **info)
            except Exception as e:  # noqa: BLE001  실패도 결과로 남긴다
                record.update(failed=True, error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc(),
                              attempts=getattr(e, "attempts", None))  # 형식 실패면 시도별 원문을 남긴다
            record["seconds"] = round(time.time() - t0, 1)
            common.write_json(out, record)
            ma.log(f"{sample} {cond} {'실패: ' + record['error'] if record['failed'] else '완료'} ({record['seconds']}초)")


if __name__ == "__main__":
    main()
