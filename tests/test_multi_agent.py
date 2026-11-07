"""후보 생성 호출별로 넘기는 참고문헌 범위 테스트. LLM은 가짜로 바꾼다."""
import json
import re
import threading
import unittest

from src import config, multi_agent as ma

REFS = [{"id": f"ref-{i:03}", "title": f"Title {i}", "abstract": f"Abstract {i}"} for i in range(1, 7)]
ALL = {r["id"] for r in REFS}
# 에이전트마다 다른 논문을 인용하게 한다.
SUPPORT = {"assumption": ["ref-001"], "transfer": ["ref-002", "ref-003"], "empirical": ["ref-004"], "formal": ["ref-001", "ref-005"]}


def ref_ids(text):
    return set(re.findall(r"^\[(ref-\d+)\]", text, re.M))


def strip_persona(text):
    """user 메시지 끝에 붙은 관점을 뗀다."""
    return text.split(PERSONA, 1)[0]


PERSONA = "\n\nYour research perspective: "


def hypothesis(support):
    return ma.Hypothesis(title="Proposal", summary=" ".join(["proposal"] * 65), gap="Gap", approach="Method", evaluation="Baseline", support=support)


class CorpusScopeTests(unittest.TestCase):
    def setUp(self):
        self.method = ma.Method(config.ROOT_DIR / "src/generation_method", "context")
        self.calls, lock = [], threading.Lock()

        def call(system, user, schema, temperature=None):
            with lock:
                self.calls.append((schema, system, user))
            if schema is ma.Proposals:
                agent = next(a for a, persona in self.method.agents.items() if user.endswith(persona))
                return ma.Proposals(hypotheses=[hypothesis(SUPPORT[agent]) for _ in range(3)])
            if schema is ma.Review:
                return ma.Review(unsupported="", vague="", already_done="", suggestion="Clarify")
            if schema is ma.Hypothesis:
                text = user.split("Your hypothesis:\n", 1)[1].split("\n\nReviews:", 1)[0]
                return ma.Hypothesis(**json.loads(text))
            if schema is ma.Ballot:
                return ma.Ballot(winner="A", rationale="Grounded")
            self.fail("Unexpected schema: " + schema.__name__)
        self.method.call = call
        self.result = ma.run_sample(self.method, "test", REFS, 1)

    def of(self, schema):
        return [(s, u) for k, s, u in self.calls if k is schema]

    def test_propose_and_refine_get_full_corpus(self):
        self.assertEqual(len(self.of(ma.Proposals)), 4)
        self.assertEqual(len(self.of(ma.Hypothesis)), 12)
        for schema in (ma.Proposals, ma.Hypothesis):
            for _, user in self.of(schema):
                self.assertEqual(ref_ids(user), ALL)

    def test_review_gets_only_cited_refs(self):
        reviews = self.of(ma.Review)
        self.assertEqual(len(reviews), 36)
        for _, user in reviews:
            refs, text = strip_persona(user).split("\n\nHypothesis:\n", 1)
            self.assertEqual(ref_ids(refs), set(json.loads(text)["support"]))
        self.assertNotIn("ref-006", "".join(u for _, u in reviews))

    def test_ballot_gets_union_of_both_cited_refs(self):
        ballots = self.of(ma.Ballot)
        self.assertEqual(len(ballots), 72)
        for _, user in ballots:
            refs, rest = strip_persona(user).split("\n\nHypothesis A:\n", 1)
            a, b = rest.split("\n\nHypothesis B:\n", 1)
            self.assertEqual(ref_ids(refs), set(json.loads(a)["support"]) | set(json.loads(b)["support"]))
            self.assertLess(len(ref_ids(refs)), len(ALL))

    def test_shared_prefix_for_cache(self):
        """관점은 user 맨 뒤쪽에만 있다. 같은 단계의 호출은 system이 같고, 제안·수정은 참고문헌 전체가 관점보다 앞에 있다."""
        personas = list(self.method.agents.values())
        for schema in (ma.Proposals, ma.Review, ma.Hypothesis, ma.Ballot):
            calls = self.of(schema)
            self.assertEqual(len({s for s, _ in calls}), 1, schema.__name__)
            for system, user in calls:
                self.assertFalse(any(p in system for p in personas))
                self.assertTrue(any(p in user for p in personas))
        for schema in (ma.Proposals, ma.Hypothesis):
            heads = {u.split(PERSONA, 1)[0] for _, u in self.of(schema)}
            self.assertEqual(len(heads), 1, schema.__name__)  # 관점 앞까지가 모두 같다
            self.assertEqual(ref_ids(heads.pop()), ALL)

    def test_corpus_text_keeps_reference_order(self):
        text = ma.corpus_text(REFS, {"ref-003", "ref-001"})
        self.assertLess(text.index("[ref-001]"), text.index("[ref-003]"))
        self.assertEqual(ref_ids(text), {"ref-001", "ref-003"})


