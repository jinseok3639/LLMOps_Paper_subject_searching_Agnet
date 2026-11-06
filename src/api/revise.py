"""POST /api/revise: ③·④ 후보 고치기 (이슈 #12). 대화로 수정안을 만들고 kind "revision" 결과로 저장한다.

화면은 수정안 카드(바뀐 필드만)를 보여 주고, [적용]을 누르면 picks.revision_run_id로 이 결과를 가리킨다.
바뀐 게 없으면 저장하지 않고 {run_id: null, changes: [], reason}을 돌려준다.
verify_run_id가 있으면 그 검증 결과(최근접 논문, 비평)를 수정 근거로 함께 넘긴다 (④ "결과를 반영해 고치기").
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import revise, runs
from src.api.common import ApiError, Message, Profile, check, history
from src.api.sse import stream_job

router = APIRouter()


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    research_run_id: str = ""
    topic_id: str = ""
    revision_run_id: str = ""  # 지금 버전. 비면 원본(v1)
    verify_run_id: str = ""
    messages: list[Message] = []


def load_research(research_run_id: str) -> dict:
    research = runs.load(research_run_id.strip())
    if research is None or research.get("kind") != "research":
        raise ApiError(404, "not_found", "후보 생성 결과가 없거나 보관 기간(24시간)이 지났습니다.")
    return research


def current_topic(research: dict, topic_id: str, revision_run_id: str) -> tuple[dict, int, dict | None]:
    """(지금 버전의 후보, 버전, revision 결과). ④·⑤도 쓴다. 없거나 다른 후보의 수정안이면 404."""
    revision = None
    if revision_run_id.strip():
        revision = runs.load(revision_run_id.strip())
        if revision is None:
            raise ApiError(404, "not_found", "후보 수정 결과가 없거나 보관 기간(24시간)이 지났습니다.")
    try:
        topic, version = revise.resolve(research, topic_id.strip(), revision)
    except ValueError:
        raise ApiError(404, "not_found", "선택한 후보의 수정 결과가 아닙니다.")
    if topic is None:
        raise ApiError(404, "not_found", "선택한 후보를 찾을 수 없습니다.")
    return topic, version, revision


@router.post("/api/revise")
def post_revise(body: Body, request: Request):
    profile = check(body.profile, research_run_id=body.research_run_id, topic_id=body.topic_id)
    research = load_research(body.research_run_id)
    topic, version, _ = current_topic(research, body.topic_id, body.revision_run_id)
    verify_run = None
    if body.verify_run_id.strip():
        verify_run = runs.load(body.verify_run_id.strip())
        if verify_run is None or verify_run.get("kind") != "verify" or verify_run.get("topic_id") != topic["id"]:
            raise ApiError(404, "not_found", "선택한 후보의 검증 결과가 없거나 보관 기간(24시간)이 지났습니다.")
    conversation = history(body.messages)

    def job(progress, delta):
        progress("고칠 내용을 정리하는 중", step="propose")
        got = revise.propose(profile, topic, conversation, verify_run)
        out = {"research_run_id": research["run_id"], "topic_id": topic["id"],
               "base_run_id": body.revision_run_id.strip() or None, "base_version": version, "version": version + 1,
               "from_verify": verify_run is not None, **got}
        if not got["changes"]:
            return {"run_id": None, **out}
        return {"run_id": runs.save("revision", out, body.session_id), **out}

    return stream_job(request, body.session_id, job)
