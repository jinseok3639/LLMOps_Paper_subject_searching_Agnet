"""Semantic Scholar 논문 검색 Tool."""
from pydantic import BaseModel, ConfigDict, Field

from src.agents.tools.base import Tool, compact_paper
from src.clients import semantic_scholar as s2

PAPER_FIELDS = "paperId,externalIds,title,abstract,year,venue,publicationTypes,citationCount"


class SearchPapersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, description="영어 검색어")
    year: str | None = Field(default=None, description="연도 범위. 예: 2024-, 2021-2023, 2025")
    limit: int = Field(default=10, ge=1, le=100, description="결과 수")


def run(args: SearchPapersInput) -> dict:
    res = s2.search_papers(args.query, PAPER_FIELDS, limit=args.limit, year=args.year)
    papers = res.get("data") or []
    return {"total": res.get("total", len(papers)), "papers": [compact_paper(p) for p in papers]}


TOOL = Tool(
    name="search_papers",
    description="Semantic Scholar에서 Computer Science 논문을 관련도순으로 검색한다. 제목, 초록, 연도, 인용 수를 돌려준다.",
    input_model=SearchPapersInput,
    run=run,
)
