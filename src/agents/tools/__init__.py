"""LLM에 노출하는 Tool. ToolBox로 묶어서 쓴다."""
from src.agents.tools import get_reference, search_papers
from src.agents.tools.base import Tool, ToolBox


def default_toolbox() -> ToolBox:
    return ToolBox([search_papers.TOOL])


def chat_toolbox(research_run: dict | None = None, verify_run: dict | None = None) -> ToolBox:
    """후속 대화용 (이슈 #36). 저장된 ③④ 결과 조회와 S2 검색. 툴 목록은 결과 유무와 관계없이 같다 (prefix 캐시)."""
    return ToolBox([get_reference.make_tool(research_run, verify_run), search_papers.TOOL])
