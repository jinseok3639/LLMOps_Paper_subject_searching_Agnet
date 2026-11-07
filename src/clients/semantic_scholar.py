"""Semantic Scholar Graph API 호출 래퍼. 서버 안의 모든 S2 호출은 이 모듈의 대기열 하나를 지난다.

- 대기열: 먼저 온 요청부터 하나씩 내보내고, 요청 시작 사이에 간격을 둔다.
  키가 있든 없든 3초. 키 한도는 초당 1회지만 S2가 붐빌 때는 1.1초 간격에도 429가 이어졌다 (2026-11-07).
- 429나 5xx를 받으면 대기열 전체를 쉬게 한다 (2, 4, 8 ... 최대 60초). 줄 선 다른 요청이 곧바로 또 429를 받지 않게 하기 위해서다.
  연결 오류도 같다. 재시도는 줄 맨 뒤로 다시 선다. 429가 아닌 4xx는 바로 실패.
- 재시도 횟수(tries)는 호출하는 쪽이 정한다. 사용자가 기다리는 ③④는 짧게, 일괄 수집은 길게.
"""
import logging
import os

import requests

from src import config  # noqa: F401  (.env 로드)
from src.clients.ratequeue import RateQueue

BASE_URL = "https://api.semanticscholar.org/graph/v1"
MAX_RETRIES = 10
TIMEOUT = 60
INTERVAL_KEY, INTERVAL_PUBLIC = 3.0, 3.0
BACKOFF, MAX_BACKOFF = 2.0, 60.0  # 429·5xx 때 대기열을 쉬게 하는 시간 (두 배씩)

log = logging.getLogger(__name__)


class S2Error(RuntimeError):
    """S2 호출 실패 (4xx 즉시 실패, 또는 재시도 초과). status는 마지막 HTTP 상태 (연결 오류면 None)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


_queue = RateQueue()


def _api_key() -> str:
    return os.getenv("S2_API_KEY", "").strip()


def get(path: str, params: dict | None = None, tries: int = MAX_RETRIES) -> dict:
    """GET {BASE_URL}{path}. 성공하면 JSON. 실패하면 S2Error."""
    key = _api_key()
    headers = {"x-api-key": key} if key else {}
    interval = INTERVAL_KEY if key else INTERVAL_PUBLIC
    backoff, status, detail = BACKOFF, None, ""
    for attempt in range(1, tries + 1):
        _queue.pass_through(interval)
        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            status, detail = None, str(e)[:300]
        else:
            if resp.status_code == 200:
                return resp.json()
            status, detail = resp.status_code, resp.text[:300]
            if 400 <= status < 500 and status != 429:
                raise S2Error(f"S2 {status} {path}: {detail}", status)
        if attempt < tries:
            log.warning("S2 %s 재시도 %d/%d, 대기열 %.0f초 쉼", status, attempt, tries - 1, backoff)
            _queue.pause(backoff)  # 줄 전체를 쉬게 하고, 재시도는 줄 맨 뒤에 다시 선다
            backoff = min(backoff * 2, MAX_BACKOFF)
    raise S2Error(f"S2 재시도 초과 {path}: {status} {detail}", status)


def search_papers(query: str, fields: str, limit: int = 10, year: str | None = None,
                  fields_of_study: str | None = "Computer Science", tries: int = MAX_RETRIES) -> dict:
    """관련도 검색 (/paper/search). limit 최대 100."""
    params = {"query": query, "fields": fields, "limit": limit}
    if year:
        params["year"] = year
    if fields_of_study:
        params["fieldsOfStudy"] = fields_of_study
    return get("/paper/search", params, tries=tries)


def paper(paper_id: str, fields: str, tries: int = MAX_RETRIES) -> dict:
    """논문 하나 (/paper/{id}). paper_id는 S2 ID 또는 ARXIV:..., DOI:... 형식."""
    return get("/paper/" + requests.utils.quote(paper_id, safe=":"), {"fields": fields}, tries=tries)
