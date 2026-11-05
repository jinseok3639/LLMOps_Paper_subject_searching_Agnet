"""Upstage Studio 에이전트 호출: 로컬 파일 1개 → 단계별 결과 (설계서 4.6, 7장).

- 업로드 → job 생성 (include=["all"]) → 2.5초 간격 상태 확인 → 업로드한 파일 삭제 (실패해도 무시).
- 취소(should_cancel)는 상태 확인만 멈춘다. job 자체는 취소하지 않는다.
- 파싱 단계 출력은 문서에 "text"라고만 되어 있어 방어적으로 읽는다 (JSON이면 content.text/html/markdown,
  아니면 그대로. HTML 태그 제거). 원본 응답은 raw로 함께 돌려준다.
"""
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import requests

from src import calllog, config  # noqa: F401  (config: .env 로드)

BASE_URL = "https://api.upstage.ai/v2"
CARD_AGENT_ENV = "STUDIO_CARD_AGENT_ID"  # 에이전트 B: 논문 PDF → 연구 카드
OP_AGENT_ENV = "STUDIO_OP_AGENT_ID"      # 에이전트 A: 서베이 PDF → Open Problems
POLL_SEC = 2.5
TIMEOUT_SEC = 180   # job당 (설계서 7장)
HTTP_TIMEOUT = 60   # 요청 1회
HTTP_TRIES = 3

log = logging.getLogger(__name__)


class StudioError(Exception):
    """job 실패, 시간 초과, 결과 형식 오류, HTTP 오류."""


class StudioUnavailable(StudioError):
    """에이전트 ID나 API 키가 없음, 또는 401/403/404. Studio를 쓸 수 없는 상태."""


class StudioCancelled(StudioError):
    """should_cancel()로 상태 확인을 멈춤. job은 서버에서 계속 돌 수 있다."""


def agent_id(env: str = CARD_AGENT_ENV) -> str:
    value = os.getenv(env, "").strip()
    if not value:
        raise StudioUnavailable(f"{env}가 .env에 없습니다. Studio 콘솔에서 에이전트를 만든 뒤 ID를 넣으세요 (설계서 4.6)")
    return value


def _call(method: str, path: str, tries: int = HTTP_TRIES, **kw) -> dict:
    """429, 5xx, 연결 오류는 재시도. 401/403/404는 StudioUnavailable, 그 밖의 4xx는 StudioError."""
    key = os.getenv("UPSTAGE_API_KEY", "").strip()
    if not key:
        raise StudioUnavailable("UPSTAGE_API_KEY가 .env에 없습니다")
    status = detail = None
    for attempt in range(tries):
        try:
            resp = requests.request(method, BASE_URL + path, headers={"Authorization": f"Bearer {key}"},
                                    timeout=HTTP_TIMEOUT, **kw)
        except requests.RequestException as e:
            status, detail = None, str(e)
        else:
            if resp.status_code < 300:
                try:
                    return resp.json() if resp.content else {}
                except ValueError:
                    raise StudioError(f"JSON이 아닌 응답 {method} {path}: {resp.text[:200]}")
            status, detail = resp.status_code, resp.text[:300]
            if status in (401, 403, 404):
                raise StudioUnavailable(f"{status} {method} {path}: {detail}")
            if status != 429 and status < 500:
                raise StudioError(f"{status} {method} {path}: {detail}")
        if attempt + 1 < tries:
            time.sleep(2 ** (attempt + 1))
    raise StudioError(f"재시도 초과 {method} {path}: {status} {detail}")


def run_agent(path: str | Path, agent: str | None = None, timeout: float = TIMEOUT_SEC,
              poll: float = POLL_SEC, should_cancel: Callable[[], bool] | None = None) -> dict:
    """파일 1개로 에이전트를 돌린다. agent가 없으면 STUDIO_CARD_AGENT_ID.

    반환: {job_id, agent_id, steps: [{step, text, additional_values}], final: 마지막 단계 JSON (dict),
           parse_text: 마지막 단계 앞 단계들의 텍스트, elapsed_sec, raw: job 응답 전체}
    예외: StudioUnavailable (ID·키 없음, 401/403/404), StudioCancelled, StudioError (failed, 시간 초과, 형식)
    호출 로그: provider studio, model = 에이전트 ID, job 하나에 한 줄 (취소는 cancelled).
    """
    agent = agent or agent_id()
    with calllog.timed("studio", agent, Path(path).name) as c:
        result = _run(path, agent, timeout, poll, should_cancel)
        c.output = {"job_id": result["job_id"], "steps": [s["step"] for s in result["steps"]], "final": result["final"]}
    return result


