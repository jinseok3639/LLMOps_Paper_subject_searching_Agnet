"""POST /api/verify: ③에서 고른 후보 검증 (API 명세 6.4, 설계서 4장). 검증하는 동안 화면은 입력을 잠근다 (F-5).

revision_run_id가 있으면 고친 후보(이슈 #12)를 검증하고, 결과에 revision_run_id와 topic_version을 남긴다."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import runs, studio
from src.api.common import ApiError, Profile, check
from src.api.revise import current_topic, load_research
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
    revision_run_id: str = ""


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
    research = load_research(body.research_run_id)
    topic, version, revision = current_topic(research, body.topic_id, body.revision_run_id)
    versioned = {"revision_run_id": (revision or {}).get("run_id"), "topic_version": version}

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
        result = {**result, **versioned}
        run_id = runs.save("verify", {"research_run_id": research["run_id"], **result}, body.session_id)
        if result["status"] == "budget":  # 한 데까지는 저장해 두고 budget 이벤트로 알린다
            b = result["budget"]
            raise BudgetExceeded(b["used"], b["limit"], b["scope"])
        return {"run_id": run_id, **public(result)}

    return stream_job(request, body.session_id, job)
