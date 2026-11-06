"""POST /api/chat: 질의응답 스트리밍 (API 명세 6.3). POST /api/chat/compact: 대화 압축 (이슈 #31).

actions가 있으면 먼저 채팅이 그중 하나를 요청하는지 판별한다 (이슈 #23). 요청이면 답변 대신 result에 action을 담는다.
화면은 그 행동을 버튼과 같은 함수로 실행한다. actions = [{name, target, label}], 화면이 지금 단계에서 할 수 있는 것.

답변 result에는 이번 호출의 입력 토큰 수(tokens_in)와 압축이 필요한지(compact)를 담는다. compact가 true면 화면이
/api/chat/compact를 따로 불러 받은 memo를 세션에 저장하고, 다음 요청부터 함께 보낸다. 서버는 memo를 저장하지 않는다.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src import conversation
from src.api.common import Memo, Message, Profile, check, history, memo_dict
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
    memo: Memo | None = None


@router.post("/api/chat")
def post_chat(body: Body, request: Request):
    profile = check(body.profile, message=body.message)
    conv = history(body.messages)
    actions = [a.model_dump() for a in body.actions]
    memo = memo_dict(body.memo)

    def job(progress, delta):
        if actions:
            progress("요청 확인 중", step="intent")
            action = decide_action(conv, body.message.strip(), actions, body.stage)
            if action:
                return {"action": action}
        progress("답변 작성 중", step="answer")
        parts, usage = [], {}
        for piece in reply_stream(profile, body.picks, conv, body.message.strip(), memo, usage):
            delta(piece)
            parts.append(piece)
        tokens = usage.get("tokens_in")
        return {"content": "".join(parts), "tokens_in": tokens,
                "compact": bool(tokens and tokens > conversation.COMPACT_TOKENS)}

    return stream_job(request, body.session_id, job)


class CompactBody(BaseModel):
    session_id: str
    messages: list[Message] = []
    memo: Memo | None = None


@router.post("/api/chat/compact")
def post_compact(body: CompactBody, request: Request):
    """오래된 주변 대화를 항목 목록으로 줄인다. 요약할 것이 없으면 memo는 null (화면은 기존 memo를 유지)."""
    conv = history(body.messages)
    memo = memo_dict(body.memo)

    def job(progress, delta):
        progress("대화 정리 중", step="compact")
        return {"memo": conversation.compact(conv, memo)}

    return stream_job(request, body.session_id, job, lock=False)
