"""POST /api/research: ② → ③ 참고문헌 검색과 후보 생성 (API 명세 6.2)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import conversation, runs
from src.api.common import ApiError, Memo, Message, Profile, check, history, memo_dict
from src.api.sse import stream_job
from src.recommend import problem_card
from src.research import generate_research

router = APIRouter()


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    problem_id: str = ""
    messages: list[Message] = []
    memo: Memo | None = None


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
    # 약 130회 호출에 반복해서 들어가므로 정해진 것만 넣는다 (src.conversation.research_conversation)
    convo = conversation.research_conversation(history(body.messages), memo_dict(body.memo))

    def job(progress, delta):
        result = generate_research(profile, convo, problem, progress)
        return {"run_id": runs.save("research", result, body.session_id), **public(result)}

    return stream_job(request, body.session_id, job)
