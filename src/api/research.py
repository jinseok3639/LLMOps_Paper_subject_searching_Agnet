"""POST /api/research: ② → ③ 참고문헌 검색과 후보 생성 (API 명세 6.2)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import runs
from src.api.common import ApiError, Message, Profile, check, history
from src.api.sse import stream_job
from src.recommend import problem_card
from src.research import generate_research

router = APIRouter()


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    problem_id: str = ""
    messages: list[Message] = []


def public(result: dict) -> dict:
    """generation(토너먼트 기록), trace, 참고문헌 초록은 /api/runs에만 둔다."""
    out = {k: v for k, v in result.items() if k not in ("trace", "generation")}
    out["references"] = [{k: v for k, v in r.items() if k != "abstract"} for r in result["references"]]
    return out


@router.post("/api/research")
def post_research(body: Body, request: Request):
    profile = check(body.profile, problem_id=body.problem_id)
    problem = problem_card(body.problem_id.strip())
    if problem is None:
        raise ApiError(404, "not_found", "선택한 문제를 찾을 수 없습니다.")
    conversation = history(body.messages)

    def job(progress, delta):
        result = generate_research(profile, conversation, problem, progress)
        return {"run_id": runs.save("research", result, body.session_id), **public(result)}

    return stream_job(request, body.session_id, job)
