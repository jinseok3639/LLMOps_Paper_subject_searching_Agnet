"""대화 기록 규칙과 압축 (src.conversation), 채팅 컨텍스트 (src.chat) 테스트. LLM은 가짜로 바꾼다."""
import unittest
from unittest.mock import patch

from src import chat, conversation as cv
from src.conversation import Change, Delta

TOPICS_RUN = {"topics": [{"id": "s1-a", "title": "후보 A", "support": ["ref-001"]},
                         {"id": "s2-b", "title": "후보 B", "support": []}],
              "references": [{"id": "ref-001", "title": "Cited", "year": 2025, "abstract": "인용된 초록",
                              "openAccessPdf": {"url": "x"}, "paper_id": "p1"},
                             {"id": "ref-002", "title": "Other", "year": 2024, "abstract": "안 쓰는 초록"}]}


def m(i, role, content, **extra):
    return {"id": f"m{i}", "role": role, "content": content, **extra}


HISTORY = [
    m(1, "user", "연구실: 보안", kind="profile"),
    m(2, "assistant", "추천한 Open Problem:\n1. A\n2. B", kind="problems", run_id="r_rec"),
    m(3, "user", "GPU는 1장뿐이야"),
    m(4, "assistant", "그럼 작은 모델로 " + "설명 " * 200),
    m(5, "assistant", "선택한 문제: B", kind="problem"),
    m(6, "user", "3개월 안에 끝내고 싶어"),
    m(7, "assistant", "발표문 " * 500, kind="topics", run_id="r_res"),
    m(8, "user", "후보 A가 좋아"),
    m(9, "assistant", "후보 A는 ..."),
]


def load(run_id):
    return TOPICS_RUN if run_id == "r_res" else None


@patch("src.conversation.runs.load", side_effect=load)
class HistoryRuleTests(unittest.TestCase):
    def test_cards_become_one_line_and_profile_is_dropped(self, _):
        out, summary = cv.chat_messages(HISTORY)
        self.assertEqual(summary, "")
        texts = [x["content"] for x in out]
        self.assertNotIn("연구실: 보안", texts)
        self.assertIn("[③ 연구 주제 후보 2개를 보여 줌: 후보 A / 후보 B]", texts)
        self.assertIn("[추천한 Open Problem: 1. A 2. B]", texts)
        self.assertTrue(all(set(x) == {"role", "content"} for x in out))  # 메타 필드는 뗀다
        self.assertEqual(len(out), 8)  # 자르지 않는다

    def test_memo_replaces_old_chat_but_keeps_cards(self, _):
        memo = {"items": {"conditions": [{"id": "i1", "text": "GPU 1장", "source": "m3"}]}, "through": "m6"}
        out, summary = cv.chat_messages(HISTORY, memo)
        self.assertIn("조건: GPU 1장", summary)
        texts = [x["content"] for x in out]
        self.assertFalse(any("GPU는 1장뿐" in t or "3개월" in t for t in texts))  # 요약 범위의 주변 대화는 빠진다
        self.assertIn("[선택한 문제: B]", texts)  # 카드는 남는다
        self.assertEqual(texts[-2:], ["후보 A가 좋아", "후보 A는 ..."])

    def test_unknown_through_ignores_memo(self, _):  # 요약 범위 안을 재시도해 잘린 history
        memo = {"items": {"conditions": [{"id": "i1", "text": "GPU 1장", "source": "m3"}]}, "through": "m9"}
        out, summary = cv.chat_messages(HISTORY[:5], memo)
        self.assertEqual(summary, "")
        self.assertIn("GPU는 1장뿐이야", [x["content"] for x in out])

    def test_hard_limit_drops_old_chat_not_cards(self, _):
        long = [m(100 + i, "user", "x" * 30_000) for i in range(6)]
        out, _ = cv.chat_messages(HISTORY[:3] + long)
        self.assertLessEqual(sum(len(x["content"]) for x in out), cv.HARD_LIMIT_CHARS)
        self.assertTrue(any(x["content"].startswith("[추천한") for x in out))

    def test_research_conversation(self, _):
        out = cv.research_conversation(HISTORY)
        self.assertEqual(out["recent_user_messages"], ["GPU는 1장뿐이야", "3개월 안에 끝내고 싶어", "후보 A가 좋아"])
        self.assertEqual(out["last_assistant_reply"], "후보 A는 ...")
        self.assertEqual(out["previous_candidates"], ["후보 A", "후보 B"])
        text = str(out)
        self.assertNotIn("발표문", text)
        self.assertNotIn("연구실: 보안", text)

    def test_research_conversation_budget_and_memo(self, _):
        history = [m(i, "user", "가" * 900) for i in range(5)]
        out = cv.research_conversation(history)
        self.assertEqual(len(out["recent_user_messages"]), 2)  # 900자씩, 합계 2,000자까지 최근 것부터
        memo = {"items": {"rejected": [{"id": "i1", "text": "강화학습은 싫다", "source": "m1"}]}, "through": "m2"}
        out = cv.research_conversation(history, memo)
        self.assertIn("거절한 방향: 강화학습은 싫다", out["earlier_conversation_summary"])


