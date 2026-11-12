import json
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import main
from app_server import CodexAppServer
from qq_link import QQClient
from store import FriendStore



class AuditTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def test_global_memory_is_disabled_before_startup_and_for_resumed_thread(self):
        server = CodexAppServer(300000)
        server._request = Mock(side_effect=[{}, {'config': {
            'mcp_servers': {'outside': {'enabled': True}}, 'plugins': {'outside-plugin': {'enabled': True}}}}])
        server._send = Mock()
        with patch('app_server.shutil.which', return_value='codex'), \
                patch('app_server.subprocess.Popen') as popen, patch('app_server.threading.Thread'):
            server.start()
            args = popen.call_args.args[0]
            self.assertIn('web_search="live"', args)
            for setting in ('features.memories=false', 'memories.generate_memories=false', 'memories.use_memories=false',
                            'features.hooks=false', 'notify=[]'):
                self.assertIn(setting, args)
            self.assertTrue(server._request.call_args_list[0].args[1]['capabilities']['experimentalApi'])
        with tempfile.TemporaryDirectory() as folder:
            server.start = Mock()
            server._request = Mock(side_effect=[{'thread': {'id': 'friend-thread', 'cwd': folder}}, {}, {'turn': {'id': 'turn'}}, {}])
            server._next = Mock(side_effect=[
                {'method': 'item/completed', 'params': {'turnId': 'turn', 'item': {'type': 'agentMessage', 'text': '回答'}}},
                {'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': 'completed'}}}])
            self.assertEqual(server.generate('test' + chr(10) + ' [WEB_SEARCH] ', Path(folder), 'friend-thread'),
                             ('回答', 'friend-thread'))  # a reply task (search allowed)
            resume = server._request.call_args_list[0].args
            self.assertEqual(resume[0], 'thread/resume')
            self.assertEqual(resume[1]['config']['web_search'], 'live')
            self.assertFalse(resume[1]['config']['memories.use_memories'])
            self.assertFalse(resume[1]['config']['memories.generate_memories'])
            self.assertFalse(resume[1]['config']['features.hooks'])
            self.assertEqual(resume[1]['config']['notify'], [])
            self.assertFalse(resume[1]['config']['mcp_servers']['outside']['enabled'])
            self.assertFalse(resume[1]['config']['plugins']['outside-plugin']['enabled'])
            self.assertEqual(server._request.call_args_list[1].args[:2],
                             ('thread/memoryMode/set', {'threadId': 'friend-thread', 'mode': 'disabled'}))

    def test_thread_cannot_be_reused_for_another_friend(self):
        server = CodexAppServer(300000)
        server.start = Mock()
        server._request = Mock()
        with tempfile.TemporaryDirectory() as folder:
            server.loaded['shared-thread'] = str((Path(folder) / 'first').resolve())
            with self.assertRaisesRegex(RuntimeError, 'friend'):
                server.generate('test', Path(folder) / 'second', 'shared-thread')
            server._request.assert_not_called()
            server.loaded.clear()
            (Path(folder) / 'second').mkdir()
            server._request.return_value = {'thread': {'id': 'shared-thread', 'cwd': str(Path(folder) / 'first')}}
            with self.assertRaisesRegex(RuntimeError, '目录'):
                server.generate('test', Path(folder) / 'second', 'shared-thread')
            self.assertEqual(server._request.call_count, 1)

    def test_friend_qq_skill_does_not_copy_another_friends_persona(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            try:
                context = main.conversation_context(store, {'qq_id': '88888', 'style': '独立青梅竹马人设', 'skills': ['qq']})
                self.assertIn('独立青梅竹马人设', context)
                self.assertNotIn('555000001', context)
                self.assertNotIn('高二学生', context)
                self.assertNotIn('6–10', context)
            finally:
                store.close()

    def test_compaction_count_uses_actual_codex_home_and_both_json_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            sessions = Path(folder) / 'sessions'
            sessions.mkdir()
            events = [{'type': 'compacted'}, {'type': 'event_msg', 'payload': {'type': 'compacted'}}]
            (sessions / 'rollout-test-thread.jsonl').write_text(json.dumps(events[0]) + '\n' +
                json.dumps(events[0], separators=(',', ':')) + '\n' + json.dumps(events[1]) + '\n', encoding='utf-8')
            with patch.dict('os.environ', {'CODEX_HOME': folder}):
                self.assertEqual(main.completed_compactions('test-thread'), 2)

    def test_napcat_rejects_remote_host_disguised_as_loopback(self):
        with patch('qq_link.connect') as connect:
            with self.assertRaises(ValueError):
                QQClient(url='ws://127.0.0.1:3001@outside.example/socket', token='fixture-secret')
            connect.assert_not_called()

    def test_auto_reply_toggle_is_rechecked_after_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / 'friends/12345'
            profile = main.default_profile('12345', 'testfriend', False)
            profile.update(bridge_enabled=True)
            main.dump_json(directory / 'friend.json', profile)
            record = {'qq_id': '12345', 'directory': str(directory), 'self_id': '77777'}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            client = Mock()
            client.next_event.return_value = None
            bridge = main.Bridge(client)
            bridge.self_id = '77777'
            def generate(*args, **kwargs):
                main.dump_json(directory / 'friend.json', {**profile, 'auto_reply': False})
                return '不应发送'
            event = {'post_type': 'message', 'message_type': 'private', 'self_id': 77777, 'user_id': 12345,
                     'message_id': 42, 'message': [{'type': 'text', 'data': {'text': 'test'}}]}
            with patch.object(main, 'REGISTRY', root / 'friends.json'), patch.object(main, 'FRIENDS_DIR', root / 'friends'), \
                    patch.object(main, 'make_reply', side_effect=generate), patch.object(main, 'refresh_memory_if_due'):
                bridge.receive(event)
            client.call.assert_not_called()

    def test_proactive_toggle_is_rechecked_after_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / 'friends' / '12345'
            profile = main.default_profile('12345', 'testfriend', True)
            profile.update(bridge_enabled=True)
            profile['proactive'].update(bubbles_min=1, bubbles_max=1)
            main.dump_json(directory / 'friend.json', profile)
            record = {'qq_id': '12345', 'directory': str(directory), 'self_id': '77777'}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            client = Mock()
            client.next_event.return_value = None
            bridge = main.Bridge(client)
            bridge.self_id = '77777'
            def generate(*args, **kwargs):
                main.dump_json(directory / 'friend.json', {**profile, 'proactive': {**profile['proactive'], 'enabled': False}})
                return ['不应发送']
            store = FriendStore(directory)
            try:
                with patch.object(main, 'make_round', side_effect=generate), \
                        patch.object(main, 'REGISTRY', root / 'friends.json'), \
                        patch.object(main, 'FRIENDS_DIR', root / 'friends'):
                    bridge.send_round(record, profile, store, 'toggle-test', test=True)
                client.call.assert_not_called()
                self.assertEqual(store.get('test_slot:toggle-test'), 'cancelled')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
