"""연구 카드 (설계서 4.2, J-1, J-2): 초록 카드(Solar)와 본문 카드(Studio 에이전트 B).

- 필드 5개는 {text, quote, page}. 원문에 없으면 빈 문자열, 지어내지 않고 결과 수치는 넣지 않는다.
- quote는 코드가 원문과 대조한다 (초록 카드는 LaTeX·태그를 걷어낸 초록, 본문 카드는 Studio 파싱 결과).
  대조에 실패한 필드(quote 없이 text만 있는 경우 포함)는 비우고 이름을 quote_failed에 적는다.
- 같은 논문은 다시 만들지 않는다: 초록 카드 DB/abstract_cards/, 본문 카드 data/papers/studio/ (6.1).
- Studio 콘솔 설정용 지침과 스키마: prompts/studio_card_instruction.md, prompts/studio_card_schema.json
  (중첩 객체를 받지 않으면 studio_card_schema_flat.json. 두 형식 모두 읽는다)
"""
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import requests
from pydantic import BaseModel, Field

from src import calllog, config, llm, studio
from src.clients import arxiv
from src.clients import semantic_scholar as s2
from src.textnorm import contains

FIELDS = ("problem", "method", "assumptions", "evaluation", "limitations")
ABSTRACT_CARD_DIR = config.DB_DIR / "abstract_cards"              # {paper_id}.json
PAPER_PDF_DIR = config.ROOT_DIR / "data" / "papers" / "pdf"       # {paper_id}.pdf
STUDIO_CARD_DIR = config.ROOT_DIR / "data" / "papers" / "studio"  # {paper_id}.card.json, {paper_id}.raw.json
PROMPT_PATH = config.PROMPT_DIR / "abstract_card.md"
S2_PDF_FIELDS = "openAccessPdf,externalIds"
PDF_FIELDS = ("openAccessPdf", "externalIds")
S2_TRIES = 3  # 사용자가 기다리는 ④라 짧게
ARXIV_PDF_URL = "https://arxiv.org/pdf/{}"
PDF_TIMEOUT = 30
MAX_PDF_BYTES = 50 * 2 ** 20

log = logging.getLogger(__name__)


class CardField(BaseModel):
    text: str = ""
    quote: str = ""
    page: int | None = None


class Card(BaseModel):
    paper_id: str
    title: str
    level: Literal["abstract", "fulltext"]
    problem: CardField = Field(default_factory=CardField)
    method: CardField = Field(default_factory=CardField)
    assumptions: CardField = Field(default_factory=CardField)
    evaluation: CardField = Field(default_factory=CardField)
    limitations: CardField = Field(default_factory=CardField)
    quote_failed: list[str] = Field(default_factory=list)  # 대조 실패로 비운 필드


class Quoted(BaseModel):
    text: str
    quote: str


class AbstractCardOut(BaseModel):
    """Solar 응답 형식 (초록 카드는 page가 없다)."""
    problem: Quoted
    method: Quoted
    assumptions: Quoted
    evaluation: Quoted
    limitations: Quoted


_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    """같은 논문을 동시에 두 번 만들지 않도록 논문별 잠금."""
    with _locks_guard:
        return _locks[key]


def _key(paper: dict) -> str:
    """파일 이름으로 쓸 paper_id (Windows에서 못 쓰는 문자는 _로)."""
    pid = (paper.get("paper_id") or "").strip()
    if not pid:
        raise ValueError("paper_id가 없습니다")
    return re.sub(r"[^\w.-]", "_", pid)


