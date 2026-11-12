import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app_server import CodexAppServer, search_budget


class ChatSearchLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.server = CodexAppServer(300000)
        self.server.start = Mock()
        self.server._request = Mock(side_effect=self.request)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, params, deadline):
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': 'thread', 'cwd': str(self.directory)}}
        return {'turn': {'id': 'turn'}} if method == 'turn/start' else {}

    def generate(self, search=False, thread_id=None, failed=False, prompt='test' + chr(10) + ' [WEB_SEARCH] '):
        events = []
        if search:
            for method in ('item/started', 'item/completed', 'item/started', 'item/completed'):
                events.append({'method': method, 'params': {'turnId': 'turn', 'item': {'type': 'webSearch'}}})
        events.extend([
            {'method': 'item/completed', 'params': {'turnId': 'turn', 'item': {'type': 'agentMessage', 'text': '回复'}}},
            {'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': 'failed' if failed else 'completed'}}}])
        self.server._next = Mock(side_effect=events)
        return self.server.generate(prompt, self.directory, thread_id)

    def test_regular_chat_does_not_consume_search_rounds(self):
        for _ in range(8):
            self.generate(thread_id='thread' if self.server.loaded else None)
        self.assertEqual(search_budget(self.directory), 5)

    def test_thread_id_resumes_that_thread_and_releases_it(self):
        for thread_id, method in ((None, 'thread/start'), ('thread', 'thread/resume')):
            self.server._request.reset_mock(side_effect=False)
            self.server._request.side_effect = self.request
            self.assertEqual(self.generate(thread_id=thread_id), ('回复', 'thread'))
            calls = self.server._request.call_args_list
            self.assertEqual(calls[0].args[0], method)
            config = calls[0].args[1]
            self.assertIn('QQ聊天助手', config['baseInstructions'])
            self.assertFalse(config['config']['features.plugins'])
            self.assertTrue(all(not skill['enabled'] for skill in config['config']['skills.config']))
            if thread_id:
                self.assertEqual((config['threadId'], config['excludeTurns']), ('thread', True))
            turn = next(call.args[1] for call in calls if call.args[0] == 'turn/start')
            self.assertEqual(turn['threadId'], 'thread')
            self.assertTrue(turn['input'][0]['text'].startswith('test'))
            self.assertEqual(calls[-1].args[:2], ('thread/unsubscribe', {'threadId': 'thread'}))
        self.assertEqual(search_budget(self.directory), 5)

    def test_multiple_searches_in_one_reply_count_once(self):
        self.generate(search=True)
        self.assertEqual(search_budget(self.directory), 4)
        self.assertEqual(len(self.server.last_searches), 2)
        self.generate(thread_id='thread')
        self.assertEqual(self.server.last_searches, [])

    def test_sixth_round_disables_search_on_loaded_thread(self):
        for _ in range(5):
            self.generate(search=True, thread_id='thread' if self.server.loaded else None)
        self.server._request.reset_mock(side_effect=False)
        self.generate(thread_id='thread')
        resume = self.server._request.call_args_list[0]
        self.assertEqual(resume.args[0], 'thread/resume')
        self.assertEqual(resume.args[1]['config']['web_search'], 'disabled')
        self.assertEqual(search_budget(self.directory), 0)
        self.assertEqual(self.generate(thread_id='thread'), ('回复', 'thread'))

    def test_rolling_hour_survives_restart_and_is_separate_per_friend(self):
        with patch('app_server.time.time', return_value=10000):
            for _ in range(5):
                search_budget(self.directory, record=True)
        self.server = CodexAppServer(300000)
        with patch('app_server.time.time', return_value=13599):
            self.assertEqual(search_budget(self.directory), 0)
            other = self.directory / 'other'
            other.mkdir()
            self.assertEqual(search_budget(other), 5)
        with patch('app_server.time.time', return_value=13600):
            self.assertEqual(search_budget(self.directory), 5)

    def test_expired_quota_reenables_search_on_existing_thread(self):
        with patch('app_server.time.time', return_value=10000):
            for _ in range(5):
                search_budget(self.directory, record=True)
            self.generate()
        self.server._request.reset_mock(side_effect=False)
        with patch('app_server.time.time', return_value=13600):
            self.generate(thread_id='thread')
        resume = self.server._request.call_args_list[0]
        self.assertEqual(resume.args[0], 'thread/resume')
        self.assertEqual(resume.args[1]['config']['web_search'], 'live')

    def test_searched_failed_generation_still_consumes_one_round(self):
        with self.assertRaises(RuntimeError):
            self.generate(search=True, failed=True)
        self.assertEqual(search_budget(self.directory), 4)

    def test_tasks_without_web_search_section_get_no_search_tool(self):
        # 2026-10-10 user: only the reply task (with  [WEB_SEARCH] ) may search; other modes get the tool switched off
        self.generate(prompt='续话判断')
        start = self.server._request.call_args_list[0]
        self.assertEqual(start.args[1]['config']['web_search'], 'disabled')
        turn = next(call.args[1] for call in self.server._request.call_args_list if call.args[0] == 'turn/start')
        self.assertNotIn('WEB_SEARCH_BUDGET', turn['input'][0]['text'])
        self.assertEqual(search_budget(self.directory), 5)