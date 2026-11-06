"""연구 카드 테스트 (Solar, Studio, HTTP는 가짜). 실행: python -m unittest discover tests"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from src import calllog, cards, paper_cache, studio
from src.clients import arxiv, semantic_scholar
from src.clients.ratequeue import RateQueue
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


class FakeRemote:
    """Supabase cache 테이블 대신. fail=True면 HTTP 오류."""

    def __init__(self, fail=False):
        self.rows, self.fail, self.gets, self.puts = {}, fail, [], []

    def get_many(self, kind, keys):
        self.gets.append((kind, list(keys)))
        if self.fail:
            raise requests.ConnectionError("down")
        return {k: self.rows[(kind, k)] for k in keys if (kind, k) in self.rows}

    def put(self, kind, key, data):
        self.puts.append((kind, key))
        if self.fail:
            raise requests.HTTPError("500")
        self.rows[(kind, key)] = json.loads(json.dumps(data))


class TmpDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(paper_cache, "backend", None),
                        patch.object(cards, "ABSTRACT_CARD_DIR", root / "abstract_cards"),
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


class RemoteCacheTest(TmpDirs):
    """로컬 파일이 지워져도(Render 재배포) Supabase cache 테이블에서 카드를 다시 읽는다."""

    def wipe_local(self):
        for folder in (cards.ABSTRACT_CARD_DIR, cards.STUDIO_CARD_DIR):
            for p in folder.glob("*") if folder.exists() else []:
                p.unlink()

    def test_abstract_card_survives_local_wipe(self):
        remote = FakeRemote()
        with patch.object(paper_cache, "backend", remote), \
                patch("src.cards.llm.chat_json", return_value=ABSTRACT_OUT) as chat:
            card = cards.abstract_card(PAPER)
            self.wipe_local()
            again = cards.abstract_card(PAPER)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(card, again)
        self.assertEqual(remote.puts, [("abstract_card", "s2abc")])
        self.assertTrue((cards.ABSTRACT_CARD_DIR / "s2abc.json").exists())  # 받아 온 카드를 로컬에도 둔다

    def test_studio_card_survives_local_wipe(self):
        remote = FakeRemote()
        with patch.object(paper_cache, "backend", remote), \
                patch("src.cards.fetch_pdf", return_value=cards.PAPER_PDF_DIR / "s2abc.pdf"), \
                patch("src.cards.studio.run_agent", return_value=studio_result()) as run:
            card = cards.fulltext_card(PAPER)
            self.wipe_local()
            again = cards.fulltext_card(PAPER)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(card, again)
        self.assertEqual(remote.rows[("studio_card", "s2abc")]["parse_text"], PARSE_TEXT)

    def test_prefetch_one_request_and_raw_paper_id(self):
        remote = FakeRemote()
        remote.rows[("abstract_card", "ARXIV:hep/123")] = {"paper_id": "ARXIV:hep/123", "level": "abstract"}
        papers = [{"paper_id": "ARXIV:hep/123"}, {"paper_id": "s2abc"}]
        with patch.object(paper_cache, "backend", remote):
            cards.prefetch(papers, "abstract")
            cards.prefetch(papers[:1], "abstract")  # 이미 로컬에 있으면 묻지 않는다
        self.assertEqual(remote.gets, [("abstract_card", ["ARXIV:hep/123", "s2abc"])])
        self.assertTrue((cards.ABSTRACT_CARD_DIR / "ARXIV_hep_123.json").exists())
        self.assertFalse((cards.ABSTRACT_CARD_DIR / "s2abc.json").exists())

    def test_supabase_failure_falls_back_to_local(self):
        remote = FakeRemote(fail=True)
        with patch.object(paper_cache, "backend", remote), \
                patch("src.cards.llm.chat_json", return_value=ABSTRACT_OUT) as chat, \
                self.assertLogs("src.paper_cache", "WARNING"):
            card = cards.abstract_card(PAPER)
            again = cards.abstract_card(PAPER)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(card, again)
        self.assertEqual(len(remote.puts), 1)

    def test_remote_request_format(self):
        remote = paper_cache.Remote("https://x.supabase.co/", "service")
        resp = unittest.mock.MagicMock(status_code=200, content=b"[]")
        resp.json.return_value = [{"key": 'a"b', "json": {"x": 1}}]
        with patch("src.paper_cache.requests.get", return_value=resp) as get, \
                patch("src.paper_cache.requests.post", return_value=resp) as post:
            self.assertEqual(remote.get_many("studio_card", ['a"b', "c"]), {'a"b': {"x": 1}})
            remote.put("studio_card", "c", {"y": 2})
        self.assertEqual(get.call_args.args[0], "https://x.supabase.co/rest/v1/cache")
        self.assertEqual(get.call_args.kwargs["params"]["key"], 'in.("a\\"b","c")')
        self.assertEqual(post.call_args.kwargs["params"], {"on_conflict": "kind,key"})
        self.assertIn("merge-duplicates", post.call_args.kwargs["headers"]["Prefer"])


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

    def test_no_pdf_raises_without_studio_call(self):
        with patch("src.cards.fetch_pdf", side_effect=cards.NoPdf("not_pdf")), patch("src.cards.studio.run_agent") as run:
            with self.assertRaises(cards.NoPdf):
                cards.fulltext_card(PAPER)
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
    def __init__(self, status=200, body=None, data=b"", text=""):
        self.status_code, self._body, self._data, self.text = status, body, data, text

    def json(self):
        return self._body

    def iter_content(self, size):
        yield self._data

    def close(self):
        pass


S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/"
ARXIV_API = "https://export.arxiv.org/api/query"


def atom(*entries):
    """arXiv API 응답. entries: (arXiv ID, 제목)."""
    items = "".join(f"<entry><id>http://arxiv.org/abs/{i}v2</id><title>{t}</title></entry>" for i, t in entries)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>'


class FetchPdfTest(TmpDirs):
    def setUp(self):
        super().setUp()
        for p in (patch.object(semantic_scholar, "INTERVAL_KEY", 0), patch.object(semantic_scholar, "INTERVAL_PUBLIC", 0),
                  patch.object(semantic_scholar, "BACKOFF", 0), patch.object(arxiv, "INTERVAL", 0)):
            p.start()
            self.addCleanup(p.stop)

    def route(self, routes):
        """URL 앞부분 → 응답 (목록이면 차례로). S2, arXiv, PDF 요청은 모두 requests.get 하나를 지난다."""
        self.urls = []
        routes = {k: list(v) if isinstance(v, list) else v for k, v in routes.items()}

        def get(url, **kw):
            self.urls.append(url)
            for prefix, resp in routes.items():
                if url.startswith(prefix):
                    return resp.pop(0) if isinstance(resp, list) else resp
            raise AssertionError("unexpected " + url)
        return patch("src.cards.requests.get", side_effect=get)

    def test_open_access_url_from_paper(self):
        paper = {**PAPER, "openAccessPdf": {"url": "https://oa.example/p.pdf"}, "externalIds": {"ArXiv": "2401.1"}}
        with self.route({"https://oa.example/": Resp(data=b"%PDF-1.7 body")}):
            path = cards.fetch_pdf(paper)
        self.assertEqual(self.urls, ["https://oa.example/p.pdf"])  # S2도 arXiv 검색도 부르지 않음
        self.assertEqual(path.read_bytes(), b"%PDF-1.7 body")

    def test_s2_lookup_then_arxiv_when_oa_is_not_pdf(self):
        s2 = Resp(body={"openAccessPdf": {"url": "https://oa.example/landing"}, "externalIds": {"ArXiv": "2401.00001"}})
        with self.route({S2_PAPER: s2, "https://oa.example/": Resp(data=b"<html>"),
                         "https://arxiv.org/pdf/": Resp(data=b"%PDF-1.5 arxiv")}):
            path = cards.fetch_pdf(PAPER)
        self.assertEqual(self.urls[-1], "https://arxiv.org/pdf/2401.00001")
        self.assertEqual(path.name, "s2abc.pdf")
        with patch("src.cards.requests.get") as get:  # 두 번째는 저장된 파일
            self.assertEqual(cards.fetch_pdf(PAPER), path)
        get.assert_not_called()

    def test_s2_retries_429_then_succeeds(self):
        s2 = [Resp(429, text="Too Many Requests"),
              Resp(body={"openAccessPdf": None, "externalIds": {"ArXiv": "2401.00002"}})]
        with self.route({S2_PAPER: s2, "https://arxiv.org/pdf/": Resp(data=b"%PDF-1.5")}):
            cards.fetch_pdf(PAPER)
        self.assertEqual(sum(u.startswith(S2_PAPER) for u in self.urls), 2)

    def test_arxiv_title_search_when_s2_fails(self):
        paper = {**PAPER, "title": "Detecting Backdoor Attacks via Similarity in Semantic Communication Systems"}
        found = atom(("2502.03721", "Detecting Backdoor Attacks via Similarity\n  in Semantic Communication Systems"))
        with self.route({S2_PAPER: Resp(429), ARXIV_API: Resp(text=found),
                         "https://arxiv.org/pdf/": Resp(data=b"%PDF-1.5 preprint")}):
            path = cards.fetch_pdf(paper)
        self.assertEqual(sum(u.startswith(S2_PAPER) for u in self.urls), cards.S2_TRIES)
        self.assertEqual(self.urls[-1], "https://arxiv.org/pdf/2502.03721")  # 버전은 뗀다
        self.assertEqual(path.read_bytes(), b"%PDF-1.5 preprint")

    def test_arxiv_title_search_after_blocked_download(self):
        paper = {**PAPER, "title": "Securing LLM-based agents: a survey",
                 "openAccessPdf": {"url": "https://publisher.example/p.pdf"}, "externalIds": {"DOI": "10.1/x"}}
        found = atom(("2601.00009", "Securing LLM based Agents: A Survey"))  # 대소문자·기호만 다름
        with self.route({"https://publisher.example/": Resp(403), ARXIV_API: Resp(text=found),
                         "https://arxiv.org/pdf/": Resp(data=b"%PDF-1.5")}):
            cards.fetch_pdf(paper)
        self.assertEqual(self.urls[-1], "https://arxiv.org/pdf/2601.00009")

    def test_arxiv_title_must_match(self):
        paper = {**PAPER, "title": "Semantic-Aware Framework for Backdoor Detection in AI Models",
                 "openAccessPdf": None, "externalIds": {"DOI": "10.4018/x"}}
        similar = atom(("2501.00001", "Semantic-Aware Framework for Backdoor Detection in Vision Models"))
        with self.route({ARXIV_API: Resp(text=similar)}):
            with self.assertRaises(cards.NoPdf) as e:
                cards.fetch_pdf(paper)
        self.assertEqual(e.exception.reason, "no_url")
        self.assertFalse(any(u.startswith("https://arxiv.org/pdf/") for u in self.urls))

    def test_no_pdf_reasons(self):
        no_arxiv = {ARXIV_API: Resp(text=atom())}
        cases = [
            ({**PAPER, "openAccessPdf": None, "externalIds": {"DOI": "10.1/x"}}, {}, "no_url"),
            (PAPER, {S2_PAPER: Resp(404, {"error": "not found"})}, "no_url"),
            (PAPER, {S2_PAPER: Resp(503)}, "s2_failed"),
            ({**PAPER, "openAccessPdf": {"url": "https://igi.example/ViewTitle"}, "externalIds": {}},
             {"https://igi.example/": Resp(data=b"<html>landing</html>")}, "not_pdf"),
            ({**PAPER, "openAccessPdf": {"url": "https://publisher.example/p.pdf"}, "externalIds": {}},
             {"https://publisher.example/": Resp(403)}, "download_failed"),
            ({**PAPER, "openAccessPdf": None, "externalIds": {"ArXiv": "1"}}, {"https://arxiv.org/pdf/": Resp(404)},
             "download_failed"),
        ]
        for paper, routes, reason in cases:
            with self.subTest(reason=reason, routes=list(routes)), self.route({**no_arxiv, **routes}):
                with self.assertRaises(cards.NoPdf) as e:
                    cards.fetch_pdf(paper)
                self.assertEqual(e.exception.reason, reason)
        self.assertEqual(list(cards.PAPER_PDF_DIR.glob("*")), [])  # PDF가 아닌 응답이나 임시 파일을 남기지 않음

    def test_arxiv_api_failure_is_not_fatal(self):
        paper = {**PAPER, "title": "A long enough paper title", "openAccessPdf": None, "externalIds": {}}
        with self.route({ARXIV_API: Resp(503, text="busy")}):
            with self.assertRaises(cards.NoPdf) as e:
                cards.fetch_pdf(paper)
        self.assertEqual(e.exception.reason, "no_url")
        self.assertEqual(len(self.urls), arxiv.TRIES)


class ArxivTest(unittest.TestCase):
    def test_normalize_and_parse(self):
        self.assertEqual(arxiv.normalize_title("LLM-based Agents:  A  Survey!"), "llm based agents a survey")
        self.assertEqual(arxiv._entries(atom(("2401.1", "T"), ("hep-th/9901001", "U"))),
                         [("2401.1", "T"), ("hep-th/9901001", "U")])
        with self.assertRaises(arxiv.ArxivError):
            arxiv._entries("<not xml")

    def test_short_title_is_not_searched(self):
        with patch("src.clients.arxiv.requests.get") as get:
            self.assertIsNone(arxiv.find_by_title("Survey"))
        get.assert_not_called()


class QueueTest(unittest.TestCase):
    def test_requests_pass_in_order_with_interval(self):
        q, times, order = RateQueue(), [], []

        def call(i):
            q.pass_through(0.05)
            times.append(time.monotonic())
            order.append(i)

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=call, args=(i,)))
            threads[-1].start()
            time.sleep(0.01)  # 번호표를 i 순서로 받게
        for t in threads:
            t.join()
        self.assertEqual(order, [0, 1, 2, 3])
        self.assertTrue(all(b - a >= 0.045 for a, b in zip(times, times[1:])))
        self.assertEqual(q.waiting(), 0)

    def test_pause_holds_everyone(self):
        q = RateQueue()
        q.pass_through(0)
        q.pause(0.1)
        t0 = time.monotonic()
        q.pass_through(0)
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)


if __name__ == "__main__":
    unittest.main()