def _run(path, agent, timeout, poll, should_cancel) -> dict:
    cancelled = should_cancel or (lambda: False)
    if cancelled():
        raise StudioCancelled("시작 전 취소됨")
    path = Path(path)
    t0 = time.monotonic()
    file_id = _call("POST", "/files", files={"file": (path.name, path.read_bytes())},
                    data={"purpose": "user_data"}).get("id")
    if not file_id:
        raise StudioError("업로드 응답에 id가 없습니다")
    try:
        job = _call("POST", "/responses", json={
            "model": agent, "include": ["all"],
            "input": [{"role": "user", "content": [{"type": "input_file", "file_id": file_id}]}]})
        job_id = job.get("id")
        if not job_id:
            raise StudioError("job 생성 응답에 id가 없습니다")
        deadline = time.monotonic() + timeout
        while job.get("status") in ("queued", "in_progress"):
            if cancelled():
                raise StudioCancelled(f"취소됨 (job {job_id})")
            if time.monotonic() > deadline:
                raise StudioError(f"시간 초과 {timeout:.0f}초 (job {job_id}, status={job.get('status')})")
            time.sleep(poll)
            try:
                job = _call("GET", f"/responses/{job_id}", tries=1, params={"include[]": "all"})
            except StudioUnavailable:
                raise
            except StudioError as e:  # 일시 오류는 시간 제한 안에서 계속 확인
                log.warning("Studio 상태 확인 실패, 계속: %s", e)
    finally:
        try:
            _call("DELETE", f"/files/{file_id}", tries=1)
        except Exception as e:  # noqa: BLE001  (삭제는 best effort)
            log.warning("Studio 업로드 파일 삭제 실패 %s: %s", file_id, e)
    if job.get("status") != "completed":
        raise StudioError(f"job {job_id} {job.get('status')}: {str(job.get('error'))[:300]}")
    steps = _steps(job)
    final = _parse_json(steps[-1]["text"] if steps else "")
    if final is None:
        final = _parse_json(_text(job.get("output_text")))
    if not isinstance(final, dict):
        raise StudioError(f"job {job_id} 마지막 단계가 JSON 객체가 아닙니다")
    return {"job_id": job_id, "agent_id": agent, "steps": steps, "final": final,
            "parse_text": "\n\n".join(plain_text(s["text"]) for s in steps[:-1]).strip(),
            "elapsed_sec": round(time.monotonic() - t0, 1), "raw": job}


def _text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _steps(job: dict) -> list[dict]:
    """output[] → [{step, text, additional_values}]. step은 output[].model (예: step_1_parse)."""
    steps = []
    for item in job.get("output") or []:
        if not isinstance(item, dict):
            continue
        contents = [c for c in item.get("content") or [] if isinstance(c, dict)]
        steps.append({"step": item.get("model") or "",
                      "text": "\n".join(_text(c.get("text")) for c in contents),
                      "additional_values": [c["additional_values"] for c in contents if c.get("additional_values")]})
    return steps


def _parse_json(text: str):
    t = re.sub(r"^```\w*\s*|\s*```$", "", text.strip())
    try:
        return json.loads(t)
    except ValueError:
        return None


def _pick(d) -> str:
    """dict에서 text, html, markdown 중 처음으로 비어 있지 않은 값."""
    if not isinstance(d, dict):
        return ""
    return next((d[k] for k in ("text", "html", "markdown") if isinstance(d.get(k), str) and d[k].strip()), "")


def plain_text(text: str) -> str:
    """파싱 단계 text → 평문. JSON(Document Parse 형식)이면 content 또는 elements[].content를 쓴다."""
    data = _parse_json(text) if text.lstrip().startswith("{") else None
    if isinstance(data, dict):
        picked = _pick(data.get("content")) or _pick(data)
        if not picked:
            picked = "\n".join(_pick(e.get("content")) or _pick(e)
                               for e in data.get("elements") or [] if isinstance(e, dict))
        text = picked or text
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
