import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import config, multi_agent as ma
from src.references import Queries, search_references
from src.research import generate_research

PROFILE = {"lab_name": "lab", "interests": "model safety", "extra": "one GPU"}
PROBLEM = {"problem_id": "p1", "title": "Safety"}
REF = {"id": "ref-001", "title": "Reference", "abstract": "An abstract", "paper_id": "s2-1", "year": 2026, "url": "https://example.com"}


class PipelineTests(unittest.TestCase):
    def test_search_uses_conversation_filters_and_deduplicates(self):
        rows = [{"paperId": "p", "title": "Paper", "abstract": "Abstract"},
                {"paperId": "missing", "title": "No abstract"}]
        with patch('src.references.llm.chat_json', return_value=Queries(queries=['model safety evaluation', 'language model robustness', 'reward model attacks'])) as plan, patch('src.references._search', return_value=rows):
            result = search_references(PROFILE, [{"role": "user", "content": "focus on rewards"}], PROBLEM)
        self.assertEqual(len(result['refs']), 1)
        self.assertEqual(result['search_trace'][0]['missing_abstract'], 1)
        self.assertIn('focus on rewards', plan.call_args.args[0][1]['content'])

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

    def test_orchestration_handoff_and_db_persistence(self):
        generated = {'candidates': [{'id': 'c1', 'refined': {'title': 'Topic', 'support': ['ref-001']}}], 'chosen': ['c1']}
        with tempfile.TemporaryDirectory(dir=config.DB_DIR) as folder, patch.object(config, 'DB_DIR', Path(folder)), patch('src.research.search_references', return_value={'refs': [REF], 'search_trace': [], 'context': 'conversation'}), patch('src.research.run_sample', return_value=generated), patch('src.research.llm.chat_text', return_value='후보 설명') as presenter:
            result = generate_research(PROFILE, [], PROBLEM)
            self.assertTrue((Path(folder) / 'sessions' / (result['run_id'] + '.json')).exists())
            self.assertFalse(result['novelty_checked'])
            self.assertIn('ref-001', presenter.call_args.args[0][1]['content'])

    def test_invalid_support_is_rejected(self):
        method = ma.Method(config.ROOT_DIR / 'src/generation_method')
        bad = ma.Hypothesis(title='x', summary=' '.join(['proposal']*65), gap='x', approach='x', evaluation='x', support=['unknown'])
        method.call = lambda *args, **kwargs: bad
        with self.assertRaises(ValueError):
            ma.with_retry(method, '', '', ma.Hypothesis, lambda h: ma.problems(method, h, {'ref-001'}))


if __name__ == '__main__':
    unittest.main()
