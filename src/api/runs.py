"""GET /api/runs/{run_id}: 저장된 작업 결과 전체 (API 명세 6.5). 세션을 다시 열 때 화면 복원에 쓴다."""
from fastapi import APIRouter

from src import runs as store
from src.api.common import ApiError

router = APIRouter()


@router.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.load(run_id)
    if run is None:
        raise ApiError(404, "not_found", "결과가 없거나 보관 기간(24시간)이 지났습니다.")
    return run
