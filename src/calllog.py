"""외부 호출 로그 (요구사항 O-1, O-4)와 피드백 저장 (O-3). 설계서 6.2, 6.3.

- 호출 하나 = 한 줄: DB/logs/calls-YYYYMMDD.jsonl (UTC 날짜). 필드는 record() 참고.
- 문맥(session_id, run_kind, step)은 contextvars로 넘긴다. src/api/sse.py가 작업을 시작할 때 start(),
  progress(step=...)마다 set_step(), 끝날 때 finish()를 부른다. finish()는 provider "job" 줄
  (run_id, 상태 ok·error·cancelled·budget, 총 시간, 호출 수)을 남긴다.
- 스레드풀은 문맥을 물려받지 않는다. 풀에 넘기는 함수는 bind로 감싼다:
      ex.map(calllog.bind(fn), items)  /  ex.submit(calllog.bind(fn), *args)
- 호출 쪽은 with timed(provider, model, input) as c: 블록 하나가 호출 하나. 블록 안에서 c.output, c.reply(resp),
  c.status("cache" 등)를 채운다. 예외는 error, 이름이 Cancelled로 끝나는 예외와 GeneratorExit(스트림을 중간에 닫음)은
  cancelled로 남기고 다시 던진다. 로그 기록이 실패해도 호출은 그대로 진행한다.
- prompt_name, prompt_hash (O-4): 시스템 메시지가 프롬프트 파일(src/prompts, src/generation_method/prompts)
  템플릿과 맞으면 그 파일 이름과 파일 내용 해시, 아니면(코드 안의 지시문) 시스템 메시지 해시.
- 저장은 append(table, row) 하나만 거친다. Supabase 단계(설계서 6.3)에서는 이 함수만 바꾼다 (call_log, feedback 테이블).
- API 키와 인증 헤더는 넘기지 않는다. 줄에 키 값이나 Bearer 토큰이 섞이면 *** 로 바꾼다.
"""
import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from src import config

ROOT = config.DB_DIR  # 테스트에서 임시 폴더로 바꾼다
PROMPT_DIRS = {"": config.PROMPT_DIR, "generation_method/": config.ROOT_DIR / "src" / "generation_method" / "prompts"}
MAX_CHARS = 100_000   # input·output 한 필드 상한. 넘으면 앞부분만 남긴다
SECRET_ENVS = ("UPSTAGE_API_KEY", "S2_API_KEY")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{16,}")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_lock = threading.Lock()
log = logging.getLogger(__name__)


@dataclass
class Job:
    """작업 하나의 문맥. 풀 스레드들이 같은 객체를 보므로 step과 호출 수는 작업 전체에 걸린다."""
    session_id: str | None = None
    run_kind: str | None = None
    step: str | None = None
    calls: int = 0
    t0: float = 0.0


_job: contextvars.ContextVar[Job | None] = contextvars.ContextVar("calllog_job", default=None)


def start(session_id: str, run_kind: str | None) -> Job:
    job = Job(session_id, run_kind, t0=time.monotonic())
    _job.set(job)
    return job


def set_step(step: str | None) -> None:
    job = _job.get()
    if job and step:
        job.step = step


def finish(status: str, run_id: str | None = None, error: str | None = None) -> None:
    """provider "job" 줄. 작업이 끝난 뒤에야 run_id를 알 수 있어 여기서 한 번 남긴다."""
    job = _job.get()
    if job:
        record("job", job.run_kind, input=None, output=None, latency_ms=_ms(job.t0), status=status,
               error=error, run_id=run_id, calls=job.calls)
        _job.set(None)


def bind(fn):
    """지금 문맥을 잡아 두고, 다른 스레드에서도 그 문맥으로 fn을 부른다. 부를 때마다 복사본에서 돌려 동시에 써도 된다."""
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.copy().run(fn, *args, **kwargs)


