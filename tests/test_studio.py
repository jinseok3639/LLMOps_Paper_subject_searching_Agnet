"""Studio 에이전트 호출 테스트 (HTTP는 가짜). 실행: python -m unittest discover tests"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import studio

ENV = {"UPSTAGE_API_KEY": "test-key", "STUDIO_CARD_AGENT_ID": "agt_test"}
FINAL = {"title": "T", "problem": {"text": "p", "quote": "We study robustness.", "page": 1}}
PARSE_HTML = "<h1 id='0'>T</h1><p id='1' data-category='paragraph'>We study robust-\nness &amp; safety.</p>"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


def job(status, output=None):
    return {"id": "job_1", "status": status, "output": output or []}


def completed(final=FINAL, parse=PARSE_HTML):
    return job("completed", [
        {"type": "message", "model": "step_1_parse", "content": [{"type": "output_text", "text": parse}]},
        {"type": "message", "model": "step_2_extract",
         "content": [{"type": "output_text", "text": json.dumps(final), "additional_values": "{\"confidence\": 0.9}"}]},
    ])


class FakeStudio:
    """(method, 경로) → 응답. GET은 polls 목록을 차례로 돌려준다 (마지막 값 반복)."""

    def __init__(self, polls, create=None, upload=None):
        self.polls, self.calls = list(polls), []
        self.create = create or Resp(body=job("queued"))
        self.upload = upload or Resp(body={"id": "file_1"})

    def __call__(self, method, url, **kw):
        path = url.removeprefix(studio.BASE_URL)
        self.calls.append((method, path, kw))
        if method == "POST" and path == "/files":
            return self.upload
        if method == "POST" and path == "/responses":
            return self.create
        if method == "GET":
            r = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return r if isinstance(r, Resp) else Resp(body=r)
        if method == "DELETE":
            return Resp(body={"id": "file_1", "deleted": True})
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [(m, p) for m, p, _ in self.calls]


class StudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")
        self.env = patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_with(self, fake, **kw):
        with patch("src.studio.requests.request", side_effect=fake):
            return studio.run_agent(self.pdf, poll=0, **kw)

    def test_happy_path(self):
        fake = FakeStudio([job("in_progress"), completed()])
        result = self.run_with(fake)
        self.assertEqual(result["final"], FINAL)
        self.assertEqual([s["step"] for s in result["steps"]], ["step_1_parse", "step_2_extract"])
        self.assertNotIn("<p", result["parse_text"])
        self.assertIn("robust-\nness & safety.", result["parse_text"])
        self.assertEqual(result["steps"][1]["additional_values"], ["{\"confidence\": 0.9}"])
        self.assertEqual(result["raw"]["status"], "completed")
        create = next(kw for m, p, kw in fake.calls if p == "/responses")["json"]
        self.assertEqual((create["model"], create["include"]), ("agt_test", ["all"]))
        self.assertEqual(create["input"][0]["content"][0], {"type": "input_file", "file_id": "file_1"})
        poll = next(kw for m, p, kw in fake.calls if m == "GET")
        self.assertEqual(poll["params"], {"include[]": "all"})
        self.assertEqual(poll["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(fake.methods()[-1], ("DELETE", "/files/file_1"))

    def test_failed_job_raises_and_deletes_file(self):
        fake = FakeStudio([job("failed") | {"error": {"message": "bad config"}}])
        with self.assertRaises(studio.StudioError) as ctx:
            self.run_with(fake)
        self.assertNotIsInstance(ctx.exception, studio.StudioUnavailable)
        self.assertIn("bad config", str(ctx.exception))
        self.assertIn(("DELETE", "/files/file_1"), fake.methods())

    def test_timeout(self):
        fake = FakeStudio([job("in_progress")])
        with self.assertRaises(studio.StudioError) as ctx:
            self.run_with(fake, timeout=0.05)
        self.assertIn("시간 초과", str(ctx.exception))
        self.assertIn(("DELETE", "/files/file_1"), fake.methods())

    def test_cancel_stops_polling(self):
        fake = FakeStudio([job("in_progress")])
        checks = iter([False, False, True])
        with self.assertRaises(studio.StudioCancelled):
            self.run_with(fake, should_cancel=lambda: next(checks))
        self.assertEqual(sum(m == "GET" for m, _ in fake.methods()), 1)
        self.assertEqual(fake.methods()[-1], ("DELETE", "/files/file_1"))

    def test_missing_agent_id_makes_no_call(self):
        fake = FakeStudio([completed()])
        with patch.dict(os.environ, {"STUDIO_CARD_AGENT_ID": ""}), self.assertRaises(studio.StudioUnavailable) as ctx:
            self.run_with(fake)
        self.assertIn("STUDIO_CARD_AGENT_ID", str(ctx.exception))
        self.assertEqual(fake.calls, [])

    def test_unauthorized_is_unavailable(self):
        fake = FakeStudio([completed()], upload=Resp(401, {"error": "invalid key"}))
        with self.assertRaises(studio.StudioUnavailable):
            self.run_with(fake)

    def test_transient_poll_error_keeps_polling(self):
        fake = FakeStudio([Resp(503, {"error": "busy"}), completed()])
        self.assertEqual(self.run_with(fake)["final"], FINAL)
        self.assertEqual(sum(m == "GET" for m, _ in fake.methods()), 2)

    def test_final_step_not_json(self):
        fake = FakeStudio([job("completed", [{"model": "step_1", "content": [{"text": "not json"}]}])])
        with self.assertRaises(studio.StudioError):
            self.run_with(fake)

    def test_plain_text_reads_document_parse_json(self):
        dp = {"content": {"html": "<p>Hello &lt;world&gt;</p>", "text": ""},
              "elements": [{"page": 1, "content": {"text": "unused"}}]}
        self.assertEqual(studio.plain_text(json.dumps(dp)), "Hello <world>")
        only_elements = {"elements": [{"page": 1, "content": {"text": "First."}}, {"page": 2, "content": {"markdown": "Second."}}]}
        self.assertEqual(studio.plain_text(json.dumps(only_elements)), "First.\nSecond.")
        self.assertEqual(studio.plain_text("plain **markdown** text"), "plain **markdown** text")


if __name__ == "__main__":
    unittest.main()
