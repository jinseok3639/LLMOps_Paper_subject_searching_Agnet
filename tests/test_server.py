"""FastAPI 서버, SSE, 결과 저장 테스트. 외부 API는 모의 처리한다. 실행: python -m unittest discover -s tests"""
import asyncio
import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import calllog, config, llm, recommend as rec, runs, store
from src.api import sse
from src.chat import reply_stream
from src.runs import BudgetExceeded, Cancelled

PROFILE = {"lab_name": "보안 연구실", "lab_topics": "AI 보안", "interests": ""}
REF = {"id": "ref-001", "paper_id": "s2", "title": "Ref", "abstract": "초록", "year": 2026, "url": "https://example.com"}


def setUpModule():  # 호출 로그(src.calllog)는 임시 폴더에 쓴다
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    for p in (patch.object(calllog, "ROOT", Path(tmp.name)),  # 로그인 저장소도 Supabase 대신 임시 폴더
              patch.object(store, "backend", store.LocalStore(Path(tmp.name) / "users"))):
        p.start()
        unittest.addModuleCleanup(p.stop)


def events(text):
    """SSE 본문 → [(event, data)]. 주석 줄(: ping)은 뺀다."""
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


class TempRuns(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=config.DB_DIR)
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(runs, "RUNS_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(server.app)


class ApiTests(TempRuns):
    def test_routers_are_discovered(self):
        paths = set(self.client.get("/openapi.json").json()["paths"])
        self.assertTrue({"/api/recommend", "/api/research", "/api/chat", "/api/runs/{run_id}"} <= paths)

    def test_index_injects_feature_modules(self):
        with tempfile.TemporaryDirectory(dir=config.DB_DIR) as folder:
            (Path(folder) / "modules").mkdir()
            (Path(folder) / "index.html").write_text("<head><script src=/app.js></script><!-- modules --></head>", "utf-8")
            for name in ("verify.js", "final.js"):
                (Path(folder) / "modules" / name).write_text("", "utf-8")
            with patch.object(server, "STATIC_DIR", Path(folder)):
                html = self.client.get("/").text
        self.assertIn('<script defer src="/modules/final.js"></script>\n<script defer src="/modules/verify.js"></script>', html)
        self.assertEqual(self.client.get("/app.js").status_code, 200)

    def test_validation_errors(self):
        r = self.client.post("/api/recommend", json={"session_id": "s1", "profile": {"lab_name": " ", "interests": "x"}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"], {"code": "validation_error", "message": "필수 항목을 입력해 주세요.",
                                             "retryable": False, "fields": ["lab_name", "lab_topics"]})
        r = self.client.post("/api/chat", json={"session_id": "s1", "profile": PROFILE, "message": "  "})
        self.assertEqual(r.json()["error"]["fields"], ["message"])
        r = self.client.post("/api/research", json={"session_id": "s1", "profile": PROFILE})
        self.assertEqual(r.json()["error"]["fields"], ["problem_id"])
        r = self.client.post("/api/research", json={"session_id": "s1", "profile": PROFILE, "problem_id": "nope"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        r = self.client.post("/api/recommend", json={"profile": PROFILE})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (422, ["session_id"]))
        r = self.client.get("/api/runs/../../.env")
        self.assertEqual(r.status_code, 404)

    def test_recommend_streams_progress_then_result_and_saves_run(self):
        calls = []

        def fake(profile, rejections, progress):
            calls.append((profile, rejections))
            progress("검색어 작성 중", step="query")
            progress("검색 중", step="search", current=1, total=2)
            return {"status": "ok", "intro": "소개", "note": "", "recommendations": [{"problem_id": "p1"}],
                    "trace": {"candidates": []}}

        body = {"session_id": "s1", "profile": {**PROFILE, "lab_name": " 보안 연구실 "},
                "rejections": [{"target": "problem", "target_id": "p0", "reason": "정책 중심"}]}
        with patch("src.api.recommend.recommend", side_effect=fake):
            r = self.client.post("/api/recommend", json=body)
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        got = events(r.text)
        self.assertEqual([e for e, _ in got], ["progress", "progress", "result"])
        self.assertTrue(all(d["session_id"] == "s1" for _, d in got))
        self.assertEqual(got[1][1], {"session_id": "s1", "step": "search", "message": "검색 중", "current": 1, "total": 2})
        self.assertEqual(calls[0][0], PROFILE)
        self.assertEqual(calls[0][1][0]["reason"], "정책 중심")
        result = got[-1][1]
        self.assertNotIn("trace", result)
        stored = self.client.get(f"/api/runs/{result['run_id']}").json()
        self.assertEqual((stored["kind"], stored["owner_session"]), ("recommend", "s1"))
        self.assertIn("trace", stored)

    def test_research_result_hides_internal_fields(self):
        seen = {}

        def fake(profile, history, problem, progress):
            seen["history"] = history
            progress("참고문헌 검색: x", step="search")
            return {"status": "ok", "problem_id": "p1", "problem_title": "T", "topics": [{"id": "c1", "title": "후보"}],
                    "presentation": "설명", "references": [REF], "novelty_checked": False,
                    "trace": [{"query": "x"}], "generation": {"rounds": []}}

        messages = [{"id": "m1", "role": "user", "content": "GPU 2장", "status": "done"},
                    {"id": "m2", "role": "assistant", "content": "부분", "status": "cancelled"}]
        with patch("src.api.research.problem_card", return_value={"problem_id": "p1", "title": "T"}), \
             patch("src.api.research.generate_research", side_effect=fake):
            r = self.client.post("/api/research", json={"session_id": "s1", "profile": PROFILE,
                                                         "problem_id": "p1", "messages": messages})
        got = events(r.text)
        self.assertEqual([e for e, _ in got], ["progress", "result"])
        self.assertEqual(seen["history"], [{"role": "user", "content": "GPU 2장"}])
        result = got[-1][1]
        self.assertFalse({"trace", "generation"} & set(result))
        self.assertNotIn("abstract", result["references"][0])
        stored = runs.load(result["run_id"])
        self.assertEqual(stored["references"][0]["abstract"], "초록")
        self.assertIn("generation", stored)

    def test_chat_streams_deltas(self):
        with patch("src.api.chat.reply_stream", return_value=iter(["안", "녕"])) as reply:
            r = self.client.post("/api/chat", json={"session_id": "s1", "profile": PROFILE, "message": "질문",
                                                     "picks": {"recommend_run_id": "r_x"}})
        got = events(r.text)
        self.assertEqual([e for e, _ in got], ["progress", "delta", "delta", "result"])
        self.assertEqual(got[-1][1], {"session_id": "s1", "content": "안녕"})
        self.assertEqual(reply.call_args.args[1], {"recommend_run_id": "r_x"})

    def test_failures_become_error_or_budget_events(self):
        cases = [(llm.LLMError("형식 재시도 실패"), "error", "upstream_failed"),
                 (KeyError("x"), "error", "internal_error"),
                 (BudgetExceeded(250, 250, "research"), "budget", None)]
        for exc, kind, code in cases:
            logs = self.assertLogs("uvicorn.error", "ERROR") if kind == "error" else contextlib.nullcontext()
            with patch("src.api.recommend.recommend", side_effect=exc), logs:
                got = events(self.client.post("/api/recommend", json={"session_id": "s9", "profile": PROFILE}).text)
            self.assertEqual(got[-1][0], kind)
            self.assertEqual(got[-1][1]["session_id"], "s9")
            if code:
                self.assertEqual(got[-1][1]["error"]["code"], code)
                self.assertTrue(got[-1][1]["error"]["retryable"])
            else:
                self.assertEqual(got[-1][1], {"session_id": "s9", "used": 250, "limit": 250, "scope": "research"})


class FakeRequest:
    def __init__(self, gone=False):
        self.gone = gone

    async def is_disconnected(self):
        return self.gone


class CancelTests(unittest.TestCase):
    def job(self, cancelled):
        def run(progress, delta):
            progress("시작", step="a")
            try:
                while True:  # 단계 사이마다 확인한다
                    time.sleep(0.01)
                    progress()
            except Cancelled:
                cancelled.set()
                raise
        return run

    def test_closing_stream_cancels_job(self):
        cancelled = threading.Event()

        async def run():
            body = sse.stream_job(FakeRequest(), "s1", self.job(cancelled)).body_iterator
            first = await body.__anext__()
            await body.aclose()  # 브라우저가 abort해서 연결이 끊긴 경우
            return first

        self.assertTrue(asyncio.run(run()).startswith("event: progress"))
        self.assertTrue(cancelled.wait(2))

    def test_detected_disconnect_cancels_job(self):
        cancelled = threading.Event()

        async def run():
            return [chunk async for chunk in sse.stream_job(FakeRequest(gone=True), "s1", self.job(cancelled)).body_iterator]

        with patch.object(sse, "POLL_SEC", 0.05):
            chunks = asyncio.run(run())
        self.assertEqual(len(chunks), 1)  # progress 하나만 보내고 멈춤 (result 없음)
        self.assertTrue(cancelled.wait(2))


class RunStoreTests(TempRuns):
    def test_save_load_and_cleanup(self):
        run_id = runs.save("recommend", {"status": "ok"}, "s1")
        self.assertRegex(run_id, r"^r_[0-9a-f]{32}$")
        data = runs.load(run_id)
        self.assertEqual((data["run_id"], data["owner_session"], data["status"]), (run_id, "s1", "ok"))
        self.assertIn("created_at", data)
        self.assertIsNone(runs.load("r_" + "0" * 32))
        self.assertIsNone(runs.load("../secret"))
        old = runs.save("research", {}, "s2")
        past = time.time() - runs.TTL_SEC - 60
        os.utime(runs.RUNS_DIR / f"{old}.json", (past, past))
        self.assertEqual(runs.cleanup(), 1)
        self.assertIsNone(runs.load(old))
        self.assertIsNotNone(runs.load(run_id))


class SrcTests(TempRuns):
    def test_chat_context_comes_from_stored_runs(self):
        by_id, ids, _ = rec.load_index()
        card = {**rec.problem_card(ids[0]), "reason": "이유", "connection": "연결", "score": 0.9}
        rec_id = runs.save("recommend", {"recommendations": [card], "trace": {"candidates": [{"problem_id": ids[1]}]}})
        res_id = runs.save("research", {"topics": [{"id": "c1", "title": "다듬을 후보"}], "references": [REF]})
        picks = {"recommend_run_id": rec_id, "problem_id": ids[0], "research_run_id": res_id, "topic_id": "c1"}
        with patch.object(llm, "chat_stream", return_value=iter([])) as stream:
            reply_stream(PROFILE, picks, [{"role": "user", "content": "이전 질문"}], "질문")
        sent = stream.call_args.args[0]
        system = sent[0]["content"]
        for text in (card["title"], by_id[ids[1]]["title"], "사용자가 고른 문제", "다듬을 후보", "연구실 연구 주제: AI 보안"):
            self.assertIn(text, system)
        self.assertEqual(sent[1:], [{"role": "user", "content": "이전 질문"}, {"role": "user", "content": "질문"}])
        with patch.object(llm, "chat_stream", return_value=iter([])) as stream:  # 만료된 결과는 빼고 답한다
            reply_stream(PROFILE, {"recommend_run_id": "r_" + "0" * 32}, [], "질문")
        self.assertIn("(없음", stream.call_args.args[0][0]["content"])

    def test_recommend_excludes_rejected_and_passes_reasons(self):
        _, ids, vecs = rec.load_index()
        rejected, other = ids[0], ids[1]
        prompts, steps = {}, []

        def chat_json(messages, schema, temperature=0.0):
            prompts[schema.__name__] = messages[1]["content"]
            if schema is rec.SearchQuery:
                return rec.SearchQuery(research_description="desc", topics=["t"])
            if schema is rec.Selection:
                return rec.Selection(problem_ids=[other, "made-up"])
            return rec.Answer(intro="소개", note="", recommendations=[
                rec.RecommendationText(problem_id=other, reason="이유", connection="연결")])

        query_vec = (vecs[0] + vecs[1]).tolist()  # 거절한 문제와 다른 문제 모두 상위에 오는 질의
        with patch.object(llm, "chat_json", side_effect=chat_json), patch.object(llm, "embed", return_value=[query_vec]):
            result = rec.recommend(PROFILE, [{"target": "problem", "target_id": rejected, "reason": "정책 중심"}],
                                   lambda message, step=None: steps.append(step))
        self.assertEqual(steps, ["query", "search", "select", "answer"])
        self.assertIn("(not given", prompts["SearchQuery"])
        self.assertIn("Lab research topics: AI 보안", prompts["SearchQuery"])
        self.assertIn("정책 중심", prompts["Selection"])
        self.assertNotIn(f"problem_id: {rejected}\n", prompts["Selection"])
        self.assertIn(f"problem_id: {other}\n", prompts["Selection"])
        self.assertNotIn(rejected, [c["problem_id"] for c in result["trace"]["candidates"]])
        self.assertEqual([c["problem_id"] for c in result["recommendations"]], [other])
        self.assertEqual(result["recommendations"][0]["reason"], "이유")
        self.assertEqual(result["trace"]["rejected"], [rejected])


if __name__ == "__main__":
    unittest.main()
