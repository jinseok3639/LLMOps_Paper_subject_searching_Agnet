"""섹션 OpenProblems → 단위 OpenProblems 추출 (P2).

실행: python -m src.extract [--refresh] [--paper PAPER_ID]

입력: 3_section_open_problems/{paper_id}/{섹션}.json
결과: 4_unit_open_problems/{paper_id}/{섹션}.json  섹션 1개 = 단위 파일 1개. LLM이 뽑은 항목 전체를 quote_verified로 표시해 저장
      4_unit_open_problems/all.json              검증 통과 항목만 모은 통합본 (앱이 읽음)

- 섹션 파일 1개당 LLM 1회 호출. 단위 파일이 이미 있으면 그 항목을 다시 쓰고 API를 호출하지 않는다 (--refresh로 강제).
- quote 가 섹션 본문에 실제로 있는지 textnorm.contains 로 매 실행마다 다시 검증한다.
"""
import argparse
import json
from datetime import datetime, timezone

from pydantic import BaseModel

from src import config, llm
from src.textnorm import contains

PROMPT_PATH = config.PROMPT_DIR / "extract_open_problems.md"


class ExtractedProblem(BaseModel):
    title: str
    summary: str
    keywords: list[str]
    quote: str


class Response(BaseModel):
    problems: list[ExtractedProblem]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def section_key(stem: str) -> str:
    """파일명 앞의 섹션 번호 토큰 (끝의 점 제거). 예: '2.2 Adversarial...' -> '2.2', 'VI. DISCUSSION' -> 'VI'."""
    return stem.split()[0].rstrip(".")


def _sort_key(stem: str):
    """섹션 번호 순 정렬 키. 숫자는 정수, 그 외(로마자 등)는 문자열로 비교."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in section_key(stem).split(".")]


def load_page_texts(paper_id: str) -> dict[int, tuple[int, str]]:
    """파싱 JSON에서 element id -> (page, text)."""
    data = json.loads((config.PARSED_DIR / f"{paper_id}.json").read_text(encoding="utf-8"))
    return {e["id"]: (e["page"], (e.get("content") or {}).get("text") or "") for e in data["elements"]}


def find_page(section: dict, quote: str, elements: dict[int, tuple[int, str]]) -> int:
    """quote 를 포함하는 첫 element 의 페이지. 없으면 page_start."""
    for eid in section.get("element_ids", []):
        if eid in elements and contains(elements[eid][1], quote):
            return elements[eid][0]
    return section["page_start"]


def call_llm(paper_id: str, section: dict) -> Response:
    user = (
        f"Paper title: {config.PROPOSALS[paper_id]['title']}\n"
        f"Parent section: {section.get('parent_section') or '(none)'}\n"
        f"Section title: {section['section_title']}\n\n"
        f"Section content:\n{section['content']}"
    )
    messages = [
        {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
        {"role": "user", "content": user},
    ]
    return llm.chat_json(messages, Response, temperature=0.0)


def get_items(paper_id: str, stem: str, section: dict, refresh: bool) -> tuple[list[ExtractedProblem], dict, bool]:
    """단위 파일이 있으면 그 항목을 사용. (항목, {model, generated_at}, API 호출 여부) 반환."""
    unit_path = config.UNIT_OP_DIR / paper_id / f"{stem}.json"
    if unit_path.exists() and not refresh:
        old = json.loads(unit_path.read_text(encoding="utf-8"))
        items = [ExtractedProblem.model_validate(p) for p in old["problems"]]
        return items, {"model": old["model"], "generated_at": old["generated_at"]}, False
    resp = call_llm(paper_id, section)
    return resp.problems, {"model": config.MODEL, "generated_at": now_iso()}, True


def process_section(paper_id: str, section_path, elements, refresh: bool) -> tuple[dict, bool]:
    """섹션 1개 처리 → 단위 파일 내용."""
    section = json.loads(section_path.read_text(encoding="utf-8"))
    stem = section_path.stem
    items, gen, called = get_items(paper_id, stem, section, refresh)
    key = section_key(stem)
    problems, n = [], 0
    for item in items:
        verified = contains(section["content"], item.quote)
        if verified:
            n += 1
        problems.append({
            "problem_id": f"{paper_id}:{key}:{n}" if verified else None,
            "title": item.title.strip(),
            "summary": item.summary.strip(),
            "keywords": [k.strip().lower() for k in item.keywords],
            "quote": item.quote,
            "page": find_page(section, item.quote, elements) if verified else None,
            "quote_verified": verified,
        })
    unit = {
        "paper_id": paper_id,
        "section_file": section_path.name,  # 3_section_open_problems/{paper_id}/ 아래 같은 이름
        "section_title": section["section_title"],
        "parent_section": section.get("parent_section"),
        **gen,
        "problems": problems,
    }
    return unit, called


def flatten(unit: dict) -> list[dict]:
    """단위 파일 → 통합본 항목 (검증 통과만)."""
    pid = unit["paper_id"]
    return [{
        "problem_id": p["problem_id"],
        "paper_id": pid,
        "paper_title": config.PROPOSALS[pid]["title"],
        "section_title": unit["section_title"],
        "parent_section": unit["parent_section"],
        "section_file": unit["section_file"],
        "title": p["title"],
        "summary": p["summary"],
        "keywords": p["keywords"],
        "quote": p["quote"],
        "page": p["page"],
    } for p in unit["problems"] if p["quote_verified"]]


def run(refresh: bool = False, only_paper: str | None = None) -> None:
    if only_paper and only_paper not in config.PROPOSALS:
        raise SystemExit(f"알 수 없는 paper_id: {only_paper}")
    targets = [only_paper] if only_paper else list(config.PROPOSALS)
    calls = 0
    for paper_id in targets:
        elements = load_page_texts(paper_id)
        out_dir = config.UNIT_OP_DIR / paper_id
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in (config.SECTION_OP_DIR / paper_id).glob("*.json"):
            unit, called = process_section(paper_id, f, elements, refresh)
            calls += called
            (out_dir / f.name).write_text(json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8")

    # 통합본은 항상 전체 단위 파일에서 다시 만든다 (--paper 실행 시에도 다른 논문 유지)
    problems, total = [], 0
    print()
    for paper_id in config.PROPOSALS:
        files = sorted((config.UNIT_OP_DIR / paper_id).glob("*.json"), key=lambda p: _sort_key(p.stem))
        rows = []
        for f in files:
            unit = json.loads(f.read_text(encoding="utf-8"))
            kept = flatten(unit)
            problems += kept
            total += len(unit["problems"])
            rows.append((f.stem, len(kept), len(unit["problems"])))
        print(f"{paper_id}: {sum(r[1] for r in rows)}개")
        for stem, kept, tot in rows:
            print(f"  {section_key(stem):>6} {stem[:50]:<50} {kept}/{tot}")
    out = {"model": config.MODEL, "generated_at": now_iso(), "problems": problems}
    config.OPEN_PROBLEMS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    rate = len(problems) / total * 100 if total else 0
    print(f"\n총 {len(problems)}개 (quote 통과율 {rate:.1f}% = {len(problems)}/{total}), "
          f"탈락 {total - len(problems)}개, LLM 호출 {calls}회")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="기존 단위 파일을 무시하고 LLM 재호출")
    ap.add_argument("--paper", help="이 paper_id만 처리")
    args = ap.parse_args()
    run(refresh=args.refresh, only_paper=args.paper)


if __name__ == "__main__":
    main()
