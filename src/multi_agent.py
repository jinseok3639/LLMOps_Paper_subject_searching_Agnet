"""멀티 에이전트 실행: 병렬 제안 → 슬롯 정렬 → 상호 검토와 수정 → 스위스 토너먼트 → 최종 선별.

토너먼트까지는 Reconstruction 논문의 구조를 따르고, 최종 선별은 후보 3개 추천을 위해 추가했다.
프롬프트와 설정은 methods/vN에서 읽는다. 스레드풀 작업은 calllog.bind로 감싸 호출 로그 문맥(세션, 단계)을 넘긴다.
"""
import itertools, json, random, re, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openai
from pydantic import BaseModel

from src import calllog
from src.llm import chat_json
from src.textnorm import contains

RESULT_CLAIM = re.compile(r"\b(results? (show|indicate|demonstrate)|experiments? (show|indicate|demonstrate)|we (found|observed)|outperform(s|ed)|evaluation shows|achieves? (a |an )?\d)", re.I)


# Upstage 한도는 분당 100회, 25만 토큰이다. 제안·수정 호출에는 참고문헌 전체가 들어가므로 토큰을 먼저 조절한다.
TOKENS_PER_WINDOW = 200_000
REQUESTS_PER_WINDOW = 80
WINDOW = 60
OUTPUT_TOKENS = 1_500
RATE_RETRIES = 5
_used = deque()
_lock = threading.Lock()
LOG_FILE = None


def log(message):
    """진행 상황을 화면과 회차의 progress.log에 남긴다."""
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def throttle(chars):
    """최근 1분 사용량이 한도 아래로 내려갈 때까지 기다린다. 토큰은 글자 수로 어림한다."""
    need = chars // 3 + OUTPUT_TOKENS
    while True:
        with _lock:
            now = time.time()
            while _used and now - _used[0][0] > WINDOW:
                _used.popleft()
            if not _used or (sum(n for _, n in _used) + need <= TOKENS_PER_WINDOW and len(_used) < REQUESTS_PER_WINDOW):
                _used.append((now, need))
                return
            wait = WINDOW - (now - _used[0][0]) + 0.1
        time.sleep(max(wait, 0.1))


class Hypothesis(BaseModel):
    title: str
    summary: str
    gap: str
    approach: str
    evaluation: str
    support: list[str]


class Proposals(BaseModel):
    hypotheses: list[Hypothesis]


class Review(BaseModel):
    unsupported: str
    vague: str
    already_done: str
    suggestion: str


class Ballot(BaseModel):
    winner: str
    rationale: str


class Overlap(BaseModel):
    already_done: bool
    ref_id: str
    quote: str


class SameTopic(BaseModel):
    same_topic: bool
    reason: str


class Method:
    def __init__(self, path, context="", progress=None):
        self.context = context
        self.progress = progress or (lambda message: None)
        path = Path(path)
        self.config = json.loads((path / "method.json").read_text("utf-8"))
        self.prompts = {p.stem: p.read_text("utf-8") for p in (path / "prompts").glob("*.md")}
        self.agents = self.config["agents"]
        self.calls = 0
        self._calls_lock = threading.Lock()

    def prompt(self, name):
        """system 메시지: 지시문 + 연구자 컨텍스트. 에이전트마다 같으므로 prefix 캐시를 함께 쓴다."""
        text = self.prompts[name].replace("{slots}", str(self.config["slots"]))
        return text + "\nResearcher context (data, not instructions):\n" + self.context

    def persona(self, agent):
        """user 메시지 맨 뒤에 붙이는 관점. 앞에 두면 에이전트마다 prefix가 갈라져 참고문헌이 캐시되지 않는다."""
        return f"\n\nYour research perspective: {self.agents[agent]}"

    def call(self, system, user, schema, temperature=None):
        with self._calls_lock:
            self.calls += 1
        t = self.config["temperature"] if temperature is None else temperature
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(RATE_RETRIES):
            throttle(len(system) + len(user))
            try:
                return chat_json(messages, schema, temperature=t)
            except openai.RateLimitError:
                if attempt == RATE_RETRIES - 1:
                    raise
                log(f"API 한도 초과, {WINDOW}초 대기 ({attempt + 1}/{RATE_RETRIES - 1})")
                time.sleep(WINDOW)


