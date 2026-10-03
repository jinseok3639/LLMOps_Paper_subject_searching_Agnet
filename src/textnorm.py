"""PDF 추출 텍스트와 LLM이 인용한 문장을 비교하기 위한 정규화.

Document Parse 텍스트에는 줄바꿈 하이픈("oth- ers", "robust-\nness"), 합자(ﬁ), 줄바꿈이 섞여 있어
그대로는 부분 문자열 매칭이 안 된다. 양쪽에 같은 정규화를 적용한 뒤 비교한다.
"""
import re
import unicodedata

# 큰따옴표/작은따옴표는 같은 문자로 본다 (LLM이 인용문 안의 "..."를 '...'로 바꿔 쓰는 경우가 잦음)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": "'", "”": "'", '"': "'",
                         "–": "-", "—": "-", "−": "-"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).lower()
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)  # 줄끝 하이픈 분리
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def contains(source: str, quote: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalize(source)