def _write_json(path: Path, data) -> None:
    """임시 파일에 쓴 뒤 이름을 바꾼다 (설계서 6.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def clean_abstract(text: str) -> str:
    """S2 초록의 LaTeX(\\textit{x}, \\citep{..})와 JATS/HTML 태그를 걷어낸다. LLM이 표기를 빼고 인용하면 대조에 실패하므로
    프롬프트와 대조 모두 이 텍스트를 쓴다."""
    text = re.sub(r"\s*\\cite[a-z]*\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:text[a-z]*|emph|math[a-z]*)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"</?(?:jats:[\w-]+|i|b|em|strong|sub|sup|p)\b[^<>]*>", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _verified(paper: dict, title: str, level: str, fields: dict[str, CardField], source: str) -> dict:
    """quote를 source와 대조해 실패한 필드를 비우고 카드 dict를 만든다."""
    failed = []
    for name in FIELDS:
        f = fields.get(name) or CardField()
        if (f.text or f.quote) and not contains(source, f.quote):
            log.info("카드 인용 대조 실패 %s %s %s: %r", level, paper.get("paper_id"), name, f.quote[:200])
            f = CardField()
            failed.append(name)
        fields[name] = f
    return Card(paper_id=paper["paper_id"], title=title, level=level,
                quote_failed=failed, **fields).model_dump()


def abstract_card(paper: dict) -> dict:
    """초록 → 카드 (Solar 1회, 캐시). paper: {paper_id, title, abstract, year}.

    초록이 없으면 빈 카드 (호출, 저장 안 함). 예외: llm.LLMError
    """
    key = _key(paper)
    path = ABSTRACT_CARD_DIR / f"{key}.json"
    with _lock("abstract:" + key):
        if path.exists():
            return json.loads(path.read_text("utf-8"))
        abstract = clean_abstract(paper.get("abstract") or "")
        if not abstract:
            return _verified(paper, paper.get("title") or "", "abstract", {}, "")
        user = f"Title: {paper.get('title') or ''}\n"
        if paper.get("year"):
            user += f"Year: {paper['year']}\n"
        user += f"\nAbstract:\n{abstract}"
        out = llm.chat_json([{"role": "system", "content": PROMPT_PATH.read_text("utf-8")},
                             {"role": "user", "content": user}], AbstractCardOut)
        fields = {n: CardField(text=getattr(out, n).text.strip(), quote=getattr(out, n).quote.strip())
                  for n in FIELDS}
        card = _verified(paper, paper.get("title") or "", "abstract", fields, abstract)
        _write_json(path, card)
        return card


class NoPdf(Exception):
    """PDF를 받지 못함. reason: s2_failed | no_url | not_pdf | download_failed (NO_PDF_REASONS)."""

    def __init__(self, reason: str, attempts: list[dict] | None = None):
        super().__init__(f"PDF 없음: {reason}")
        self.reason = reason
        self.attempts = attempts or []


NO_PDF_REASONS = ("s2_failed", "no_url", "not_pdf", "download_failed")


def _s2_pdf_info(paper_id: str) -> dict | None:
    """S2 paper/{id}?fields=openAccessPdf,externalIds (S2 대기열을 지남). 404면 {} (정보 없음), 그 밖의 실패는 None."""
    try:
        return s2.paper(paper_id, S2_PDF_FIELDS, tries=S2_TRIES)
    except s2.S2Error as e:
        log.warning("S2 PDF 정보 실패 %s: %s", paper_id, e)
        return {} if e.status == 404 else None


def _pdf_urls(info: dict) -> list[str]:
    urls = []
    oa = info.get("openAccessPdf")
    if isinstance(oa, dict) and oa.get("url"):
        urls.append(oa["url"])
    arxiv_id = (info.get("externalIds") or {}).get("ArXiv")
    if arxiv_id:
        urls.append(ARXIV_PDF_URL.format(arxiv_id))
    return urls


def _download(url: str, path: Path) -> str:
    """PDF 한 개를 30초 안에 받는다. 결과: ok | not_pdf | http_{상태} | too_large | timeout | error."""
    deadline = time.monotonic() + PDF_TIMEOUT
    try:
        r = requests.get(url, stream=True, timeout=PDF_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (research-topic-app)"})
        try:
            if r.status_code != 200:
                log.warning("PDF %s %s", r.status_code, url)
                return f"http_{r.status_code}"
            data = bytearray()
            for chunk in r.iter_content(1 << 16):
                data += chunk
                if len(data) > MAX_PDF_BYTES:
                    log.warning("PDF 크기 초과 %s", url)
                    return "too_large"
                if time.monotonic() > deadline:
                    log.warning("PDF 시간 초과 %s", url)
                    return "timeout"
        finally:
            r.close()
    except requests.RequestException as e:
        log.warning("PDF 다운로드 실패 %s: %s", url, e)
        return "error"
    if b"%PDF" not in bytes(data[:1024]):
        log.warning("PDF가 아닌 응답 %s", url)
        return "not_pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(bytes(data))
    os.replace(tmp, path)
    return "ok"


def _arxiv_by_title(title: str) -> str | None:
    with calllog.timed("arxiv", "query", title) as c:
        try:
            c.output = found = arxiv.find_by_title(title)
        except arxiv.ArxivError as e:
            log.warning("arXiv 제목 검색 실패 %s: %s", title, e)
            c.status, c.error = "error", str(e)[:300]
            return None
        c.status = "ok" if found else "error"
        return found


def fetch_pdf(paper: dict) -> Path:
    """오픈액세스 PDF → data/papers/pdf/{paper_id}.pdf. 받을 수 없으면 NoPdf.

    시도 순서: S2 openAccessPdf → S2 externalIds의 arXiv ID → (arXiv ID가 없으면) arXiv 제목 검색으로 찾은 ID.
    paper에 openAccessPdf와 externalIds가 이미 있으면 (③④ 검색 결과) S2를 부르지 않는다.
    출판사가 자동 요청을 막으면 우회하지 않는다.
    S2 조회(s2), arXiv 검색(arxiv), 다운로드 시도(pdf)는 호출 로그에 한 줄씩 남긴다. 실패는 status error.
    NoPdf.reason: S2 조회가 실패하고 다른 경로도 없으면 s2_failed, 시도할 주소가 없으면 no_url,
    아니면 첫 시도의 결과 (PDF가 아닌 페이지면 not_pdf, 그 밖은 download_failed).
    """
    path = PAPER_PDF_DIR / f"{_key(paper)}.pdf"
    if path.exists():
        return path
    info, s2_failed = paper, False
    if not set(PDF_FIELDS) <= paper.keys():
        with calllog.timed("s2", "paper", paper["paper_id"]) as c:
            found = _s2_pdf_info(paper["paper_id"])
            c.output = found
            c.status = "ok" if found else "error"
        s2_failed = found is None
        info = {**paper, **(found or {})}
    urls = _pdf_urls(info)
    if not (info.get("externalIds") or {}).get("ArXiv") and paper.get("title"):
        arxiv_id = _arxiv_by_title(paper["title"])
        if arxiv_id:
            urls.append(ARXIV_PDF_URL.format(arxiv_id))
    attempts = []
    for url in urls:
        with calllog.timed("pdf", "download", url) as c:
            c.output = result = _download(url, path)
            c.status = "ok" if result == "ok" else "error"
        if result == "ok":
            return path
        attempts.append({"url": url, "result": result})
    if not attempts:
        raise NoPdf("s2_failed" if s2_failed else "no_url")
    raise NoPdf("not_pdf" if attempts[0]["result"] == "not_pdf" else "download_failed", attempts)


def _page(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v if isinstance(v, int) and v > 0 else None


def _str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _untag(v) -> str:
    """Studio 추출값에 딸려 온 파싱 HTML 줄바꿈(<br>, <br/>)을 공백으로. 원문 쪽은 plain_text가 같은 처리를 한다."""
    return re.sub(r"\s*<br\s*/?>\s*", " ", _str(v)).strip()


def _studio_fields(final: dict) -> dict[str, CardField]:
    """추출 단계 JSON → 필드. 중첩({problem: {text, quote, page}})과 평면(problem_text ...)을 모두 읽는다."""
    if len(final) == 1 and isinstance(next(iter(final.values())), dict) \
            and not any(n in final or f"{n}_text" in final for n in FIELDS):
        final = next(iter(final.values()))  # {"result": {...}} 처럼 한 번 감싼 경우
    out = {}
    for n in FIELDS:
        v = final.get(n)
        if isinstance(v, dict):
            text, quote, page = v.get("text"), v.get("quote"), v.get("page")
        else:
            text, quote, page = final.get(f"{n}_text", v), final.get(f"{n}_quote"), final.get(f"{n}_page")
        out[n] = CardField(text=_untag(text), quote=_untag(quote), page=_page(page))
    return out


def fulltext_card(paper: dict, should_cancel: Callable[[], bool] | None = None) -> dict:
    """논문 PDF → 본문 카드 (Studio 에이전트 B, 캐시). 같은 논문은 다시 보내지 않는다.

    반환: 카드.
    예외: NoPdf (PDF를 받을 수 없음. reason에 이유),
          studio.StudioUnavailable (STUDIO_CARD_AGENT_ID 없음 등. PDF를 받기 전에 확인),
          studio.StudioCancelled, studio.StudioError (job 실패, 시간 초과, 파싱 결과 없음).
    NoPdf나 StudioError면 호출하는 쪽에서 abstract_card로 대체한다.
    """
    key = _key(paper)
    path = STUDIO_CARD_DIR / f"{key}.card.json"
    with _lock("fulltext:" + key):
        if path.exists():
            return json.loads(path.read_text("utf-8"))["card"]
        agent = studio.agent_id()
        pdf = fetch_pdf(paper)
        result = studio.run_agent(pdf, agent=agent, should_cancel=should_cancel)
        _write_json(STUDIO_CARD_DIR / f"{key}.raw.json", result["raw"])  # 응답 형식 확인용 원본
        parse_text = result["parse_text"]
        if not parse_text.strip():
            raise studio.StudioError("파싱 단계 텍스트가 없어 인용을 대조할 수 없습니다. 에이전트에 파싱 단계가 있는지 확인하세요 (설계서 4.6)")
        final = result["final"]
        card = _verified(paper, paper.get("title") or _str(final.get("title")), "fulltext",
                         _studio_fields(final), parse_text)
        _write_json(path, {"card": card, "parse_text": parse_text, "job_id": result["job_id"],
                           "agent_id": result["agent_id"], "steps": [s["step"] for s in result["steps"]],
                           "elapsed_sec": result["elapsed_sec"],
                           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return card