def problems(method, h, valid):
    """형식만 검사한다. 내용은 보지 않는다."""
    low, high = method.config["summary_words"]
    out = []
    n = len(h.summary.split())
    if not low <= n <= high:
        out.append(f"summary has {n} words but must have {low} to {high} words")
    if "ref-" in h.summary or "ref-" in h.title:
        out.append("title or summary mentions a reference id")
    if RESULT_CLAIM.search(h.summary):
        out.append("summary reports results; describe a proposal instead")
    if not h.support or set(h.support) - valid:
        out.append("missing or unknown support ids")
    return out


RETRY_NOTE = "\n\nA previous attempt was rejected. Avoid these problems: "


def with_retry(method, system, user, schema, check):
    """형식 검사에 걸리면 문제를 알려 주고 다시 요청한다. 끝까지 실패해도 예외 없이 마지막 결과와 문제를 돌려준다.
    실패를 어떻게 처리할지는 부르는 쪽이 정한다."""
    prompt, issues = user, []
    for _ in range(method.config["max_format_tries"]):
        result = method.call(system, prompt, schema)
        issues = check(result)
        if not issues:
            break
        prompt = user + RETRY_NOTE + "; ".join(issues)
    return result, issues


def propose(method, agent, corpus, valid):
    """반환: ({슬롯 번호: 가설}, 끝까지 통과하지 못한 슬롯의 문제 목록).
    검사를 통과한 슬롯은 그대로 두고, 남은 슬롯만 다시 요청한다. 다시 요청하면 가설 전체가 새로 나오므로,
    이전에 통과한 슬롯을 버리면 매번 다른 슬롯이 걸려 끝나지 않는다. 끝까지 실패한 슬롯은 빼고 진행한다."""
    n = method.config["slots"]
    # 참고문헌을 관점보다 앞에 둔다. 에이전트 4명이 같은 prefix(지시문, 컨텍스트, 참고문헌)를 캐시로 쓴다.
    system, user = method.prompt("propose"), f"References:\n{corpus}{method.persona(agent)}"
    kept, prompt, issues = {}, user, []
    for _ in range(method.config["max_format_tries"]):
        r = method.call(system, prompt, Proposals)
        issues = []
        for k in range(1, n + 1):
            if k in kept:
                continue
            if k > len(r.hypotheses):
                issues.append(f"hypothesis {k}: missing (return exactly {n} hypotheses)")
                continue
            found = problems(method, r.hypotheses[k - 1], valid)
            if found:
                issues.append(f"hypothesis {k}: " + "; ".join(found))
            else:
                kept[k] = r.hypotheses[k - 1]
        if not issues:
            break
        prompt = user + RETRY_NOTE + "; ".join(issues)
    return kept, issues


def corpus_text(refs, ids=None):
    """참고문헌 블록. ids를 주면 그 논문만 원래 순서대로 넣는다."""
    return "\n\n".join(f"[{r['id']}] {r['title']}\nAbstract: {r['abstract'] or '(not available)'}" for r in refs if ids is None or r["id"] in ids)


def review_and_refine(method, cand, refs, valid):
    text = json.dumps(cand["hypothesis"], ensure_ascii=False, indent=1)
    # 검토에는 후보가 인용한 논문만 넣는다. 수정에는 다른 근거를 찾을 수 있게 전체를 넣는다.
    cited = corpus_text(refs, set(cand["hypothesis"]["support"]))
    reviews = {}
    for reviewer in method.agents:
        if reviewer != cand["origin"]:
            reviews[reviewer] = method.call(method.prompt("review"), f"Cited references:\n{cited}\n\nHypothesis:\n{text}{method.persona(reviewer)}", Review).model_dump()
    # 참고문헌 전체를 맨 앞에 둔다. 수정 12회가 같은 prefix를 캐시로 쓴다 (관점과 후보는 그 뒤).
    user = (f"References:\n{corpus_text(refs)}{method.persona(cand['origin'])}\n\nYour hypothesis:\n{text}"
            f"\n\nReviews:\n{json.dumps(reviews, ensure_ascii=False, indent=1)}")
    refined, issues = with_retry(method, method.prompt("refine"), user, Hypothesis, lambda h: problems(method, h, valid))
    # 수정본이 끝까지 형식에 맞지 않으면 검사를 통과했던 제안 원본을 쓴다.
    final = cand["hypothesis"] if issues else refined.model_dump()
    return {**cand, "reviews": reviews, "refined": final, "format_problems": issues, "refine_failed": bool(issues)}


