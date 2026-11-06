"""로그인 사용자 데이터 저장 (설계서 6.2, 6.3): 프로필, 세션, 로그인 사용자의 작업 결과.

- SUPABASE_URL과 SUPABASE_SERVICE_ROLE_KEY가 있으면 Supabase 테이블(docs/supabase.sql), 없으면 로컬 폴더
  DB/users/{user_id}/ (profile.json, sessions/{id}.json, runs/{run_id}.json).
- 서버만 service_role 키로 테이블에 접근한다. 브라우저는 우리 API만 부른다.
- 세션은 통째로 저장한다. updated_at은 서버가 정하고, 저장할 때 화면이 마지막으로 받은 값(base)과 다르면
  Conflict (version_conflict). base가 None이면 새 세션이고, 이미 있으면 Conflict.
- 비로그인 사용자의 결과는 여기 오지 않는다 (src/runs.py의 DB/runs/, 24시간 보관).
"""
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

from src import config

SESSION_ID = re.compile(r"s_[0-9a-f]{8,64}")
SUMMARY = ("id", "title", "stage", "updated_at")
TIMEOUT_SEC = 15


class Conflict(Exception):
    """세션 버전이 맞지 않는다. current는 서버에 있는 세션 (없으면 None)."""

    def __init__(self, current: dict | None):
        super().__init__("version_conflict")
        self.current = current


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def summary(s: dict) -> dict:
    return {k: s.get(k) for k in SUMMARY}


class LocalStore:
    """DB/users/ 폴더. 쓰기는 임시 파일에 쓴 뒤 이름을 바꾸고, 버전 검사와 쓰기는 잠금 안에서 한다."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()

    def _user(self, uid: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", uid):
            raise ValueError("user_id 형식 오류")
        return self.root / uid

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict | None:
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def get_profile(self, uid):
        return self._read(self._user(uid) / "profile.json")

    def put_profile(self, uid, profile):
        self._write(self._user(uid) / "profile.json", profile)
        return profile

    def list_sessions(self, uid):
        folder = self._user(uid) / "sessions"
        items = [summary(json.loads(p.read_text("utf-8"))) for p in folder.glob("s_*.json")] if folder.exists() else []
        return sorted(items, key=lambda x: x["updated_at"] or "", reverse=True)

    def get_session(self, uid, sid):
        return self._read(self._user(uid) / "sessions" / f"{sid}.json")

    def put_session(self, uid, session, base):
        path = self._user(uid) / "sessions" / f"{session['id']}.json"
        with self.lock:
            current = self._read(path)
            if (current or {}).get("updated_at") != base or (current is None) != (base is None):
                raise Conflict(current)
            saved = {**session, "updated_at": now()}
            self._write(path, saved)
        return saved

    def delete_session(self, uid, sid):
        path = self._user(uid) / "sessions" / f"{sid}.json"
        existed = path.exists()
        path.unlink(missing_ok=True)
        runs = self._user(uid) / "runs"
        for p in runs.glob("r_*.json") if runs.exists() else []:
            if json.loads(p.read_text("utf-8")).get("owner_session") == sid:
                p.unlink(missing_ok=True)
        return existed

    def delete_user(self, uid):
        shutil.rmtree(self._user(uid), ignore_errors=True)

    def save_run(self, uid, run):
        self._write(self._user(uid) / "runs" / f"{run['run_id']}.json", run)

    def load_run(self, run_id):
        for path in self.root.glob(f"*/runs/{run_id}.json"):
            return self._read(path)
        return None


class SupabaseStore:
    """Supabase REST(PostgREST)로 테이블을 읽고 쓴다. 테이블은 docs/supabase.sql."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _req(self, method, table, params=None, body=None, prefer=None):
        headers = {**self.headers, **({"Prefer": prefer} if prefer else {})}
        res = requests.request(method, f"{self.base}/{table}", params=params, json=body, headers=headers,
                               timeout=TIMEOUT_SEC)
        if res.status_code == 409:
            raise Conflict(None)
        res.raise_for_status()
        return res.json() if res.content else None

    def get_profile(self, uid):
        rows = self._req("GET", "profiles", {"user_id": f"eq.{uid}", "select": "json"})
        return rows[0]["json"] if rows else None

    def put_profile(self, uid, profile):
        self._req("POST", "profiles", {"on_conflict": "user_id"},
                  {"user_id": uid, "json": profile, "updated_at": now()}, "resolution=merge-duplicates")
        return profile

    def list_sessions(self, uid):
        rows = self._req("GET", "sessions", {"user_id": f"eq.{uid}", "select": "id,title,stage,updated_at:json->>updated_at",
                                             "order": "updated_at.desc"})
        return [summary(r) for r in rows]

    def get_session(self, uid, sid):
        rows = self._req("GET", "sessions", {"id": f"eq.{sid}", "user_id": f"eq.{uid}", "select": "json"})
        return rows[0]["json"] if rows else None

    def put_session(self, uid, session, base):
        saved = {**session, "updated_at": now()}
        row = {"title": saved.get("title") or "", "stage": saved.get("stage") or "", "json": saved,
               "updated_at": saved["updated_at"]}
        if base is None:  # 새 세션. 같은 id가 있으면 409 → Conflict
            try:
                self._req("POST", "sessions", body={"id": saved["id"], "user_id": uid, **row}, prefer="return=minimal")
            except Conflict:
                raise Conflict(self.get_session(uid, saved["id"]))
            return saved
        # 마지막으로 받은 버전일 때만 바꾼다. 바뀐 행이 없으면 다른 탭이 먼저 저장했거나 없는 세션
        rows = self._req("PATCH", "sessions", {"id": f"eq.{saved['id']}", "user_id": f"eq.{uid}",
                                               "json->>updated_at": f"eq.{base}"}, row, "return=representation")
        if not rows:
            raise Conflict(self.get_session(uid, saved["id"]))
        return saved

    def delete_session(self, uid, sid):
        rows = self._req("DELETE", "sessions", {"id": f"eq.{sid}", "user_id": f"eq.{uid}"}, prefer="return=representation")
        self._req("DELETE", "runs", {"owner_session": f"eq.{sid}", "user_id": f"eq.{uid}"})
        return bool(rows)

    def delete_user(self, uid):
        for table in ("sessions", "runs", "profiles"):
            self._req("DELETE", table, {"user_id": f"eq.{uid}"})

    def save_run(self, uid, run):
        self._req("POST", "runs", body={"id": run["run_id"], "owner_session": run.get("owner_session"), "user_id": uid,
                                        "json": run, "created_at": run.get("created_at") or now()},
                  prefer="return=minimal")

    def load_run(self, run_id):
        rows = self._req("GET", "runs", {"id": f"eq.{run_id}", "select": "json"})
        return rows[0]["json"] if rows else None


def _default():
    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    return SupabaseStore(url, key) if url and key else LocalStore(config.DB_DIR / "users")


backend = _default()  # 테스트에서 LocalStore(임시 폴더)로 바꾼다
