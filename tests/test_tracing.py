"""LangSmith 추적 테스트 (src/tracing.py, src/calllog.py 연결). LangSmith 클라이언트는 모의 처리한다.
실행: python -m unittest discover tests"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import server
from src import calllog, llm, tracing
from tests.test_calllog import KEY, Out, chat_resp

ON = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "ls_test_secret_0123456789", "UPSTAGE_API_KEY": KEY}


class Base(unittest.TestCase):
    env = ON

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ls = MagicMock()
        self.solar = MagicMock()
        self.solar.chat.completions.parse.return_value = chat_resp()
        for p in (patch.object(calllog, "ROOT", Path(tmp.name)), patch.dict(os.environ, self.env),
                  patch.object(tracing, "client", return_value=self.ls),
                  patch.object(llm, "client", return_value=self.solar)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(calllog._job.set, None)
        for table in (tracing._latest, tracing._by_run):
            table.clear()

    def created(self):
        return [c.kwargs for c in self.ls.create_run.call_args_list]

    def updated(self):
        return [c.kwargs for c in self.ls.update_run.call_args_list]


class Disabled(Base):
    env = {"LANGSMITH_TRACING": "", "LANGSMITH_API_KEY": "", "UPSTAGE_API_KEY": KEY}

    def test_nothing_sent_without_env(self):
        calllog.start("s1", "recommend")
        calllog.set_step("search")
        llm.chat_json([{"role": "user", "content": "u"}], Out)
        calllog.finish("ok", "r1")
        self.ls.create_run.assert_not_called()
        self.assertFalse(tracing.feedback("s1", "problem", "p1", "up", None))

    def test_tracing_needs_key_too(self):
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true"}):
            self.assertFalse(tracing.enabled())


class Enabled(Base):
    def test_job_step_and_calls_form_one_tree(self):
        calllog.start("s1", "research", "u1")
        calllog.set_step("generate")
        msgs = [{"role": "system", "content": "inline"}, {"role": "user", "content": f"key {KEY}"}]
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(calllog.bind(lambda _: llm.chat_json(msgs, Out)), range(3)))
        calllog.finish("ok", "run_1", output={"run_id": "run_1"})

        created = self.created()
        root, step, calls = created[0], created[1], created[2:]
        self.assertEqual((root["name"], root["run_type"]), ("research", "chain"))
        self.assertEqual(root["extra"]["metadata"]["session_id"], "s1")
        self.assertEqual(root["extra"]["metadata"]["user_id"], "u1")
        self.assertEqual((step["name"], step["parent_run_id"]), ("generate", root["id"]))
        self.assertEqual(len(calls), 3)
        for c in calls:
            self.assertEqual((c["run_type"], c["parent_run_id"], c["trace_id"]), ("llm", step["id"], root["id"]))
            self.assertNotIn(KEY, str(c["inputs"]))  # 키는 가려서 보낸다
        llm_updates = [u for u in self.updated() if (u.get("outputs") or {}).get("choices")]
        self.assertEqual(len(llm_updates), 3)
        self.assertEqual(llm_updates[0]["extra"]["metadata"]["usage_metadata"]["input_tokens"], 10)
        root_update = next(u for u in self.updated() if u["run_id"] == root["id"])
        self.assertEqual(root_update["outputs"]["run_id"], "run_1")
        self.assertIsNone(root_update["error"])
        self.assertEqual(tracing._by_run["run_1"], str(root["id"]))

    def test_step_change_closes_previous_span(self):
        calllog.start("s1", "recommend")
        calllog.set_step("query")
        calllog.set_step("query")  # 같은 단계는 새 span을 열지 않는다
        calllog.set_step("select")
        calllog.finish("ok")
        names = [c["name"] for c in self.created()]
        self.assertEqual(names, ["recommend", "query", "select"])
        self.assertEqual(len(self.updated()), 3)

    def test_failed_call_and_cancelled_job_marked_error(self):
        self.solar.chat.completions.create.side_effect = RuntimeError("boom")
        calllog.start("s1", "chat")
        with self.assertRaises(RuntimeError):
            llm.chat_text([{"role": "user", "content": "u"}])
        calllog.finish("cancelled")
        errors = [u["error"] for u in self.updated()]
        self.assertTrue(errors[0].startswith("error: RuntimeError"))
        self.assertTrue(errors[1].startswith("cancelled"))

    def test_calls_outside_job_are_not_sent(self):
        llm.chat_json([{"role": "user", "content": "u"}], Out)
        self.ls.create_run.assert_not_called()

    def test_langsmith_failure_does_not_stop_work(self):
        self.ls.create_run.side_effect = ConnectionError("down")
        with self.assertLogs("src.tracing", "WARNING"):
            calllog.start("s1", "recommend")
            self.assertEqual(llm.chat_json([{"role": "user", "content": "u"}], Out).x, 1)
            calllog.finish("ok")

    def test_feedback_attaches_to_trace(self):
        calllog.start("s1", "research")
        calllog.finish("ok", "run_9")
        calllog.start("s1", "recommend")
        calllog.finish("ok", "run_10")
        research, recommend = (str(c["id"]) for c in self.created())
        tracing._pool.submit(lambda: None).result()
        with TestClient(server.app) as client:
            for target, target_id in (("topic", "run_9:s1-assumption"), ("problem", "op_3"), ("final", "x")):
                r = client.post("/api/feedback", json={"session_id": "s1", "target": target, "target_id": target_id,
                                                       "value": "down", "comment": "별로"})
                self.assertEqual(r.status_code, 204)
        tracing._pool.submit(lambda: None).result()
        sent = [(c.args[0], c.kwargs["key"], c.kwargs["score"]) for c in self.ls.create_feedback.call_args_list]
        self.assertEqual(sent, [(research, "user_topic", 0), (recommend, "user_problem", 0)])  # final은 작업이 없다


if __name__ == "__main__":
    unittest.main()
