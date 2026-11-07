# 실험 절차

세 조건(A: Solar 단일 호출, B: Solar + 워크플로우, C: Claude 단일 호출)이 같은 참고문헌 세트로 후보 3개씩을 만들고, Codex(GPT)가 블라인드로 판정합니다. 시범 운영은 t003, t004, t005로 하고, 본 실험은 루프가 끝난 뒤 확인용 샘플로 같은 절차를 반복합니다.

## 전체 흐름

| 단계 | 스크립트 | 산출물 | 호출 |
|---|---|---|---|
| 0. 사전 점검 | `check.py` | `runs/<run>/meta.json` | 조건별 1회 |
| 1. 생성 | `generate.py` | `runs/<run>/gen/<샘플>_<조건>.json` | A 3, B 약 500, C 3 |
| 2. 블라인드 포장 | `pack.py` | `runs/<run>/jobs/<작업>/`, `runs/<run>/key.json` | 없음 |
| 3. 판정 | `judge.py` | `runs/<run>/jobs/<작업>/result.json` | Codex 72 |
| 4. 집계 | `aggregate.py` | `runs/<run>/report.md` | 없음 |
| 5. 사람 검증 | `human_sample.py` | `runs/<run>/human/` | 사람 9개 |

모든 단계는 다시 실행하면 끝난 부분을 건너뛰고 이어서 합니다.

## 0. 사전 점검

본 실행 전에 설정을 기록하고, 각 실행기가 원하는 대로 동작하는지 확인합니다.

- 기록: Solar 모델(`solar-pro2`), Claude CLI 버전과 모델, Codex CLI 버전, 심판 모델, effort, 실행 시각, git 커밋.
- Claude가 우리 system prompt를 받는지 확인합니다. 도구가 꺼져 있고, CLAUDE.md나 메모리가 들어가지 않아야 합니다. 입력 토큰이 2,000개 미만이면 Claude Code 기본 프롬프트가 교체된 것으로 봅니다.
- Codex를 검색 없이 한 번, 검색을 켜고 한 번 시험으로 돌려, 입력을 읽고 스키마대로 답하는지 봅니다.

## 1. 생성

참고문헌은 `inputs/<샘플>.json`의 `refs`이고, 세 조건 모두 `corpus_text()`로 같은 블록을 만듭니다. 연구자 컨텍스트는 빈 값입니다. 조건마다 샘플당 1회만 실행합니다.

| 조건 | 실행 방법 | 설정 |
|---|---|---|
| A | `src.llm.chat_json`, system은 `prompts/single_call.md`, user는 `References:\n<corpus>` | `solar-pro2`, temperature 0.4 |
| B | `src.multi_agent.run_sample(Method("src/generation_method", context=""), 샘플, refs, 20261103)`의 `chosen` 3개 | `method.json` 그대로 |
| C | `claude -p`, system은 `prompts/single_call.md`로 교체, user는 A와 같음 | 아래 참고 |

**C (Claude) 호출 방법.** API 키가 없으므로 구독 로그인 상태의 Claude Code CLI를 단일 호출로 씁니다. Claude Code의 에이전트 기능을 모두 꺼서 모델에 우리 프롬프트만 들어가게 합니다.

```bash
claude -p --model claude-opus-5-5 --effort high \
  --system-prompt-file <렌더링한 single_call.md> \
  --tools "" --strict-mcp-config --setting-sources "" \
  --no-session-persistence --output-format json \
  --json-schema <Proposals 스키마>
```

- 빈 임시 폴더에서 실행해 프로젝트 파일과 CLAUDE.md가 보이지 않게 합니다. user 메시지는 표준 입력으로 넣습니다.
- temperature는 CLI에서 정할 수 없어 기본값을 씁니다. A·B와 다른 점으로 기록합니다.
- `--system-prompt-file`로 교체해도 CLI가 짧은 문맥을 덧붙입니다. 사전 점검(2026-11-07)에서 확인한 내용은 Agent SDK 소개 두 문장, 작업 환경(임시 폴더 경로, OS, 날짜, 모델 이름), 사용자 계정 이메일, 구조화 출력용 `StructuredOutput` 도구입니다. 모두 합쳐 입력 약 1,500토큰이고 연구 내용과는 무관하지만, 순정 API 호출과 다른 점으로 기록합니다.

