"""채팅 문헌 조회 테스트 (이슈 #36). 네트워크 없이 돈다. 실행: python -m unittest discover tests"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import calllog, chat
from src.agents.tools import chat_toolbox

RESEARCH = {"topics": [{"id": "s1-a", "title": "후보 A", "support": ["ref-001"]}],
            "references": [
                {"id": "ref-001", "paper_id": "a" * 40, "title": "Cited Paper", "year": "2024", "abstract": "cited abs",
                 "url": "https://www.semanticscholar.org/paper/" + "a" * 40},
                {"id": "ref-002", "paper_id": "b" * 40, "title": "Bypassing Backdoor Detection Algorithms in Deep Learning",
                 "year": "2019", "abstract": "hidden abs", "url": "https://www.semanticscholar.org/paper/" + "b" * 40}]}
VERIFY = {"topic_id": "s1-a", "nearest": [
    {"id": "N1", "paper_id": "c" * 40, "title": "Near", "year": "2026", "url": "https://x/c", "level": "abstract",
     "card": {"problem": {"text": "near problem", "quote": "q"}, "method": {"text": "near method"}}}]}
PROFILE = {"lab_name": "L", "lab_topics": "backdoor"}
S2_PAPER = {"paperId": "d" * 40, "title": "Gradient Backdoor", "abstract": "x" * 5000, "externalIds": {"ArXiv": "2401.00001"}}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(calllog, "ROOT", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)


def attached(text):
    """질문 뒤에 붙은 JSON 목록."""
    return json.loads(text.split("]\n", 1)[1])


class ToolsTest(Base):
    def test_get_reference_reference_nearest_and_unknown(self):
        box = chat_toolbox(RESEARCH, VERIFY)
        self.assertEqual(box.call("get_reference", {"id": "[ref-002]"})["papers"][0]["abstract"], "hidden abs")
        near = box.call("get_reference", {"id": "N1"})["papers"][0]
        self.assertEqual(near["summary"], {"problem": "near problem", "method": "near method"})
        self.assertIn("ref-001", box.call("get_reference", {"id": "ref-999"})["error"])  # 있는 id를 알려 준다
        self.assertEqual(box.unknown_citations(["b" * 40, "c" * 40, "a" * 40]), ["a" * 40])
        self.assertIn("없음", chat_toolbox().call("get_reference", {"id": "ref-001"})["error"])

    def test_search_returns_url_and_short_abstract(self):
        with patch("src.clients.semantic_scholar.search_papers", return_value={"data": [S2_PAPER]}) as s:
            got = chat_toolbox().call("search_papers", {"query": "backdoor"})["papers"][0]
        self.assertEqual((s.call_args.kwargs["limit"], s.call_args.kwargs["tries"]), (5, 4))
        self.assertEqual(got["url"], "https://www.semanticscholar.org/paper/" + "d" * 40)
        self.assertLess(len(got["abstract"]), 2000)


class PrefetchTest(Base):
    def test_ids_and_titles_but_not_cited(self):
        box, seen = chat_toolbox(RESEARCH, VERIFY), []
        got = chat.prefetch("ref-1, REF-002, n1, ref-777는 뭐야?", box, RESEARCH, VERIFY, lambda n, a: seen.append(n))
        self.assertEqual([p["id"] for p in attached(got)], ["ref-002", "N1"])  # ref-001은 인용돼 초록이 있다
        self.assertEqual(len(seen), 3)  # ref-777도 조회는 한다 (없으면 빈손)
        got = chat.prefetch("bypassing backdoor detection 논문은 어떤 실험을 했어?", box, RESEARCH, VERIFY)
        self.assertEqual([p["id"] for p in attached(got)], ["ref-002"])
        self.assertEqual(chat.prefetch("backdoor detection 평가 방법은?", box, RESEARCH, VERIFY), "")  # 두 단어는 제목으로 보지 않는다

    def test_presearch(self):
        box = chat_toolbox()
        self.assertEqual(chat.presearch(None, box), "")
        with patch("src.clients.semantic_scholar.search_papers", return_value={"data": [S2_PAPER]}) as s:
            got = chat.presearch({"query": "gradient backdoor detection", "year": "2025-"}, box)
        self.assertEqual(s.call_args.kwargs["year"], "2025-")
        self.assertIn("검색어: gradient backdoor detection, 연도: 2025-]", got)
        self.assertEqual(attached(got)[0]["paper_id"], "d" * 40)
        self.assertIn("d" * 40, box.seen_papers)
        with patch("src.clients.semantic_scholar.search_papers", side_effect=KeyError("boom")):
            with self.assertRaises(KeyError):  # 예상 밖 오류는 그대로 올린다 (S2 오류만 글로 바꾼다)
                chat.presearch({"query": "x"}, box)
        from src.clients.semantic_scholar import S2Error
        with patch("src.clients.semantic_scholar.search_papers", side_effect=S2Error("429")):
            self.assertIn("검색 실패", chat.presearch({"query": "x"}, box))


class SearchPlanTest(unittest.TestCase):
    def test_plan_from_intent(self):
        plan = lambda q, y="": chat.search_plan(chat.Intent(action="answer", search_query=q, search_year=y))
        self.assertIsNone(plan("  "))
        self.assertEqual(plan("llm backdoor", "2025-"), {"query": "llm backdoor", "year": "2025-"})
        self.assertEqual(plan("llm backdoor", "2023-2024")["year"], "2023-2024")
        self.assertEqual(plan("llm backdoor", "최근"), {"query": "llm backdoor"})  # 형식이 틀린 연도는 버린다

    def test_decide_action_fills_plan(self):
        actions = [{"name": "verify_topic", "target": "", "label": "검증"}]
        got = chat.Intent(action="answer", search_query="gradient backdoor detection")
        plan = {}
        with patch("src.chat.llm.chat_json", return_value=got) as call:
            self.assertIsNone(chat.decide_action([], "그래디언트 기반 선행 연구 찾아줘", actions, "verify", {}, plan))
        self.assertEqual(plan, {"search": {"query": "gradient backdoor detection"}})
        self.assertIn("지금 고른 것:\n(없음)", call.call_args.args[0][1]["content"])


class CitationCheckTest(unittest.TestCase):
    KNOWN = "ref https://www.semanticscholar.org/paper/" + "a" * 40 + " arxiv 2401.00001 doi 10.1/abc"

    def test_known_links_pass(self):
        answer = ("[A](https://www.semanticscholar.org/paper/Title-Slug/" + "A" * 40 + "), "
                  "https://arxiv.org/abs/2401.00001v2, https://doi.org/10.1/ABC. arXiv:2401.00001")
        self.assertEqual(chat.unverified_links(answer, self.KNOWN), [])

    def test_made_up_links_flagged(self):
        answer = "https://arxiv.org/abs/2499.12345, arXiv: 2311.99999, https://arxiv.org/list/cs.CR, https://github.com/x/y"
        self.assertEqual(chat.unverified_links(answer, self.KNOWN),
                         ["https://arxiv.org/abs/2499.12345", "https://arxiv.org/list/cs.CR", "arXiv:2311.99999"])


class ReplyStreamTest(Base):
    def run_reply(self, answer, question="질문", search=None):
        runs_by_id = {"r_res": RESEARCH}
        with patch("src.chat.runs.load", side_effect=runs_by_id.get), \
             patch("src.conversation.runs.load", side_effect=runs_by_id.get), \
             patch("src.chat.load_index", return_value=({}, None, None)), \
             patch("src.clients.semantic_scholar.search_papers", return_value={"data": [S2_PAPER]}), \
             patch("src.chat.llm.chat_stream", return_value=iter([answer])) as stream:
            out = "".join(chat.reply_stream(PROFILE, {"research_run_id": "r_res"}, [], question, search=search))
        return out, stream.call_args

    def test_no_tools_in_answer_call_and_plain_question(self):
        _, call = self.run_reply("답")
        self.assertEqual(call.args[0][-1]["content"], "질문")
        self.assertEqual(set(call.kwargs), {"usage"})  # 답변 호출에는 Tool을 주지 않는다

    def test_found_papers_go_after_question(self):
        _, call = self.run_reply("답", "ref-002 설명해 줘", {"query": "gradient backdoor"})
        last = call.args[0][-1]["content"]
        self.assertTrue(last.startswith("ref-002 설명해 줘\n\n[질문에 나온 논문"))
        self.assertIn("hidden abs", last)
        self.assertIn("[새로 검색한 논문 (Semantic Scholar, 데이터). 검색어: gradient backdoor]", last)

    def test_links_checked(self):
        out, _ = self.run_reply("https://www.semanticscholar.org/paper/" + "b" * 40)  # 저장된 참고문헌
        self.assertNotIn("확인되지 않은 링크", out)
        out, _ = self.run_reply("https://arxiv.org/abs/2401.00001", search={"query": "x"})  # 새로 검색한 논문
        self.assertNotIn("확인되지 않은 링크", out)
        out, _ = self.run_reply("참고: https://arxiv.org/abs/2499.12345")
        self.assertIn("※ 확인되지 않은 링크: https://arxiv.org/abs/2499.12345", out)


if __name__ == "__main__":
    unittest.main()
