"""Semantic Scholar 논문 검색 Tool."""
from pydantic import BaseModel, ConfigDict, Field

from src import calllog
from src.agents.tools.base import Tool, compact_paper
from src.clients import semantic_scholar as s2

PAPER_FIELDS = "paperId,externalIds,title,abstract,year,venue,publicationTypes,citationCount"
SEARCH_TRIES = 4  # 채팅은 사용자가 기다리므로 ③④ 검색처럼 짧게


class SearchPapersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, description="영어 검색어")
    year: str | None = Field(default=None, description="연도 범위. 예: 2024-, 2021-2023, 2025")
    limit: int = Field(default=5, ge=1, le=20, description="결과 수. 초록이 입력에 들어가므로 필요한 만큼만")


def run(args: SearchPapersInput) -> dict:
    with calllog.timed("s2", "paper/search", args.model_dump(exclude_none=True)) as c:
        res = s2.search_papers(args.query, PAPER_FIELDS, limit=args.limit, year=args.year, tries=SEARCH_TRIES)
        papers = [compact_paper(p) for p in res.get("data") or []]
        c.output = [{"paper_id": p["paper_id"], "title": p["title"]} for p in papers]
    return {"total": res.get("total", len(papers)), "papers": papers}


TOOL = Tool(
    name="search_papers",
    description="Semantic Scholar에서 Computer Science 논문을 관련도순으로 검색한다. 제목, 초록, 연도, 인용 수, 링크를 돌려준다. "
                "컨텍스트에 없는 논문(최신 연구, 선행 연구, 특정 방법을 쓴 논문)을 찾을 때만 쓴다.",
    input_model=SearchPapersInput,
    run=run,
)
