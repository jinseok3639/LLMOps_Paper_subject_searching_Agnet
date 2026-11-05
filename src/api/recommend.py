"""POST /api/recommend: ① 둘러보기 (API 명세 6.1)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import runs
from src.api.common import Profile, check
from src.api.sse import stream_job
from src.recommend import recommend

router = APIRouter()


class Rejection(BaseModel):
    target: str = "problem"
    target_id: str
    reason: str = ""


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    rejections: list[Rejection] = []


def public(result: dict) -> dict:
    """trace는 서버에만 둔다."""
    return {k: v for k, v in result.items() if k != "trace"}


@router.post("/api/recommend")
def post_recommend(body: Body, request: Request):
    profile = check(body.profile)
    rejections = [r.model_dump() for r in body.rejections]

    def job(progress, delta):
        result = recommend(profile, rejections, progress)
        return {"run_id": runs.save("recommend", result, body.session_id), **public(result)}

    return stream_job(request, body.session_id, job)
