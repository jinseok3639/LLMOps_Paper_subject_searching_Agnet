"""LangSmith 추적. calllog가 부르는 얇은 층이며, 다른 모듈은 이 파일을 직접 부르지 않는다 (feedback 제외).

- 켜는 조건: LANGSMITH_TRACING=true 이고 LANGSMITH_API_KEY가 있을 때. 꺼져 있으면 모든 함수가 아무 일도 하지 않는다.
  프로젝트 이름은 LANGSMITH_PROJECT (없으면 default).
- 작업(SSE job) 하나 = trace 하나: chain run, 이름은 run_kind (recommend, research, chat ...). metadata에 session_id, user_id.
  progress의 step이 바뀌면 그 아래에 chain span을 새로 연다. 작업 밖(스크립트, 평가)의 호출은 보내지 않는다.
- 외부 호출 하나 = child run. solar는 llm (토큰 수·캐시 토큰 포함), 그 밖(s2, studio, arxiv, pdf, runs)은 tool.
- 입력·출력은 calllog가 자르고(_clip) 키를 가린(_scrub) 값만 받는다.
- 피드백: 작업이 끝나면 (세션, 작업 종류) → 마지막 trace, 앱 run_id → trace를 기억해 두고, /api/feedback이 그 trace에 점수를 단다.
  메모리에만 두므로 서버를 다시 띄우기 전 trace에만 붙는다.
- 실패해도 예외를 내지 않는다 (추적 때문에 작업이 멈추면 안 된다).
"""
import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)
MAX_REMEMBERED = 2000
FEEDBACK_KIND = {"problem": "recommend", "final": "finalize"}  # topic은 target_id 안의 research run_id로 찾는다

_client = None
_lock = threading.Lock()
_latest: OrderedDict[tuple[str, str], str] = OrderedDict()  # (session_id, run_kind) → trace_id
_by_run: OrderedDict[str, str] = OrderedDict()  # 앱 run_id → trace_id
_pool = ThreadPoolExecutor(1, thread_name_prefix="langsmith-feedback")


def enabled() -> bool:
    flag = os.getenv("LANGSMITH_TRACING", "").strip().lower() in ("1", "true", "yes")
    return flag and bool(os.getenv("LANGSMITH_API_KEY", "").strip())


def client():
    global _client
    with _lock:
        if _client is None:
            from langsmith import Client
            _client = Client()
        return _client


def _guard(fn):
    def wrapper(*args, **kwargs):
        if not enabled():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.warning("LangSmith 추적 실패: %s", fn.__name__, exc_info=True)
            return None
    return wrapper


@_guard
def start_job(run_kind: str | None, session_id: str | None, user_id: str | None):
    from langsmith.run_trees import RunTree
    meta = {k: v for k, v in {"session_id": session_id, "user_id": user_id}.items() if v}
    root = RunTree(name=run_kind or "job", run_type="chain", inputs={"session_id": session_id, "run_kind": run_kind},
                   tags=[t for t in (run_kind,) if t], extra={"metadata": meta}, ls_client=client())
    root.post()
    return root


@_guard
def start_step(root, prev, step: str):
    """이전 단계 span을 닫고 새 span을 연다."""
    if root is None:
        return None
    _end(prev)
    span = root.create_child(name=step, run_type="chain", inputs={"step": step})
    span.post()
    return span


@_guard
def end_job(root, span, status: str, run_id: str | None, output, error: str | None) -> str | None:
    """trace를 닫고 trace_id를 돌려준다. ok가 아니면 error로 표시한다 (cancelled, budget 포함)."""
    if root is None:
        return None
    _end(span)
    root.set(metadata={"status": status, "run_id": run_id})
    root.end(outputs={"status": status, "run_id": run_id, "result": output},
             error=None if status == "ok" else f"{status}: {error or ''}".strip())
    root.patch()
    return str(root.trace_id)


@_guard
def start_call(parent, provider: str, model: str | None, input, prompt_name: str | None):
    if parent is None:
        return None
    llm = provider == "solar"
    if llm and isinstance(input, list) and all(isinstance(m, dict) for m in input):
        inputs = {"messages": input}
    else:
        inputs = {"input": input}
    meta = {"provider": provider, "model": model}
    if llm:
        meta |= {"ls_provider": "upstage", "ls_model_name": model}
    if prompt_name:
        meta["prompt_name"] = prompt_name
    name = prompt_name or (f"{provider}:{model}" if model else provider)
    run = parent.create_child(name=name, run_type="llm" if llm else "tool", inputs=inputs,
                              tags=[provider], extra={"metadata": meta})
    run.post()
    return run


@_guard
def end_call(run, *, output, status: str, error: str | None, prompt_hash: str | None,
             tokens_in=None, tokens_out=None, tokens_cached=None) -> None:
    if run is None:
        return
    if run.run_type == "llm":
        outputs = {"choices": [{"message": {"role": "assistant", "content": output}}]}
        if tokens_in is not None or tokens_out is not None:
            usage = {"input_tokens": tokens_in or 0, "output_tokens": tokens_out or 0,
                     "total_tokens": (tokens_in or 0) + (tokens_out or 0)}
            if tokens_cached:
                usage["input_token_details"] = {"cache_read": tokens_cached}
            run.set(usage_metadata=usage)
    else:
        outputs = {"output": output}
    if prompt_hash:
        run.set(metadata={"prompt_hash": prompt_hash})
    run.end(outputs=outputs, error=None if status in ("ok", "cache") else f"{status}: {error or ''}".strip())
    run.patch()


def _end(run) -> None:
    if run is not None:
        run.end()
        run.patch()


def remember(session_id: str | None, run_kind: str | None, run_id: str | None, trace_id: str | None) -> None:
    if not trace_id:
        return
    with _lock:
        for table, key in ((_latest, (session_id, run_kind) if session_id and run_kind else None), (_by_run, run_id)):
            if key:
                table[key] = trace_id
                table.move_to_end(key)
                while len(table) > MAX_REMEMBERED:
                    table.popitem(last=False)


def trace_for(session_id: str, target: str, target_id: str) -> str | None:
    """피드백 대상 → trace_id. topic은 "{research_run_id}:{후보 id}", 나머지는 그 세션의 마지막 작업."""
    with _lock:
        if target == "topic":
            return _by_run.get(target_id.split(":", 1)[0])
        kind = FEEDBACK_KIND.get(target)
        return _latest.get((session_id, kind)) if kind else None


def feedback(session_id: str, target: str, target_id: str, value: str, comment: str | None) -> bool:
    """찾은 trace에 점수(up=1, down=0)를 단다. 보내기는 뒤에서 한다. trace를 못 찾으면 False."""
    if not enabled():
        return False
    trace_id = trace_for(session_id, target, target_id)
    if not trace_id:
        log.info("LangSmith 피드백: trace를 찾지 못함 (%s %s)", target, target_id)
        return False

    def send():
        try:
            client().create_feedback(trace_id, key=f"user_{target}", score=1 if value == "up" else 0, value=value,
                                     comment=comment, trace_id=trace_id, extra={"target_id": target_id})
        except Exception:  # noqa: BLE001
            log.warning("LangSmith 피드백 전송 실패", exc_info=True)

    _pool.submit(send)
    return True


def flush() -> None:
    """서버를 내릴 때 쌓인 run을 보낸다."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:  # noqa: BLE001
            log.warning("LangSmith flush 실패", exc_info=True)