class DeltaTests(unittest.TestCase):
    def test_apply_delta_checks_sources_and_targets(self):
        memo = {"items": {"conditions": [{"id": "i1", "text": "GPU 1장", "source": "m3"}],
                          "rejected": [{"id": "i2", "text": "RL 싫음", "source": "m4"}]}, "through": "m4"}
        delta = Delta(changes=[
            Change(op="add", field="preferences", text="평가는 벤치마크로", source="m10"),
            Change(op="add", field="preferences", text="근거 없음", source="m3"),       # 이번 범위 밖 근거
            Change(op="update", field="conditions", item_id="i1", text="GPU 2장", source="m11"),
            Change(op="remove", field="rejected", item_id="i9", source="m11"),         # 없는 항목
            Change(op="add", field="nonsense", text="x", source="m10"),
        ])
        new = cv.apply_delta(memo, delta, {"m10", "m11"}, "m11")
        self.assertEqual(new["through"], "m11")
        self.assertEqual([i["text"] for i in new["items"]["preferences"]], ["평가는 벤치마크로"])
        self.assertEqual(new["items"]["conditions"], [{"id": "i1", "text": "GPU 2장", "source": "m11"}])
        self.assertEqual([i["text"] for i in new["items"]["rejected"]], ["RL 싫음"])
        self.assertEqual(new["items"]["preferences"][0]["id"], "i3")  # 기존 id 다음 번호
        self.assertEqual(len(new["applied"]), 2)

    def test_repeated_compaction_keeps_old_items_and_caps(self):
        memo = None
        for k in range(20):  # 압축 20번. 한 번에 조건 하나와 거절 하나씩
            delta = Delta(changes=[Change(op="add", field="conditions", text=f"조건 {k}", source=f"m{k}"),
                                   Change(op="add", field="rejected", text=f"거절 {k}", source=f"m{k}")])
            memo = cv.apply_delta(memo, delta, {f"m{k}"}, f"m{k}")
        self.assertEqual(len(memo["items"]["conditions"]), cv.FIELD_LIMIT)
        self.assertEqual(memo["items"]["conditions"][-1]["text"], "조건 19")
        self.assertEqual(len(memo["items"]["rejected"]), 20)  # 거절한 방향은 빼지 않는다
        self.assertEqual(memo["items"]["rejected"][0]["text"], "거절 0")

    @patch("src.conversation.runs.load", side_effect=load)
    def test_compact_range_and_call(self, _):
        history = HISTORY + [m(10 + i, "user", f"최근 {i}") for i in range(6)]
        delta = Delta(changes=[Change(op="add", field="conditions", text="GPU 1장", source="m3")])
        with patch("src.conversation.llm.chat_json", return_value=delta) as call:
            memo = cv.compact(history)
        user = call.call_args.args[0][1]["content"]
        self.assertIn("[m3] user: GPU는 1장뿐이야", user)
        self.assertNotIn("최근 0", user)  # 최근 KEEP_RECENT개는 원문으로 남긴다
        self.assertEqual(memo["through"], "m9")
        self.assertEqual(memo["items"]["conditions"][0]["text"], "GPU 1장")
        with patch("src.conversation.llm.chat_json") as call:  # 요약할 새 대화가 없으면 부르지 않는다
            self.assertIsNone(cv.compact(history, memo))
            call.assert_not_called()


class ChatContextTests(unittest.TestCase):
    PROFILE = {"lab_name": "L", "lab_topics": "AI 보안", "interests": ""}

    def test_references_verify_and_selection_last(self):
        verify = {"topic_id": "s1-a", "topic_title": "후보 A", "verdict": "partial", "verdict_basis": ["p9"],
                  "flags": ["초록 수준"], "scope": {"queries": ["q1"], "searched": 40, "narrowed": 15, "fulltext_read": 1,
                                                 "date": "2026-11-07"},
                  "nearest": [{"id": "N1", "title": "Near", "year": 2025, "level": "abstract",
                               "comparison": {"problem": {"match": "same", "reason": "같은 문제", "quote": "we study X"}}}],
                  "limitations": [{"id": "N1", "quote": "we leave Y"}],
                  "critique": {"requirements": {"mechanism": {"status": "undecided"}}, "suggestion": "Y를 정의"}}
        run = {**TOPICS_RUN, "topics": [{**TOPICS_RUN["topics"][0], "ko": {"summary": "화면용 번역"}}, TOPICS_RUN["topics"][1]]}
        context = chat.build_context(self.PROFILE, None, run, {"topic_id": "s1-a"}, verify)
        self.assertNotIn("화면용 번역", context)  # 화면용 한국어는 채팅 입력에 넣지 않는다 (이슈 #39)
        self.assertIn("인용된 초록", context)
        self.assertNotIn("안 쓰는 초록", context)
        self.assertNotIn("openAccessPdf", context)
        for text in ("판정: partial", "problem: 같음. 같은 문제 / 논문 인용: \"we study X\"", "we leave Y", "Y를 정의",
                     "검색한 논문 40편"):
            self.assertIn(text, context)
        self.assertGreater(context.index("## 현재 선택"), context.index("## ④ 검증 결과"))
        self.assertTrue(context.rstrip().endswith("[s1-a]"))

    def test_reply_skips_verify_of_other_topic_and_reports_usage(self):
        runs_by_id = {"r_res": TOPICS_RUN, "r_ver": {"topic_id": "s2-b", "verdict": "solved"}}
        picks = {"research_run_id": "r_res", "verify_run_id": "r_ver", "topic_id": "s1-a"}
        usage = {}

        def stream(messages, usage=None):
            usage.update(tokens_in=60_000, tokens_cached=50_000)
            yield "답"
        with patch("src.chat.runs.load", side_effect=runs_by_id.get), \
             patch("src.conversation.runs.load", side_effect=runs_by_id.get), \
             patch("src.chat.llm.chat_stream", side_effect=stream) as call:
            self.assertEqual("".join(chat.reply_stream(self.PROFILE, picks, HISTORY, "질문", None, usage)), "답")
        self.assertNotIn("## ④ 검증 결과", call.call_args.args[0][0]["content"])
        self.assertEqual(usage["tokens_in"], 60_000)


if __name__ == "__main__":
    unittest.main()
