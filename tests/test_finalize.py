"""⑤ 확정 테스트: 카드 조립, 마크다운, /api/finalize. LLM 호출 없음. 실행: python -m unittest discover tests"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server
from src import calllog, config, runs, store
from src.finalize import build_card, to_markdown


def setUpModule():  # 호출 로그(src.calllog)는 임시 폴더에 쓴다
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    for p in (patch.object(calllog, "ROOT", Path(tmp.name)),  # 로그인 저장소도 Supabase 대신 임시 폴더
              patch.object(store, "backend", store.LocalStore(Path(tmp.name) / "users"))):
        p.start()
        unittest.addModuleCleanup(p.stop)

PAPER = {"paper_id": "2109.13916v5", "title": "Unsolved Problems in ML Safety", "year": 2021, "url": "https://arxiv.org/abs/2109.13916"}
PROBLEM = {"problem_id": "2109.13916v5:5.1:3", "title": "Backdoor detection", "summary": "문제 요약", "quote": "We need detectors.",
           "page": 12, "section_title": "Backdoors", "paper": PAPER}
RECOMMEND = {"run_id": "r_" + "1" * 32, "kind": "recommend", "status": "ok",
             "recommendations": [{**PROBLEM, "reason": "연구실 주제와 맞음", "connection": ""}]}
RESEARCH = {"run_id": "r_" + "2" * 32, "kind": "research", "status": "ok", "problem_id": PROBLEM["problem_id"],
            "problem_title": "Backdoor detection", "created_at": "2026-11-05T10:00:00+00:00",
            "topics": [{"id": "c1", "title": "Round-wise | backdoor audit", "summary": "후보 요약", "gap": "공백",
                        "approach": "방법", "evaluation": "평가", "support": ["ref-001", "ref-009"]}],
            "references": [{"id": "ref-001", "paper_id": "s1", "title": "Ref One", "year": 2025, "url": "https://x.org/1",
                            "abstract": "a", "query": "backdoor detection llm"}],
            "trace": [{"query": "backdoor detection llm", "returned": 20}, {"query": "trigger inversion agents", "returned": 0},
                      {"step": "relevance_filter", "candidates": 18}]}
ELEMENT = lambda match, quote="": {"match": match, "reason": "이유 | 있음", "quote": quote, "page": 3 if quote else None,
                                   "level": "fulltext"}
VERIFY = {"run_id": "r_" + "3" * 32, "kind": "verify", "status": "ok", "topic_id": "c1", "research_run_id": RESEARCH["run_id"],
          "topic_title": "Round-wise | backdoor audit", "problem_id": PROBLEM["problem_id"],
          "verdict": "partial", "verdict_basis": ["p1"], "flags": ["초록 수준"],
          "nearest": [{"id": "N1", "paper_id": "p1", "title": "Near One", "year": 2024, "url": "https://x.org/n1",
                       "level": "fulltext", "fulltext_status": "ok",
                       "comparison": {"problem": ELEMENT("same", "We detect backdoors."), "method": ELEMENT("partial"),
                                      "assumptions": ELEMENT("different"), "evaluation": {}},
                       "card": {"level": "fulltext", "limitations": {"text": "한계", "quote": "Only one model.", "page": 9}}},
                      {"id": "N2", "paper_id": "p2", "title": "Near Two", "year": 2023, "url": "", "level": "abstract",
                       "fulltext_status": "no_pdf: not_pdf",
                       "comparison": {k: ELEMENT("unknown") for k in ("problem", "method", "assumptions", "evaluation")},
                       "card": {"level": "abstract", "limitations": {"text": "", "quote": "", "page": None}}}],
          "limitations": [{"id": "N1", "paper_id": "p1", "title": "Near One", "level": "fulltext", "text": "한계",
                           "quote": "Only one model.", "page": 9}],
          "critique": {"requirements": {"problem_scope": {"status": "defined", "note": "범위 있음"},
                                        "mechanism": {"status": "undecided", "note": "메커니즘 미정"}},
                       "unsupported": "", "vague": "절차 불명확", "already_done": "", "suggestion": "최소 판별 실험"},
          "scope": {"queries": ["q one", "q two", "q three"], "search_returned": 60, "searched": 40, "from_references": 25,
                    "narrowed": 15, "fulltext_read": 1, "date": "2026-11-05"},
          "budget": None}


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if not line.startswith(":"))
        if fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


class CardTests(unittest.TestCase):
    def test_card_rows_with_verify(self):
        card = build_card(RECOMMEND, PROBLEM, RESEARCH, "c1", VERIFY)
        self.assertEqual(list(card), ["topic", "problem", "design", "nearest", "verdict", "gaps", "critique", "scope", "source"])
        self.assertEqual(card["topic"], {"id": "c1", "title": "Round-wise | backdoor audit", "summary": "후보 요약", "version": 1})
        self.assertEqual((card["problem"]["title"], card["problem"]["reason"], card["problem"]["paper"]["title"]),
                         ("Backdoor detection", "연구실 주제와 맞음", PAPER["title"]))
        self.assertEqual(card["design"]["approach"], "방법")
        self.assertEqual([r["title"] for r in card["design"]["support"]], ["Ref One", None])  # 없는 참고문헌도 id는 남김
        n1, n2 = card["nearest"]
        self.assertEqual((n1["comparison"]["problem"]["match"], n1["comparison"]["problem"]["quote"]), ("same", "We detect backdoors."))
        self.assertEqual(n1["comparison"]["evaluation"]["match"], "unknown")  # 빠진 요소는 확인 못 함
        self.assertEqual(n2["level"], "abstract")
        self.assertEqual((n1["unread_reason"], n2["unread_reason"]), ("", "PDF 대신 웹페이지가 옴"))
        v = card["verdict"]
        self.assertEqual((v["verdict"], v["flags"], v["basis"][0]["id"]), ("partial", ["초록 수준"], "N1"))
        self.assertIn("본문 1편, 초록 1편", v["read_level"])
        self.assertEqual([(g["id"], g["quote"], g["page"]) for g in card["gaps"]], [("N1", "Only one model.", 9)])
        req = card["critique"]["requirements"]
        self.assertEqual(len(req), 7)
        self.assertEqual((req["problem_scope"]["status"], req["difference"]["status"]), ("defined", ""))
        self.assertEqual(card["critique"]["suggestion"], "최소 판별 실험")
        self.assertEqual((card["scope"]["stage"], card["scope"]["searched"], card["scope"]["fulltext_read"],
                          card["scope"]["references"], card["scope"]["date"]), ("verify", 40, 1, 1, "2026-11-05"))
        self.assertEqual(card["source"], {"recommend_run_id": RECOMMEND["run_id"], "problem_id": PROBLEM["problem_id"],
                                          "research_run_id": RESEARCH["run_id"], "topic_id": "c1",
                                          "revision_run_id": None, "verify_run_id": VERIFY["run_id"]})

    def test_card_without_verify(self):
        card = build_card(None, PROBLEM, RESEARCH, "c1", None)
        self.assertEqual((card["verdict"]["verdict"], card["verdict"]["label"]), ("not_verified", "검증 안 함"))
        self.assertEqual((card["nearest"], card["gaps"], card["critique"]), ([], [], None))
        self.assertEqual(card["problem"]["reason"], "")
        sc = card["scope"]
        self.assertEqual((sc["stage"], sc["queries"], sc["search_returned"], sc["searched"], sc["fulltext_read"], sc["date"]),
                         ("research", ["backdoor detection llm", "trigger inversion agents"], 20, 18, 0, "2026-11-05"))
        self.assertIsNone(card["source"]["verify_run_id"])

    def test_tolerates_missing_verify_keys(self):
        v = {"run_id": "r_x", "status": "budget", "topic_id": "c1", "nearest": [{**VERIFY["nearest"][0], "comparison": None}]}
        del v["nearest"][0]["level"]
        card = build_card(RECOMMEND, PROBLEM, RESEARCH, "c1", v)
        self.assertEqual((card["verdict"]["verdict"], card["verdict"]["label"]), ("none", "판정 없음"))
        self.assertEqual(card["nearest"][0]["level"], "fulltext")  # 카드의 level로 채움
        self.assertEqual(card["gaps"][0]["quote"], "Only one model.")  # limitations가 없으면 카드에서
        self.assertIsNone(card["critique"])
        self.assertEqual(card["scope"]["stage"], "research")
        self.assertIn("호출 예산 초과", to_markdown(card))

    def test_unread_reason(self):
        from src.finalize import unread_reason
        self.assertEqual([unread_reason(s) for s in (None, "ok", "no_pdf", "no_pdf: s2_failed", "no_pdf: other",
                                                     "studio_unavailable", "studio_error: job failed")],
                         ["", "", "PDF를 받지 못함", "논문 정보 조회 실패", "PDF를 받지 못함", "본문 분석 설정 없음", "본문 분석 실패"])

    def test_unknown_topic(self):
        with self.assertRaises(KeyError):
            build_card(RECOMMEND, PROBLEM, RESEARCH, "c9", None)

    def test_markdown_has_rows(self):
        md = to_markdown(build_card(RECOMMEND, PROBLEM, RESEARCH, "c1", VERIFY))
        for text in ("# Round-wise | backdoor audit", "## 출발 문제", "## 연구 설계", "## 가장 가까운 연구", "## 판정",
                     "## 빈틈", "## 비평", "## 조사 범위", "일부 겹침 (partial)", "초록 수준",
                     "[Near One](<https://x.org/n1>)", '| 문제 | 같음 | 이유 \\| 있음 | "We detect backdoors." (p.3) |',
                     '"Only one model." (p.9)', "| 핵심 메커니즘 | 미정 | 메커니즘 미정 |", "최소 판별 실험",
                     "q one / q two / q three", "본문까지 읽은 논문 1편", "2026-11-05",
                     "Near Two (2023) · 초록 수준 (본문 못 읽음: PDF 대신 웹페이지가 옴)", "원문 링크에서 확인하세요"):
            self.assertIn(text, md)
        md = to_markdown(build_card(None, PROBLEM, RESEARCH, "c1", None))
        self.assertIn("**검증 안 함**", md)
        self.assertIn("검증을 하지 않아 비교한 논문이 없습니다.", md)


class ApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=config.DB_DIR)
        self.addCleanup(tmp.cleanup)
        card = lambda pid: copy.deepcopy(PROBLEM) if pid == PROBLEM["problem_id"] else None
        for target, value in (("src.runs.RUNS_DIR", Path(tmp.name)), ("src.api.finalize.problem_card", card)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(server.app)
        strip = lambda run: {k: v for k, v in run.items() if k not in ("run_id", "kind")}  # runs.save가 새로 붙인다
        self.ids = {"recommend": runs.save("recommend", strip(RECOMMEND), "s1"),
                    "research": runs.save("research", strip(RESEARCH), "s1")}
        self.ids["verify"] = runs.save("verify", {**strip(VERIFY), "research_run_id": self.ids["research"]}, "s1")

    def body(self, **over):
        return {"session_id": "s1", "recommend_run_id": self.ids["recommend"], "problem_id": PROBLEM["problem_id"],
                "research_run_id": self.ids["research"], "topic_id": "c1", "verify_run_id": self.ids["verify"], **over}

    def test_routes_and_module_are_served(self):
        self.assertIn("/api/finalize", self.client.get("/openapi.json").json()["paths"])
        self.assertIn('<script defer src="/modules/finalize.js"></script>', self.client.get("/").text)

    def test_streams_progress_then_result_and_saves_final_run(self):
        r = self.client.post("/api/finalize", json=self.body())
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        got = events(r.text)
        self.assertEqual([e for e, _ in got], ["progress", "result"])
        self.assertTrue(all(d["session_id"] == "s1" for _, d in got))
        result = got[-1][1]
        self.assertEqual(result["card"]["verdict"]["verdict"], "partial")
        self.assertEqual(result["card"]["source"]["verify_run_id"], self.ids["verify"])
        self.assertIn("## 판정", result["markdown"])
        stored = self.client.get(f"/api/runs/{result['run_id']}").json()
        self.assertEqual((stored["kind"], stored["owner_session"]), ("final", "s1"))
        self.assertEqual((stored["card"], stored["markdown"]), (result["card"], result["markdown"]))

    def test_without_verify(self):
        result = events(self.client.post("/api/finalize", json=self.body(verify_run_id="", recommend_run_id="")).text)[-1]
        self.assertEqual((result[0], result[1]["card"]["verdict"]["label"]), ("result", "검증 안 함"))

    def test_errors(self):
        r = self.client.post("/api/finalize", json={"session_id": "s1"})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (422, ["research_run_id", "topic_id"]))
        cases = [{"research_run_id": "r_" + "9" * 32}, {"research_run_id": self.ids["recommend"]}, {"topic_id": "c9"},
                 {"recommend_run_id": "r_" + "9" * 32}, {"verify_run_id": "r_" + "9" * 32},
                 {"verify_run_id": self.ids["research"]}, {"problem_id": "nope"}]
        for over in cases:
            r = self.client.post("/api/finalize", json=self.body(**over))
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"), over)
        self.assertEqual(sorted(p.name.split(".")[0] for p in runs.RUNS_DIR.glob("*.json")), sorted(self.ids.values()))

    def test_verify_of_another_topic_is_rejected(self):
        path = runs.RUNS_DIR / f"{self.ids['verify']}.json"
        data = json.loads(path.read_text("utf-8"))
        path.write_text(json.dumps({**data, "topic_id": "c2"}, ensure_ascii=False), "utf-8")
        r = self.client.post("/api/finalize", json=self.body())
        self.assertEqual(r.status_code, 404)
        self.assertIn("검증 결과가 아닙니다", r.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