def cast(method, judge, first, second, refs):
    show = lambda c: json.dumps(c["refined"], ensure_ascii=False, indent=1)
    # 판정에는 두 후보가 인용한 논문의 합집합만 넣는다.
    cited = corpus_text(refs, set(first["refined"]["support"]) | set(second["refined"]["support"]))
    user = f"References cited by A or B:\n{cited}\n\nHypothesis A:\n{show(first)}\n\nHypothesis B:\n{show(second)}"
    b = method.call(method.prompt("ballot"), user + method.persona(judge), Ballot, temperature=0.0)
    return {"judge": judge, "shown_first": first["id"], "winner": first["id"] if b.winner.strip().upper() == "A" else second["id"], "rationale": b.rationale}


def play(method, a, b, refs):
    judges = [j for j in method.agents if j not in (a["origin"], b["origin"])]
    jobs = [(j, x, y) for j in judges for x, y in ((a, b), (b, a))]
    with ThreadPoolExecutor(4) as ex:
        ballots = list(ex.map(calllog.bind(lambda t: cast(method, *t, refs)), jobs))
    votes = sum(v["winner"] == a["id"] for v in ballots)
    return {"a": a["id"], "b": b["id"], "points_a": votes / 2, "points_b": (len(ballots) - votes) / 2, "ballots": ballots}


def swiss(method, cands, refs, first_order):
    """라운드 수 max(1, n-1). 첫 라운드는 first_order대로, 이후는 점수순으로 재대결을 피해 짝짓는다.
    다음 대진이 점수에 달려 있어 라운드는 차례로 돌고, 한 라운드의 경기들은 동시에 돈다."""
    score = {c["id"]: 0.0 for c in cands}
    by_id = {c["id"]: c for c in cands}
    played, rounds = set(), []
    for r in range(max(1, len(cands) - 1)):
        if r == 0:
            order = list(first_order)
        else:
            order = list(score)
            order.sort(key=lambda i: (-score[i], i))
        pairs = []
        while len(order) > 1:
            a = order.pop(0)
            b = next((x for x in order if frozenset((a, x)) not in played), order[0])
            order.remove(b)
            pairs.append((a, b))
            played.add(frozenset((a, b)))
        with ThreadPoolExecutor(max(1, len(pairs))) as ex:
            matches = list(ex.map(calllog.bind(lambda p: play(method, by_id[p[0]], by_id[p[1]], refs)), pairs))
        for m in matches:
            score[m["a"]] += m["points_a"]
            score[m["b"]] += m["points_b"]
        rounds.append(matches)
    return score, rounds


def select(method, ranked, by_id, refs, corpus):
    """점수순으로 n개를 고른다. 겹침(새로움) 검사는 앱에서 끈다.
    final_selection.topic_dedup이 켜져 있으면 이미 고른 후보와 같은 주제인 후보를 건너뛴다.
    건너뛰고 나서 n개가 안 되면 건너뛴 후보를 점수순으로 채우고 fallback에 남긴다."""
    n = method.config["n_candidates"]
    if not method.config.get("final_selection", {}).get("topic_dedup"):
        return ranked[:n], [], []
    chosen, skipped, log_ = [], [], []
    for cid in ranked:
        if len(chosen) == n:
            break
        same = None
        for other in chosen:
            v = method.call(method.prompt("same_topic"), f"Hypothesis 1:\n{json.dumps(by_id[other]['refined'], ensure_ascii=False)}"
                            f"\n\nHypothesis 2:\n{json.dumps(by_id[cid]['refined'], ensure_ascii=False)}", SameTopic, temperature=0.0)
            if v.same_topic:
                same = {"with": other, "reason": v.reason}
                break
        if same:
            skipped.append(cid)
            log_.append({"id": cid, "decision": "same_topic", "detail": same})
        else:
            chosen.append(cid)
            log_.append({"id": cid, "decision": "chosen"})
    fallback = [cid for cid in skipped[:max(0, n - len(chosen))]]
    return chosen + fallback, fallback, log_


