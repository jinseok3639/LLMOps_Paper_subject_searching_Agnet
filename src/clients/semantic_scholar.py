"""Semantic Scholar Graph API 호출 래퍼.

- S2_API_KEY가 있으면 x-api-key 헤더 사용 (초당 1회), 없으면 공용 한도로 호출 (3초 간격).
- 429, 5xx는 지수 백오프로 재시도. 400, 401, 403, 404는 바로 실패.
"""
import logging
import os
import threading
import time

import requests

from src import config  # noqa: F401  (.env 로드)

BASE_URL = "https://api.semanticscholar.org/graph/v1"
MAX_RETRIES = 10

log = logging.getLogger(__name__)

_api_key = os.getenv("S2_API_KEY", "").strip()
_interval = 1.1 if _api_key else 3.0
_session = requests.Session()
if _api_key:
    _session.headers["x-api-key"] = _api_key
_lock = threading.Lock()
_last_call = 0.0


class S2Error(Exception):
    pass


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def get(path: str, params: dict | None = None) -> dict:
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = _session.get(f"{BASE_URL}{path}", params=params, timeout=60)
        except requests.RequestException as e:
            status, detail = None, str(e)
        else:
            if resp.status_code == 200:
                return resp.json()
            status, detail = resp.status_code, resp.text[:300]
            if status in (400, 401, 403, 404):
                raise S2Error(f"{status} {path}: {detail}")
        log.warning("S2 %s 재시도 %d/%d, %.0f초 대기", status, attempt, MAX_RETRIES, delay)
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise S2Error(f"재시도 초과 {path}: {status} {detail}")


def search_papers(query: str, fields: str, limit: int = 10, year: str | None = None,
                  fields_of_study: str | None = "Computer Science") -> dict:
    """관련도 검색 (/paper/search). limit 최대 100."""
    params = {"query": query, "fields": fields, "limit": limit}
    if year:
        params["year"] = year
    if fields_of_study:
        params["fieldsOfStudy"] = fields_of_study
    return get("/paper/search", params)