**형식 검사.** A와 C의 출력도 `problems()`로 검사합니다. 후보 수가 3개가 아니거나 검사에 실패하면, B와 같은 문구로 최대 4번까지 다시 요청합니다. 4번 모두 실패하면 그 샘플과 조건은 실패로 기록하고 다시 돌리지 않습니다.

**저장.** 후보 3개(`title`, `summary`, `gap`, `approach`, `evaluation`, `support`), 시도 횟수, 호출 수, 소요 시간, 원문 응답을 저장합니다.

## 2. 블라인드 포장

- 후보를 고정 필드 순서의 JSON으로 정규화합니다. 조건, 모델, 워크플로우에서 온 필드(`origin`, `slot`, 점수)는 뺍니다.
- 세트 안 후보 순서는 고정 시드로 섞고, id를 `x1~x3`, `y1~y3`(쌍대 비교) 또는 `c1`(후보별)로 바꿉니다.
- 작업 폴더 이름은 무작위 id(`j001` 등)입니다. 어떤 작업이 어떤 조건인지는 판정 폴더 밖의 `key.json`에만 남깁니다.
- 작업 폴더에는 그 판정에 필요한 파일만 둡니다: `instructions.md`, `references.txt`, 후보 파일, `schema.json`.
- 판정할 때는 지시문 뒤에 데이터 파일 내용을 붙여 프롬프트로 넣습니다. Codex의 읽기 전용 샌드박스가 임시 폴더 파일 읽기를 막았고, 파일을 읽게 하지 않는 편이 블라인드도 확실하기 때문입니다.

| 작업 | 프롬프트 | 폴더 내용 | 개수 |
|---|---|---|---|
| 후보별 점수 | `judge_score.md` | `references.txt`, `candidate.json` | 3조건 × 3후보 × 3샘플 = 27 |
| 외부 새로움 | `judge_novelty.md` | `references.txt`, `candidate.json` | 27 |
| 세트 쌍대 비교 | `judge_pairwise.md` | `references.txt`, `set_x.json`, `set_y.json` | 3쌍 × 2순서 × 3샘플 = 18 |

## 3. 판정 (Codex)

작업마다 새 세션으로 한 번씩 실행합니다. 작업 폴더를 실험 폴더 밖의 임시 폴더로 복사해 그곳에서 돌리고, 사용자 Codex 설정(`config.toml`, 규칙)은 읽지 않습니다.

```bash
codex [--search] exec -C <임시 폴더> --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules \
  -s read-only -m gpt-6.1-sol -c model_reasoning_effort="high" \
  --output-schema <임시 폴더>/schema.json -o <임시 폴더>/result.json -   # 프롬프트는 표준 입력
```

- 외부 새로움 작업에만 `--search`를 붙입니다. 나머지는 프롬프트에 넣은 데이터만 봅니다.
- 순서대로 하나씩 돌립니다. 사용량 한도에 걸리면 멈추고, 다시 실행하면 `result.json`이 없는 작업부터 이어갑니다.
- `result.json`이 스키마에 맞지 않으면 한 번 더 돌리고, 그래도 실패하면 `result_failed.json`에 남깁니다. 다음 실행 때 다시 시도합니다.

## 4. 집계

`key.json`으로 조건을 되돌린 뒤 계산합니다.

**후보별 점수.** 조건과 샘플별로 항목 평균, 세트 안 최고점과 최저점(best-of-3, worst-of-3)을 냅니다. 외부 새로움은 따로 표로 냅니다.

**쌍대 비교.** 같은 쌍의 두 순서 결과를 합칩니다.

| 두 순서의 결과 | 최종 |
|---|---|
| 같은 조건이 이김 | 그 조건 승 |
| 한쪽은 무승부, 다른 쪽은 한 조건 승 | 그 조건 승 |
| 서로 다른 조건이 이김 | 무승부 (위치 편향) |
| 둘 다 무승부 | 무승부 |

비교 쌍(A↔B, B↔C, A↔C)과 항목(최고, 최저, 다양성, 종합)별로 샘플 3개의 승, 무, 패를 표로 냅니다. 위치 편향으로 무승부가 된 비율도 함께 냅니다.

**읽는 법.**
- B > A: 워크플로우의 효과
- B ≥ C: 워크플로우를 쓴 Solar가 Claude 단일 호출과 같거나 낫다
- A ↔ C: 모델 차이

