"""POST /api/feedback 테스트 (요구사항 O-3). 실행: python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import calllog


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = patch.object(calllog, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(server.app)

    def rows(self):
        path = self.root / "feedback.jsonl"
        return [json.loads(l) for l in path.read_text("utf-8").splitlines()] if path.exists() else []

    def test_saves_one_line(self):
        r = self.client.post("/api/feedback", json={"session_id": "s1", "target": "problem",
                                                    "target_id": " 2109.13916v5:5.1:3 ", "value": "up"})
        self.assertEqual((r.status_code, r.content), (204, b""))
        r = self.client.post("/api/feedback", json={"session_id": "s1", "target": "topic", "target_id": "r_x:s1-formal",
                                                    "value": "down", "comment": "너무 넓음"})
        self.assertEqual(r.status_code, 204)
        first, second = self.rows()
        self.assertEqual({k: first[k] for k in ("session_id", "target", "target_id", "value", "comment")},
                         {"session_id": "s1", "target": "problem", "target_id": "2109.13916v5:5.1:3",
                          "value": "up", "comment": None})
        self.assertRegex(first["id"], r"^f_[0-9a-f]{16}$")
        self.assertIn("created_at", first)
        self.assertEqual((second["target"], second["value"], second["comment"]), ("topic", "down", "너무 넓음"))

    def test_validation_errors(self):
        cases = [({"value": "meh"}, ["value"]), ({"target": "paper"}, ["target"]), ({"target_id": "  "}, ["target_id"]),
                 ({"session_id": None}, ["session_id"])]
        base = {"session_id": "s1", "target": "problem", "target_id": "p1", "value": "up"}
        for change, fields in cases:
            r = self.client.post("/api/feedback", json={k: v for k, v in {**base, **change}.items() if v is not None})
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.json()["error"], {"code": "validation_error", "message": "필수 항목을 입력해 주세요.",
                                                 "retryable": False, "fields": fields})
        self.assertEqual(self.rows(), [])

    def test_router_and_module_are_discovered(self):
        self.assertIn("/api/feedback", self.client.get("/openapi.json").json()["paths"])
        self.assertIn('<script defer src="/modules/feedback.js"></script>', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
