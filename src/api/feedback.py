"""POST /api/feedback: 추천·후보·최종 주제에 좋아요/싫어요 (API 명세 6.5, 요구사항 O-3).

- target: problem (① 추천 카드, target_id = problem_id) · topic (③ 후보, target_id = "{research_run_id}:{후보 id}".
  후보 id(s1-assumption 등)는 작업마다 다시 쓰이므로 run_id를 붙인다) · final (⑤ 확정 주제).
- 한 줄씩 쌓기만 한다 (calllog.append → DB/feedback.jsonl, Supabase 단계에서는 feedback 테이블).
  같은 대상에 여러 번 누르면 마지막 줄이 유효하다.
"""
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, StringConstraints

from src import calllog

router = APIRouter()
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class Body(BaseModel):
    session_id: Text
    target: Literal["problem", "topic", "final"]
    target_id: Text
    value: Literal["up", "down"]
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None


@router.post("/api/feedback", status_code=204)
def post_feedback(body: Body):
    calllog.append("feedback", {"id": "f_" + secrets.token_hex(8), **body.model_dump(),
                                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return Response(status_code=204)