def short(support):
    """형식 검사에 걸리는 가설 (summary가 너무 짧다)."""
    return ma.Hypothesis(title="Short", summary="too short", gap="Gap", approach="Method", evaluation="Baseline", support=support)


class FormatFailureTests(unittest.TestCase):
    """에이전트 하나가 형식 검사에 끝내 실패해도 후보 생성 전체가 멈추지 않는다."""

    def setUp(self):
        self.method = ma.Method(config.ROOT_DIR / "src/generation_method", "")
        self.lock = threading.Lock()

    def fake(self, proposals, refine_ok=True):
        """proposals(agent, attempt) -> 가설 목록. attempt는 그 에이전트의 몇 번째 제안 호출인지(1부터)."""
        attempts, self.prompts = {}, []

        def call(system, user, schema, temperature=None):
            if schema is ma.Proposals:
                agent = next(a for a, p in self.method.agents.items() if strip_retry(user).endswith(p))
                with self.lock:
                    attempts[agent] = attempts.get(agent, 0) + 1
                    self.prompts.append((agent, user))
                return ma.Proposals(hypotheses=proposals(agent, attempts[agent]))
            if schema is ma.Review:
                return ma.Review(unsupported="", vague="", already_done="", suggestion="Clarify")
            if schema is ma.Hypothesis:
                if not refine_ok:
                    return short(["ref-001"])
                text = user.split("Your hypothesis:\n", 1)[1].split("\n\nReviews:", 1)[0]
                return ma.Hypothesis(**json.loads(text))
            if schema is ma.Ballot:
                return ma.Ballot(winner="A", rationale="Grounded")
            self.fail("Unexpected schema: " + schema.__name__)
        self.method.call = call
        return attempts

    def test_retry_keeps_slots_that_already_passed(self):
        """1회차에 2번만 실패, 2회차에는 1번이 실패하고 2번이 통과하면 3개 모두 남는다."""
        good = hypothesis(["ref-001"])

        def proposals(agent, attempt):
            if agent != "formal":
                return [good] * 3
            return [good, short(["ref-001"]), good] if attempt == 1 else [short(["ref-001"]), good, short(["ref-001"])]
        attempts = self.fake(proposals)
        kept, issues = ma.propose(self.method, "formal", "corpus", ALL)
        self.assertEqual(sorted(kept), [1, 2, 3])
        self.assertEqual(issues, [])
        self.assertEqual(attempts["formal"], 2)
        retry = self.prompts[-1][1]
        self.assertIn("hypothesis 2: summary has 2 words but must have", retry)
        self.assertNotIn("hypothesis 1", retry)

    def test_persistent_failure_drops_slot_instead_of_aborting(self):
        good = hypothesis(["ref-001"])
        attempts = self.fake(lambda agent, attempt: [good, short(["ref-001"]), good] if agent == "formal" else [good] * 3)
        result = ma.run_sample(self.method, "test", REFS, 1)
        self.assertEqual(attempts["formal"], self.method.config["max_format_tries"])
        self.assertEqual(len(result["candidates"]), 11)
        self.assertNotIn("s2-formal", {c["id"] for c in result["candidates"]})
        self.assertIn("hypothesis 2", result["propose_problems"]["formal"][0])
        self.assertEqual(len(result["chosen"]), 3)

    def test_refine_failure_falls_back_to_proposal(self):
        good = hypothesis(["ref-001"])
        self.fake(lambda agent, attempt: [good] * 3, refine_ok=False)
        result = ma.run_sample(self.method, "test", REFS, 1)
        for c in result["candidates"]:
            self.assertTrue(c["refine_failed"])
            self.assertEqual(c["refined"], c["hypothesis"])
        self.assertEqual(len(result["chosen"]), 3)

    def test_all_failed_raises(self):
        self.fake(lambda agent, attempt: [short(["ref-001"])] * 3)
        with self.assertRaises(ValueError):
            ma.run_sample(self.method, "test", REFS, 1)


def strip_retry(user):
    return user.split(ma.RETRY_NOTE, 1)[0]


if __name__ == "__main__":
    unittest.main()
