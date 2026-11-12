import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from store import FriendStore


class HotBindTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def test_packaged_binding_does_not_require_stopping_live_process(self):
        with patch.dict(os.environ, QQ_BRIDGE_HOME=str(main.ROOT)):
            import packaged_bridge
        with patch.object(packaged_bridge, 'bridge_process', return_value=Mock()), \
                patch.object(packaged_bridge, 'stop') as stop, patch.object(main, 'bind_friend') as bind:
            code, _ = packaged_bridge.invoke('bind -QQ 23456')
        self.assertEqual(code, 0)
        bind.assert_called_once_with('23456', False, None)
        stop.assert_not_called()

    def test_live_bridge_recognizes_new_friend_and_keeps_existing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            friends = root / 'friends'
            registry = root / 'friends.json'
            existing = friends / '12345'
            old_profile = {'qq_id': '12345', 'style': '保留风格', 'proactive': {'enabled': True}}
            main.dump_json(existing / 'friend.json', old_profile)
            main.dump_json(registry, {'friends': {'12345': {'qq_id': '12345', 'self_id': '77777'}}})
            old_store = FriendStore(existing)
            old_store.set('thread_id', 'old-thread')
            old_store.add_message('old', 'friend', '保留历史')
            old_store.close()
            fake = Mock()
            fake.call.side_effect = lambda action: {'user_id': 77777} if action == 'get_login_info' else [
                {'user_id': 23456, 'nickname': 'friend'}]
            bridge = main.Bridge.__new__(main.Bridge)
            bridge.client = fake
            bridge.self_id = '77777'
            bridge.process_reply = Mock()
            bridge.log = Mock()
            with patch.object(main, 'REGISTRY', registry), patch.object(main, 'FRIENDS_DIR', friends), \
                    patch.object(main, 'QQClient', return_value=fake):
                main.bind_friend('23456')
                bridge.receive({'post_type': 'message', 'message_type': 'private', 'self_id': 77777,
                                'user_id': 23456, 'message_id': 42,
                                'message': [{'type': 'text', 'data': {'text': '新来信'}}]})
                bridge.process_reply.assert_called_once()
                self.assertEqual(bridge.process_reply.call_args.args[0]['qq_id'], '23456')
                self.assertEqual(set(main.load_registry()['friends']), {'12345', '23456'})
            self.assertEqual(json.loads((existing / 'friend.json').read_text(encoding='utf-8')), old_profile)
            old_store = FriendStore(existing)
            self.assertEqual(old_store.get('thread_id'), 'old-thread')
            self.assertEqual(old_store.message_count(), 1)
            old_store.close()
            self.assertFalse(json.loads((friends / '23456' / 'friend.json').read_text(encoding='utf-8'))['proactive']['enabled'])
            fake.close.assert_called_once()

    def test_registry_publication_is_atomic_and_new_store_is_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / 'friends.json'
            main.dump_json(registry, {'friends': {}})
            fake = Mock()
            fake.call.side_effect = lambda action: {'user_id': 77777} if action == 'get_login_info' else [
                {'user_id': 23456, 'nickname': 'friend'}]
            original_replace = os.replace
            def publish(source, destination):
                if Path(destination) != registry:
                    return original_replace(source, destination)
                self.assertEqual(json.loads(registry.read_text(encoding='utf-8')), {'friends': {}})
                store = FriendStore(root / 'friends' / '23456')
                self.assertEqual(store.get('dotlink_self_id'), '77777')
                store.close()
                return original_replace(source, destination)
            with patch.object(main, 'REGISTRY', registry), patch.object(main, 'FRIENDS_DIR', root / 'friends'), \
                    patch.object(main, 'QQClient', return_value=fake), patch.object(os, 'replace', side_effect=publish) as replace:
                main.bind_friend('23456')
                publications = [call for call in replace.call_args_list if Path(call.args[1]) == registry]
                self.assertEqual(len(publications), 1)


if __name__ == '__main__':
    unittest.main()
