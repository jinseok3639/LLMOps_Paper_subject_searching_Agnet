import unittest
import tempfile
from pathlib import Path
TEST_TMP = Path(__file__).resolve().parents[1] / "DB" / "test_tmp"
TEST_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(TEST_TMP)
from unittest.mock import patch
from streamlit.testing.v1 import AppTest


class WorkflowTests(unittest.TestCase):
    def test_browse_explore_refine_and_back(self):
        card = {'problem_id':'p1','title':'Problem','summary':'Summary','reason':'Reason',
                'paper':{'title':'Paper','year':2026,'url':'https://example.com'},'page':1,
                'section_title':'Section','quote':'Quote','keywords':['safety']}
        at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20)
        at.session_state['profile']={'lab_name':'Lab','interests':'Safety','extra':''}
        at.session_state['result']={'intro':'Intro','recommendations':[card], 'note':'', 'trace':{'candidates':[]}}
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['stage'], 'browse')
        self.assertEqual(len(at.chat_input), 1)
        next(b for b in at.button if b.label=='이 문제 파고들기').click().run()
        self.assertEqual(at.session_state['stage'], 'explore')
        result={'run_id':'test','presentation':'후보 설명','references':[], 'trace':[],
                'topics':[{'id':'c1','title':'Candidate'}]}
        with patch('src.research.generate_research', return_value=result) as generate:
            next(b for b in at.button if b.label=='참고문헌 검색하고 후보 생성').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['stage'], 'refine')
        self.assertEqual(generate.call_args.args[2]['problem_id'], 'p1')
        next(b for b in at.button if b.label=='다른 문제 둘러보기').click().run()
        self.assertEqual(at.session_state['stage'], 'browse')
        self.assertNotIn('topic_result', at.session_state)

    def test_failed_generation_keeps_explore(self):
        at=AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'), default_timeout=20)
        at.session_state['profile']={'lab_name':'Lab','interests':'Safety','extra':''}
        at.session_state['result']={'intro':'','recommendations':[],'note':'','trace':{'candidates':[]}}
        at.session_state['problem']={'problem_id':'p','title':'Problem'}
        at.session_state['stage']='explore'
        at.run()
        with patch('src.research.generate_research', side_effect=ValueError('no references')):
            next(b for b in at.button if b.label=='참고문헌 검색하고 후보 생성').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['stage'],'explore')
        self.assertIn('no references',at.error[0].value)
