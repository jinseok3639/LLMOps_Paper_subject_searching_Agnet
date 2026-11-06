"""기준선 비교 실험의 공용 경로, 입력, 스키마, 실행기 위치."""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUTS = HERE / "inputs"
PROMPTS = HERE / "prompts"
RUNS = HERE / "runs"

CONDITIONS = ("A", "B", "C", "D")
# 주 비교는 A vs B. C(Opus)와 D(Sonnet)는 참고용 기준점이다.
PAIRS = (("A", "B"), ("B", "C"), ("A", "C"), ("B", "D"), ("A", "D"))
LABELS = {"A": "Solar 단일 호출", "B": "Solar + 워크플로우", "C": "Claude Opus 5.5 단일 호출", "D": "Claude Sonnet 5.5 단일 호출"}
FIELDS = ("title", "summary", "gap", "approach", "evaluation", "support")
CRITERIA = ("grounding", "gap", "specificity", "coherence", "value")
SET_CRITERIA = ("best", "worst", "diversity", "overall")
N_CANDIDATES = 3

SOLAR_TEMPERATURE = 0.4
B_SEED = 20261103  # src/research.py와 같은 시드
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "high"
# 조건별 Claude 모델과 effort. 프롬프트와 호출 방식은 같다.
CLAUDE = {"C": (CLAUDE_MODEL, CLAUDE_EFFORT), "D": ("claude-sonnet-5-5", "high")}
JUDGE_MODEL = "gpt-6.1-sol"
JUDGE_EFFORT = "high"


def codex_bin():
    found = shutil.which("codex")
    if found:
        return found
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/OpenAI/Codex/bin/codex.exe"
    if path.exists():
        return str(path)
    raise FileNotFoundError("Codex CLI를 찾지 못했습니다")


def claude_bin():
    found = shutil.which("claude")
    if not found:
        raise FileNotFoundError("Claude CLI를 찾지 못했습니다")
    return found


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def run_dir(name):
    path = RUNS / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def samples(arg=""):
    names = sorted(p.stem for p in INPUTS.glob("t*.json"))
    if arg:
        wanted = arg.split(",")
        missing = set(wanted) - set(names)
        if missing:
            raise SystemExit(f"입력이 없는 샘플: {sorted(missing)}")
        names = wanted
    return names


def refs(sample):
    return read_json(INPUTS / f"{sample}.json")["refs"]


def single_call_prompt(context=""):
    text = (PROMPTS / "single_call.md").read_text("utf-8")
    return text.replace("{n}", str(N_CANDIDATES)).replace("{context}", context)


def proposals_schema():
    """Claude --json-schema용. src.multi_agent.Proposals와 같은 모양."""
    item = {"type": "object", "additionalProperties": False, "required": list(FIELDS),
            "properties": {f: {"type": "string"} for f in FIELDS if f != "support"} | {"support": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False, "required": ["hypotheses"],
            "properties": {"hypotheses": {"type": "array", "items": item}}}


def normalize(candidate):
    """판정에 넣을 후보. 고정 필드 순서, 조건을 드러내는 필드는 뺀다."""
    return {f: candidate[f] for f in FIELDS}


def _rationale_score():
    return {"type": "object", "additionalProperties": False, "required": ["rationale", "score"],
            "properties": {"rationale": {"type": "string"}, "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}}}


def judge_schema(kind):
    """Codex --output-schema용. 모든 필드 필수, 추가 필드 금지."""
    if kind == "score":
        return {"type": "object", "additionalProperties": False, "required": list(CRITERIA),
                "properties": {c: _rationale_score() for c in CRITERIA}}
    if kind == "novelty":
        close = {"type": "object", "additionalProperties": False, "required": ["title", "year", "url", "how_close"],
                 "properties": {"title": {"type": "string"}, "year": {"type": "integer"}, "url": {"type": "string"}, "how_close": {"type": "string"}}}
        return {"type": "object", "additionalProperties": False, "required": ["queries", "closest", "rationale", "score"],
                "properties": {"queries": {"type": "array", "items": {"type": "string"}}, "closest": {"type": "array", "items": close},
                               "rationale": {"type": "string"}, "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}}}
    if kind == "pairwise":
        ids = lambda p: {"type": "string", "enum": [f"{p}{i}" for i in range(1, N_CANDIDATES + 1)]}
        pick = {"type": "object", "additionalProperties": False, "required": ["X", "Y"], "properties": {"X": ids("x"), "Y": ids("y")}}
        verdict = {"type": "object", "additionalProperties": False, "required": ["rationale", "verdict"],
                   "properties": {"rationale": {"type": "string"}, "verdict": {"type": "string", "enum": ["X", "Y", "tie"]}}}
        return {"type": "object", "additionalProperties": False, "required": ["strongest", "weakest", *SET_CRITERIA],
                "properties": {"strongest": pick, "weakest": pick, **{c: verdict for c in SET_CRITERIA}}}
    raise ValueError(kind)


def valid_result(kind, data):
    """스키마의 필수 필드와 값 범위만 확인한다."""
    try:
        if kind == "score":
            return all(data[c]["score"] in range(1, 6) for c in CRITERIA)
        if kind == "novelty":
            return data["score"] in range(1, 6) and isinstance(data["queries"], list)
        if kind == "pairwise":
            return all(data[c]["verdict"] in ("X", "Y", "tie") for c in SET_CRITERIA)
    except (KeyError, TypeError):
        return False
    return False
