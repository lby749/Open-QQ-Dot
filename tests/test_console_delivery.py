import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import agent_service
from qq_link import QQClient
from store import FriendStore


class ConsoleDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.friend = self.root / 'friends/12345'
        self.friend.mkdir(parents=True)
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}))
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'12345': {'self_id': '77777'}}}))
        self.patches = [patch.object(agent_service, 'ROOT', self.root),
                        patch.object(agent_service, '_friend_dir', return_value=self.friend),
                        patch('session_manager.ROOT', self.root),
                        patch('message_classification.enqueue_for_database')]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def client(self):
        client = Mock()
        client.call.return_value = {'user_id': 77777}
        client.send_private_msg.return_value = {'message_id': 99}
        return client

    def test_none_friend_fails_before_network_access(self):
        with patch('qq_link.QQClient') as factory, self.assertRaises(ValueError):
            agent_service.send_friend_message(None, 'test')
        factory.assert_not_called()

    def test_real_receipt_required_before_recording_sent_message(self):
        client = self.client()
        with patch('qq_link.QQClient', return_value=client):
            result = agent_service.send_friend_message('12345', '验收消息')
        self.assertTrue(result['local_saved'])
        client.send_private_msg.assert_called_once_with('12345', '验收消息')
        client.close.assert_called_once()
        store = FriendStore(self.friend)
        try:
            self.assertEqual(store.recent_messages(1)[0]['body'], '验收消息')
        finally:
            store.close()

    def test_timeout_is_not_misreported_as_queued_or_sent(self):
        client = self.client()
        client.send_private_msg.side_effect = TimeoutError('结果未确认')
        with patch('qq_link.QQClient', return_value=client), self.assertRaises(TimeoutError):
            agent_service.send_friend_message('12345', '不可假装发送成功')
        self.assertFalse((self.friend / 'history.sqlite3').exists())
        client.close.assert_called_once()

    def test_wrong_login_account_is_rejected(self):
        client = self.client()
        client.call.return_value = {'user_id': 88888}
        with patch('qq_link.QQClient', return_value=client), self.assertRaises(PermissionError):
            agent_service.send_friend_message('12345', 'test')
        client.send_private_msg.assert_not_called()

    def test_client_rejects_missing_send_receipt(self):
        client = object.__new__(QQClient)
        client.call = Mock(return_value={})
        with self.assertRaisesRegex(RuntimeError, '未返回发送回执'):
            client.send_private_msg('12345', 'test')

    def test_normal_console_turn_uses_real_memory_and_topic_storage(self):
        import session_manager
        session = session_manager.create_session('12345', title='控制台回归')
        with patch('topic_research.items', return_value=[{'title': '可靠自动化', 'summary': '真实话题接口中的摘要'}]), \
                patch('main.codex_text', return_value=('控制台验收成功', None)) as model:
            result = agent_service.execute_agent_turn('12345', 'default', session['id'], '请回复验收成功', mode='chat')
        self.assertEqual(result['reply'], '控制台验收成功')
        self.assertIn('真实话题接口中的摘要', model.call_args.args[0])
