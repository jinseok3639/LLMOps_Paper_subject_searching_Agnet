"""작업 결과 저장 (DB/runs/{run_id}.json, 설계서 6.1)과 작업 중단 예외.

- run_id는 추측할 수 없는 무작위 값이다. 결과는 한 번 쓰면 바꾸지 않는다.
- 비로그인 결과는 만든 지 24시간이 지나면 지운다 (API 명세 6.5). 서버 시작 때와 하루 한 번 저장할 때 정리한다.
- 로그인 사용자의 결과는 src/store.py(Supabase)에 저장하고 세션을 지울 때 함께 지운다. 누구 것인지는
  src/api/sse.py가 작업 스레드에서 set_owner(user_id)로 알려 준다. 조회는 run_id만 알면 누구나 할 수 있다.
"""
import contextvars
import json
import re
import secrets
import time
from datetime import datetime, timezone

from src import config, store

RUNS_DIR = config.DB_DIR / "runs"
TTL_SEC = 24 * 3600
_ID = re.compile(r"r_[0-9a-f]{32}")
_last_cleanup = 0.0
_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_owner", default=None)


def set_owner(user_id: str | None) -> None:
    """이 스레드에서 저장하는 결과의 주인 (로그인 사용자 id, 비로그인은 None)."""
    _owner.set(user_id)


class Cancelled(Exception):
    """연결이 끊겨 작업을 멈춘다. 서버가 넘긴 progress 콜백이 던진다."""


class BudgetExceeded(Exception):
    """요청당 호출 예산 초과 (설계서 J-7). 서버는 budget 이벤트로 알린다."""

    def __init__(self, used: int, limit: int, scope: str):
        super().__init__(f"호출 예산 초과: {scope} {used}/{limit}")
        self.used, self.limit, self.scope = used, limit, scope


def save(kind: str, result: dict, owner_session: str | None = None) -> str:
    """결과 전체를 저장하고 run_id를 돌려준다. 임시 파일에 쓴 뒤 이름을 바꾼다."""
    run_id = "r_" + secrets.token_hex(16)
    data = {"run_id": run_id, "kind": kind, "owner_session": owner_session,
            "created_at": datetime.now(timezone.utc).isoformat(), **result}
    if user_id := _owner.get():
        store.backend.save_run(user_id, data)
        return run_id
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_DIR / f"{run_id}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(RUNS_DIR / f"{run_id}.json")
    if time.time() - _last_cleanup > TTL_SEC:
        cleanup()
    return run_id


def load(run_id: str | None) -> dict | None:
    """없거나 형식이 틀린 id면 None."""
    if not run_id or not _ID.fullmatch(run_id):
        return None
    path = RUNS_DIR / f"{run_id}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else store.backend.load_run(run_id)


def adopt(run_id: str, user_id: str, owner_session: str) -> bool:
    """비로그인 때 만든 결과를 로그인 사용자 것으로 옮긴다 (A-5). 이미 옮겼거나 없으면 False."""
    if not _ID.fullmatch(run_id or ""):
        return False
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        return False
    data = {**json.loads(path.read_text("utf-8")), "owner_session": owner_session}
    store.backend.save_run(user_id, data)
    path.unlink(missing_ok=True)
    return True


def cleanup(max_age: float = TTL_SEC) -> int:
    """만든 지 max_age초가 지난 결과를 지우고 지운 개수를 돌려준다. 결과는 바뀌지 않으므로 수정 시각 = 만든 시각."""
    global _last_cleanup
    _last_cleanup = now = time.time()
    removed = 0
    for path in list(RUNS_DIR.glob("r_*.json")) + list(RUNS_DIR.glob("r_*.tmp")):
        if now - path.stat().st_mtime > max_age:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
