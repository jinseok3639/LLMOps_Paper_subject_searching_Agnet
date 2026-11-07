"""Codex(ChatGPT 로그인) 사용량을 확인한다.

판정 호출은 --ephemeral이라 세션 기록이 남지 않는다. 그래서 아주 짧은 요청을 세션 기록을 남기게 한 번 보내고,
그 기록(~/.codex/sessions/.../rollout-*.jsonl)의 마지막 rate_limits를 읽는다.
  primary   5시간 창 사용률(%)
  secondary 1주 창 사용률(%)

예: python eval/baseline_comparison/codex_usage.py --log runs/final/usage.jsonl
    종료 코드: 0 = 읽음, 2 = 읽지 못함
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import common

SESSIONS = Path.home() / ".codex" / "sessions"


def probe():
    """짧은 요청을 보내고 그 세션 기록에서 rate_limits를 읽는다. 반환: dict 또는 None."""
    start = time.time()
    with tempfile.TemporaryDirectory(prefix="bc_usage_") as tmp:
        proc = subprocess.run([common.codex_bin(), "exec", "-C", tmp, "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                               "-s", "read-only", "-m", common.JUDGE_MODEL, "-c", 'model_reasoning_effort="low"', "-"],
                              input="Reply with the single word: ok", capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=tmp, timeout=300)
    limited = "usage limit" in (proc.stdout + proc.stderr).lower()
    files = [p for p in SESSIONS.glob("*/*/*/rollout-*.jsonl") if p.stat().st_mtime >= start - 5]
    latest = None
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if '"rate_limits":{' not in line:
                continue
            try:
                found = find_rate_limits(json.loads(line))
            except json.JSONDecodeError:
                continue
            if found:
                latest = found
    return latest, limited


def find_rate_limits(obj):
    if isinstance(obj, dict):
        rl = obj.get("rate_limits")
        if isinstance(rl, dict) and rl.get("primary"):
            return rl
        for v in obj.values():
            r = find_rate_limits(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_rate_limits(v)
            if r:
                return r
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", default="", help="결과를 한 줄씩 덧붙일 jsonl 경로 (eval/baseline_comparison 기준)")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    rl, limited = probe()
    row = {"at": datetime.now().isoformat(timespec="seconds"), "label": args.label, "limited": limited}
    if rl:
        for name in ("primary", "secondary"):
            w = rl[name]
            row[name] = {"used_percent": w.get("used_percent"), "window_minutes": w.get("window_minutes"),
                         "resets_at": datetime.fromtimestamp(w["resets_at"]).isoformat(timespec="minutes") if w.get("resets_at") else None}
    if args.log:
        path = common.HERE / args.log
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if not rl:
        print(json.dumps(row, ensure_ascii=False))
        print("사용량을 읽지 못했습니다" + (" (사용량 한도에 걸림)" if limited else ""))
        sys.exit(2)
    p, s = row["primary"], row["secondary"]
    print(f"5시간 창 {p['used_percent']}% (초기화 {p['resets_at']}), 1주 창 {s['used_percent']}% (초기화 {s['resets_at']})")


if __name__ == "__main__":
    main()