샘플이 3개라 통계적 유의성은 주장하지 않습니다. "3개 샘플 중 몇 개에서 우세"로 보고합니다.

## 5. 사람 검증

조건별 3개씩, 후보 9개를 무작위로 고릅니다. 판정 폴더와 같은 블라인드 형식으로 보고 `rubric.md`의 항목 1~5를 채점합니다. 심판 점수와 비교해 항목별 완전 일치율과 ±1점 일치율을 냅니다. 일치율이 낮은 항목은 결론에서 비중을 낮추고, 루브릭의 기준 문장을 고칩니다.

## 보고에 함께 적을 한계

- 판정은 GPT(Codex) 하나가 했습니다.
- 샘플 3개, 조건당 1회 실행입니다.
- 시범 운영 샘플(t003~t005)은 워크플로우 개선에 쓰인 샘플입니다.
- Claude는 CLI로 호출해 temperature를 맞추지 못했고, CLI가 덧붙이는 짧은 문맥이 있습니다.
- A·C 프롬프트는 서로 다른 후보 3개를 요구하지만, B의 최종 선별은 같은 주제를 거르지 않습니다.

## 단계별 판정 (사후 진단)

B가 어느 단계에서 품질을 얻고 잃는지 보기 위해 `stage_eval.py`로 중간 결과를 같은 점수 루브릭으로 채점합니다. 추가 생성은 없고, 저장된 `gen/<샘플>_B_trace.json`을 씁니다.

| 단계 | 비교 | 질문 |
|---|---|---|
| ① 제안 | 처음 제안 12개의 평균과 최고 | Solar의 생성 능력 자체가 한계인가 |
| ② 검토와 수정 | 같은 후보의 수정 전후 (항목별) | 수정이 후보를 낫게 만드는가 |
| ③ 토너먼트 | 경기 승패와 GPT 점수의 일치율, 순위 상관 | Solar 토너먼트가 품질을 가리는가 |
| ④ 최종 선별 | 최종 3개 vs GPT 점수 상위 3개 | 선별에서 얼마나 잃는가 |
| ⑤ 같은 주제 거르기 | 거르기 전 토너먼트 상위 3개 vs 최종 3개 | 다양성의 대가가 얼마인가 |

- 처음 제안과 수정본은 같은 형식이라 심판은 어느 단계인지 모릅니다. 작업 순서도 섞습니다.
- 점수는 단계별 판정 안에서만 비교합니다. 본 판정과는 심판 실행이 달라 절대값이 조금 다를 수 있습니다.
- 결과는 `runs/<run>/stage/stage_report.md`에 있습니다.

## 최종 측정 (2026-11-07 사전 고정)

시간 제약으로 개선은 v3 한 번(그 뒤 단계별 진단에서 찾은 투표 기준 변경 v4를 후보로 추가), 최종 측정은 한 번만 합니다. 아래 설계는 최종 결과를 보기 전에 정했고, 결과를 본 뒤 바꾸지 않습니다. 결과가 어떻게 나오든 방법론을 더 고치지 않고 그대로 보고합니다.

| 항목 | 정한 값 |
|---|---|
| 샘플 | 확인용 t006~t010 (5개). `inputs/`에 복사만 했고 내용은 열지 않음 |
| 조건 | A Solar 단일, B Solar + 워크플로우 v3 또는 v4(아래 규칙), C Claude Opus 5.5, D Claude Sonnet 5.5. 각 1회 생성 |
| B 방법론 규칙 | v4(v3 + 토너먼트 투표 기준 변경, 2026-11-07 추가)는 개발용 t003~t005의 평균 선별 손실이 v3(0.44)보다 작으면 쓰고, 아니면 v3. 확인용 샘플은 v3, v4 둘 다 판정 전에 생성해 두고, 결정은 개발용 결과로만 한다 |
| 주 가설 | B > A |
| 주 지표 | B vs A 세트 쌍대 비교의 종합(overall) 승수(5개 샘플 중), 후보 평균 점수 차이(B−A) |
| 보조 지표 | 세트당 쓸 만한 후보 수(항목 1~5 모두 3점 이상), 강한 후보 수(모두 4점 이상), best/worst-of-3, 다양성 승수, 공백 메움 비율 (B−A)/(C−A)·(B−A)/(D−A), 샘플당 호출 수와 시간 |
| 판정 | 후보별 점수 4조건 × 3후보 × 5샘플 = 60, 쌍대 비교 5쌍 × 2순서 × 5샘플 = 50 |
| 생략 | 외부 새로움(원 논문 제외 문제 미해결), 사람 검증 |
| 보고 방식 | "5개 샘플 중 몇 개에서 우세"로 보고하고 통계적 유의성은 주장하지 않음 |

