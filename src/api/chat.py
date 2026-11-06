"""POST /api/chat: 질의응답 스트리밍 (API 명세 6.3).

actions가 있으면 먼저 채팅이 그중 하나를 요청하는지 판별한다 (이슈 #23). 요청이면 답변 대신 result에 action을 담는다.
화면은 그 행동을 버튼과 같은 함수로 실행한다. actions = [{name, target, label}], 화면이 지금 단계에서 할 수 있는 것.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.api.common import Message, Profile, check, history
from src.api.sse import stream_job
from src.chat import decide_action, reply_stream

router = APIRouter()


class Action(BaseModel):
    name: str
    target: str = ""
    label: str = ""


class Body(BaseModel):
    session_id: str
    profile: Profile = Profile()
    picks: dict = {}
    messages: list[Message] = []
    message: str = ""
    actions: list[Action] = []
    stage: str = ""  # 세션의 현재 단계 (browse, explore, ...). 판별이 다음 단계를 유추하는 데 쓴다


@router.post("/api/chat")
def post_chat(body: Body, request: Request):
    profile = check(body.profile, message=body.message)
    conversation = history(body.messages)
    actions = [a.model_dump() for a in body.actions]

    def job(progress, delta):
        if actions:
            progress("요청 확인 중", step="intent")
            action = decide_action(conversation, body.message.strip(), actions, body.stage)
            if action:
                return {"action": action}
        progress("답변 작성 중", step="answer")
        parts = []
        for piece in reply_stream(profile, body.picks, conversation, body.message.strip()):
            delta(piece)
            parts.append(piece)
        return {"content": "".join(parts)}

    return stream_job(request, body.session_id, job)
