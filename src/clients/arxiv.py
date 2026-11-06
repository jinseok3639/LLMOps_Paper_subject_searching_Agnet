"""arXiv API 제목 검색: S2에 arXiv ID가 없는 논문의 프리프린트를 찾는다 (④ V6, cards.fetch_pdf).

- arXiv 이용 규칙에 맞춰 요청 사이에 3초를 둔다 (프로세스 안 대기열 하나).
- 제목을 정규화(소문자, 기호를 공백으로)해 완전히 같을 때만 ID를 돌려준다. 비슷한 다른 논문을 받지 않기 위해서다.
- 실패(연결 오류, 5xx, 형식 오류)는 ArxivError. 찾지 못하면 None.
"""
import logging
import re
import xml.etree.ElementTree as ET

import requests

from src.clients.ratequeue import RateQueue

API_URL = "https://export.arxiv.org/api/query"
INTERVAL = 3.0
TIMEOUT = 20
MAX_RESULTS = 5
TRIES = 2
ATOM = "{http://www.w3.org/2005/Atom}"
HEADERS = {"User-Agent": "research-topic-app"}

log = logging.getLogger(__name__)
_queue = RateQueue()


class ArxivError(RuntimeError):
    pass


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (title or "").lower()).split())


def _arxiv_id(entry_id: str) -> str:
    """http://arxiv.org/abs/2502.03721v1 → 2502.03721 (버전은 뗀다. 최신판을 받는다)."""
    return re.sub(r"v\d+$", "", entry_id.rstrip("/").split("/abs/")[-1])


def _entries(xml_text: str) -> list[tuple[str, str]]:
    """Atom 응답 → [(arXiv ID, 제목)]."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ArxivError(f"arXiv 응답 형식 오류: {e}") from e
    out = []
    for entry in root.iter(f"{ATOM}entry"):
        entry_id, title = entry.findtext(f"{ATOM}id") or "", entry.findtext(f"{ATOM}title") or ""
        if "/abs/" in entry_id:
            out.append((_arxiv_id(entry_id), title))
    return out


def find_by_title(title: str) -> str | None:
    """제목이 같은 arXiv 논문의 ID. 없으면 None."""
    target = normalize_title(title)
    if len(target.split()) < 3:  # 너무 짧은 제목은 다른 논문과 겹칠 수 있다
        return None
    params = {"search_query": f'ti:"{target}"', "max_results": MAX_RESULTS}
    detail = ""
    for attempt in range(TRIES):
        _queue.pass_through(INTERVAL)
        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as e:
            detail = str(e)[:200]
        else:
            if r.status_code == 200:
                for arxiv_id, found in _entries(r.text):
                    if normalize_title(found) == target:
                        return arxiv_id
                return None
            detail = f"{r.status_code} {r.text[:200]}"
            if r.status_code < 500 and r.status_code != 429:
                break
            _queue.pause(INTERVAL * (attempt + 2))
    raise ArxivError(f"arXiv 검색 실패: {detail}")
