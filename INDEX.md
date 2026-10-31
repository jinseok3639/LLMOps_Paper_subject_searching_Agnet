# 레포지토리 인덱스

이 문서는 목표 구조와 각 코드의 위치를 안내합니다.

> **상태:** 아래 목표 구조는 설계안이며 아직 적용되지 않았습니다. 지금 동작하는 코드는 [현재 위치](#현재-위치)를 참고하세요.

## 시스템 구성

| 영역 | 역할 | 목표 위치 |
|---|---|---|
| 논문 DB 구축 | S2 검색 → PDF 다운로드 → 선별 → 파싱 → Open Problem 추출 → 임베딩 | `src/paper_db/` |
| 채팅 에이전트 | 사용자 정보 기반 RAG 추천, 후속 대화, S2 논문 탐색 | `src/agents/chat/` |
| 검증 에이전트 | 연구 현황과 S2 최신 검색으로 주제 중복 여부 판정 | `src/agents/verify/` |

## 목표 구조

```text
작업폴더/
├─ app.py                     # Streamlit 진입점 (화면만 담당)
├─ src/
│  ├─ core/                   # 설정, 공용 스키마, textnorm(인용 검증)
│  ├─ clients/                # 외부 API 호출만 담당
│  │  ├─ semantic_scholar.py  # 호출 제한, 재시도
│  │  └─ upstage.py           # Solar, Embedding, Parse, Extract
│  ├─ store/                  # 논문, 문제, 연구 현황, 벡터 읽기/쓰기
│  ├─ paper_db/
│  │  ├─ collect.py           # S2 검색, PDF 다운로드
│  │  ├─ select.py            # 제목·초록·목차로 선별
│  │  ├─ parse.py             # Document Parse
│  │  ├─ extract.py           # Open Problem 추출
│  │  ├─ index.py             # 임베딩 생성
│  │  ├─ pipeline.py          # 단계 연결, 논문별 진행 상태 관리
│  │  └─ prompts/
│  └─ agents/
│     ├─ tools/               # LLM에 노출하는 Tool
│     │  ├─ search_problems.py  # RAG 검색
│     │  ├─ search_papers.py    # S2 검색
│     │  └─ read_paper.py       # 다운로드 + 파싱
│     ├─ chat/
│     │  ├─ agent.py          # 추천, 후속 대화
│     │  └─ prompts/
│     └─ verify/
│        ├─ agent.py          # 중복 검증
│        ├─ judge.py          # solved / partial / not_addressed 판정
│        └─ prompts/
├─ eval/                      # 라벨 데이터, 회차별 지표 측정
├─ tests/
├─ data/                      # PDF, 파싱 결과, DB 파일
├─ mock/                      # 기존 목업 데이터 (참고용)
├─ mock_diagram/              # 목업 구조도
└─ plan/                      # 구현·실행 계획
```

## 설계 규칙

### 의존 방향

```text
app → agents → tools → paper_db, store, clients
                       paper_db → store, clients
모든 계층 → core
```

- `paper_db`는 `agents`를 import하지 않습니다.
- `chat`과 `verify`는 서로 import하지 않습니다. 공통 기능은 `agents/tools`로 내립니다.

### `clients`와 `agents/tools`의 구분

| 계층 | 담당 | 사용처 |
|---|---|---|
| `clients` | API 호출, 재시도, 호출 제한 | 파이프라인, Tool |
| `agents/tools` | LLM용 설명, 입력 형식, 결과 검증 | 에이전트 |

에이전트는 Tool이 돌려준 논문만 인용할 수 있습니다. 이 검증은 `agents/tools`에서 처리합니다.

### 점진 개선 루프

에이전트는 파이프라인을 직접 실행하지 않고, `store`를 통해 연결합니다.

1. 에이전트가 검증 결과, 조사 날짜, 다음 조사 대상을 `store`에 기록합니다.
2. `paper_db/pipeline.py`가 조사 대상을 읽고 호출 예산 안에서 처리합니다.
3. `eval/`에서 회차별 지표를 측정합니다.

## 현재 위치

| 현재 | 목표 | 메모 |
|---|---|---|
| `src/llm.py` | `src/clients/upstage.py` | |
| `mock/scripts/s2_client.py` | `src/clients/semantic_scholar.py` | |
| `mock/scripts/step2_download_pdf.py` | `src/paper_db/collect.py` | |
| `mock/scripts/common.py` | 해당 기능을 쓰는 모듈 | 내용을 보고 나눔 |
| `mock/scripts/step1_citations.py` | `src/paper_db/collect.py` | 후속 논문 수집 |
| `mock/scripts/open_problem_sections.py` | 이동 안 함 | 수동 지정 데이터, `mock/`에 유지 |
| `src/extract.py` | `src/paper_db/extract.py` | |
| `src/embed.py` | `src/paper_db/index.py` | |
| `src/textnorm.py` | `src/core/` | |
| `src/config.py` | `src/core/` | `PROPOSALS` 하드코딩은 `store` 조회로 교체 |
| `src/recommend.py`, `src/topic.py`, `src/chat.py` | `src/agents/chat/` | |
| `src/prompts/extract_open_problems.md` | `src/paper_db/prompts/` | |
| `src/prompts/recommend_*.md`, `topic_generate.md`, `chat_followup.md` | `src/agents/chat/prompts/` | |

## 구현 순서

11/7까지는 후속 논문 분석을 중심으로 구현합니다. 빈 폴더는 미리 만들지 않고, 코드가 생길 때 위 구조에 맞춰 추가합니다.

1. `src/clients/`
2. `src/store/`
3. `src/paper_db/`
4. `src/agents/verify/`, `eval/` 병행
5. `src/agents/chat/`: 파일만 먼저 옮기고 Tool 연결은 나중에 진행

## 관련 문서

- [구현 계획](plan/구현%20계획.md)
- [실행 계획](plan/실행%20계획.md)
- [시스템 개요](mock_diagram/01_system_overview.md)
- [오프라인 파이프라인](mock_diagram/02_offline_pipeline.md)
- [런타임 시퀀스](mock_diagram/03_runtime_sequence.md)
- [데이터 폴더](mock_diagram/04_data_folder.md)