**실행 (저장소 루트에서):**

생성 (Codex 없이, 2026-11-07 실행함):

```bash
PY=../LLMOps_Paper_subject_searching_Agnet/.venv/Scripts/python.exe
S=t006,t007,t008,t009,t010
$PY eval/baseline_comparison/check.py --run final --part gen
$PY eval/baseline_comparison/generate.py --run final --samples $S --conditions A,C,D
$PY eval/baseline_comparison/generate.py --run final_B_v4 --samples $S --conditions B --method eval/baseline_comparison/methods/v4
$PY eval/baseline_comparison/generate.py --run final_B_v3 --samples $S --conditions B --method eval/baseline_comparison/methods/v3
```

판정 (Codex). Codex 사용량과 시간이 제한되어 있어, 판정을 작은 묶음으로 나눠 중요한 것부터 하나씩 돌리고 사이사이 사용량을 확인합니다. 끝난 판정은 건너뛰므로 같은 묶음을 다시 실행하면 이어서 합니다. 각 묶음 뒤에 집계가 자동으로 갱신됩니다.

| 순서 | 묶음 | 판정 | 개수 | 답하는 질문 |
|---|---|---|---|---|
| 1~3 | `dev4-t003`, `dev4-t004`, `dev4-t005` | dev_v4 수정본 점수 | 12, 9, 12 | v4 투표 기준이 선별 손실을 줄였나 |
| 4 | `prep` | B 방법론 결정(`runs/final/b_method.json`), Codex 사전 점검, 포장 | 호출 2 | |
| 5 | `ab-pair` | A-B 쌍대 비교 | 8 | **주 지표 1** |
| 6, 7 | `a-score`, `b-score` | A, B 후보 점수 | 12, 15 | **주 지표 2**, 쓸 만한 후보 수 |
| 8, 9 | `c-score`, `d-score` | C, D 후보 점수 | 15, 15 | 공백 메움 비율 |
| 10, 11 | `bc-pair`, `bd-pair` | B-C, B-D 쌍대 비교 | 10, 10 | Claude와의 세트 비교 |
| 12 | `acd-pair` | A-C, A-D 쌍대 비교 | 16 | 선택: 모델 차이 |

```bash
$PY eval/baseline_comparison/run_final_judging.py
```

```bash
$PY eval/baseline_comparison/run_final_judging.py ab-pair
```

- 첫 명령은 묶음 목록과 진행 상황을 보여 주고, 두 번째처럼 묶음 이름을 주면 그 묶음만 판정합니다.
- 7번(`b-score`)까지 끝나면 주 가설의 답이 나옵니다. 보고서의 빈 칸은 판정하지 않은 부분입니다.
- 사후 진단(B의 단계별 판정)은 `stage_eval.py pack --run final` 뒤 `judge.py --run final/stage --kinds score --stages refined --samples t006`처럼 샘플별로 나눠 돌립니다. 본 지표와 별개이고, 결과를 보고 방법론을 고치지 않습니다.

- B는 참고문헌이 많은 샘플(60~70편)에서 샘플당 10~15분 걸릴 수 있으므로 백그라운드로 돌립니다. 끊기면 같은 명령으로 이어 갑니다.
- 생성이 실패하면 `--retry-failed`로 한 번 다시 돌리고, 그래도 실패하면 실패로 보고합니다.
- 판정은 약 40분 걸립니다. 사용량 한도에 걸리면 멈추고, 같은 명령으로 이어 갑니다.

**함께 보고할 한계:** GPT 심판 하나, 샘플 5개, 조건당 생성 1회, 사람 검증과 외부 새로움 판정 없음, 검토·수정 지시는 B에만 있고 단일 호출 프롬프트에는 없음, Claude는 CLI로 호출해 temperature를 맞추지 못함, v1→v3 개선은 개발용 샘플 t003~t005에서만 확인함.
