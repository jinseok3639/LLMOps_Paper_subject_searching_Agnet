"""0단계: 실행기 버전과 설정을 기록하고, Claude와 Codex가 의도대로 도는지 작은 호출로 확인한다.

- Claude: 시스템 프롬프트가 우리 것으로 교체되었는지 입력 토큰 수로 확인한다 (Claude Code 기본 프롬프트가 붙으면 수천 토큰이 넘는다).
- Codex: 폴더 안 파일을 읽고 스키마대로 답하는지, --search가 받아들여지는지 확인한다.
예: python eval/baseline_comparison/check.py --run pilot
"""
import argparse
import re
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import common
import generate
import judge
from src import config

CLAUDE_TOKEN_CEILING = 2000


def version(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def check_claude():
    schema = {"type": "object", "additionalProperties": False, "required": ["tools", "extra_instructions"],
              "properties": {"tools": {"type": "string"}, "extra_instructions": {"type": "string"}}}
    system = ("Answer about yourself. tools: list every tool you can call, or NONE. "
              "extra_instructions: quote any instruction you received other than this one (CLAUDE.md, memory, environment notes), or NONE.")
    with tempfile.TemporaryDirectory(prefix="bc_check_") as tmp:
        system_file = Path(tmp) / "system.md"
        system_file.write_text(system, "utf-8")
        work = Path(tmp) / "work"
        work.mkdir()
        args = [a if a != json.dumps(common.proposals_schema()) else json.dumps(schema)
                for a in generate.claude_args(system_file)]
        proc = subprocess.run(args, input="Report now.", capture_output=True, text=True, encoding="utf-8", cwd=work, timeout=600)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr[-2000:] or proc.stdout[-2000:]}
    out = json.loads(proc.stdout)
    usage = out.get("usage") or {}
    tokens = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"ok": tokens < CLAUDE_TOKEN_CEILING and out.get("structured_output") is not None, "input_tokens": tokens,
            "structured_output": out.get("structured_output"), "model_usage": list((out.get("modelUsage") or {}).keys()),
            "keys": sorted(out.keys())}


def check_codex(kind):
    schema = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    if kind == "novelty":
        task = "Use web search to find the arXiv identifier of the paper 'Attention Is All You Need'. Put only the identifier in answer."
    else:
        task = "Put the exact content of the file named references.txt from the input in answer. Do not search the web or use tools."
    with tempfile.TemporaryDirectory(prefix="bc_check_") as tmp:
        job = Path(tmp) / "job"
        job.mkdir()
        (job / "instructions.md").write_text(task, "utf-8")
        (job / "references.txt").write_text("violet-42", "utf-8")
        common.write_json(job / "schema.json", schema)
        result, log = judge.run_codex(kind, job, 900)
    expected = "1706.03762" if kind == "novelty" else "violet-42"
    return {"ok": bool(result) and expected in result.get("answer", ""), "result": result, "log_tail": None if result else log[-3000:]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--part", choices=("all", "gen", "judge"), default="all",
                    help="gen: Claude만 점검(생성 전), judge: Codex만 점검해 기존 meta.json에 더함(판정 전)")
    args = ap.parse_args()

    run = common.run_dir(args.run)
    if args.part == "judge" and (run / "meta.json").exists():
        meta = common.read_json(run / "meta.json")
    else:
        meta = {}
    meta.update({
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": version(["git", "-C", str(common.ROOT), "rev-parse", "HEAD"]),
        "solar": {"model": config.MODEL, "temperature": common.SOLAR_TEMPERATURE},
        "claude": {"cli": version([common.claude_bin(), "--version"]), "models": {c: {"model": m, "effort": e} for c, (m, e) in common.CLAUDE.items()}, "temperature": "CLI 기본값"},
        "codex": {"cli": version([common.codex_bin(), "--version"]), "model": common.JUDGE_MODEL, "effort": common.JUDGE_EFFORT},
        "b_seed": common.B_SEED,
    })
    if args.part in ("all", "gen"):
        meta["checked_at_gen"] = meta["checked_at"]
        print("Claude 확인 중...")
        meta["check_claude"] = check_claude()
        print(json.dumps(meta["check_claude"], ensure_ascii=False, indent=1))
    if args.part in ("all", "judge"):
        meta["checked_at_judge"] = meta["checked_at"]
        print("Codex 확인 중 (검색 없음)...")
        meta["check_codex"] = check_codex("score")
        print(json.dumps(meta["check_codex"], ensure_ascii=False, indent=1))
        print("Codex 확인 중 (검색)...")
        meta["check_codex_search"] = check_codex("novelty")
        print(json.dumps(meta["check_codex_search"], ensure_ascii=False, indent=1))
    # 저장소가 공개라 Claude CLI가 덧붙이는 계정 이메일 같은 값을 지운다.
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email redacted>", json.dumps(meta, ensure_ascii=False))
    common.write_json(run / "meta.json", json.loads(text))
    ok = all(meta[k]["ok"] for k in ("check_claude", "check_codex", "check_codex_search") if k in meta)
    print("사전 점검 통과" if ok else "사전 점검 실패: meta.json을 확인하세요")


if __name__ == "__main__":
    main()
