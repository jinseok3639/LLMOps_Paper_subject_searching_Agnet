"""POST /api/verify: ③에서 고른 후보 검증 (API 명세 6.4, 설계서 4장). 검증하는 동안 화면은 입력을 잠근다 (F-5)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import runs, studio
from src.api.common import ApiError, Profile, check
from src.api.sse import stream_job
from src.recommend import problem_card
from src.runs import BudgetExceeded, Cancelled
from src.verify import verify

router = APIRouter()


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    problem_id: str = ""
    research_run_id: str = ""
    topic_id: str = ""


def public(result: dict) -> dict:
    """trace는 /api/runs에만 둔다."""
    return {k: v for k, v in result.items() if k != "trace"}


@router.post("/api/verify")
def post_verify(body: Body, request: Request):
    profile = check(body.profile, problem_id=body.problem_id, research_run_id=body.research_run_id,
                    topic_id=body.topic_id)
    problem = problem_card(body.problem_id.strip())
    if problem is None:
        raise ApiError(404, "not_found", "선택한 문제를 찾을 수 없습니다.")
    research = runs.load(body.research_run_id.strip())
    if research is None or research.get("kind") != "research":
        raise ApiError(404, "not_found", "후보 생성 결과가 없거나 보관 기간(24시간)이 지났습니다.")
    topic = next((t for t in research.get("topics") or [] if t.get("id") == body.topic_id.strip()), None)
    if topic is None:
        raise ApiError(404, "not_found", "선택한 후보를 찾을 수 없습니다.")

    def job(progress, delta):
        def should_cancel():  # 인자 없는 progress()는 취소 확인만 한다
            try:
                progress()
            except Cancelled:
                return True
            return False

        try:
            result = verify(profile, problem, topic, research.get("references") or [], progress, should_cancel)
        except studio.StudioCancelled:  # Studio 상태 확인을 멈춘 것도 연결이 끊겨서다
            raise Cancelled()
        run_id = runs.save("verify", {"research_run_id": research["run_id"], **result}, body.session_id)
        if result["status"] == "budget":  # 한 데까지는 저장해 두고 budget 이벤트로 알린다
            b = result["budget"]
            raise BudgetExceeded(b["used"], b["limit"], b["scope"])
        return {"run_id": run_id, **public(result)}

    return stream_job(request, body.session_id, job)
