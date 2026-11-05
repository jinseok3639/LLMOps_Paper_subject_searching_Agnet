"""POST /api/verify 테스트 (verify와 문제 카드는 가짜). 실행: python -m unittest discover tests"""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import calllog, runs, studio
from src.api import sse, verify as api

PROFILE = {"lab_name": "보안 연구실", "lab_topics": "AI 보안", "interests": ""}
PROBLEM = {"problem_id": "p1", "title": "Reward hacking"}
REF = {"id": "ref-001", "paper_id": "s2", "title": "Ref", "abstract": "초록", "year": 2026, "url": "https://example.com"}


def setUpModule():  # 호출 로그(src.calllog)는 임시 폴더에 쓴다
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    p = patch.object(calllog, "ROOT", Path(tmp.name))
    p.start()
    unittest.addModuleCleanup(p.stop)


TOPICS = [{"id": "c1", "title": "후보 1"}, {"id": "c2", "title": "후보 2", "gap": "gap"}]


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def result(status="ok", budget=None):
    return {"status": status, "topic_id": "c2", "verdict": "partial" if status == "ok" else None, "flags": ["초록 수준"],
            "nearest": [], "limitations": [], "critique": None, "scope": {"queries": ["q"]}, "budget": budget,
            "trace": {"calls": {"solar": 3}}}


class FakeRequest:
    async def is_disconnected(self):
        return False


class VerifyApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(runs, "RUNS_DIR", Path(tmp.name)),
                  patch("src.api.verify.problem_card", side_effect=lambda pid: PROBLEM if pid == "p1" else None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(server.app)
        self.research_id = runs.save("research", {"topics": TOPICS, "references": [REF]}, "s1")
        self.body = {"session_id": "s1", "profile": {**PROFILE, "lab_name": " 보안 연구실 "}, "problem_id": "p1",
                     "research_run_id": self.research_id, "topic_id": "c2"}

    def post(self, fake, **body):
        with patch("src.api.verify.verify", side_effect=fake) as v:
            r = self.client.post("/api/verify", json={**self.body, **body})
        return r, v

    def test_streams_progress_then_result_and_saves_run(self):
        def fake(profile, problem, topic, refs, progress, should_cancel):
            progress("검증 검색어 작성", step="queries")
            progress("논문 검색: q", step="search", current=1, total=3)
            self.assertFalse(should_cancel())
            return result()

        r, v = self.post(fake)
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        got = events(r.text)
        self.assertEqual([e for e, _ in got], ["progress", "progress", "result"])
        self.assertTrue(all(d["session_id"] == "s1" for _, d in got))
        self.assertEqual(got[1][1], {"session_id": "s1", "step": "search", "message": "논문 검색: q", "current": 1, "total": 3})
        profile, problem, topic, refs = v.call_args.args[:4]
        self.assertEqual((profile, problem, topic, refs), (PROFILE, PROBLEM, TOPICS[1], [REF]))  # 초록까지 넘긴다
        out = got[-1][1]
        self.assertNotIn("trace", out)
        self.assertEqual(out["verdict"], "partial")
        stored = self.client.get(f"/api/runs/{out['run_id']}").json()
        self.assertEqual((stored["kind"], stored["owner_session"], stored["research_run_id"]), ("verify", "s1", self.research_id))
        self.assertIn("trace", stored)

    def test_validation_and_not_found(self):
        r = self.client.post("/api/verify", json={"session_id": "s1", "profile": PROFILE})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (422, ["problem_id", "research_run_id", "topic_id"]))
        r = self.client.post("/api/verify", json={**self.body, "profile": {"lab_name": "x"}})
        self.assertEqual(r.json()["error"]["fields"], ["lab_topics"])
        r = self.client.post("/api/verify", json={k: v for k, v in self.body.items() if k != "session_id"})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (422, ["session_id"]))
        recommend_id = runs.save("recommend", {"recommendations": []}, "s1")
        for change in ({"problem_id": "nope"}, {"research_run_id": "r_" + "0" * 32}, {"research_run_id": "../x"},
                       {"research_run_id": recommend_id}, {"topic_id": "c9"}):
            with patch("src.api.verify.verify") as v:
                r = self.client.post("/api/verify", json={**self.body, **change})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"), change)
            v.assert_not_called()

    def test_budget_stop_saves_partial_run_and_sends_budget_event(self):
        r, _ = self.post(lambda *a: result("budget", {"scope": "solar", "used": 1, "limit": 10, "need": 15}))
        got = events(r.text)
        self.assertEqual(got[-1], ("budget", {"session_id": "s1", "used": 1, "limit": 10, "scope": "solar"}))
        saved = [json.loads(p.read_text("utf-8")) for p in runs.RUNS_DIR.glob("r_*.json")]
        self.assertEqual(sorted(d["kind"] for d in saved), ["research", "verify"])

    def test_upstream_error_event(self):
        from src import llm
        with self.assertLogs("uvicorn.error", "ERROR"):
            r, _ = self.post(llm.LLMError("형식 재시도 실패"))
        self.assertEqual(events(r.text)[-1][1]["error"]["code"], "upstream_failed")

    def test_closing_stream_cancels_studio_wait_without_error(self):
        saw_cancel = threading.Event()

        def fake(profile, problem, topic, refs, progress, should_cancel):
            progress("본문 분석 중 (0/3)", step="fulltext")
            while not should_cancel():  # cards.fulltext_card의 Studio 상태 확인처럼
                time.sleep(0.01)
            saw_cancel.set()
            raise studio.StudioCancelled("취소됨")

        async def run():
            response = api.post_verify(api.Body(**self.body), FakeRequest())
            stream = response.body_iterator
            first = await stream.__anext__()
            await stream.aclose()  # 브라우저가 abort한 경우
            return first

        with patch("src.api.verify.verify", side_effect=fake), patch.object(sse, "log") as log:
            self.assertTrue(asyncio.run(run()).startswith("event: progress"))
            self.assertTrue(saw_cancel.wait(2))
            for _ in range(200):
                if log.info.called:
                    break
                time.sleep(0.01)
        log.info.assert_called_once()  # "작업 취소"로 끝난다 (error 아님)
        log.exception.assert_not_called()
        self.assertEqual([p.name for p in runs.RUNS_DIR.glob("r_*.json")], [f"{self.research_id}.json"])  # 저장 안 함


if __name__ == "__main__":
    unittest.main()
