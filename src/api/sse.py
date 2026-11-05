"""동기 작업을 스레드에서 돌리고 SSE로 보낸다 (API 명세 3장).

job(progress, delta) -> 결과 dict
- progress(message, current=None, total=None, step=None): 하위 단계 표시 (F-4). 인자 없이 부르면 취소 확인만 한다.
- delta(text): 답변 조각 (C-3).
- 연결이 끊기면 취소 표시를 세우고, 다음 progress/delta 호출이 Cancelled를 던진다 (3.1. 단계 사이에서 멈춤).
- 이벤트: progress, delta, 마지막에 result | error | budget 하나. 모두 session_id가 붙는다 (G-1).
- 호출 로그 (O-1): 작업 스레드에서 calllog 문맥(session_id, 작업 종류 = 경로 끝, progress의 step)을 세우고,
  끝나면 provider "job" 줄 하나를 남긴다 (run_id, ok · error · cancelled · budget, 총 시간, 호출 수).
"""
import asyncio
import json
import logging
import threading

import openai
import requests
from fastapi.responses import StreamingResponse

from src import calllog, llm
from src.runs import BudgetExceeded, Cancelled

log = logging.getLogger("uvicorn.error")
UPSTREAM = (openai.OpenAIError, llm.LLMError, requests.RequestException, ValueError, RuntimeError)
FINAL = {"result", "error", "budget"}
POLL_SEC = 1.0  # 이벤트가 없을 때 연결 확인 간격
PING_EVERY = 15  # 이 횟수만큼 조용하면 주석 줄을 보내 연결을 유지한다


def error_body(exc: Exception) -> dict:
    """API 명세 2.3 형식. Solar·S2 호출과 그 출력 형식 실패는 upstream_failed (F-6)."""
    if isinstance(exc, UPSTREAM):
        return {"code": "upstream_failed", "message": str(exc) or type(exc).__name__, "retryable": True}
    return {"code": "internal_error", "message": f"서버 오류: {exc}", "retryable": True}


def stream_job(request, session_id: str, job, kind: str | None = None) -> StreamingResponse:
    cancel = threading.Event()
    url = getattr(request, "url", None)
    run_kind = kind or (url.path.rstrip("/").rsplit("/", 1)[-1] if url else None)  # /api/recommend → recommend

    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def emit(kind, data):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (kind, {"session_id": session_id, **data}))
            except RuntimeError:  # 서버가 멈춘 뒤
                pass

        def check():
            if cancel.is_set():
                raise Cancelled()

        def progress(message=None, current=None, total=None, step=None, **_):
            check()
            if isinstance(message, dict):
                message, current, total, step = (message.get(k) for k in ("message", "current", "total", "step"))
            calllog.set_step(step)
            if message:
                info = {"step": step or "", "message": str(message), "current": current, "total": total}
                emit("progress", {k: v for k, v in info.items() if v is not None})

        def delta(text):
            check()
            emit("delta", {"text": text})

        def work():
            calllog.start(session_id, run_kind)
            status, run_id, error = "ok", None, None
            try:
                result = job(progress, delta)
                run_id = result.get("run_id")
                emit("result", result)
            except Cancelled:
                status = "cancelled"
                log.info("작업 취소: %s", session_id)
            except BudgetExceeded as e:
                status, error = "budget", str(e)
                emit("budget", {"used": e.used, "limit": e.limit, "scope": e.scope})
            except Exception as e:
                status, error = "error", f"{type(e).__name__}: {e}"
                log.exception("작업 실패: %s", session_id)
                emit("error", {"error": error_body(e)})
            finally:
                calllog.finish(status, run_id, error)

        threading.Thread(target=work, daemon=True).start()
        idle = 0
        try:
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), POLL_SEC)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    idle += 1
                    if idle % PING_EVERY == 0:
                        yield ": ping\n\n"
                    continue
                idle = 0
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if kind in FINAL:
                    return
        finally:  # 끝났거나, 연결이 끊겨 취소된 경우 모두
            cancel.set()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
