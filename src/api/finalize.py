"""POST /api/finalize: ⑤ 확정. 저장된 결과로 최종 주제 카드를 만든다 (API 명세 6.4, 설계서 5장). LLM 호출 없음.

result와 저장된 결과(/api/runs)는 같은 모양 {run_id, card, markdown}이다. verify_run_id가 비면 판정은 "검증 안 함".
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import runs
from src.api.common import ApiError
from src.api.revise import current_topic
from src.api.sse import stream_job
from src.finalize import build_card, to_markdown
from src.recommend import problem_card

router = APIRouter()


class Body(BaseModel):
    session_id: str
    recommend_run_id: str = ""
    problem_id: str = ""
    research_run_id: str = ""
    topic_id: str = ""
    revision_run_id: str = ""  # 고친 후보 (이슈 #12). 검증한 버전과 달라도 막지 않는다
    verify_run_id: str = ""


def _load(run_id: str, kind: str, label: str) -> dict:
    run = runs.load(run_id)
    if run is None or run.get("kind") != kind:
        raise ApiError(404, "not_found", f"{label} 결과가 없거나 보관 기간(24시간)이 지났습니다.")
    return run


@router.post("/api/finalize")
def post_finalize(body: Body, request: Request):
    ids = {k: v.strip() for k, v in body.model_dump().items() if k != "session_id"}
    missing = [k for k in ("research_run_id", "topic_id") if not ids[k]]
    if missing:
        raise ApiError(422, "validation_error", "필수 항목을 입력해 주세요.", fields=missing)
    research = _load(ids["research_run_id"], "research", "후보 생성")
    _, _, revision = current_topic(research, ids["topic_id"], ids["revision_run_id"])
    recommend = _load(ids["recommend_run_id"], "recommend", "추천") if ids["recommend_run_id"] else None
    verify = _load(ids["verify_run_id"], "verify", "검증") if ids["verify_run_id"] else None
    if verify and (verify.get("topic_id") != ids["topic_id"]
                   or verify.get("research_run_id", ids["research_run_id"]) != ids["research_run_id"]):
        raise ApiError(404, "not_found", "선택한 후보의 검증 결과가 아닙니다. ④에서 다시 검증해 주세요.")
    problem = problem_card(ids["problem_id"] or research.get("problem_id") or "")
    if problem is None:
        raise ApiError(404, "not_found", "선택한 문제를 찾을 수 없습니다.")

    def job(progress, delta):
        progress("최종 주제 카드 조립 중", step="assemble")
        card = build_card(recommend, problem, research, ids["topic_id"], verify, revision)
        result = {"card": card, "markdown": to_markdown(card)}
        return {"run_id": runs.save("final", result, body.session_id), **result}

    return stream_job(request, body.session_id, job)
