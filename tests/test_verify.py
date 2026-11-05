"""④ 검증 테스트 (Solar, S2, 임베딩, 카드, Studio는 가짜). 실행: python -m unittest discover tests"""
import json
import math
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import cards, config, llm, studio, verify as vf

PROFILE = {"lab_name": "lab", "lab_topics": "AI security", "interests": "reward model robustness"}
PROBLEM = {"problem_id": "p1", "title": "Reward hacking"}
TOPIC = {"id": "c1", "title": "Master key robust reward models", "summary": "We propose adversarial augmentation.",
         "gap": "Reward models are fooled by master key tokens.", "approach": "Train on truncated adversarial outputs.",
         "evaluation": "Compare with standard reward models; measure attack success rate.", "support": ["ref-001"]}
QUERIES = ["master key reward attacks", "adversarial reward model augmentation", "robust reward model master key"]
STEPS = ["queries", "search", "narrow", "abstract_cards", "nearest", "fulltext", "compare", "critique"]
EMPTY = {"text": "", "quote": "", "page": None}


def card(pid, level="abstract", empty=()):
    tag = " (full)" if level == "fulltext" else ""
    fields = {f: dict(EMPTY) if f in empty else
              {"text": f"{f} text of {pid}", "quote": f"{pid} {f} quote sentence{tag}", "page": 3 if tag else None}
              for f in cards.FIELDS}
    return {"paper_id": pid, "title": f"Paper {pid}", "level": level, **fields, "quote_failed": []}


def comparison(c, **matches):
    """matches에 없는 요소는 different. quote는 카드 인용을 그대로 쓴다."""
    return vf.Comparison(**{f: vf.Element(match=matches.get(f, "different"), reason="이유", quote=c[f]["quote"])
                            for f in vf.ELEMENTS})


def near(pid, problem, method, assumptions="different", evaluation="different", level="fulltext", year=2024):
    m = dict(problem=problem, method=method, assumptions=assumptions, evaluation=evaluation)
    return {"paper_id": pid, "year": year, "card": {"level": level}, "comparison": {f: {"match": v} for f, v in m.items()}}


def row(pid, year=2024, title=None):
    return {"paperId": pid, "title": title or f"Paper {pid}", "abstract": f"Abstract of {pid}", "year": year,
            "url": f"https://s2/{pid}"}


CRITIQUE = vf.Critique(
    requirements=vf.Requirements(**{k: vf.Requirement(status="defined", note="명시") for k in vf.REQUIREMENTS[:5]},
                                 alternative_explanations=vf.Requirement(status="Not applicable", note="해당 없음"),
                                 failure_feasibility=vf.Requirement(status="maybe", note="?")),
    unsupported="", vague="절차가 빠짐", already_done="[N1] 이미 함", suggestion="최소 실험")


class JudgeTests(unittest.TestCase):
    def test_solved_needs_problem_and_method_same_in_one_paper(self):
        out = vf.judge([near("a", "same", "same"), near("b", "different", "different")])
        self.assertEqual((out["verdict"], out["basis"], out["flags"]), ("solved", ["a"], []))

    def test_combination_across_papers_is_not_solved(self):
        out = vf.judge([near("a", "same", "different"), near("b", "different", "same")])
        self.assertEqual((out["verdict"], out["basis"]), ("partial", ["a", "b"]))

    def test_partial_cases(self):
        for core in (("same", "partial"), ("partial", "partial"), ("different", "same"), ("same", "unknown")):
            self.assertEqual(vf.judge([near("a", *core)])["verdict"], "partial", core)

    def test_not_addressed_cases(self):
        for core in (("partial", "different"), ("different", "different"), ("unknown", "partial"), ("unknown", "unknown")):
            self.assertEqual(vf.judge([near("a", *core)])["verdict"], "not_addressed", core)
        self.assertEqual(vf.judge([near("a", "different", "partial", "same", "same")])["verdict"], "not_addressed")

    def test_flags(self):
        self.assertEqual(vf.judge([near("a", "same", "same"), near("b", "different", "different", level="abstract")])["flags"],
                         [vf.FLAG_ABSTRACT])
        self.assertEqual(vf.judge([near("a", "different", "unknown")])["flags"], [vf.FLAG_UNKNOWN])
        self.assertEqual(vf.judge([near("a", "different", "different", "unknown", "unknown")])["flags"], [])