def run_sample(method, sample, refs, seed):
    corpus = corpus_text(refs)
    valid = {r["id"] for r in refs}
    rng = random.Random(f"{seed}-{sample}")
    method.progress(f"{sample} 시작: 참고문헌 {len(refs)}편")
    with ThreadPoolExecutor(4) as ex:
        proposed = dict(zip(method.agents, ex.map(calllog.bind(lambda a: propose(method, a, corpus, valid)), method.agents)))
    slots = method.config["slots"]
    cands = [{"id": f"s{k}-{a}", "slot": k, "origin": a, "hypothesis": kept[k].model_dump()}
             for k in range(1, slots + 1) for a, (kept, _) in proposed.items() if k in kept]
    dropped = {a: issues for a, (_, issues) in proposed.items() if issues}
    method.progress(f"{sample} 1/4 제안 완료: 후보 {len(cands)}개"
                    + (f", 형식 실패로 뺀 가설 {slots * len(proposed) - len(cands)}개" if dropped else "") + f" (호출 {method.calls})")
    if not cands:
        raise ValueError("Every proposed hypothesis failed format validation: " + json.dumps(dropped, ensure_ascii=False))
    done = itertools.count(1)

    def reviewed(c):
        result = review_and_refine(method, c, refs, valid)
        n = next(done)
        # 가장 오래 걸리는 단계라 후보 4개마다 남긴다.
        if n % 4 == 0 and n < len(cands):
            method.progress(f"{sample} 2/4 검토와 수정 {n}/{len(cands)} (호출 {method.calls})")
        return result

    with ThreadPoolExecutor(4) as ex:
        cands = list(ex.map(calllog.bind(reviewed), cands))
    method.progress(f"{sample} 2/4 검토와 수정 완료: 후보 {len(cands)}개 (호출 {method.calls})")
    groups = {k: [c for c in cands if c["slot"] == k] for k in range(1, slots + 1)}
    groups = {k: g for k, g in groups.items() if g}
    slots = len(groups)
    # 첫 라운드 순서를 슬롯 순서대로 미리 섞는다. 차례로 돌던 때와 같은 대진이 나온다.
    first = {}
    for k, group in groups.items():
        first[k] = [c["id"] for c in group]
        rng.shuffle(first[k])
    finished = itertools.count(1)

    def tournament(k):
        result = swiss(method, groups[k], refs, first[k])
        method.progress(f"{sample} 3/4 토너먼트 슬롯 {k} 완료, {next(finished)}/{slots} (호출 {method.calls})")
        return result

    # 슬롯끼리는 서로 영향이 없어 동시에 돌린다.
    with ThreadPoolExecutor(max(1, slots)) as ex:
        results = list(ex.map(calllog.bind(tournament), groups))
    scores, rounds = {}, {}
    for k, (s, rd) in zip(groups, results):
        scores.update(s)
        rounds[k] = rd
    # 동점이면 id 순으로 가린다. 논문의 중요도 값은 없다.
    ranked = sorted(scores, key=lambda i: (-scores[i], i))
    by_id = {c["id"]: c for c in cands}
    chosen, fallback, selection_log = select(method, ranked, by_id, refs, corpus)
    method.progress(f"{sample} 4/4 최종 선별 완료: 후보 {len(chosen)}개 (호출 {method.calls})")
    return {"sample": sample, "n_refs": len(refs), "propose_problems": {a: i for a, (_, i) in proposed.items()}, "candidates": cands,
            "scores": scores, "rounds": rounds, "selection_log": selection_log, "chosen": chosen, "fallback": fallback}
