"""eval/query_recall 채점 함수 (API 없이)."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("query_recall", Path(__file__).resolve().parents[1] / "eval/query_recall/run.py")
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)

GOLD = [{"paper_id": "a", "title": "A", "grade": "core"}, {"paper_id": "b", "title": "B", "grade": "core"},
        {"paper_id": "", "title": "No id", "grade": "core"}, {"paper_id": "c", "title": "C", "grade": "related"}]


def s(*ids):
    return {"results": [{"paperId": i} for i in ids]}


class ScoreTest(unittest.TestCase):
    def test_references_counts_core_in_union(self):
        sc = qr.score_references([s("a", "x"), s("x", "c")], GOLD)
        self.assertEqual((sc["core_hit"], sc["core_total"], sc["gold_hit"], sc["pooled"]), (1, 2, 2, 3))
        self.assertEqual((sc["hit_titles"], sc["missed_titles"]), (["A"], ["B"]))
        self.assertEqual(sc["per_query_core_hit"], [1, 0])

    def test_verify_best_rank(self):
        sc = qr.score_verify([s("x", "y", "t"), s(), s("t")], "t")
        self.assertEqual((sc["found"], sc["best_rank"], sc["per_query_rank"]), (True, 1, [3, None, 1]))
        self.assertFalse(qr.score_verify([s("x")], "t")["found"])


if __name__ == "__main__":
    unittest.main()
