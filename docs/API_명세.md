# API 명세 (초안)

Streamlit 화면을 HTML + JavaScript 화면과 FastAPI 서버로 나누기 위한 API 명세입니다.

- **작성일:** 2026-11-05
- **상태:** 초안. 지금까지 정한 것은 [9장 결정 기록](#9-결정-기록)에 있습니다.
- **근거 문서:** [요구사항.md](요구사항.md). 표의 `요구` 열은 요구사항 ID입니다.
- **범위:** ① 둘러보기~③ 주제 다듬기는 지금 코드가 있습니다. ④ 검증과 ⑤ 확정은 자리만 잡아 두었습니다.

## 1. 구조

```text
브라우저 (HTML + JS)
  ├─ 화면, 현재 상태, 입력 잠금 처리
  ├─ 비로그인: 세션을 sessionStorage에 저장
  └─ 로그인:   세션을 /api/sessions로 서버에 저장
        │
        │ JSON 요청 / SSE 응답 (같은 출처라 CORS 없음)
        ▼
FastAPI
  ├─ /api/me        로그인 사용자 확인 (Supabase Auth 토큰 검증)
  ├─ /api/sessions  로그인 사용자의 세션 저장 (Supabase. 설정이 없으면 DB/users/ 폴더)
  ├─ /api/recommend, /api/research, /api/chat   작업 (src/ 함수 호출)
  ├─ /api/runs/*    작업 결과 조회
  └─ /              정적 파일 (index.html, app.js)
```

### 원칙

1. **작업 API는 대화 상태를 기억하지 않습니다.** 브라우저가 요청할 때마다 프로필과 대화를 보냅니다. 그래서 비로그인 사용자와 로그인 사용자가 같은 API를 씁니다.
2. **작업 결과는 서버에 `run_id`로 저장합니다.** 추천 결과와 후보 생성 결과는 참고문헌과 추적 정보가 붙어 수십 KB가 됩니다. 매번 주고받지 않도록 세션에는 `run_id`만 넣습니다. 결과는 한 번 만들어지면 바뀌지 않습니다. 지금 `generate_research`도 `db/sessions/{run_id}.json`에 저장하고 있습니다.
3. **모든 작업 요청과 응답 이벤트에는 `session_id`가 붙습니다.** 브라우저는 응답의 `session_id`가 가리키는 세션에만 결과를 씁니다 (G-1).

## 2. 데이터 형식

### 2.1 프로필

```json
{
  "lab_name": "보안 신뢰 컴퓨팅 연구실",
  "lab_topics": "AI 보안, 네트워크·시스템 보안, 트래픽 분석",
  "interests": "LLM 에이전트 프롬프트 인젝션 방어"
}
```

| 필드 | 필수 | 설명 | 요구 |
|---|---|---|---|
| `lab_name` | ● | 연구실 정보 (이름) | P-1, P-5 |
| `lab_topics` | ● | 연구실 연구 주제 | P-1, P-5 |
| `interests` | | 사용자 관심사. 비어 있으면 연구실 연구 주제만으로 추천 | P-1, P-5 |

지금 코드의 `extra`(보유 자원·제약)는 P-2가 보류라서 뺐습니다. `lab_topics`는 새 필드라서 `src/recommend.py`의 프롬프트 입력에 추가해야 합니다.

### 2.2 세션

sessionStorage와 서버 DB가 **같은 형식**을 씁니다. 그래야 로그인할 때 그대로 옮길 수 있습니다 (A-5).

```json
{
  "id": "s_8f3a...",
  "title": "Backdoor Detection Algorithms",
  "created_at": "2026-11-05T10:00:00Z",
  "updated_at": "2026-11-05T10:12:00Z",
  "profile": { "lab_name": "...", "lab_topics": "...", "interests": "..." },
  "stage": "refine",
  "picks": {
    "recommend_run_id": "r_1a2b...",
    "problem_id": "2109.13916v5:5.1:3",
    "research_run_id": "r_9c8d...",
    "topic_id": "c3"
  },
  "messages": [
    { "id": "m_1", "role": "user", "content": "GPU는 2장뿐입니다", "status": "done", "created_at": "..." },
    { "id": "m_2", "role": "assistant", "content": "그 조건이면...", "status": "cancelled", "created_at": "..." }
  ],
  "rejections": [
    { "target": "problem", "target_id": "2109.13916v5:5.1:1", "reason": "정책 중심이라 제외" }
  ]
}
```

| 필드 | 설명 | 요구 |
|---|---|---|
| `profile` | 세션을 시작할 때 복사한 프로필. 나중에 계정 프로필을 고쳐도 바뀌지 않음 | P-4 |
| `stage` | `browse` · `explore` · `refine` · `verify` · `final` | F-1 |
| `picks` | 상태별로 고른 것. 다시 열 때 이 값과 `/api/runs`로 화면을 복원 | S-5 |
| `messages[].status` | `done` · `cancelled` · `error`. 취소된 부분 응답도 남김 | G-4 |
| `messages[].kind` | 결과 메시지의 종류: `profile` · `problems` · `problem` · `topics` · `verify` · `final`. 없으면 일반 대화. 화면은 `run_id`로 카드를 그리고, `content`는 LLM에 넘길 짧은 요약 | C-5 |
| `messages[].run_id` 등 | 결과 메시지가 가리키는 `run_id`. `problem_id`(고른 문제), `picked`(그 카드에서 고른 항목), `research_run_id`·`topic_id`(검증한 후보), `repost`(후보 카드를 다시 보인 메시지)가 붙을 수 있음. 서버 작업 API는 무시함 | C-5 |
| `v` | 세션 형식 버전. `2`는 결과가 대화 메시지로 들어간 형식. 없으면 화면이 열 때 `picks`로 결과 메시지를 만들어 넣음 | S-5 |
| `rejections` | 거절한 문제와 이유. 다음 추천 요청에 함께 보냄 | C-6 |

### 2.3 오류

모든 오류는 같은 형식입니다.

```json
{ "error": { "code": "validation_error", "message": "필수 항목을 입력해 주세요.", "retryable": false,
             "fields": ["lab_topics"] } }
```

| HTTP | `code` | 언제 | 요구 |
|---|---|---|---|
| 401 | `unauthorized` | 로그인이 필요한 API를 비로그인으로 호출 | A-1 |
| 404 | `not_found` | 없는 세션 또는 `run_id` | |
| 409 | `job_running` | 로그인 사용자가 같은 세션을 다른 탭에서 이미 작업 중. 화면은 "다른 탭에서 진행 중"을 표시 | G-2 |
| 409 | `version_conflict` | 로그인 사용자의 세션 저장 시 `updated_at`이 서버 값과 다름. 다른 탭에서 먼저 저장한 경우. 화면은 최신 세션을 다시 불러옴 | |
| 422 | `validation_error` | 필수 항목 누락. `fields`에 빈 항목 이름. 화면은 그 항목에 빨간 안내를 띄움 | P-5 |
| 429 | `rate_limited` | 하루 요청 수 초과 | O-5 |
| 502 | `upstream_failed` | Solar, Upstage, S2 호출 실패. `retryable: true` | F-6 |

## 3. 작업 API의 SSE 응답

`/api/recommend`, `/api/research`, `/api/chat`은 모두 `text/event-stream`으로 응답합니다. 이벤트 종류는 다섯 개입니다.

| 이벤트 | 데이터 | 용도 | 요구 |
|---|---|---|---|
| `progress` | `{session_id, step, message, current?, total?}` | 하위 단계 표시. 예: `"참고문헌 검색 (2/5)"` | F-4 |
| `delta` | `{session_id, text}` | 답변 스트리밍 조각 | C-3 |
| `result` | `{session_id, ...결과}` | 작업 성공. 마지막 이벤트 | |
| `error` | `{session_id, error: {...2.3 형식}}` | 작업 실패. 마지막 이벤트 | F-6 |
| `budget` | `{session_id, used, limit, scope}` | 호출 예산 초과로 멈춤. 마지막 이벤트 | O-2 |

예시:

```text
event: progress
data: {"session_id":"s_8f3a","step":"search","message":"참고문헌 검색: backdoor detection LLM agents","current":2,"total":5}

event: result
data: {"session_id":"s_8f3a","run_id":"r_9c8d", ...}
```

### 3.1 취소

브라우저는 `fetch`를 `AbortController`로 끊어서 취소합니다. 별도 취소 API는 없습니다.

| 상황 | 브라우저 | 서버 | 요구 |
|---|---|---|---|
| 다른 세션으로 이동 | 진행 중인 요청을 abort. 원래 세션에 받은 데까지 `status: "cancelled"`로 저장 | 연결이 끊기면 다음 단계에 들어가기 전에 멈춤. 호출 로그에 "취소" 기록 | G-3, G-4, O-1 |
| 세션 삭제, 로그아웃 | abort 후 결과를 버림 | 위와 같음 | G-7 |
| 취소 후 | `stage`는 바꾸지 않음 | 이미 받은 S2 검색 결과는 캐시에 남아 재시도에 재사용됨 | G-5, G-6 |

서버는 `src/`의 `progress` 콜백 안에서 연결이 끊겼는지 확인하고, 끊겼으면 예외를 던져 멈춥니다. LLM 호출 하나가 도는 도중에는 멈추지 못하므로, 취소는 **단계 사이에서** 적용됩니다.

## 4. 인증

Google 로그인 하나만 지원합니다 (A-4). 로그인 처리는 **Supabase Auth**가 맡습니다. 우리 서버에는 로그인·콜백 경로가 없습니다.

```text
1. 화면이 supabase-js의 signInWithOAuth({provider: "google"}) 호출
2. Google 로그인 → Supabase 콜백(https://<프로젝트>.supabase.co/auth/v1/callback) → 우리 페이지로 복귀
3. 브라우저가 Supabase 세션(access token)을 가짐
4. 우리 API를 부를 때 Authorization: Bearer <access token> 헤더를 붙임
5. 서버는 Supabase로 토큰을 확인해 user_id를 얻음. 헤더가 없으면 비로그인으로 처리
```

| 메서드 | 경로 | 설명 | 요구 |
|---|---|---|---|
| GET | `/api/config` | 화면이 쓸 공개 설정 `{supabase_url, supabase_anon_key}` | A-1 |
| GET | `/api/me` | `{user: {id, email, name} \| null}`. 화면이 처음 열릴 때 호출 | A-2 |
| DELETE | `/api/me` | 계정과 모든 데이터 삭제 | A-6 |

로그아웃(A-3)은 화면에서 supabase-js의 `signOut()`으로 처리합니다. `anon` 키는 공개해도 되는 키이고, `service_role` 키는 서버 환경 변수에만 둡니다 (N-1).

## 5. 프로필과 세션 (로그인 필요)

| 메서드 | 경로 | 요청 | 응답 | 요구 |
|---|---|---|---|---|
| GET | `/api/profile` | | 프로필 또는 `null` | P-3 |
| PUT | `/api/profile` | 프로필 | 저장된 프로필 | P-3 |
| GET | `/api/sessions` | | `[{id, title, stage, updated_at}]`, 최근 순 | S-4 |
| GET | `/api/sessions/{id}` | | 세션 전체 | S-5 |
| PUT | `/api/sessions/{id}` | `{session: 세션 전체, base_updated_at}` | 저장된 세션 (`updated_at`은 서버가 정함). 없으면 새로 만듦. `base_updated_at`이 서버 값과 다르면 409 `version_conflict` + `current` | S-2, S-4, S-6 |
| DELETE | `/api/sessions/{id}` | | 204 | S-7 |
| POST | `/api/sessions/import` | `{sessions: [세션, ...]}` | `{imported: [{old_id, new_id}]}` | A-5 |

- 세션 id는 화면이 만듭니다(`s_` + 16진수). 저장하기 전에도 작업 요청의 `session_id`로 써야 하기 때문입니다. 그래서 만들기도 `PUT`으로 하고, 처음 저장할 때는 `base_updated_at`을 `null`로 보냅니다.
- `base_updated_at`은 화면이 서버에서 마지막으로 받은 `updated_at`입니다. 409 응답의 `error.current`에 서버의 최신 세션이 있어 화면은 그것으로 바꿉니다.
- 세션을 지우면 그 세션의 작업 결과(`runs`)도 지웁니다. `import`는 세션이 가리키는 비로그인 결과도 계정으로 옮깁니다.
- 세션은 **통째로 저장**합니다(`PUT`). 메시지 하나를 추가할 때도 전체를 보냅니다. 세션 하나는 메시지 수십 개라 크지 않고, 부분 수정 API를 따로 만들지 않아도 됩니다.
- 다른 사용자의 세션 ID로 요청하면 403이 아니라 404로 응답합니다. 세션이 있는지 없는지도 알려 주지 않기 위해서입니다 (S-1).

## 6. 작업 API (비로그인도 사용)

### 6.1 `POST /api/recommend`: ① 둘러보기

```json
{ "session_id": "s_8f3a", "profile": { ... }, "rejections": [ ... ] }
```

`result` 이벤트 데이터는 지금 `recommend()`의 반환값에 `run_id`를 더한 것입니다.

```json
{
  "session_id": "s_8f3a", "run_id": "r_1a2b", "status": "ok",
  "intro": "...", "note": "...",
  "recommendations": [
    { "problem_id": "...", "title": "...", "summary": "...", "reason": "...", "connection": "...",
      "quote": "...", "page": 12, "section_title": "...",
      "paper": { "paper_id": "...", "title": "...", "year": 2021, "url": "..." } }
  ]
}
```

- `status`가 `no_match`면 `recommendations`가 비어 있고 `note`에 이유가 있습니다.
- 카드의 `quote`, `paper`는 LLM 출력이 아니라 DB 값입니다 (C-4).
- `trace`는 응답에서 빼고 서버에만 저장합니다. 화면에 필요하면 `/api/runs/{run_id}`로 받습니다.

### 6.2 `POST /api/research`: ② → ③ 후보 생성

```json
{ "session_id": "s_8f3a", "profile": { ... }, "problem_id": "...", "messages": [ ... ] }
```

`progress` 이벤트는 `step` 값으로 `queries` · `search` · `generate` · `present` 단계를 보냅니다. `result`는 지금 `generate_research()`의 반환값에서 `generation`(토너먼트 내부 기록)을 뺀 것입니다.

```json
{
  "session_id": "s_8f3a", "run_id": "r_9c8d", "status": "ok",
  "problem_id": "...", "problem_title": "...",
  "topics": [ { "id": "c3", "title": "...", "research_question": "...", "approach": "...", "...": "..." } ],
  "presentation": "...",
  "references": [ { "id": "ref-001", "title": "...", "year": 2025, "url": "..." } ],
  "novelty_checked": false
}
```

`references[].abstract`는 응답에서 빼고 `/api/runs/{run_id}`에만 둡니다.

### 6.3 `POST /api/chat`: 질의응답

```json
{
  "session_id": "s_8f3a",
  "profile": { ... },
  "picks": { "recommend_run_id": "r_1a2b", "research_run_id": "r_9c8d", "topic_id": "c3" },
  "messages": [ ... ],
  "message": "GPU 2장으로 가능한 후보만 남겨 줘"
}
```

`delta` 이벤트로 답변을 보내고, 마지막 `result`는 `{session_id, content}`입니다. 서버는 `picks`의 `run_id`로 추천과 후보를 불러와 컨텍스트를 만듭니다. 지금 `reply_stream()`이 받는 `result`, `topic_result`를 서버가 직접 읽는 방식입니다.

### 6.4 `POST /api/verify`, `POST /api/finalize`: ④ 검증, ⑤ 확정

자리만 잡아 둡니다. 형식은 6.2와 같은 SSE이고, 입력은 `session_id`, `profile`, `research_run_id`, `topic_id`입니다. 검증이 도는 동안 화면은 입력을 잠급니다 (F-5).

두 API 모두 `revision_run_id`(6.4.1)를 더 받습니다. 있으면 고친 후보로 검증하거나 카드를 만듭니다. ④ 결과에는 `revision_run_id`와 `topic_version`이 남습니다. ⑤는 검증한 버전과 확정하는 버전이 달라도 막지 않고, 카드에 판정이 몇 번째 버전 기준인지 적습니다.

### 6.4.1 `POST /api/revise`: ③·④ 후보 고치기 (이슈 #12)

대화로 고른 후보의 수정안을 만듭니다. 형식은 6.2와 같은 SSE입니다.

- **입력:** `session_id`, `profile`, `research_run_id`, `topic_id`, `revision_run_id`(지금 버전, 비면 원본 v1), `verify_run_id`(있으면 검증 결과를 근거로 함께 넘김), `messages`
- **결과:** `{run_id, version, base_version, base_run_id, changes: [{field, before, after}], topic, reason, from_verify}`
  - `changes`는 바뀐 필드(`title`, `summary`, `gap`, `approach`, `evaluation`)만 담습니다.
  - 바뀐 게 없으면 저장하지 않고 `run_id: null`을 돌려줍니다.
- **저장:** 수정안은 `kind: "revision"` 결과로 따로 저장하고, 원본 후보는 바꾸지 않습니다. 화면이 [적용]을 누르면 `picks.revision_run_id`가 이 결과를 가리키며, 서버에는 따로 적용 단계가 없습니다.

### 6.5 결과 조회와 피드백

| 메서드 | 경로 | 설명 | 요구 |
|---|---|---|---|
| GET | `/api/runs/{run_id}` | 저장된 작업 결과 전체 (trace, 초록 포함). 세션을 다시 열 때 사용 | S-5 |
| POST | `/api/feedback` | `{session_id, target, target_id, value: "up" \| "down", comment?}` | O-3 |

`run_id`는 추측할 수 없는 무작위 값입니다. 결과는 공개 논문 정보와 생성 결과라서, 비로그인 사용자도 `run_id`만 알면 조회할 수 있게 둡니다.

**결과 보관 기간**

| 사용자 | 브라우저 | 서버의 `run_id` 결과 |
|---|---|---|
| 비로그인 | 탭을 닫으면 세션 스토리지와 함께 사라짐 | 서버는 탭이 닫힌 걸 알 수 없으므로, **만든 지 24시간이 지나면 삭제** |
| 로그인 | DB에 세션 유지 | 그 결과를 가리키는 세션이 있는 동안 유지. 세션을 삭제하면 함께 삭제 |

평가용 호출 로그(O-1)와 피드백(O-3)은 결과 파일과 따로 보관하며 위 기간과 상관없이 남깁니다.

## 7. 서버가 지킬 것

| 항목 | 내용 | 요구 |
|---|---|---|
| 작업 잠금 | **로그인 사용자만** `session_id`별로 진행 중인 작업을 메모리에 기록. 끝나거나 끊기면 해제. 비로그인은 세션이 탭 하나에만 있으므로 화면의 입력 잠금으로 충분 | G-2 |
| 결과 정리 | 하루 한 번 24시간 지난 비로그인 `run_id` 결과 삭제 | |
| 호출 로그 | 호출마다 `session_id`, 단계, 프롬프트 파일 이름과 해시, 입력, 출력, 지연 시간, 토큰 수, 결과(성공·실패·취소)를 기록 | O-1, O-4 |
| 호출 예산 | 요청마다 S2·LLM 호출 수를 세고 한도를 넘으면 `budget` 이벤트로 멈춤 | O-2 |
| 요청 제한 | 로그인 사용자는 계정, 비로그인은 IP 기준으로 하루 요청 수 제한 | O-5 |
| API 키 | Solar, Upstage, S2 키는 서버 환경 변수에만 둠 | N-1 |
| 동시 처리 | 작업 함수는 동기 함수이므로 스레드풀에서 실행. 동시 5명 기준 | N-3 |

## 8. 지금 코드에서 바꿀 것

| 파일 | 변경 |
|---|---|
| `app.py` | 삭제. `server.py`(FastAPI)와 `static/index.html`, `static/app.js`로 대체 |
| `src/recommend.py` | `lab_topics` 입력 추가, `extra` 제거, `rejections` 반영, 결과를 `run_id`로 저장 |
| `src/research.py` | `generation`을 응답에서 분리. `progress` 콜백에서 취소 확인 |
| `src/references.py` | 검색어마다 `progress`에 `current/total` 전달 |
| `src/chat.py` | `result`, `topic_result`를 `run_id`로 불러오도록 변경 |
| `requirements.txt` | `streamlit` 제거. `fastapi`, `uvicorn`, `authlib`, `itsdangerous` 추가 |

## 9. 결정 기록

| 날짜 | 항목 | 결정 |
|---|---|---|
| 2026-11-05 | 비로그인 저장소 | sessionStorage. 탭을 닫으면 사라지고 탭끼리 공유하지 않음 |
| 2026-11-05 | 필수 항목 | `lab_name`, `lab_topics` 필수. `interests` 선택 |
| 2026-11-05 | 비로그인 결과 보관 | 브라우저는 탭과 함께 삭제. 서버 결과 파일은 24시간 후 삭제 (6.5) |
| 2026-11-05 | 로그인·저장 | Supabase Auth로 Google 로그인. 사용자 데이터와 결과는 Supabase로 (설계서 6.3) |
| 2026-11-05 | 여러 탭 | 비로그인은 탭마다 독립이라 충돌 없음. 로그인 사용자만 `job_running`, `version_conflict`로 처리 |
| 2026-11-06 | 세션 만들기 | `POST /api/sessions` 대신 `PUT /api/sessions/{id}`(`base_updated_at: null`). id는 화면이 만듦 |
| 2026-11-06 | 로그인 사용자 결과 | `runs`를 Supabase에 저장. 비로그인 결과는 그대로 서버 디스크에 24시간 |
| 2026-11-06 | 화면 흐름 (이슈 #15) | 단계별 화면 대신 하나의 대화. 결과는 `kind`가 붙은 메시지로 쌓고, 앞 상태로 돌아가는 기능(F-3)은 없앰. 작업 API는 바꾸지 않음 |

### 알아 둘 점: 탭 복제

브라우저의 "탭 복제"는 sessionStorage를 **복사**해서 새 탭에 넘깁니다. 두 탭이 같은 `session_id`를 갖지만 이후로는 각자 따로 바뀝니다. 비로그인은 서버 잠금이 없으므로 둘 다 문제없이 동작하고, 서로의 결과가 섞이지도 않습니다 (결과는 각 탭의 sessionStorage에만 기록됨).
