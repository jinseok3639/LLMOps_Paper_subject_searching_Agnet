"""로그인 사용자의 프로필과 세션 (API 명세 5장). 모두 로그인이 필요하다 (없으면 401).

- 세션은 통째로 PUT한다. 본문은 {session, base_updated_at}. base_updated_at은 화면이 서버에서 마지막으로 받은
  updated_at이고, 처음 저장하는 세션이면 null이다. 서버 값과 다르면 409 version_conflict (다른 탭이 먼저 저장).
  세션 id는 화면이 만든다 (s_ + 16진수). 그래야 저장 전에도 작업 요청에 쓸 수 있다.
- 다른 사용자의 세션은 403이 아니라 404 (S-1).
- import: 비로그인 때 이 탭에서 만든 세션을 계정으로 옮긴다 (A-5). 세션이 가리키는 비로그인 결과(DB/runs/)도
  함께 옮겨 24시간 정리에서 빠지게 한다. 이미 같은 id가 있으면 새 id를 준다.
"""
import json
import secrets

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from src import auth, runs, store
from src.api.common import ApiError, Profile

router = APIRouter()
MAX_BYTES = 2_000_000  # 세션 하나의 상한


class PutBody(BaseModel):
    session: dict
    base_updated_at: str | None = None


class ImportBody(BaseModel):
    sessions: list[dict]


def _check(session: dict, sid: str | None = None) -> None:
    if sid is not None and session.get("id") != sid:
        raise ApiError(422, "validation_error", "세션 id가 경로와 다릅니다.", fields=["id"])
    if not store.SESSION_ID.fullmatch(str(session.get("id") or "")):
        raise ApiError(422, "validation_error", "세션 id 형식이 틀렸습니다.", fields=["id"])
    if len(json.dumps(session, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ApiError(413, "too_large", "세션이 너무 큽니다.")


def _not_found():
    return ApiError(404, "not_found", "세션이 없습니다.")


@router.get("/api/profile")
def get_profile(request: Request):
    return store.backend.get_profile(auth.require_user(request)["id"])


@router.put("/api/profile")
def put_profile(profile: Profile, request: Request):
    data = {k: v.strip() for k, v in profile.model_dump().items()}
    return store.backend.put_profile(auth.require_user(request)["id"], data)


@router.get("/api/sessions")
def list_sessions(request: Request):
    return store.backend.list_sessions(auth.require_user(request)["id"])


@router.get("/api/sessions/{sid}")
def get_session(sid: str, request: Request):
    user = auth.require_user(request)
    s = store.backend.get_session(user["id"], sid) if store.SESSION_ID.fullmatch(sid) else None
    if s is None:
        raise _not_found()
    return s


@router.put("/api/sessions/{sid}")
def put_session(sid: str, body: PutBody, request: Request):
    user = auth.require_user(request)
    _check(body.session, sid)
    try:
        return store.backend.put_session(user["id"], body.session, body.base_updated_at)
    except store.Conflict as e:
        raise ApiError(409, "version_conflict", "다른 탭에서 먼저 저장했습니다. 최신 세션을 다시 불러옵니다.",
                       current=e.current)


@router.delete("/api/sessions/{sid}", status_code=204)
def delete_session(sid: str, request: Request):
    user = auth.require_user(request)
    if not store.SESSION_ID.fullmatch(sid) or not store.backend.delete_session(user["id"], sid):
        raise _not_found()
    return Response(status_code=204)


@router.post("/api/sessions/import")
def import_sessions(body: ImportBody, request: Request):
    user = auth.require_user(request)
    for s in body.sessions:
        _check(s)
    imported = []
    for s in body.sessions:
        old = s["id"]
        new = old
        for _ in range(3):
            try:
                store.backend.put_session(user["id"], {**s, "id": new}, None)
                break
            except store.Conflict:
                new = "s_" + secrets.token_hex(8)
        else:
            continue
        for value in (s.get("picks") or {}).values():
            if isinstance(value, str):
                runs.adopt(value, user["id"], new)
        imported.append({"old_id": old, "new_id": new})
    return {"imported": imported}
