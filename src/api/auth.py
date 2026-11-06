"""로그인 (API 명세 4장): 공개 설정, 로그인 사용자 확인, 계정 삭제.

로그인·로그아웃은 화면이 supabase-js로 한다. 우리 서버에는 로그인·콜백 경로가 없다.
"""
from fastapi import APIRouter, Request, Response

from src import auth, store

router = APIRouter()


@router.get("/api/config")
def get_config():
    """anon 키는 공개해도 되는 키다. 로그인이 꺼져 있으면 둘 다 null."""
    on = auth.enabled()
    return {"supabase_url": auth.url() if on else None, "supabase_anon_key": auth.anon_key() if on else None}


@router.get("/api/me")
def get_me(request: Request):
    return {"user": auth.current_user(request)}


@router.delete("/api/me", status_code=204)
def delete_me(request: Request):
    """계정과 모든 데이터 삭제 (A-6)."""
    user = auth.require_user(request)
    store.backend.delete_user(user["id"])
    auth.delete_auth_user(user["id"])
    return Response(status_code=204)
