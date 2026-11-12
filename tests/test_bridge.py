import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from store import FriendStore
from qq_link import QQClient


class FakeNapCat:
    def __init__(self):
        self.calls = []

    def call(self, action, params=None, timeout=10):
        self.calls.append((action, params))
        if action == 'get_login_info':
            return {'user_id': 77777}
        if action == "send_private_msg":
            return {"message_id": 9001}
        raise AssertionError(action)

    def next_event(self, timeout=1):
        return None


class BridgeTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def test_independent_round_does_not_use_the_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'friends' / '12345'
            profile = main.default_profile('12345', 'friend', False)
            profile.update(bridge_enabled=True)
            profile['proactive'].update(enabled=True, bubbles_min=1, bubbles_max=1)
            main.dump_json(path / 'friend.json', profile)
            record = {'qq_id': '12345', 'directory': str(path), 'self_id': '77777'}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = '77777'
            store = FriendStore(path)
            try:
                with patch.object(main, 'make_round', return_value=['测试开场白']), \
                        patch.object(main, 'REGISTRY', root / 'friends.json'), \
                        patch.object(main, 'FRIENDS_DIR', root / 'friends'), \
                        patch.object(main.random, 'randint', return_value=1):
                    bridge.send_round(record, profile, store, 'manual', test=True)
                self.assertEqual(len([call for call in fake.calls if call[0] == 'send_private_msg']), 1)
                self.assertEqual(store.get('test_slot:manual'), 'sent')
                self.assertEqual(store.get('proactive_test_rounds'), '1')
                self.assertEqual(store.get('unanswered_rounds', '0'), '0')
            finally:
                store.close()

    def test_private_message_routes_by_stable_qq_and_sends_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            friend = root / "friends" / "12345"
            friend.mkdir(parents=True)
            profile = main.default_profile("12345", "friend", False)
            profile["bridge_enabled"] = True
            (friend / "friend.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
            (root / "friends.json").write_text(json.dumps({"friends": {"12345": {
                "qq_id": "12345", "directory": str(friend), "display_name": "friend", "self_id": "77777"
            }}}), encoding="utf-8")
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = "77777"
            event = {"post_type": "message", "message_type": "private", "self_id": 77777,
                     "user_id": 12345, "message_id": 42,
                     "message": [{"type": "text", "data": {"text": "Hello"}}]}
            with patch.object(main, "REGISTRY", root / "friends.json"), patch.object(main, "FRIENDS_DIR", root / "friends"), patch.object(main, "make_reply", return_value="在呢"), patch.object(main, "refresh_memory_if_due"):
                bridge.receive(event)
                bridge.receive(event)
                bridge.receive({**event, "self_id": 99999, "message_id": 43})
                bridge.receive({**event, "message_type": "group", "message_id": 44})
            sends = [call for call in fake.calls if call[0] == 'send_private_msg']
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0][1]["user_id"], 12345)
            store = FriendStore(friend)
            self.assertEqual(store.message_count(), 2)
            self.assertTrue(store.is_handled("42"))
            store.close()

    def test_fails_closed_when_login_account_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            main.dump_json(path / "friend.json", {"qq_id": "12345", "bridge_enabled": True})
            store = FriendStore(path)
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = "99999"
            with self.assertRaisesRegex(RuntimeError, "登录号"):
                bridge.send_text({"qq_id": "12345", "directory": str(path), "self_id": "77777"}, store, "Hello")
            self.assertFalse(fake.calls)
            store.close()

    def test_quote_uses_onebot_reply_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'friends' / '12345'
            main.dump_json(path / "friend.json", {"qq_id": "12345", "bridge_enabled": True})
            store = FriendStore(path)
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = "77777"
            record = {"qq_id": "12345", "directory": str(path), "self_id": "77777"}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            try:
                with patch.object(main, 'REGISTRY', root / 'friends.json'), \
                        patch.object(main, 'FRIENDS_DIR', root / 'friends'):
                    bridge.send_text(record, store, "收到", quote_to="42")
                sends = [call for call in fake.calls if call[0] == 'send_private_msg']
                self.assertEqual(sends[0][1]["message"][0],
                                 {"type": "reply", "data": {"id": "42"}})
            finally:
                store.close()

    def test_false_voice_claim_is_rewritten_before_send(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'friends' / '12345'
            main.dump_json(path / 'friend.json', {'qq_id': '12345', 'bridge_enabled': True})
            store = FriendStore(path)
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = '77777'
            record = {'qq_id': '12345', 'directory': str(path), 'self_id': '77777'}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            try:
                with patch.object(main, 'REGISTRY', root / 'friends.json'), \
                        patch.object(main, 'FRIENDS_DIR', root / 'friends'):
                    bridge.send_text(record, store, '语音发你了，听听看')
                sends = [call for call in fake.calls if call[0] == 'send_private_msg']
                text = ''.join(part.get('data', {}).get('text', '') for part in sends[0][1]['message'])
                self.assertIn('语音还没发出去', text)
                self.assertNotIn('发你了', text)
            finally:
                store.close()

    def test_segment_parser_keeps_image_marker(self):
        body, image = main.message_parts({"message": [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "abc"}},
        ]})
        self.assertEqual(body, "看这个[图片]")
        self.assertEqual(image, "abc")

    def test_native_faces_keep_names_ids_and_message_order(self):
        body, image = main.message_parts({'message': [
            {'type': 'text', 'data': {'text': '这个'}},
            {'type': 'face', 'data': {'id': '14'}},
            {'type': 'text', 'data': {'text': '然后'}},
            {'type': 'face', 'data': {'id': 5}},
        ]})
        self.assertEqual(body, '这个[QQ原生表情：微笑，编号14]然后[QQ原生表情：流泪，编号5]')
        self.assertIsNone(image)

    def test_unknown_face_keeps_number_without_guessing(self):
        body, image = main.message_parts({'message': [{'type': 'face', 'data': {'id': '999999'}}]})
        self.assertEqual(body, '[QQ原生表情：未知表情，编号999999]')
        self.assertIsNone(image)

    def test_outgoing_native_names_agree_with_installed_qq_mapping(self):
        for name, face_id in main.FACE_IDS.items():
            self.assertEqual(main.FACE_NAMES[str(face_id)], name)

    def test_websocket_action_and_event_share_connection(self):
        from websockets.sync.server import serve

        def handler(ws):
            request = json.loads(ws.recv())
            ws.send(json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0,
                                "data": {"user_id": 77777}}))
            ws.send(json.dumps({"post_type": "message", "message_type": "private",
                                "self_id": 77777, "user_id": 12345, "message_id": 42,
                                "message": [{"type": "text", "data": {"text": "Hello"}}]}))

        server = serve(handler, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = QQClient(url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        try:
            self.assertEqual(client.call("get_login_info")["user_id"], 77777)
            self.assertEqual(client.next_event(1)["user_id"], 12345)
        finally:
            client.close()
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
