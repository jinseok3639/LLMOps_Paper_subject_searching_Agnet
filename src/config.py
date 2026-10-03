"""경로, 모델, 제안 논문 목록."""
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent  # 작업폴더
load_dotenv(ROOT_DIR / ".env")

MODEL = "solar-pro2"
UPSTAGE_BASE_URL = "https://api.upstage.ai/v1"
EMBED_PASSAGE_MODEL = "embedding-passage"  # 문서(단위 OpenProblems) 임베딩
EMBED_QUERY_MODEL = "embedding-query"      # 검색 질의 임베딩

MOCK_DIR = ROOT_DIR / "mock"
PROPOSAL_DIR = MOCK_DIR / "proposal_paper"
PDF_DIR = PROPOSAL_DIR / "1_pdf"                                    # {paper_id}.pdf
PARSED_DIR = PROPOSAL_DIR / "2_parsed"                              # {paper_id}.json
SECTION_OP_DIR = PROPOSAL_DIR / "3_section_open_problems"           # {paper_id}/{섹션}.json
UNIT_OP_DIR = PROPOSAL_DIR / "4_unit_open_problems"                 # {paper_id}/{섹션}.json
OPEN_PROBLEMS_PATH = UNIT_OP_DIR / "all.json"                       # 단위 OpenProblems 통합본 (검증 통과만)
EMBEDDINGS_PATH = UNIT_OP_DIR / "embeddings.npy"                    # all.json 순서의 임베딩 (정규화됨)
EMBEDDINGS_META_PATH = UNIT_OP_DIR / "embeddings.json"              # {model, generated_at, ids, text_hashes}

# 추천 (P3)
RETRIEVE_K = 30          # 임베딩 검색 후보 수
MIN_SCORE = 0.40         # 최고 유사도가 이보다 낮으면 "관련 항목 없음". 임시값 (보정은 추후 과제, plan/실행 계획.md 5절)
MAX_RECOMMENDATIONS = 5
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# 제안 논문 5편. paper_id = mock 파일명 ({arxiv}v{version})
PROPOSALS = {
    "2109.13916v5": {"title": "Unsolved Problems in ML Safety", "year": 2021},
    "2609.03178v1": {"title": "Open Problems in AI Risk Modeling: Insights from a Workshop on the Technical Foundations of AI Risk Modeling", "year": 2026},
    "2607.05163v1": {"title": "Open Problems in AI Incident Governance", "year": 2026},
    "2607.09756v1": {"title": "LLM-Centric Agentic AI for UAV Swarms: Architecture, Enabling Technologies, and Open Problems", "year": 2026},
    "2606.30116v1": {"title": "Open Problems in Constitutional Preference Reconstruction", "year": 2026},
}
for _pid, _p in PROPOSALS.items():
    _p["url"] = f"https://arxiv.org/abs/{_pid}"
