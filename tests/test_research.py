import unittest
from unittest.mock import patch

from src import config, multi_agent as ma, research
from src.references import Queries, Screening, search_references
from src.research import Presentation, TopicKo, generate_research
from src.runs import BudgetExceeded, Cancelled

PROFILE = {"lab_name": "lab", "lab_topics": "model safety", "interests": "one GPU"}
PROBLEM = {"problem_id": "p1", "title": "Safety"}
REF = {"id": "ref-001", "title": "Reference", "abstract": "An abstract", "paper_id": "s2-1", "year": 2026, "url": "https://example.com"}


class PipelineTests(unittest.TestCase):
    def test_search_uses_conversation_filters_and_deduplicates(self):
        rows = [{"paperId": "p", "title": "Paper", "abstract": "Abstract"},
                {"paperId": "missing", "title": "No abstract"}]
        queries = Queries(queries=['model safety evaluation', 'language model robustness', 'reward model attacks'])
        # 검색어 작성과 관련성 필터를 스키마로 나눠 응답한다.
        answer = lambda messages, schema, **kw: queries if schema is Queries else Screening(verdicts=[])
        with patch('src.references.llm.chat_json', side_effect=answer) as plan, patch('src.references._search', return_value=rows):
            result = search_references(PROFILE, [{"role": "user", "content": "focus on rewards"}], PROBLEM)
        self.assertEqual(len(result['refs']), 1)
        self.assertEqual(result['search_trace'][0]['missing_abstract'], 1)
        self.assertIn('focus on rewards', plan.call_args_list[0].args[0][1]['content'])
        self.assertEqual(result['search_trace'][-1]['step'], 'relevance_filter')
        self.assertFalse(result['search_trace'][-1]['status'].startswith('failed'))

    def test_multi_agent_generation_without_novelty_calls(self):
        method = ma.Method(config.ROOT_DIR / 'src/generation_method', 'one GPU')
        calls = []
        hypothesis = dict(title='Proposal', summary=' '.join(['proposal'] * 65), gap='Gap', approach='Method', evaluation='Baseline and metric', support=['ref-001'])
        def call(system, user, schema, temperature=None):
            calls.append(schema.__name__)
            if schema is ma.Proposals:
                return ma.Proposals(hypotheses=[ma.Hypothesis(**hypothesis) for _ in range(3)])
            if schema is ma.Review:
                return ma.Review(unsupported='', vague='', already_done='', suggestion='Clarify')
            if schema is ma.Hypothesis:
                return ma.Hypothesis(**hypothesis)
            if schema is ma.Ballot:
                return ma.Ballot(winner='A', rationale='Grounded')
            self.fail('Unexpected schema: ' + schema.__name__)
        method.call = call
        result = ma.run_sample(method, 'test', [REF], 1)
        self.assertEqual(len(result['candidates']), 12)
        self.assertEqual(len(result['chosen']), 3)
        self.assertEqual(calls.count('Proposals'), 4)
        self.assertEqual(calls.count('Review'), 36)
        self.assertNotIn('Overlap', calls)
        self.assertNotIn('SameTopic', calls)

    def test_orchestration_handoff_and_progress_steps(self):
        generated = {'candidates': [{'id': 'c1', 'refined': {'title': 'Topic', 'support': ['ref-001']}}], 'chosen': ['c1']}
        steps = []
        with patch('src.research.search_references', return_value={'refs': [REF], 'search_trace': [{'query': 'q'}], 'context': 'conversation'}) as search, patch('src.research.run_sample', return_value=generated), patch('src.research.llm.chat_json', return_value=Presentation(intro='후보 설명', topics=[TopicKo(id='c1', summary='요약', gap='공백', approach='방법', evaluation='평가')])) as presenter:
            result = generate_research(PROFILE, [], PROBLEM, lambda *a, step=None, **k: steps.append(step))
        self.assertEqual(steps, ['queries', 'generate', 'present'])
        self.assertEqual(search.call_args.args[:3], (PROFILE, [], PROBLEM))
        self.assertNotIn('run_id', result)  # 저장은 서버가 한다 (src.runs)
        # 후보 필드는 영어 그대로 두고 화면용 한국어만 ko에 붙인다 (이슈 #39)
        self.assertEqual(result['topics'], [{'id': 'c1', 'title': 'Topic', 'support': ['ref-001'],
                                             'ko': {'summary': '요약', 'gap': '공백', 'approach': '방법', 'evaluation': '평가'}}])
        self.assertEqual(result['presentation'], '후보 설명')
        self.assertEqual((result['trace'], result['generation']), ([{'query': 'q'}], generated))
        self.assertFalse(result['novelty_checked'])
        self.assertIn('ref-001', presenter.call_args.args[0][1]['content'])

    def test_cancel_before_generation_stops_pipeline(self):
        def progress(*args, step=None, **kwargs):
            if step == 'generate':
                raise Cancelled()
        with patch('src.research.search_references', return_value={'refs': [REF], 'search_trace': [], 'context': ''}), patch('src.research.run_sample') as run:
            with self.assertRaises(Cancelled):
                generate_research(PROFILE, [], PROBLEM, progress)
        run.assert_not_called()

    def test_method_checks_cancel_and_budget_before_each_call(self):
        stop = []
        def progress(*args, **kwargs):
            if stop:
                raise Cancelled()
        method = research._Method(config.ROOT_DIR / 'src/generation_method', 'ctx', progress)
        review = ma.Review(unsupported='', vague='', already_done='', suggestion='')
        with patch('src.multi_agent.chat_json', return_value=review) as llm_call:
            self.assertEqual(method.call('system', 'user', ma.Review), review)
            stop.append(True)
            with self.assertRaises(Cancelled):
                method.call('system', 'user', ma.Review)
            stop.clear()
            method.calls = config.RESEARCH_LLM_BUDGET
            with self.assertRaises(BudgetExceeded):
                method.call('system', 'user', ma.Review)
        self.assertEqual(llm_call.call_count, 1)

    def test_invalid_support_is_rejected(self):
        method = ma.Method(config.ROOT_DIR / 'src/generation_method')
        bad = ma.Hypothesis(title='x', summary=' '.join(['proposal']*65), gap='x', approach='x', evaluation='x', support=['unknown'])
        method.call = lambda *args, **kwargs: bad
        with self.assertRaises(ValueError):
            ma.with_retry(method, '', '', ma.Hypothesis, lambda h: ma.problems(method, h, {'ref-001'}))


if __name__ == '__main__':
    unittest.main()