class CheckTests(unittest.TestCase):
    def test_empty_card_field_is_unknown_even_if_model_says_same(self):
        c = card("x", empty=("method",))
        out = vf.check(c, comparison(c, problem="same", method="same"))
        self.assertEqual(out["method"], {"match": "unknown", "reason": "논문 카드에 이 요소가 없습니다.", "quote": "",
                                         "page": None, "level": "abstract", "check": "empty_card"})
        self.assertEqual((out["problem"]["match"], out["problem"]["check"]), ("same", "ok"))

    def test_quote_not_in_card_quote_is_downgraded(self):
        c = card("x", level="fulltext")
        out = comparison(c, problem="same", method="same")
        out.problem.quote = "An invented sentence."
        out.method.quote = c["method"]["text"]  # 카드 text(의역)는 원문 인용이 아니다
        checked = vf.check(c, out)
        for f in ("problem", "method"):
            self.assertEqual({k: checked[f][k] for k in ("match", "quote", "model_match", "check")},
                             {"match": "unknown", "quote": "", "model_match": "same", "check": "quote_failed"})

    def test_part_of_quote_with_quote_marks_and_period_passes(self):
        c = card("x", level="fulltext")
        out = comparison(c, problem="partial")
        out.problem.quote = '"x problem quote."'
        checked = vf.check(c, out)
        self.assertEqual({k: checked["problem"][k] for k in ("match", "quote", "page", "level", "check")},
                         {"match": "partial", "quote": "x problem quote.", "page": 3, "level": "fulltext", "check": "ok"})

    def test_invalid_match_and_failed_call(self):
        c = card("x", empty=("evaluation",))
        out = comparison(c, problem="Similar")
        self.assertEqual(vf.check(c, out)["problem"]["check"], "invalid_match")
        failed = vf.check(c, None)
        self.assertEqual({f: (e["match"], e["check"]) for f, e in failed.items()},
                         {"problem": ("unknown", "call_failed"), "method": ("unknown", "call_failed"),
                          "assumptions": ("unknown", "call_failed"), "evaluation": ("unknown", "empty_card")})


class RankTests(unittest.TestCase):
    def test_j4_order(self):
        papers = [near("d_side_new", "different", "different", "same", "same", year=2026),
                  near("b_pp", "partial", "partial"),
                  near("e_side_old", "different", "different", "same", "same", year=2020),
                  near("a_sp", "same", "partial"),
                  near("c_sd_side", "same", "different", "same"),
                  near("f_sd", "same", "different", year=2026)]
        order = [p["paper_id"] for p in sorted(papers, key=vf.rank_key, reverse=True)]
        self.assertEqual(order, ["a_sp", "c_sd_side", "f_sd", "b_pp", "d_side_new", "e_side_old"])


