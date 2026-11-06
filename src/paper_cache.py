"""논문별 결과 캐시 (설계서 6.3 cache 테이블): 초록 카드와 Studio 본문 카드.

- 키는 S2 paper_id. 같은 논문은 Solar·Studio를 다시 부르지 않는다.
- 읽는 순서: 로컬 파일 → Supabase cache 테이블 (찾으면 로컬 파일에도 쓴다). 쓸 때는 둘 다에 쓴다.
  Render 디스크는 재배포·재시작 때 지워지므로 Supabase가 원본이고, 로컬 파일은 같은 프로세스 안의 빠른 사본이다.
- SUPABASE_URL과 SUPABASE_SERVICE_ROLE_KEY가 없으면 로컬 파일만 쓴다.
- Supabase 호출이 실패해도 예외를 올리지 않는다. 읽기 실패는 캐시 없음, 쓰기 실패는 로그만 남긴다 (카드는 이미 만들었다).
"""
import json
import logging
import os
import threading
from pathlib import Path

import requests

from src import config  # noqa: F401  (.env 로드)

KINDS = ("abstract_card", "studio_card")
TABLE = "cache"
TIMEOUT_SEC = 5  # 사용자가 기다리는 ④라 짧게. 느리면 캐시 없이 진행한다

log = logging.getLogger(__name__)


class Remote:
    """Supabase REST(PostgREST)의 cache 테이블 (kind, key, json, created_at). 테이블은 docs/supabase.sql."""

    def __init__(self, url: str, key: str):
        self.base = f"{url.rstrip('/')}/rest/v1/{TABLE}"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def get_many(self, kind: str, keys: list[str]) -> dict[str, dict]:
        """{key: json}. 없는 키는 빠진다."""
        if not keys:
            return {}
        values = ",".join('"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"' for k in keys)
        res = requests.get(self.base, params={"kind": f"eq.{kind}", "key": f"in.({values})", "select": "key,json"},
                           headers=self.headers, timeout=TIMEOUT_SEC)
        res.raise_for_status()
        return {r["key"]: r["json"] for r in res.json()}

    def put(self, kind: str, key: str, data: dict) -> None:
        res = requests.post(self.base, params={"on_conflict": "kind,key"}, json={"kind": kind, "key": key, "json": data},
                            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                            timeout=TIMEOUT_SEC)
        res.raise_for_status()


def _default() -> Remote | None:
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return Remote(url, key) if url and key else None


backend = _default()  # 테스트에서 None이나 가짜로 바꾼다


def _write_local(path: Path, data: dict) -> None:
    """임시 파일에 쓴 뒤 이름을 바꾼다 (설계서 6.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def prefetch(kind: str, paths: dict[str, Path]) -> None:
    """{paper_id: 로컬 경로} 중 로컬에 없는 것을 Supabase에서 한 번에 읽어 로컬 파일로 쓴다.
    ④가 예산을 셀 때 로컬 파일만 보고도 캐시 여부를 알 수 있게 하기 위해서다."""
    missing = {k: p for k, p in paths.items() if not p.exists()}
    if backend is None or not missing:
        return
    try:
        found = backend.get_many(kind, list(missing))
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.warning("Supabase 캐시 읽기 실패 %s %d건: %s", kind, len(missing), str(e)[:200])
        return
    for k, data in found.items():
        if isinstance(data, dict) and k in missing:
            _write_local(missing[k], data)


def load(kind: str, key: str, path: Path) -> dict | None:
    """로컬 파일 → Supabase. 없으면 None."""
    if not path.exists():
        prefetch(kind, {key: path})
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def save(kind: str, key: str, path: Path, data: dict) -> None:
    _write_local(path, data)
    if backend is None:
        return
    try:
        backend.put(kind, key, data)
    except requests.RequestException as e:
        log.warning("Supabase 캐시 쓰기 실패 %s %s: %s", kind, key, str(e)[:200])
