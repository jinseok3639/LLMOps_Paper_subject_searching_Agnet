# 후보 생성 기준선 비교

같은 참고문헌 세트를 네 조건에 넣고 최종 후보 3개를 만든 뒤, GPT 심판으로 비교합니다.

| 조건 | 생성 방식 | 프롬프트 |
|---|---|---|
| A | Solar Pro 2 단일 호출 | A·C 공통 프롬프트 |
| B | Solar Pro 2 + 후보 생성 워크플로우 (`src/multi_agent.py`, `src/generation_method`) | 워크플로우 그대로 |
| C | Claude Opus 5.5 단일 호출 (effort high) | A·C 공통 프롬프트 |
| D | Claude Sonnet 5.5 단일 호출 (effort high) | A·C 공통 프롬프트 |

- **주 비교는 A vs B**(같은 Solar에서 워크플로우의 효과)입니다. C, D는 참고용 기준점이며, 워크플로우가 A와 Claude의 차이를 얼마나 메우는지 보는 데 씁니다.
- 개선 루프: 개발용 샘플(t001~t005)로 워크플로우를 고치고, 확인용 샘플(t006~t010)로 마지막에 한 번 평가합니다.
- B의 방법론 폴더는 `generate.py --method`로 바꿉니다. 시험 중인 방법론은 `methods/vN`에 두고, 효과가 확인되면 `src/generation_method`로 옮깁니다.
- 판정은 GPT가 합니다. 세트 단위 쌍대 비교를 A/B, B/A 두 순서로 하고, 후보별 루브릭 점수를 따로 매깁니다.

## A·C 공통 프롬프트

`prompts/single_call.md`. 원칙은 워크플로우가 **무엇을** 만들려는지는 주고, **어떻게** 만드는지는 주지 않는 것입니다.

| 워크플로우의 요소 | 출처 | 공통 프롬프트 |
|---|---|---|
| 태스크 설정 (아직 쓰이지 않은 논문의 참고문헌에서 아이디어 제안) | `propose.md` | 그대로 |
| 후보 조건 (구체적, 검증 가능, 가장 가까운 문헌과 다른 점 하나) | `propose.md` | 그대로 |
| 필드와 summary 규칙 (60~120단어, 결과 보고 금지, id 언급 금지) | `propose.md`, `problems()` | 그대로 |
| 품질 기준 (근거, gap과 기여의 명확성, 구체성, 새로움 자체는 우대하지 않음) | `ballot.md` | 기준으로 명시 |
| 후보 수 | `method.json`의 `n_candidates` | 3개 |
| 연구자 관점 4개, 상호 검토, 수정, 스위스 토너먼트 | `method.json`, `review.md`, `refine.md`, `multi_agent.py` | 넣지 않음 |

"서로 다른 가설 3개" 요구는 둡니다. B의 최종 선별은 같은 주제를 거르지 않지만(`topic_dedup: false`), 사용자에게 보여줄 후보라면 서로 달라야 한다는 요구가 합리적이기 때문입니다. 그래서 다양성 항목에서 B가 불리할 수 있고, 결과에 이를 함께 적습니다.

호출 조건도 B에 맞춥니다.

- 참고문헌 블록은 `corpus_text()`로 만들어 user 메시지에 `References:\n...`로 넣습니다.
- 출력은 `problems()`로 같은 형식 검사를 하고, 실패하면 B와 같이 최대 4번까지 재시도합니다.
- 연구자 컨텍스트는 세 조건 모두 빈 값입니다.
- temperature는 B와 같은 0.4입니다. Claude 모델이 temperature를 받지 않으면 기본값을 쓰고 기록합니다.

## 방법론 버전

| 버전 | 위치 | 바꾼 것 | 근거 | 시험 회차 |
|---|---|---|---|---|
| v1 | `src/generation_method` | 제품 그대로 | - | `runs/pilot` |
| v2 | `methods/v2` | 검토가 gap 과장, 실험 설계 누락(데이터, 베이스라인, 지표, ablation), 평가와 주장의 불일치를 확인하고, 수정이 이를 반영 (`review.md`, `refine.md`만 변경) | 시범 운영에서 B가 C보다 구체성, gap, 일관성이 낮았고, 판정 근거가 주로 베이스라인·지표 누락과 근거 없는 "아무도 안 했다" 주장을 지적함 | `runs/dev_v2` |
| v3 | `methods/v3` | v2 + 최종 선별에서 같은 주제 거르기(`topic_dedup: true`, `same_topic.md`) + 수정 단계에서 approach에 핵심 기법(입출력, 목적 함수나 알고리즘, 가장 가까운 방법과 다른 점) 정의 | dev_v2에서 다양성 0/3 패배, 구체성 3.00 정체(판정 근거가 기법 정의 누락을 지적) | `runs/dev_v3`(생성 점검만), 최종 측정 `runs/final` |

