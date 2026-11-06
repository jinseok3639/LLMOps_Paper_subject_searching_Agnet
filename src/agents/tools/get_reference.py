"""저장된 결과에서 논문 한 편을 꺼내는 Tool (이슈 #36).

채팅 컨텍스트에는 후보가 인용한 ③ 참고문헌만 초록이 들어간다. 나머지 참고문헌(ref-NNN)과 ④ 최근접 논문(N1~)의
내용은 이 Tool로 읽는다. S2를 부르지 않고 저장된 ③ 결과와 ④ 검증 결과만 본다.
"""
from pydantic import BaseModel, ConfigDict, Field

from src import calllog
from src.agents.tools.base import Tool

CARD_FIELDS = ("problem", "method", "assumptions", "evaluation", "limitations")


class GetReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, description="컨텍스트에 적힌 논문 id. 예: ref-012, N1")


def _reference(r: dict) -> dict:
    return {"id": r.get("id"), "paper_id": r.get("paper_id"), "title": r.get("title"), "year": r.get("year"),
            "abstract": r.get("abstract") or "(초록 없음)", "url": r.get("url")}


def _nearest(n: dict) -> dict:
    """④ 최근접 논문. 초록 대신 검증 때 만든 요소별 요약(card)을 준다."""
    card = n.get("card") or {}
    summary = {k: (card[k].get("text") if isinstance(card.get(k), dict) else card[k]) for k in CARD_FIELDS if card.get(k)}
    return {"id": n.get("id"), "paper_id": n.get("paper_id"), "title": n.get("title"), "year": n.get("year"),
            "url": n.get("url"), "read_level": n.get("level"), "summary": summary}


def make_tool(research_run: dict | None, verify_run: dict | None) -> Tool:
    """picks가 가리키는 ③④ 결과로 Tool을 만든다. 결과가 없어도 Tool은 만든다 (툴 목록이 턴마다 같아야 prefix가 캐시된다)."""
    refs = {r.get("id"): r for r in (research_run or {}).get("references") or []}
    nearest = {n.get("id"): n for n in (verify_run or {}).get("nearest") or []}

    def run(args: GetReferenceInput) -> dict:
        key = args.id.strip().strip("[]")
        with calllog.timed("runs", "get_reference", key) as c:
            if key in refs:
                paper = _reference(refs[key])
            elif key in nearest:
                paper = _nearest(nearest[key])
            else:
                known = list(refs) + list(nearest)
                c.output = "not found"
                return {"error": f"{key}는 저장된 결과에 없다. 있는 id: {', '.join(known) or '없음'}"}
            c.output = paper["title"]
            return {"papers": [paper]}

    return Tool(
        name="get_reference",
        description="컨텍스트의 검색 참고문헌(ref-NNN)이나 ④ 검증의 가까운 논문(N1 등) 한 편의 제목, 연도, 초록(또는 요약), "
                    "링크를 저장된 결과에서 꺼낸다. 컨텍스트에 초록이 없는 논문의 내용을 물을 때 쓴다.",
        input_model=GetReferenceInput,
        run=run,
    )
