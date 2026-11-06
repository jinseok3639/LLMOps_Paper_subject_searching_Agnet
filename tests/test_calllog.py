"""호출 로그 테스트 (src/calllog.py, 요구사항 O-1, O-4). 외부 API는 모의 처리한다. 실행: python -m unittest discover tests"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from pydantic import BaseModel

import server
from src import calllog, cards, config, llm, references, runs, studio
from src.api import sse
from src.clients import semantic_scholar

KEY, S2_KEY = "up_test_secret_0123456789", "s2_test_secret_0123456789"


class Out(BaseModel):
    x: int


def chat_resp(content='{"x": 1}', parsed=None, usage=(10, 3)):
    return NS(choices=[NS(message=NS(content=content, parsed=parsed or Out(x=1)), finish_reason="stop")],
              usage=NS(prompt_tokens=usage[0], completion_tokens=usage[1]))


def chunk(text=None, usage=None):
    return NS(choices=[NS(delta=NS(content=text))] if text is not None else [], usage=usage)


class Logs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (patch.object(calllog, "ROOT", self.root), patch.object(runs, "RUNS_DIR", self.root / "runs"),
                  patch.dict(os.environ, {"UPSTAGE_API_KEY": KEY, "S2_API_KEY": S2_KEY}),
                  patch.object(semantic_scholar, "INTERVAL_KEY", 0)):
            p.start()
            self.addCleanup(p.stop)
        self.client = MagicMock()
        p = patch.object(llm, "client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(calllog._job.set, None)

    def text(self):
        return "".join(p.read_text("utf-8") for p in sorted((self.root / "logs").glob("calls-*.jsonl")))

    def lines(self, wait_for=None):
        """로그 줄들. wait_for가 있으면 그 provider 줄이 생길 때까지 2초 기다린다 (작업 스레드가 쓰는 줄)."""
        deadline = time.monotonic() + 2
        while True:
            rows = [json.loads(l) for l in self.text().splitlines()]
            if not wait_for or any(r["provider"] == wait_for for r in rows) or time.monotonic() > deadline:
                return rows
            time.sleep(0.02)


class CallTests(Logs):
    def test_one_line_per_call_with_context_through_thread_pool(self):
        self.client.chat.completions.parse.return_value = chat_resp()
        calllog.start("s1", "research")
        calllog.set_step("generate")
        msgs = [{"role": "system", "content": "inline instruction"}, {"role": "user", "content": "u"}]
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(calllog.bind(lambda _: llm.chat_json(msgs, Out)), range(6)))
            list(ex.map(lambda _: llm.chat_json(msgs, Out), range(1)))  # bind 없이는 문맥이 없다
        rows = self.lines()
        self.assertEqual(len(rows), 7)
        bound, unbound = rows[:6], rows[6:]
        for r in bound:
            self.assertEqual((r["provider"], r["model"], r["session_id"], r["run_kind"], r["step"], r["status"]),
                             ("solar", config.MODEL, "s1", "research", "generate", "ok"))
            self.assertEqual((r["tokens_in"], r["tokens_out"], r["output"]), (10, 3, '{"x": 1}'))
            self.assertEqual(r["input"], msgs)
            self.assertIsInstance(r["latency_ms"], int)
            self.assertIsNone(r["prompt_name"])
            self.assertEqual(r["prompt_hash"], calllog._hash("inline instruction"))
        self.assertIsNone(unbound[0]["session_id"])
        self.assertEqual(calllog._job.get().calls, 6)

    def test_prompt_file_name_and_hash(self):
        text = (config.PROMPT_DIR / "recommend_query.md").read_text("utf-8")
        self.assertEqual(calllog.prompt_version(text), ("recommend_query", calllog._hash(text)))
        self.assertEqual(calllog.prompt_version(text + "\n\n[컨텍스트] ...")[0], "recommend_query")  # 뒤에 붙인 문맥
        propose = (config.ROOT_DIR / "src/generation_method/prompts/propose.md").read_text("utf-8")
        filled = propose.replace("{slots}", "3").replace("{persona}", "You question assumptions.") + "\nResearcher context: x"
        self.assertEqual(calllog.prompt_version(filled), ("generation_method/propose", calllog._hash(propose)))
        refine = (config.ROOT_DIR / "src/generation_method/prompts/refine.md").read_text("utf-8")
        self.assertEqual(calllog.prompt_version(refine.replace("{persona}", "P"))[0], "generation_method/refine")

    def test_format_retry_and_failure_are_separate_lines(self):
        self.client.chat.completions.parse.side_effect = [ValueError("bad json"), chat_resp()]
        with patch.object(llm, "ValidationError", ValueError):
            llm.chat_json([{"role": "user", "content": "u"}], Out)
        self.assertEqual([r["status"] for r in self.lines()], ["error", "ok"])
        self.assertIn("bad json", self.lines()[0]["error"])

    def test_stream_logged_once_at_end(self):
        self.client.chat.completions.create.return_value = iter(
            [chunk("안"), chunk("녕"), chunk(usage=NS(prompt_tokens=7, completion_tokens=2))])
        self.assertEqual("".join(llm.chat_stream([{"role": "user", "content": "q"}])), "안녕")
        self.assertTrue(self.client.chat.completions.create.call_args.kwargs["stream_options"]["include_usage"])
        (row,) = self.lines()
        self.assertEqual((row["status"], row["output"], row["tokens_in"], row["tokens_out"]), ("ok", "안녕", 7, 2))
        self.assertIsNone(row["tokens_cached"])  # 응답에 캐시 정보가 없으면 None

    def test_cached_tokens_logged(self):
        usage = NS(prompt_tokens=7312, completion_tokens=2, prompt_tokens_details=NS(cached_tokens=7280))
        self.client.chat.completions.create.return_value = iter([chunk("a"), chunk(usage=usage)])
        "".join(llm.chat_stream([{"role": "user", "content": "q"}]))
        (row,) = self.lines()
        self.assertEqual((row["tokens_in"], row["tokens_cached"]), (7312, 7280))

    def test_stream_closed_early_is_cancelled(self):
        self.client.chat.completions.create.return_value = iter([chunk("안"), chunk("녕"), chunk("!")])
        gen = llm.chat_stream([{"role": "user", "content": "q"}])
        self.assertEqual(next(gen), "안")
        self.assertEqual(self.lines(), [])  # 도는 중에는 남기지 않는다
        gen.close()
        (row,) = self.lines()
        self.assertEqual((row["status"], row["output"]), ("cancelled", "안"))
        self.assertTrue(row["error"].startswith("GeneratorExit"))

    def test_s2_search_is_not_cached(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": [{"paperId": "p1", "title": "T", "abstract": "long abstract"}]}
        with patch.object(config, "DB_DIR", self.root), patch("src.clients.semantic_scholar.requests.get", return_value=resp) as get:
            references._search("agent safety eval")
            references._search("agent safety eval")
        self.assertEqual(get.call_count, 2)
        self.assertFalse((self.root / "search_cache").exists())
        rows = self.lines()
        self.assertEqual([(r["provider"], r["status"]) for r in rows], [("s2", "ok"), ("s2", "ok")])
        self.assertEqual(rows[1]["output"], [{"paperId": "p1", "title": "T"}])  # 초록은 빼고 남긴다
        self.assertEqual(rows[0]["input"], "agent safety eval")

    def test_studio_and_pdf(self):
        result = {"job_id": "job_1", "agent_id": "agt_x", "steps": [{"step": "parse"}, {"step": "extract"}],
                  "final": {"title": "T"}, "parse_text": "x", "elapsed_sec": 1.0, "raw": {}}
        with patch.object(studio, "_run", return_value=result):
            studio.run_agent("paper.pdf", agent="agt_x")
        with patch.object(studio, "_run", side_effect=studio.StudioCancelled("취소됨")), \
                self.assertRaises(studio.StudioCancelled):
            studio.run_agent("paper.pdf", agent="agt_x")
        with patch.object(cards, "PAPER_PDF_DIR", self.root / "pdf"), \
                patch.object(cards, "_s2_pdf_info", return_value={"externalIds": {"ArXiv": "2401.1"}}), \
                patch.object(cards, "_download", return_value=False):
            with self.assertRaises(cards.NoPdf):
                cards.fetch_pdf({"paper_id": "p1"})
        rows = self.lines()
        self.assertEqual([(r["provider"], r["model"], r["status"]) for r in rows],
                         [("studio", "agt_x", "ok"), ("studio", "agt_x", "cancelled"),
                          ("s2", "paper", "ok"), ("pdf", "download", "error")])
        self.assertEqual(rows[0]["output"]["job_id"], "job_1")
        self.assertEqual(rows[3]["input"], "https://arxiv.org/pdf/2401.1")

    def test_no_secrets_in_lines(self):
        with self.assertRaises(RuntimeError), calllog.timed("s2", "paper/search", "q"):
            raise RuntimeError(f"401 for key {KEY} / {S2_KEY}, Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": []}
        with patch.object(config, "DB_DIR", self.root), patch("src.clients.semantic_scholar.requests.get", return_value=resp) as get:
            references._search("q2")
        self.assertEqual(get.call_args.kwargs["headers"], {"x-api-key": S2_KEY})
        text = self.text()
        self.assertEqual(len(text.splitlines()), 2)
        for secret in (KEY, S2_KEY, "abcdefghijklmnopqrstuvwxyz"):
            self.assertNotIn(secret, text)
        self.assertIn("Bearer ***", text)

    def test_long_fields_are_clipped(self):
        with patch.object(calllog, "MAX_CHARS", 10), calllog.timed("solar", "m", "x" * 50) as c:
            c.output = "y" * 5
        (row,) = self.lines()
        self.assertTrue(row["input"].startswith("x" * 10 + "...(+40"))
        self.assertEqual(row["output"], "yyyyy")


class FakeRequest:
    url = NS(path="/api/research")

    async def is_disconnected(self):
        return False


class JobTests(Logs):
    def test_recommend_job_lines(self):
        self.client.chat.completions.parse.return_value = chat_resp()

        def fake(profile, rejections, progress, exclude=()):
            progress("검색어 작성 중", step="query")
            llm.chat_json([{"role": "user", "content": "u"}], Out)
            return {"status": "ok", "intro": "", "note": "", "recommendations": [], "trace": {}}

        with patch("src.api.recommend.recommend", side_effect=fake):
            r = TestClient(server.app).post("/api/recommend", json={
                "session_id": "s7", "profile": {"lab_name": "연구실", "lab_topics": "AI 보안"}})
        run_id = json.loads(r.text.strip().split("data: ")[-1])["run_id"]
        call, job = self.lines(wait_for="job")
        self.assertEqual((call["provider"], call["session_id"], call["run_kind"], call["step"]),
                         ("solar", "s7", "recommend", "query"))
        self.assertEqual((job["provider"], job["model"], job["status"], job["run_id"], job["calls"], job["session_id"]),
                         ("job", "recommend", "ok", run_id, 1, "s7"))

    def test_cancelled_job_is_recorded(self):
        def job(progress, delta):
            progress("시작", step="search")
            while True:
                time.sleep(0.01)
                progress()

        async def run():
            body = sse.stream_job(FakeRequest(), "s8", job).body_iterator
            await body.__anext__()
            await body.aclose()  # 브라우저가 abort한 경우

        asyncio.run(run())
        (row,) = self.lines(wait_for="job")
        self.assertEqual((row["provider"], row["model"], row["status"], row["session_id"], row["step"], row["run_id"]),
                         ("job", "research", "cancelled", "s8", "search", None))

    def test_failed_job_is_recorded(self):
        def job(progress, delta):
            raise KeyError("x")

        async def run():
            return [c async for c in sse.stream_job(FakeRequest(), "s9", job, kind="verify").body_iterator]

        with self.assertLogs("uvicorn.error", "ERROR"):
            asyncio.run(run())
        (row,) = self.lines(wait_for="job")
        self.assertEqual((row["model"], row["status"]), ("verify", "error"))
        self.assertIn("KeyError", row["error"])


if __name__ == "__main__":
    unittest.main()
