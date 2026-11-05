"""참고문헌 검색 에이전트 테스트. LLM과 S2는 가짜로 바꾼다."""
import re
import unittest
from unittest.mock import patch

from src import llm, references as rf

PROFILE = {"lab_name": "lab", "lab_topics": "AI security", "interests": "backdoor detection"}
PROBLEM = {"problem_id": "p1", "title": "Backdoor Detection Algorithms"}
QUERIES = ["backdoor detection neural networks", "trojan model trigger inversion", "poisoned data detection"]


def rows(prefix, n):
    return [{"paperId": f"{prefix}{i}", "title": f"{prefix} paper {i}", "abstract": f"{prefix} abstract {i}", "year": 2024, "url": f"https://s2/{prefix}{i}"} for i in range(1, n + 1)]


class SearchReferencesTests(unittest.TestCase):
    def run_search(self, results, unrelated=(), extra=(), filter_error=None):
        """results: 검색어별 S2 결과. unrelated: 필터가 무관하다고 판정할 후보 id. extra: 입력에 없는 id."""
        calls = []

        def chat_json(messages, schema, temperature=0.0):
            calls.append((schema, messages))
            if schema is rf.Queries:
                return rf.Queries(queries=QUERIES)
            if filter_error:
                raise filter_error
            ids = re.findall(r"^\[(c\d+)\]", messages[1]["content"], re.M) + list(extra)
            return rf.Screening(verdicts=[rf.Verdict(id=i, clearly_unrelated=i.strip(" []") in unrelated) for i in ids])
        with patch("src.references.llm.chat_json", side_effect=chat_json), patch("src.references._search", side_effect=lambda q: results.get(q, [])):
            result = rf.search_references(PROFILE, [{"role": "user", "content": "focus on LLM agents"}], PROBLEM)
        return result, calls

    def test_round_robin_order_and_cap(self):
        results = dict(zip(QUERIES, (rows("a", 20), rows("b", 20), rows("c", 20))))
        result, _ = self.run_search(results)
        refs = result["refs"]
        self.assertEqual(len(refs), rf.MAX_REFS)
        self.assertEqual([r["paper_id"] for r in refs[:6]], ["a1", "b1", "c1", "a2", "b2", "c2"])
        self.assertEqual([r["query"] for r in refs[:3]], QUERIES)
        self.assertEqual([t["passed"] for t in result["search_trace"][:3]], [9, 8, 8])
        self.assertEqual(refs[0], {"id": "ref-001", "paper_id": "a1", "title": "a paper 1", "abstract": "a abstract 1", "year": 2024, "url": "https://s2/a1", "query": QUERIES[0]})

    def test_duplicates_and_missing_abstracts_are_skipped_and_short_lists_pass_whole(self):
        shared = {"paperId": "x", "title": "Shared", "abstract": "Shared abstract"}
        results = {QUERIES[0]: [shared, *rows("a", 2)], QUERIES[1]: [shared, {"paperId": "na", "title": "No abstract"}],
                   QUERIES[2]: rows("c", 1)}
        result, _ = self.run_search(results)
        self.assertEqual([r["paper_id"] for r in result["refs"]], ["x", "c1", "a1", "a2"])
        self.assertEqual(result["search_trace"][1]["missing_abstract"], 1)
        self.assertEqual(result["search_trace"][1]["passed"], 0)
        self.assertEqual(result["search_trace"][-1]["candidates"], 4)

    def test_filter_drops_only_named_results(self):
        results = dict(zip(QUERIES, (rows("a", 3), rows("b", 3), rows("c", 3))))
        result, calls = self.run_search(results, unrelated={"c2", "c5"})
        ids = [r["paper_id"] for r in result["refs"]]
        # 후보 순서: a1 a2 a3 b1 b2 b3 c1 c2 c3 → c2=a2, c5=b2
        self.assertNotIn("a2", ids)
        self.assertNotIn("b2", ids)
        self.assertEqual(len(ids), 7)
        summary = result["search_trace"][-1]
        self.assertEqual((summary["status"], summary["candidates"], summary["unrelated"], summary["passed"]), ("ok", 9, 2, 7))
        self.assertEqual(summary["unrelated_titles"], ["a paper 2", "b paper 2"])
        self.assertEqual(result["search_trace"][0]["unrelated"], 1)
        # 필터 입력에는 고른 문제, 대화, 모든 후보의 제목과 초록이 들어간다.
        schema, messages = calls[-1]
        self.assertIs(schema, rf.Screening)
        user = messages[1]["content"]
        for text in ("Backdoor Detection Algorithms", "focus on LLM agents", "a paper 1", "c abstract 3", "[c9]"):
            self.assertIn(text, user)

    def test_filter_failure_keeps_everything(self):
        results = dict(zip(QUERIES, (rows("a", 2), rows("b", 2), rows("c", 2))))
        for kwargs in ({"unrelated": {"c1"}, "extra": ["c99"]}, {"unrelated": {f"c{i}" for i in range(1, 7)}},
                       {"filter_error": llm.LLMError("bad format")}):
            with self.subTest(kwargs=kwargs):
                result, _ = self.run_search(results, **kwargs)
                self.assertEqual(len(result["refs"]), 6)
                summary = result["search_trace"][-1]
                self.assertTrue(summary["status"].startswith("failed"))
                self.assertEqual(summary["unrelated"], 0)

    def test_missing_verdicts_are_kept(self):
        results = dict(zip(QUERIES, (rows("a", 2), rows("b", 2), rows("c", 2))))
        # 후보 c1(=a1)만 판정을 받았다. 판정이 없는 나머지는 남는다.
        screening = rf.Screening(verdicts=[rf.Verdict(id="[c1]", clearly_unrelated=True)])
        with patch("src.references.llm.chat_json", side_effect=[rf.Queries(queries=QUERIES), screening]), \
                patch("src.references._search", side_effect=lambda q: results[q]):
            result = rf.search_references(PROFILE, [], PROBLEM)
        self.assertEqual([r["paper_id"] for r in result["refs"]], ["a2", "b1", "c1", "b2", "c2"])
        self.assertEqual(result["search_trace"][-1]["status"], "ok")

    def test_filter_truncates_long_abstracts(self):
        long = [{"paperId": "l", "title": "Long", "abstract": "x" * 5000}]
        result, calls = self.run_search({q: long for q in QUERIES})
        self.assertEqual(len(result["refs"][0]["abstract"]), 5000)
        self.assertNotIn("x" * (rf.FILTER_ABSTRACT_CHARS + 1), calls[-1][1][1]["content"])


if __name__ == "__main__":
    unittest.main()
