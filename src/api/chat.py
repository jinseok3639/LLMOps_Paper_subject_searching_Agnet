"""POST /api/chat: 질의응답 스트리밍 (API 명세 6.3)."""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.api.common import Message, Profile, check, history
from src.api.sse import stream_job
from src.chat import reply_stream

router = APIRouter()


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    picks: dict = {}
    messages: list[Message] = []
    message: str = ""


@router.post("/api/chat")
def post_chat(body: Body, request: Request):
    profile = check(body.profile, message=body.message)
    conversation = history(body.messages)

    def job(progress, delta):
        progress("답변 작성 중", step="answer")
        parts = []
        for piece in reply_stream(profile, body.picks, conversation, body.message.strip()):
            delta(piece)
            parts.append(piece)
        return {"content": "".join(parts)}

    return stream_job(request, body.session_id, job)