class Call:
    """timed()가 넘기는 기록용 객체."""

    def __init__(self):
        self.output = self.tokens_in = self.tokens_out = self.error = None
        self.status = "ok"

    def usage(self, usage) -> None:
        if usage is not None:
            self.tokens_in = getattr(usage, "prompt_tokens", None)
            self.tokens_out = getattr(usage, "completion_tokens", None)

    def reply(self, resp) -> None:
        """OpenAI 호환 chat 응답에서 출력과 토큰 수를 읽는다."""
        self.output = resp.choices[0].message.content
        self.usage(getattr(resp, "usage", None))


@contextmanager
def timed(provider: str, model: str | None, input):
    t0, call = time.monotonic(), Call()
    try:
        yield call
    except BaseException as e:
        cancelled = isinstance(e, GeneratorExit) or type(e).__name__.endswith("Cancelled")
        call.status = "cancelled" if cancelled else "error"
        call.error = f"{type(e).__name__}: {e}"[:1000]
        raise
    finally:
        record(provider, model, input=input, output=call.output, latency_ms=_ms(t0), tokens_in=call.tokens_in,
               tokens_out=call.tokens_out, status=call.status, error=call.error)


def record(provider, model, *, input, output, latency_ms, tokens_in=None, tokens_out=None, status,
           prompt_name=None, prompt_hash=None, error=None, run_id=None, **extra) -> None:
    """호출 로그 한 줄 (설계서 6.2). 실패해도 예외를 내지 않는다."""
    try:
        job = _job.get() or Job()
        if provider != "job" and job.session_id is not None:
            with _lock:
                job.calls += 1
        if prompt_hash is None and isinstance(input, list):
            system = next((m.get("content") for m in input if isinstance(m, dict) and m.get("role") == "system"), None)
            if isinstance(system, str):
                prompt_name, prompt_hash = prompt_version(system)
        append("call_log", {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": job.session_id, "run_id": run_id, "run_kind": job.run_kind, "step": job.step,
            "provider": provider, "model": model, "prompt_name": prompt_name, "prompt_hash": prompt_hash,
            "input": _clip(input), "output": _clip(output), "latency_ms": latency_ms,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "status": status, "error": error, **extra})
    except Exception:  # noqa: BLE001  (로그 때문에 작업이 멈추면 안 된다)
        log.exception("호출 로그 기록 실패")


def append(table: str, row: dict) -> None:
    """저장 계층. 지금은 DB/ 아래 jsonl에 한 줄을 더한다 (call_log → logs/calls-YYYYMMDD.jsonl, 그 밖은 {table}.jsonl)."""
    if table == "call_log":
        path = Path(ROOT) / "logs" / f"calls-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    else:
        path = Path(ROOT) / f"{table}.jsonl"
    line = _scrub(json.dumps(row, ensure_ascii=False, default=str))
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def prompt_version(system: str) -> tuple[str | None, str]:
    """시스템 메시지 → (프롬프트 이름, 짧은 해시). 이름 예: recommend_query, generation_method/propose."""
    for prefix, folder in PROMPT_DIRS.items():
        for path in sorted(Path(folder).glob("*.md")):
            parts, digest = _template(path, path.stat().st_mtime_ns)
            if _matches(system, parts):
                return prefix + path.stem, digest
    return None, _hash(system)


@lru_cache(maxsize=64)
def _template(path: Path, mtime: int) -> tuple[list[str], str]:
    """{persona} 같은 자리를 뺀 글자 조각들과 파일 해시. 파일이 바뀌면 mtime이 달라져 다시 읽는다."""
    text = path.read_text("utf-8")
    return _PLACEHOLDER.split(text), _hash(text)


def _matches(text: str, parts: list[str]) -> bool:
    """text가 첫 조각으로 시작하고 나머지 조각을 차례로 담고 있으면 그 템플릿으로 만든 것이다. 뒤에 붙인 문맥은 무시."""
    if not text.startswith(parts[0]):
        return False
    i = len(parts[0])
    for part in parts[1:]:
        i = text.find(part, i)
        if i < 0:
            return False
        i += len(part)
    return True


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _ms(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)


def _clip(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"...(+{len(text) - MAX_CHARS}자 생략)"


def _scrub(text: str) -> str:
    for name in SECRET_ENVS:
        secret = os.getenv(name, "").strip()
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    return _BEARER.sub("Bearer ***", text)
