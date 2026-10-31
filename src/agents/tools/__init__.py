"""LLM에 노출하는 Tool. 에이전트 연결 전이며, default_toolbox()로 묶어서 쓴다."""
from src.agents.tools import search_papers
from src.agents.tools.base import Tool, ToolBox


def default_toolbox() -> ToolBox:
    return ToolBox([search_papers.TOOL])
