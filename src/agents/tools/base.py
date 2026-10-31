"""LLM에 노출하는 Tool의 공통 틀.

- Tool: 이름, 설명, 입력 형식(pydantic), 실행 함수. schema()는 OpenAI 호환 function 형식이다.
- ToolBox: 인자 검증과 실행을 맡고, 오류는 LLM이 읽을 수 있게 {"error": ...}로 돌려준다.
  세션 동안 Tool이 돌려준 논문을 기억해, 그 밖의 논문을 인용하는지 검사한다.
"""
import json
from dataclasses import dataclass
from typing import Callable

import requests
from pydantic import BaseModel, ValidationError

from src.clients.semantic_scholar import S2Error

TOOL_ERRORS = (S2Error, requests.RequestException)


def compact_paper(paper: dict) -> dict:
    """S2 논문 객체에서 LLM에 보여 줄 필드만 남긴다."""
    ext = paper.get("externalIds") or {}
    return {
        "paper_id": paper.get("paperId"),
        "title": paper.get("title"),
        "year": paper.get("year"),
        "venue": paper.get("venue"),
        "abstract": paper.get("abstract"),
        "citation_count": paper.get("citationCount"),
        "publication_types": paper.get("publicationTypes"),
        "arxiv": ext.get("ArXiv"),
        "doi": ext.get("DOI"),
    }


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[BaseModel], dict]

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.input_model.model_json_schema()}}


class ToolBox:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}
        self.seen_papers: dict[str, dict] = {}  # paper_id, ARXIV:{id} → 논문

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, arguments: str | dict) -> dict:
        """arguments: LLM이 준 JSON 문자열 또는 dict."""
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"알 수 없는 Tool: {name}"}
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            result = tool.run(tool.input_model.model_validate(args))
        except (json.JSONDecodeError, ValidationError) as e:
            return {"error": f"잘못된 입력: {e}"}
        except TOOL_ERRORS as e:
            return {"error": f"{name} 실패: {e}"}
        self._remember(result)
        return result

    def _remember(self, result: dict) -> None:
        for p in result.get("papers", []):
            if p.get("paper_id"):
                self.seen_papers[p["paper_id"]] = p
            if p.get("arxiv"):
                self.seen_papers[f"ARXIV:{p['arxiv']}"] = p

    def unknown_citations(self, paper_ids: list[str]) -> list[str]:
        """Tool이 돌려준 적 없는 논문 id. 비어 있지 않으면 그 인용은 쓰면 안 된다."""
        return [pid for pid in paper_ids if pid not in self.seen_papers]
