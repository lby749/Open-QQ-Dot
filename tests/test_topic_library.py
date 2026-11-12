import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import api_backend
from store import FriendStore
from topic_library import TOOLS, call_tool


def today():
    return datetime.now().astimezone().date().isoformat()


class LibraryTests(unittest.TestCase):
    """2026-10-10 user: the public topic library was deleted; 2026-10-11: rebuilt per friend (topic_research.py),
    so search_topic_library is back and reads only the current friend's library (tests/test_topic_research.py)."""

    def prepare(self, directory):
        (directory / 'friend.json').write_text(json.dumps({'basic_info': {}}), encoding='utf-8')
        return FriendStore(directory)

    def test_tools_and_arguments(self):
        self.assertEqual([tool['name'] for tool in TOOLS],
                         ['search_friend_memory', 'search_topic_library', 'get_today_chat'])
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.prepare(directory).close()
            self.assertEqual(call_tool(directory, 'search_topic_library', {'query': 'astronomy'})['items'], [])
            with self.assertRaises(ValueError):
                call_tool(directory, 'search_public_topics', {'query': 'astronomy'})
            with self.assertRaises(ValueError):
                call_tool(directory, 'get_today_chat', {'query': 'astronomy', 'directory': 'other'})

    def test_today_chat_is_on_demand(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store = self.prepare(directory)
            try:
                store.add_message('old', 'friend', '过时聊天', '2000-01-01T12:00:00+09:00')
                store.add_message('now', 'friend', '今天正在谈论陶艺', today() + 'T12:00:00+09:00')
                chat = call_tool(directory, 'get_today_chat', {'query': '陶艺'})
                self.assertEqual(chat['items'], [{'speaker': 'friend', 'text': '今天正在谈论陶艺'}])
            finally:
                store.close()

    def test_deepseek_tool_call_fetches_only_requested_results(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store = self.prepare(directory)
            store.add_message('now', 'friend', '今天正在谈论陶艺', today() + 'T12:00:00+09:00')
            store.close()
            responses = [
                {'choices': [{'finish_reason': 'tool_calls', 'message': {'content': None, 'tool_calls': [
                    {'id': 'call1', 'type': 'function', 'function': {'name': 'get_today_chat', 'arguments': '{"query":"陶艺"}'}}]}}]},
                {'choices': [{'finish_reason': 'stop', 'message': {'content': '自然接话'}}]},
            ]
            requests = []
            def respond(request, timeout):
                requests.append(json.loads(request.data))
                return io.StringIO(json.dumps(responses[len(requests) - 1]))
            with patch.object(api_backend, 'load_key', return_value='test-key'), patch.object(api_backend.urllib.request, 'urlopen', side_effect=respond):
                self.assertEqual(api_backend.generate('按需聊天', directory, {}, library=True), '自然接话')
            self.assertNotIn('今天正在谈论陶艺', json.dumps(requests[0], ensure_ascii=False))
            results = json.loads(requests[1]['messages'][-1]['content'])
            self.assertEqual(results['items'], [{'speaker': 'friend', 'text': '今天正在谈论陶艺'}])
            self.assertEqual(requests[1]['messages'][-1]['tool_call_id'], 'call1')