최종 측정 설계는 `protocol.md`의 "최종 측정" 절에 결과를 보기 전에 고정했습니다.

v2는 워크플로우 단계(검토, 수정)의 지시만 바꿨고 A·C·D의 프롬프트는 그대로입니다. 그래서 v2의 효과에는 "검토 구조"와 "더 자세한 설계 요구"가 섞여 있습니다. 단일 호출에 같은 요구를 넣으면 A도 좋아질 수 있으므로, 결과를 쓸 때 이 점을 함께 적습니다.

## 판정

기준은 `rubric.md`에 있고, 심판 프롬프트는 그 영어판입니다. 심판은 Codex CLI(`codex exec`, ChatGPT 로그인)입니다. 실행 순서와 집계 규칙은 `protocol.md`에 있습니다.

| 판정 | 프롬프트 | 단위 | 검색 | 작업 수 (세트 3개) |
|---|---|---|---|---|
| 후보별 점수 | `prompts/judge_score.md` | 후보 1개, 항목 1~5 | 없음 | 4조건 × 3후보 × 3세트 = 36 |
| 외부 새로움 | `prompts/judge_novelty.md` | 후보 1개 | 웹 검색 | 36 (원 논문 제외 문제를 고치기 전까지 개발 회차에서는 생략) |
| 세트 쌍대 비교 | `prompts/judge_pairwise.md` | 세트 2개, 최고·최저·다양성·종합 | 없음 | 5쌍(A-B, B-C, A-C, B-D, A-D) × 2순서 × 3세트 = 30 |

- 후보는 조건 이름 없이 고정 필드 순서의 JSON으로 넣고, 세트 안의 id는 `x1~x3`, `y1~y3`로 바꿉니다.
- 쌍대 비교는 X/Y, Y/X 두 번 평가하고, 결과가 다르면 무승부로 처리합니다.
- 외부 새로움은 검색을 켜야 해서 따로 판정하고 따로 보고합니다.

## 실행

`protocol.md`의 단계대로 원본 폴더의 가상환경으로 실행합니다. 모두 다시 실행하면 끝난 부분을 건너뜁니다.

```bash
PY=../LLMOps_Paper_subject_searching_Agnet/.venv/Scripts/python.exe
$PY eval/baseline_comparison/check.py --run pilot
$PY eval/baseline_comparison/generate.py --run pilot --conditions A,C,B
$PY eval/baseline_comparison/pack.py --run pilot
$PY eval/baseline_comparison/judge.py --run pilot --workers 3
$PY eval/baseline_comparison/aggregate.py --run pilot
```

## 결과와 기록 (`runs/<run>/`)

| 파일 | 내용 |
|---|---|
| `report.md`, `report.json` | 조건별 점수, best/worst-of-3, 외부 새로움, 쌍대 비교 승·무·패 |
| `details.md` | 샘플, 조건, 후보별 내용과 심판 점수, 근거, 작업 번호. 결과가 이상할 때 여기서 시작합니다 |
| `meta.json` | 실행기 버전, 모델, effort, 사전 점검 결과 |
| `pipeline.log`, `progress.log` | 단계별 진행 기록 |
| `gen/<샘플>_<조건>.json` | 최종 후보, 형식 검사 시도별 원문 응답, 호출 수, 소요 시간, 실패 사유 |
| `gen/<샘플>_B_trace.json` | B의 제안, 검토, 수정, 토너먼트 점수 전체 |
| `calllog/` | Solar 호출 한 건씩의 입력과 출력 (git에 올리지 않음) |
| `key.json` | 판정 작업과 조건의 대응. 사람 검증이 끝나기 전에는 열지 않습니다 |
| `jobs/<번호>/` | 심판이 받은 지시문과 데이터, `codex_*.log`, `result.json`, 실패 시 `result_failed.json` |
| `human/` | 사람 검증 채점지와 점수 |
| `stage/` | B의 단계별 판정 (`stage_eval.py`). `stage_report.md`에 제안 → 수정 → 토너먼트 → 선별 단계별 점수와 손익 |

## 입력

`inputs/t00X.json`은 `evaluation_v2/recon/inputs`의 복사본입니다. 형식은 `run_sample`이 받는 `refs` 목록(`id`, `title`, `abstract`)과 같습니다.

| 샘플 | 슬롯 | 참고문헌 | 초록 없음 |
|---|---|---|---|
| t003 | 3. 응용 도메인 | 25 | 5 |
| t004 | 4. 키워드 불일치 | 20 | 2 |
| t005 | 5. 이론 | 24 | 0 |

## 한계

- t003~t005는 개선용 샘플이라 워크플로우가 이 샘플에 맞춰져 있을 수 있습니다. 이번 실행은 평가 파이프라인 시범 운영입니다.
- 본 실험은 루프가 끝난 뒤 확인용 샘플(슬롯 1, 3, 5)로 다시 돌립니다.
