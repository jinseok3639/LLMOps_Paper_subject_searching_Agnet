"""라우터가 같이 쓰는 요청 형식과 오류 (API 명세 2.1, 2.3)."""
from pydantic import BaseModel

REQUIRED_PROFILE = ("lab_name", "lab_topics")


class ApiError(Exception):
    """server.py가 {"error": {...}} 형식의 JSON으로 바꾼다."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": {"code": code, "message": message, "retryable": retryable, **extra}}


class Profile(BaseModel):
    lab_name: str = ""
    lab_topics: str = ""
    interests: str = ""


class Message(BaseModel):
    """세션 메시지 (API 명세 2.2). id, created_at 같은 나머지 필드는 무시한다."""
    role: str
    content: str = ""
    status: str = "done"


def check(profile: Profile, **required: str) -> dict:
    """필수 항목(프로필 + required)이 비면 422. 앞뒤 공백을 지운 프로필 dict를 돌려준다."""
    data = {k: v.strip() for k, v in profile.model_dump().items()}
    fields = [k for k in REQUIRED_PROFILE if not data[k]] + [k for k, v in required.items() if not (v or "").strip()]
    if fields:
        raise ApiError(422, "validation_error", "필수 항목을 입력해 주세요.", fields=fields)
    return data


def history(messages: list[Message]) -> list[dict]:
    """LLM에 넘길 대화. 중단·실패한 메시지는 뺀다."""
    return [{"role": m.role, "content": m.content} for m in messages
            if m.role in ("user", "assistant") and m.status == "done" and m.content.strip()]
