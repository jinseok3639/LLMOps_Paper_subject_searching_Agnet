"""로그인 사용자 확인 (API 명세 4장). 로그인 자체는 브라우저의 supabase-js가 하고, 서버는 토큰만 확인한다.

- Authorization: Bearer <Supabase access token> 헤더가 없으면 비로그인(None).
- 헤더가 있는데 토큰이 틀리거나 만료되었으면 401 unauthorized. 화면은 새 토큰으로 다시 부른다.
- 확인은 Supabase Auth의 GET /auth/v1/user로 한다. 같은 토큰은 CACHE_SEC 동안 다시 묻지 않는다.
- SUPABASE_URL, SUPABASE_ANON_KEY가 없으면 로그인을 끈다 (모두 비로그인).
"""
import os
import threading
import time

import requests

from src.api.common import ApiError

CACHE_SEC = 60
TIMEOUT_SEC = 10
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def url() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def anon_key() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "")


def service_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def enabled() -> bool:
    return bool(url() and anon_key())


def verify_token(token: str) -> dict | None:
    """토큰이 맞으면 {id, email, name}, 아니면 None. 테스트에서 바꿔 끼운다."""
    res = requests.get(f"{url()}/auth/v1/user", timeout=TIMEOUT_SEC,
                       headers={"apikey": anon_key(), "Authorization": f"Bearer {token}"})
    if res.status_code in (401, 403):
        return None
    res.raise_for_status()
    u = res.json()
    meta = u.get("user_metadata") or {}
    return {"id": u["id"], "email": u.get("email"), "name": meta.get("full_name") or meta.get("name")}


def current_user(request) -> dict | None:
    """요청의 로그인 사용자. 헤더가 없으면 None, 틀린 토큰이면 401."""
    headers = getattr(request, "headers", None) or {}
    value = headers.get("authorization") or ""
    if not value.lower().startswith("bearer ") or not enabled():
        return None
    token = value[7:].strip()
    now = time.time()
    with _lock:
        hit = _cache.get(token)
        if hit and hit[0] > now:
            return hit[1]
    try:
        user = verify_token(token)
    except requests.RequestException as e:
        raise ApiError(502, "upstream_failed", f"로그인 확인 실패: {e}", retryable=True)
    if user is None:
        raise ApiError(401, "unauthorized", "로그인이 만료되었습니다. 다시 로그인해 주세요.")
    with _lock:
        if len(_cache) > 1000:
            _cache.clear()
        _cache[token] = (now + CACHE_SEC, user)
    return user


def require_user(request) -> dict:
    user = current_user(request)
    if user is None:
        raise ApiError(401, "unauthorized", "로그인이 필요합니다.")
    return user


def delete_auth_user(user_id: str) -> None:
    """Supabase Auth 계정 삭제 (A-6). 테이블 행은 auth.users를 참조하므로 함께 지워진다."""
    if not (url() and service_key()):
        return
    res = requests.delete(f"{url()}/auth/v1/admin/users/{user_id}", timeout=TIMEOUT_SEC,
                          headers={"apikey": service_key(), "Authorization": f"Bearer {service_key()}"})
    if res.status_code != 404:
        res.raise_for_status()
    with _lock:
        for token in [t for t, (_, u) in _cache.items() if u["id"] == user_id]:
            del _cache[token]
