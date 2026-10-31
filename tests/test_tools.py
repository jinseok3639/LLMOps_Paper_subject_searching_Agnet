"""네트워크 없이 도는 Tool 테스트. 실행: python -m unittest discover tests"""
import unittest
from unittest import mock

from src.agents.tools import default_toolbox
from src.clients.semantic_scholar import S2Error

PAPER = {"paperId": "abc", "externalIds": {"ArXiv": "2307.10569"}, "title": "T"}


class ToolBoxTest(unittest.TestCase):
    def setUp(self):
        self.box = default_toolbox()

    def test_schemas(self):
        names = [s["function"]["name"] for s in self.box.schemas()]
        self.assertEqual(names, ["search_papers"])

    def test_invalid_input_returns_error(self):
        self.assertIn("error", self.box.call("search_papers", {"query": ""}))
        self.assertIn("error", self.box.call("search_papers", "{not json"))
        self.assertIn("error", self.box.call("search_papers", {"query": "x", "unknown": 1}))
        self.assertIn("error", self.box.call("no_such_tool", {}))

    def test_api_failure_returns_error(self):
        with mock.patch("src.clients.semantic_scholar.search_papers", side_effect=S2Error("429")):
            self.assertIn("429", self.box.call("search_papers", {"query": "x"})["error"])

    def test_only_returned_papers_are_citable(self):
        with mock.patch("src.clients.semantic_scholar.search_papers", return_value={"total": 1, "data": [PAPER]}):
            self.box.call("search_papers", {"query": "x"})
        self.assertEqual(self.box.unknown_citations(["abc", "ARXIV:2307.10569", "made-up"]), ["made-up"])


if __name__ == "__main__":
    unittest.main()
