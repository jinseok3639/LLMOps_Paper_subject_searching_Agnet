"""연구 카드 테스트 (Solar, Studio, HTTP는 가짜). 실행: python -m unittest discover tests"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import calllog, cards, studio
from src.cards import AbstractCardOut, Quoted

ABSTRACT = ("We study “reward hacking” in reinforcement learning from human feedback. "
            "We propose a robust-\nness penalty that regularizes the policy. "
            "We evaluate on three benchmarks against PPO baselines.")
PAPER = {"paper_id": "s2abc", "title": "Paper", "abstract": ABSTRACT, "year": 2026}


def setUpModule():  # 호출 로그(src.calllog)는 임시 폴더에 쓴다
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    p = patch.object(calllog, "ROOT", Path(tmp.name))
    p.start()
    unittest.addModuleCleanup(p.stop)


ABSTRACT_OUT = AbstractCardOut(
    problem=Quoted(text="Reward hacking in RLHF.", quote='We study "reward hacking" in reinforcement learning'),
    method=Quoted(text="A penalty.", quote="We propose an invented method"),
    assumptions=Quoted(text="", quote=""),
    evaluation=Quoted(text="Benchmarks vs PPO.", quote=""),
    limitations=Quoted(text="", quote=""))

PARSE_TEXT = "Intro. Our goal is to detect reward hacking. We train a penalty model on preference data. Limits remain."
STUDIO_FINAL = {"title": "Studio title",
                "problem": {"text": "Detect reward hacking.", "quote": "Our goal is to detect reward hacking.", "page": 1},
                "method": {"text": "Penalty model.", "quote": "We train a penalty model on preference data.", "page": "3"},
                "assumptions": {"text": "", "quote": "", "page": 0},
                "evaluation": {"text": "Made up.", "quote": "We report 95% accuracy.", "page": 7},
                "limitations": {"text": "Limits.", "quote": "Limits remain.", "page": 9}}


def studio_result(final=STUDIO_FINAL, parse_text=PARSE_TEXT):
    return {"job_id": "job_1", "agent_id": "agt_test", "final": final, "parse_text": parse_text,
            "steps": [{"step": "step_1_parse"}, {"step": "step_2_extract"}], "elapsed_sec": 1.0, "raw": {"id": "job_1"}}


class TmpDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(cards, "ABSTRACT_CARD_DIR", root / "abstract_cards"),
                        patch.object(cards, "PAPER_PDF_DIR", root / "pdf"),
                        patch.object(cards, "STUDIO_CARD_DIR", root / "studio"),
                        patch.dict(os.environ, {"STUDIO_CARD_AGENT_ID": "agt_test"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()


class AbstractCardTest(TmpDirs):
    def test_quotes_checked_and_cached(self):
        with patch("src.cards.llm.chat_json", return_value=ABSTRACT_OUT) as chat:
            card = cards.abstract_card(PAPER)
            again = cards.abstract_card(PAPER)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(card, again)
        self.assertEqual(card["level"], "abstract")
        self.assertEqual(card["problem"]["text"], "Reward hacking in RLHF.")
        self.assertIsNone(card["problem"]["page"])
        self.assertEqual(card["method"], {"text": "", "quote": "", "page": None})      # 지어낸 인용
        self.assertEqual(card["evaluation"], {"text": "", "quote": "", "page": None})  # 인용 없는 text
        self.assertEqual(card["quote_failed"], ["method", "evaluation"])              # 원래 빈 필드는 실패 아님
        self.assertTrue((cards.ABSTRACT_CARD_DIR / "s2abc.json").exists())
        self.assertIn(ABSTRACT, chat.call_args.args[0][1]["content"])

    def test_latex_and_tags_are_cleaned_before_prompt_and_check(self):
        raw = ("<jats:p>One roadblock is \\textit{polysemanticity}, where neurons activate in many contexts. "
               "We localize features on the IOI task \\citep{wang2022}.</jats:p>")
        out = ABSTRACT_OUT.model_copy(update={
            "problem": Quoted(text="Polysemanticity.", quote="One roadblock is polysemanticity, where neurons activate in many contexts."),
            "method": Quoted(text="Localize.", quote="We localize features on the IOI task."),
            "evaluation": Quoted(text="", quote="")})
        with patch("src.cards.llm.chat_json", return_value=out) as chat:
            card = cards.abstract_card({**PAPER, "abstract": raw})
        self.assertEqual(card["quote_failed"], [])
        self.assertNotIn("\\textit", chat.call_args.args[0][1]["content"])
        self.assertNotIn("jats", chat.call_args.args[0][1]["content"])

    def test_no_abstract_makes_no_call(self):
        with patch("src.cards.llm.chat_json") as chat:
            card = cards.abstract_card({"paper_id": "x", "title": "T", "abstract": None})
        chat.assert_not_called()
        self.assertEqual(card["problem"]["text"], "")
        self.assertFalse((cards.ABSTRACT_CARD_DIR / "x.json").exists())

    def test_unsafe_paper_id_is_sanitized(self):
        with patch("src.cards.llm.chat_json", return_value=ABSTRACT_OUT):
            card = cards.abstract_card({**PAPER, "paper_id": "ARXIV:hep/123"})
        self.assertEqual(card["paper_id"], "ARXIV:hep/123")
        self.assertTrue((cards.ABSTRACT_CARD_DIR / "ARXIV_hep_123.json").exists())


class FulltextCardTest(TmpDirs):
    def test_card_from_studio_and_cached(self):
        pdf = cards.PAPER_PDF_DIR / "s2abc.pdf"
        with patch("src.cards.fetch_pdf", return_value=pdf) as fetch, \
                patch("src.cards.studio.run_agent", return_value=studio_result()) as run:
            card = cards.fulltext_card(PAPER, should_cancel=lambda: False)
            again = cards.fulltext_card(PAPER)
        self.assertEqual((fetch.call_count, run.call_count), (1, 1))
        self.assertEqual(card, again)
        self.assertEqual(run.call_args.kwargs["agent"], "agt_test")
        self.assertEqual((card["level"], card["title"]), ("fulltext", "Paper"))
        self.assertEqual(card["problem"]["page"], 1)
        self.assertEqual(card["method"]["page"], 3)
        self.assertEqual(card["assumptions"], {"text": "", "quote": "", "page": None})
        self.assertEqual(card["evaluation"], {"text": "", "quote": "", "page": None})
        self.assertEqual(card["quote_failed"], ["evaluation"])
        saved = json.loads((cards.STUDIO_CARD_DIR / "s2abc.card.json").read_text("utf-8"))
        self.assertEqual(saved["parse_text"], PARSE_TEXT)
        self.assertTrue((cards.STUDIO_CARD_DIR / "s2abc.raw.json").exists())

    def test_flat_and_wrapped_studio_output(self):
        flat = {"problem_text": "Detect.", "problem_quote": "Our goal is to detect reward hacking.", "problem_page": 2}
        for final in (flat, {"result": flat}):
            fields = cards._studio_fields(final)
            self.assertEqual((fields["problem"].quote, fields["problem"].page),
                             ("Our goal is to detect reward hacking.", 2))
            self.assertEqual(fields["method"].text, "")

    def test_br_in_studio_quote_is_removed_and_matches_parse_text(self):
        # 실제 Studio 응답(2205.01663): 파싱 HTML의 줄바꿈 <br>이 추출 인용에 <br/>로 딸려 온다
        final = {"problem_text": "Avoid<br/>failures.",
                 "problem_quote": "avoiding even a sin-<br/>gle catastrophic failure while in deployment.",
                 "problem_page": 1}
        fields = cards._studio_fields(final)
        self.assertEqual(fields["problem"].text, "Avoid failures.")
        self.assertEqual(fields["problem"].quote, "avoiding even a sin- gle catastrophic failure while in deployment.")
        source = cards.studio.plain_text("<p>goals: avoiding even a sin-<br>gle catastrophic failure while in deployment.</p>")
        card = cards._verified(PAPER, "Paper", "fulltext", fields, source)
        self.assertEqual(card["quote_failed"], [])
        self.assertEqual(card["problem"]["page"], 1)

    def test_no_pdf_returns_none(self):
        with patch("src.cards.fetch_pdf", return_value=None), patch("src.cards.studio.run_agent") as run:
            self.assertIsNone(cards.fulltext_card(PAPER))
        run.assert_not_called()

    def test_missing_agent_id_raises_before_download(self):
        with patch.dict(os.environ, {"STUDIO_CARD_AGENT_ID": ""}), patch("src.cards.fetch_pdf") as fetch:
            with self.assertRaises(studio.StudioUnavailable):
                cards.fulltext_card(PAPER)
        fetch.assert_not_called()

    def test_studio_error_propagates_without_cache(self):
        with patch("src.cards.fetch_pdf", return_value=Path("x.pdf")), \
                patch("src.cards.studio.run_agent", side_effect=studio.StudioError("failed")):
            with self.assertRaises(studio.StudioError):
                cards.fulltext_card(PAPER)
        self.assertFalse((cards.STUDIO_CARD_DIR / "s2abc.card.json").exists())

    def test_empty_parse_text_raises(self):
        with patch("src.cards.fetch_pdf", return_value=Path("x.pdf")), \
                patch("src.cards.studio.run_agent", return_value=studio_result(parse_text="")):
            with self.assertRaises(studio.StudioError):
                cards.fulltext_card(PAPER)
        self.assertFalse((cards.STUDIO_CARD_DIR / "s2abc.card.json").exists())


class Resp:
    def __init__(self, status=200, body=None, data=b""):
        self.status_code, self._body, self._data = status, body, data

    def json(self):
        return self._body

    def iter_content(self, size):
        yield self._data

    def close(self):
        pass


class FetchPdfTest(TmpDirs):
    def route(self, routes):
        self.urls = []

        def get(url, **kw):
            self.urls.append(url)
            for prefix, resp in routes.items():
                if url.startswith(prefix):
                    return resp
            raise AssertionError("unexpected " + url)
        return patch("src.cards.requests.get", side_effect=get)

    def test_open_access_url_from_paper(self):
        paper = {**PAPER, "openAccessPdf": {"url": "https://oa.example/p.pdf"}, "externalIds": {}}
        with self.route({"https://oa.example/": Resp(data=b"%PDF-1.7 body")}):
            path = cards.fetch_pdf(paper)
        self.assertEqual(self.urls, ["https://oa.example/p.pdf"])  # S2를 부르지 않음
        self.assertEqual(path.read_bytes(), b"%PDF-1.7 body")

    def test_s2_lookup_then_arxiv_when_oa_is_not_pdf(self):
        s2 = Resp(body={"openAccessPdf": {"url": "https://oa.example/landing"}, "externalIds": {"ArXiv": "2401.00001"}})
        with self.route({cards.S2_PAPER_URL: s2, "https://oa.example/": Resp(data=b"<html>"),
                         "https://arxiv.org/pdf/": Resp(data=b"%PDF-1.5 arxiv")}):
            path = cards.fetch_pdf(PAPER)
        self.assertEqual(self.urls[-1], "https://arxiv.org/pdf/2401.00001")
        self.assertEqual(path.name, "s2abc.pdf")
        with patch("src.cards.requests.get") as get:  # 두 번째는 저장된 파일
            self.assertEqual(cards.fetch_pdf(PAPER), path)
        get.assert_not_called()

    def test_no_pdf_available(self):
        s2 = Resp(body={"openAccessPdf": None, "externalIds": {"DOI": "10.1/x"}})
        with self.route({cards.S2_PAPER_URL: s2}):
            self.assertIsNone(cards.fetch_pdf(PAPER))
        with self.route({cards.S2_PAPER_URL: Resp(404, {"error": "not found"})}):
            self.assertIsNone(cards.fetch_pdf(PAPER))
        with self.route({"https://arxiv.org/pdf/": Resp(404)}):
            self.assertIsNone(cards.fetch_pdf({**PAPER, "openAccessPdf": None, "externalIds": {"ArXiv": "1"}}))
        self.assertEqual(list(cards.PAPER_PDF_DIR.glob("*")), [])  # PDF가 아닌 응답이나 임시 파일을 남기지 않음


if __name__ == "__main__":
    unittest.main()