class Cancel(Exception):
    pass


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(cards, "ABSTRACT_CARD_DIR", root / "abstract_cards"),
                        patch.object(cards, "STUDIO_CARD_DIR", root / "studio")]
        for p in self.patches:
            p.start()
        self.specs, self.full_specs, self.compare_error, self.queries = {}, {}, set(), QUERIES

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def chat_json(self, messages, schema, temperature=0.0):
        self.schemas.append(schema.__name__)
        if schema is vf.Queries:
            return vf.Queries(queries=self.queries)
        if schema is vf.Critique:
            return CRITIQUE
        pid = re.search(r"Paper card: Paper (\S+)", messages[1]["content"]).group(1)
        level = "fulltext" if "(full)" in messages[1]["content"] else "abstract"
        if pid in self.compare_error:
            raise llm.LLMError("format")
        spec = (self.full_specs if level == "fulltext" else self.specs).get(pid, {})
        return comparison(card(pid, level), **spec)

    @staticmethod
    def embed(texts, model):
        """쿼리는 [1, 0]. 논문 pNN은 번호가 작을수록 쿼리와 가깝다."""
        if model == config.EMBED_QUERY_MODEL:
            return [[1.0, 0.0]]
        angles = [int(re.search(r"Abstract of \D*(\d+)", t).group(1)) * 0.05 for t in texts]
        return [[math.cos(a), math.sin(a)] for a in angles]

    def run_verify(self, refs, results, studio_ready=False, fulltext=None, limits=None, progress=None):
        self.schemas, self.events = [], []
        progress = progress or (lambda message, **kw: self.events.append((kw["step"], message, kw)))
        agent = {"return_value": "agt"} if studio_ready else {"side_effect": studio.StudioUnavailable("no agent")}
        with patch("src.verify.llm.chat_json", side_effect=self.chat_json), \
                patch("src.verify.llm.embed", side_effect=self.embed) as self.embed_mock, \
                patch("src.references._search", side_effect=lambda q: results.get(q, [])) as self.search, \
                patch("src.verify.cards.abstract_card", side_effect=lambda p: card(p["paper_id"])) as self.abstract, \
                patch("src.verify.studio.agent_id", **agent), \
                patch("src.verify.cards.fulltext_card", side_effect=fulltext or (lambda p, should_cancel=None: None)) as self.fulltext:
            return vf.verify(PROFILE, PROBLEM, TOPIC, refs, progress=progress, should_cancel=lambda: False, limits=limits)

    def steps(self):
        return [s for i, (s, _, _) in enumerate(self.events) if i == 0 or self.events[i - 1][0] != s]

    def big_inputs(self):
        """③ 참고문헌 4편 (초록 없음 1, 검색 결과와 id 중복 1, 제목 중복 1) + 검색 3회 (두 번째는 빈 결과라 재검색)."""
        refs = [{"id": "ref-001", "paper_id": "p01", "title": "Paper p01", "abstract": "Abstract of p01", "year": 2023, "url": "u"},
                {"id": "ref-002", "paper_id": "p02", "title": "No abstract", "abstract": "", "year": 2023, "url": "u"},
                {"id": "ref-003", "paper_id": "p03", "title": "Paper p03", "abstract": "Abstract of p03", "year": 2022, "url": "u"},
                {"id": "ref-004", "paper_id": "r04", "title": "Paper p04!", "abstract": "Abstract of p04", "year": 2022, "url": "u"}]
        results = {QUERIES[0]: [row(f"p{i:02}") for i in range(3, 13)],               # p03 중복
                   "adversarial reward": [row(f"p{i:02}") for i in range(4, 16)],      # p04는 r04와 제목 중복
                   QUERIES[2]: [row(f"p{i:02}") for i in range(16, 22)] + [{"paperId": "na", "title": "N", "abstract": None}]}
        return refs, results

    def test_studio_unavailable_keeps_abstract_cards(self):
        refs, results = self.big_inputs()
        self.specs = {"p02": {"problem": "same", "method": "same"},  # 초록이 없어 빠진 논문
                      "p05": {"problem": "same", "method": "same"}, "p20": {"problem": "same", "method": "same"},
                      "p07": {"problem": "partial", "method": "partial", "assumptions": "same"},
                      "r04": {"problem": "partial"}}
        result = self.run_verify(refs, results)
        json.dumps(result, ensure_ascii=False)
        self.assertEqual(self.steps(), STEPS)
        self.assertEqual([e[2].get("current") for e in self.events if e[0] == "search"], [1, 2, 3])
        self.assertEqual(result["status"], "ok")
        self.fulltext.assert_not_called()  # 에이전트가 없으면 PDF도 받지 않는다
        self.assertEqual(result["scope"]["searched"], 20)  # p01, p03, r04 + p05~p21
        self.assertEqual(result["scope"]["from_references"], 3)
        self.assertEqual((result["scope"]["narrowed"], result["scope"]["fulltext_read"]), (15, 0))
        self.assertEqual(result["trace"]["search"][1]["retried_query"], "adversarial reward")
        compared = {c["paper_id"] for c in result["trace"]["compared"]}
        self.assertEqual(compared, {"p01", "p03", "r04", *(f"p{i:02}" for i in range(5, 17))})  # p17~p21은 유사도 15위 밖
        self.assertEqual([p["paper_id"] for p in result["nearest"]], ["p05", "p07", "r04"])
        self.assertEqual([p["id"] for p in result["nearest"]], ["N1", "N2", "N3"])
        self.assertEqual(result["nearest"][2]["ref_id"], "ref-004")
        self.assertEqual({p["level"] for p in result["nearest"]}, {"abstract"})
        self.assertEqual({p["fulltext_status"] for p in result["nearest"]}, {"studio_unavailable"})
        self.assertEqual((result["verdict"], result["verdict_basis"], result["flags"]), ("solved", ["p05"], [vf.FLAG_ABSTRACT]))
        self.assertEqual(result["nearest"][0]["comparison"]["problem"]["quote"], "p05 problem quote sentence")
        self.assertEqual(len(result["limitations"]), 3)
        self.assertEqual(result["critique"]["requirements"]["alternative_explanations"]["status"], "not_applicable")
        self.assertEqual(result["critique"]["requirements"]["failure_feasibility"]["status"], "undecided")
        self.assertEqual(result["trace"]["calls"], {"solar": 32, "s2": 4, "studio": 0, "embed": 2})  # V7 없음
        self.assertEqual(self.schemas.count("Comparison"), 15)

    def test_fulltext_cards_are_recompared(self):
        refs = [{"id": f"ref-{i:03}", "paper_id": f"p{i:02}", "title": f"Paper p{i:02}", "abstract": f"Abstract of p{i:02}",
                 "year": 2024, "url": "u"} for i in range(1, 6)]
        self.specs = {"p01": {"problem": "same", "method": "partial"}, "p02": {"problem": "partial", "method": "partial"},
                      "p03": {"problem": "same", "method": "different"}}
        self.full_specs = {"p01": {"problem": "same", "method": "same"}}
        seen = []

        def fulltext(p, should_cancel=None):
            seen.append((p["paper_id"], should_cancel()))
            if p["paper_id"] == "p03":
                raise studio.StudioError("job failed")
            return card("p01", "fulltext") if p["paper_id"] == "p01" else None

        result = self.run_verify(refs, {}, studio_ready=True, fulltext=fulltext)
        self.embed_mock.assert_not_called()  # 15편 이하면 줄이지 않는다
        self.assertEqual(sorted(seen), [("p01", False), ("p02", False), ("p03", False)])
        self.assertEqual(self.schemas.count("Comparison"), 5 + 1)  # 본문 카드를 얻은 p01만 다시 비교
        n1 = result["nearest"][0]
        self.assertEqual((n1["paper_id"], n1["level"], n1["comparison"]["method"]["page"]), ("p01", "fulltext", 3))
        self.assertEqual({p["paper_id"]: p["fulltext_status"] for p in result["nearest"]},
                         {"p01": "ok", "p02": "no_pdf", "p03": "studio_error: job failed"})
        self.assertEqual((result["verdict"], result["verdict_basis"]), ("solved", ["p01"]))
        self.assertEqual(result["flags"], [vf.FLAG_ABSTRACT])  # p02, p03은 초록 수준
        self.assertEqual(result["scope"]["fulltext_read"], 1)
        self.assertEqual(result["trace"]["recompare"], [{"paper_id": "p01", "problem": ["same", "same"],
                                                         "method": ["partial", "same"], "assumptions": ["different", "different"],
                                                         "evaluation": ["different", "different"]}])
        self.assertEqual(result["trace"]["calls"], {"solar": 1 + 5 + 5 + 1 + 1, "s2": 6 + 3, "studio": 3, "embed": 0})
        self.assertEqual([e[2]["current"] for e in self.events if e[0] == "fulltext"], [0, 1, 2, 3])

    def test_long_queries_are_cut_to_six_words(self):
        refs = [{"id": "ref-001", "paper_id": "p01", "title": "Paper p01", "abstract": "Abstract of p01", "year": 2024, "url": "u"}]
        self.queries = ["a b c d e f g", "a b c d e f h", "too short"]
        result = self.run_verify(refs, {})
        self.assertEqual(result["scope"]["queries"], ["a b c d e f"])
        self.assertEqual(result["trace"]["queries_raw"], self.queries)

    def test_studio_cancel_propagates(self):
        refs = [{"id": "ref-001", "paper_id": "p01", "title": "Paper p01", "abstract": "Abstract of p01", "year": 2024, "url": "u"}]

        def fulltext(p, should_cancel=None):
            raise studio.StudioCancelled("cancelled")
        with self.assertRaises(studio.StudioCancelled):
            self.run_verify(refs, {}, studio_ready=True, fulltext=fulltext)

    def test_progress_exception_is_not_swallowed(self):
        refs, results = self.big_inputs()

        def progress(message, step, **kw):
            if step == "abstract_cards" and kw.get("current") == 2:
                raise Cancel()
        with self.assertRaises(Cancel):
            self.run_verify(refs, results, progress=progress)

    def test_compare_failure_marks_unknown(self):
        refs = [{"id": "ref-001", "paper_id": "p01", "title": "Paper p01", "abstract": "Abstract of p01", "year": 2024, "url": "u"}]
        self.compare_error = {"p01"}
        result = self.run_verify(refs, {})
        self.assertEqual(result["verdict"], "not_addressed")
        self.assertEqual(result["flags"], [vf.FLAG_ABSTRACT, vf.FLAG_UNKNOWN])
        self.assertEqual(result["trace"]["checks"], {"call_failed": 4})

    def test_budget_stops_before_abstract_cards(self):
        refs, results = self.big_inputs()
        result = self.run_verify(refs, results, limits={"solar": 10})
        json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "budget")
        self.assertEqual(result["budget"], {"scope": "solar", "used": 1, "limit": 10, "need": 15})
        self.assertEqual(self.steps(), STEPS[:3])
        self.abstract.assert_not_called()
        self.assertEqual((result["verdict"], result["nearest"], result["critique"]), (None, [], None))
        self.assertEqual(result["scope"]["queries"], QUERIES)
        self.assertEqual(result["scope"]["narrowed"], 15)

    def test_budget_counts_only_uncached_abstract_cards(self):
        refs, results = self.big_inputs()
        cards.ABSTRACT_CARD_DIR.mkdir(parents=True)
        for pid in ("p01", "p03", "p05", "p06", "p07", "p08"):
            (cards.ABSTRACT_CARD_DIR / f"{pid}.json").write_text("{}", "utf-8")
        result = self.run_verify(refs, results, limits={"solar": 10 + 15})  # 1 + 9 + 15 = 25, 비평 1회가 넘친다
        self.assertEqual(result["status"], "budget")
        self.assertEqual(result["budget"]["scope"], "solar")
        self.assertEqual(self.steps(), STEPS[:-1])
        self.assertEqual(result["verdict"], "not_addressed")  # 판정까지는 했다
        self.assertIsNone(result["critique"])

    def test_budget_s2(self):
        refs, results = self.big_inputs()
        result = self.run_verify(refs, results, limits={"s2": 2})
        self.assertEqual((result["status"], result["budget"]["scope"]), ("budget", "s2"))
        self.assertEqual(self.search.call_count, 2)  # 두 번째 검색어의 재검색 전에 멈춤
        self.assertEqual(len(result["trace"]["search"]), 1)


if __name__ == "__main__":
    unittest.main()
