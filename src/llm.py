"""Upstage chat (OpenAI 호환) 호출 래퍼.

- 429, 5xx, 연결 오류는 SDK 재시도(max_retries)에 맡긴다.
- 응답은 pydantic 모델로 받는다 (json_schema strict). 형식 오류나 잘린 응답은 여기서 다시 호출한다.
"""
import os
from typing import TypeVar

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from src import config

T = TypeVar("T", bound=BaseModel)

MAX_FORMAT_RETRIES = 3

_client: OpenAI | None = None


class LLMError(Exception):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("UPSTAGE_API_KEY", "").strip()
        if not key:
            raise LLMError("UPSTAGE_API_KEY가 .env에 없습니다")
        _client = OpenAI(api_key=key, base_url=config.UPSTAGE_BASE_URL, max_retries=6, timeout=120)
    return _client


def chat_json(messages: list[dict], schema: type[T], temperature: float = 0.0) -> T:
    """messages를 보내고 schema 형식의 응답을 받는다."""
    last_error = None
    for _ in range(MAX_FORMAT_RETRIES):
        try:
            resp = client().chat.completions.parse(
                model=config.MODEL, messages=messages,
                response_format=schema, temperature=temperature,
            )
        except (ValidationError, openai.LengthFinishReasonError) as e:
            last_error = e
            continue
        parsed = resp.choices[0].message.parsed
        if parsed is not None:
            return parsed
        last_error = LLMError(f"빈 응답 (finish_reason={resp.choices[0].finish_reason})")
    raise LLMError(f"형식 재시도 {MAX_FORMAT_RETRIES}회 실패: {last_error}")


def chat_text(messages: list[dict], temperature: float = 0.0) -> str:
    resp = client().chat.completions.create(model=config.MODEL, messages=messages, temperature=temperature)
    return resp.choices[0].message.content or ""


def embed(texts: list[str], model: str, batch_size: int = 50) -> list[list[float]]:
    """Upstage 임베딩. 입력 순서대로 벡터 반환."""
    out = []
    for i in range(0, len(texts), batch_size):
        resp = client().embeddings.create(model=model, input=texts[i:i + batch_size])
        out += [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
    return out


def chat_stream(messages: list[dict], temperature: float = 0.3):
    """텍스트 응답을 조각 단위로 내보낸다 (Streamlit st.write_stream용)."""
    stream = client().chat.completions.create(model=config.MODEL, messages=messages,
                                              temperature=temperature, stream=True)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
