"""후보 고치기 테스트 (이슈 #12): 수정안, /api/revise, 고친 후보로 ④ 검증·⑤ 확정, 채팅 컨텍스트. LLM은 가짜.
실행: python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import calllog, chat, revise, runs, store
from src.finalize import build_card, to_markdown

PROFILE = {"lab_name": "보안 연구실", "lab_topics": "AI 보안", "interests": ""}
PROBLEM = {"problem_id": "p1", "title": "Reward hacking"}
TOPICS = [{"id": "c1", "title": "후보 1", "summary": "요약 1", "gap": "공백 1", "approach": "70B 파인튜닝",
           "evaluation": "MovieLens", "support": ["ref-001"]},
          {"id": "c2", "title": "후보 2", "summary": "요약 2", "gap": "공백 2", "approach": "방법 2", "evaluation": "평가 2"}]
MESSAGES = [{"role": "user", "content": "GPU가 1장이라 7B LoRA로 바꾸고 평가는 인용 그래프로 해 줘"}]


def setUpModule():  # 호출 로그(src.calllog)와 로그인 저장소는 임시 폴더
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    for p in (patch.object(calllog, "ROOT", Path(tmp.name)),
              patch.object(store, "backend", store.LocalStore(Path(tmp.name) / "users"))):
        p.start()
        unittest.addModuleCleanup(p.stop)


def proposal(**fields):
    return revise.Proposal(**{"title": "", "summary": "", "gap": "", "approach": "", "evaluation": "", "reason": "요청대로",
                              **fields})


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


class RunsDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(runs, "RUNS_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.research_id = runs.save("research", {"problem_id": "p1", "topics": TOPICS, "references": []}, "s1")
        self.research = runs.load(self.research_id)

    def revision(self, topic_id="c1", version=2, **fields):
        topic = {**{k: TOPICS[0].get(k, "") for k in revise.FIELDS}, **fields}
        return runs.save("revision", {"research_run_id": self.research_id, "topic_id": topic_id, "version": version,
                                      "base_version": version - 1, "topic": topic, "changes": [], "reason": ""}, "s1")


class ResolveTests(RunsDirTest):
    def test_original_is_v1(self):
        topic, version = revise.resolve(self.research, "c1")
        self.assertEqual((topic["approach"], version), ("70B 파인튜닝", 1))
        self.assertEqual(revise.resolve(self.research, "nope"), (None, 1))

    def test_revision_overrides_fields_and_keeps_support(self):
        rev = runs.load(self.revision(approach="7B LoRA"))
        topic, version = revise.resolve(self.research, "c1", rev)
        self.assertEqual((topic["approach"], topic["evaluation"], topic["support"], version), ("7B LoRA", "MovieLens", ["ref-001"], 2))

    def test_revision_of_other_topic_is_rejected(self):
        rev = runs.load(self.revision(topic_id="c2"))
        with self.assertRaises(ValueError):
            revise.resolve(self.research, "c1", rev)


class ProposeTests(unittest.TestCase):
    def test_keeps_only_changed_fields(self):
        got = proposal(title="후보 1", approach="7B LoRA", evaluation="  ")  # 같은 값, 공백은 바뀐 것이 아니다
        with patch("src.revise.llm.chat_json", return_value=got) as llm:
            out = revise.propose(PROFILE, TOPICS[0], MESSAGES)
        self.assertEqual(out["changes"], [{"field": "approach", "before": "70B 파인튜닝", "after": "7B LoRA"}])
        self.assertEqual((out["topic"]["approach"], out["topic"]["evaluation"]), ("7B LoRA", "MovieLens"))
        user = llm.call_args[0][0][1]["content"]
        self.assertIn("7B LoRA로 바꾸고", user)
        self.assertNotIn("Verification result", user)

    def test_passes_verify_result(self):
        verify_run = {"verdict": "solved", "nearest": [{"id": "N1", "title": "Near", "year": 2024,
                                                        "comparison": {"method": {"match": "same", "reason": "같은 방법"}}}],
                      "critique": {"suggestion": "평가를 바꿔라"}}
        with patch("src.revise.llm.chat_json", return_value=proposal()) as llm:
            out = revise.propose(PROFILE, TOPICS[0], MESSAGES, verify_run)
        self.assertEqual(out["changes"], [])
        user = llm.call_args[0][0][1]["content"]
        self.assertIn("[N1] Near (2024)", user)
        self.assertIn("method: same - 같은 방법", user)
        self.assertIn("critique.suggestion: 평가를 바꿔라", user)


class ReviseApiTests(RunsDirTest):
    def setUp(self):
        super().setUp()
        self.client = TestClient(server.app)
        self.body = {"session_id": "s1", "profile": PROFILE, "research_run_id": self.research_id, "topic_id": "c1",
                     "messages": MESSAGES}

    def post(self, got, **body):
        with patch("src.revise.llm.chat_json", return_value=got):
            r = self.client.post("/api/revise", json={**self.body, **body})
        return r, events(r.text) if r.status_code == 200 else None

    def test_saves_revision_and_counts_versions(self):
        _, ev = self.post(proposal(approach="7B LoRA"))
        kind, d = ev[-1]
        self.assertEqual((kind, d["version"], d["base_version"], d["base_run_id"]), ("result", 2, 1, None))
        saved = runs.load(d["run_id"])
        self.assertEqual((saved["kind"], saved["topic"]["approach"], saved["topic_id"]), ("revision", "7B LoRA", "c1"))

        _, ev = self.post(proposal(evaluation="인용 그래프"), revision_run_id=d["run_id"])  # v2에서 다시 고친다
        d2 = ev[-1][1]
        self.assertEqual((d2["version"], d2["base_run_id"]), (3, d["run_id"]))
        self.assertEqual((d2["topic"]["approach"], d2["topic"]["evaluation"]), ("7B LoRA", "인용 그래프"))
        self.assertEqual(d2["changes"], [{"field": "evaluation", "before": "MovieLens", "after": "인용 그래프"}])

    def test_no_change_is_not_saved(self):
        _, ev = self.post(proposal(reason="무엇을 바꿀지 모르겠습니다"))
        kind, d = ev[-1]
        self.assertEqual((kind, d["run_id"], d["changes"], d["reason"]), ("result", None, [], "무엇을 바꿀지 모르겠습니다"))

    def test_rejects_revision_of_other_topic_and_wrong_verify(self):
        r, _ = self.post(proposal(), revision_run_id=self.revision(topic_id="c2"))
        self.assertEqual(r.status_code, 404)
        other = runs.save("verify", {"topic_id": "c2", "research_run_id": self.research_id}, "s1")
        r, _ = self.post(proposal(), verify_run_id=other)
        self.assertEqual(r.status_code, 404)


class VersionedVerifyFinalizeTests(RunsDirTest):
    def setUp(self):
        super().setUp()
        self.client = TestClient(server.app)
        self.rev_id = self.revision(approach="7B LoRA", evaluation="인용 그래프")

    def test_verify_checks_revised_topic_and_records_version(self):
        seen = {}

        def fake(profile, problem, topic, refs, progress, should_cancel):
            seen.update(topic)
            return {"status": "ok", "topic_id": topic["id"], "verdict": "partial", "nearest": [], "budget": None, "trace": {}}

        body = {"session_id": "s1", "profile": PROFILE, "problem_id": "p1", "research_run_id": self.research_id,
                "topic_id": "c1", "revision_run_id": self.rev_id}
        with patch("src.api.verify.verify", side_effect=fake), \
                patch("src.api.verify.problem_card", side_effect=lambda pid: PROBLEM if pid == "p1" else None):
            r = self.client.post("/api/verify", json=body)
        kind, d = events(r.text)[-1]
        self.assertEqual((kind, seen["approach"], seen["evaluation"]), ("result", "7B LoRA", "인용 그래프"))
        self.assertEqual((d["topic_version"], d["revision_run_id"]), (2, self.rev_id))
        self.assertEqual(runs.load(d["run_id"])["topic_version"], 2)

    def test_finalize_with_older_verdict_is_allowed_and_marked(self):
        verify_id = runs.save("verify", {"topic_id": "c1", "research_run_id": self.research_id, "topic_version": 1,
                                         "status": "ok", "verdict": "partial", "nearest": []}, "s1")
        body = {"session_id": "s1", "problem_id": "", "research_run_id": self.research_id, "topic_id": "c1",
                "revision_run_id": self.rev_id, "verify_run_id": verify_id}
        with patch("src.api.finalize.problem_card", return_value=PROBLEM):
            r = self.client.post("/api/finalize", json=body)
        kind, d = events(r.text)[-1]
        card = d["card"]
        self.assertEqual(kind, "result")
        self.assertEqual((card["topic"]["version"], card["design"]["approach"], card["verdict"]["topic_version"]),
                         (2, "7B LoRA", 1))
        self.assertEqual(card["source"]["revision_run_id"], self.rev_id)
        self.assertIn("## 연구 설계 (v2, ③에서 고침)", d["markdown"])
        self.assertIn("이 판정은 v1 후보 기준입니다", d["markdown"])

    def test_same_version_has_no_warning(self):
        card = build_card(None, PROBLEM, self.research, "c1",
                          {"topic_id": "c1", "topic_version": 2, "status": "ok", "verdict": "partial"},
                          runs.load(self.rev_id))
        self.assertNotIn("후보 기준입니다", to_markdown(card))
        self.assertNotIn("(v", to_markdown(build_card(None, PROBLEM, self.research, "c1")).split("## 연구 설계")[1][:5])


class ChatContextTests(RunsDirTest):
    def test_context_shows_current_version(self):
        rev = runs.load(self.revision(approach="7B LoRA"))
        with patch("src.chat.load_index", return_value=({}, None, None)):
            text = chat.build_context(PROFILE, None, self.research, {"topic_id": "c1"}, rev)
            other = chat.build_context(PROFILE, None, self.research, {"topic_id": "c2"}, rev)
        self.assertIn("지금 버전: v2", text)
        self.assertIn("7B LoRA", text.split("지금 버전")[1])
        self.assertNotIn("지금 버전", other)  # 다른 후보의 수정안은 무시


if __name__ == "__main__":
    unittest.main()
