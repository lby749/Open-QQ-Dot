import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from datetime import datetime
from topic_library import call_tool
from store import FriendStore


def today():
    return datetime.now().astimezone().date().isoformat()


class ProactivePreferenceTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def test_familiarity_uses_actual_two_way_history_not_persona_or_outgoing_only(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            try:
                self.assertIn('FIRST_BUBBLE=GREETING', main.familiarity_context(store))
                for index in range(30):
                    store.add_message('out' + str(index), 'me', '单向消息')
                self.assertIn('STAGE=NO_REPLY_YET', main.familiarity_context(store))
                for index in range(4):
                    store.record_inbound('in' + str(index), '回复')
                self.assertIn('STAGE=FIRST_ACQUAINTANCE', main.familiarity_context(store))
                store.record_inbound('in4', '回复')
                self.assertIn('STAGE=GETTING_TO_KNOW', main.familiarity_context(store))
                for index in range(5, 20):
                    store.record_inbound('in' + str(index), '回复')
                self.assertIn('STAGE=ESTABLISHED', main.familiarity_context(store))
            finally:
                store.close()

    def test_reserve_decision_uses_today_chat_in_both_proactive_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            try:
                profile = {'basic_info': {'notes': 'astronomy'}, 'proactive': {}, 'model_api': {}}
                (Path(folder) / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
                store.add_message('yesterday', 'friend', '昨天的过时话题', '2000-01-01T12:00:00+09:00')
                store.add_message('early', 'friend', '今天先聊的事情，不想频繁被推新话题', today() + 'T10:00:00+09:00')
                for index in range(45):
                    store.add_message(str(index), 'me', 'daysnormal', today() + 'T11:00:00+09:00')
                generate = Mock(return_value=(json.dumps({'messages': ['自然接话']}), ''))
                with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', generate):
                    main.make_round(store, profile, Path(folder), 1)
                self.assertIn('今天先聊的事情', generate.call_args.args[0])  # Preserve the complete latest conversation round.
                self.assertIn('get_today_chat', generate.call_args.args[0])
                self.assertIn('今天先聊的事情', json.dumps(call_tool(Path(folder), 'get_today_chat', {'query': '今天先聊'}), ensure_ascii=False))
                self.assertNotIn('昨天的过时话题', generate.call_args.args[0])
            finally:
                store.close()

    def test_both_generation_paths_use_only_current_friend_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            try:
                for style, topic, other in (("偏轻松的短句甲", "astronomy", "偏正式乙"), ("偏正式乙", "历史乙", "偏轻松的短句甲")):
                    profile = {"style": "normal reply", "proactive": {"style": style, "topic_types": [topic]}, "model_api": {}, "basic_info": {"nickname": "测试称呼"}}
                    generate = Mock(return_value=(json.dumps({"messages": ["一个新的开场白"]}), ""))
                    with patch.object(main, "handoff_if_due"), patch.object(main, "codex_text", generate):
                        self.assertEqual(main.make_round(store, profile, Path(folder), 1), ["一个新的开场白"])
                    prompt = generate.call_args.args[0]
                    self.assertIn("测试称呼", prompt)
                    self.assertIn("NOT_EVERY_BUBBLE", prompt)
                    self.assertIn(style, prompt)
                    self.assertIn(topic, prompt)
                    self.assertNotIn(other, prompt)
                    self.assertIn('NO_QUESTION_OK', prompt)
                    self.assertIn('PROACTIVE_STYLE_PRIORITY', prompt)
                    self.assertNotIn('彼此独立', prompt)
                    self.assertNotIn('再留一个容易接的问句或选择', prompt)
            finally:
                store.close()
