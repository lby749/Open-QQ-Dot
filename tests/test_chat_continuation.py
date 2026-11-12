import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
from chat_continuation import decide
from store import FriendStore


class ChatContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '10001'
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)
        self.store = FriendStore(self.directory)
        self.profile = main.default_profile('10001', 'test', False)
        self.record = dict(qq_id='10001', self_id='90001', directory=str(self.directory))
        main.dump_json(self.directory / 'friend.json', self.profile)
        main.dump_json(self.root / 'friends.json', {'friends': {'10001': self.record}})
        registry = patch.object(main, 'REGISTRY', self.root / 'friends.json')
        friends = patch.object(main, 'FRIENDS_DIR', self.root / 'friends')
        registry.start()
        friends.start()
        self.addCleanup(registry.stop)
        self.addCleanup(friends.stop)
        self.now = datetime.now(timezone(timedelta(hours=9)))
        self.store.record_inbound('1', '你接着说')
        self.store.add_message('2', 'me', '刚想到一点')
        self.store.begin_reply_wait()
        self.arm()
        self.result = dict(action='wait', assessment='uncertain', reason='可能还没看到，不需要马上补充',
                           messages=[], next_check_at=(self.now + timedelta(minutes=5)).isoformat())
        self.model = Mock(side_effect=lambda *a, **k: (json.dumps(self.result), None))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def arm(self):
        self.store.set('chat_continuation', json.dumps(dict(state='pending',
                       generation=self.store.get('inbound_generation', '0'), next_check_at=self.now.isoformat())))

    def decide(self, now=None):
        return decide(self.store, self.profile, self.directory, now or self.now, self.model)

    def test_followups_stop_at_continuation_max_without_a_model_call(self):
        # 2026-10-13 user: how many follow-ups while the friend stays silent is a setting (default 1, 0 = none)
        self.decide()
        self.assertIn('DATA_FOLLOWUPS_LEFT: 1', self.model.call_args.args[0])
        plan = json.loads(self.store.get('chat_continuation'))
        self.store.set('chat_continuation', json.dumps({**plan, 'state': 'waiting', 'sent_count': 1,
                                                        'next_check_at': self.now.isoformat()}))
        self.assertIsNone(self.decide())
        self.model.assert_called_once()
        self.assertEqual(json.loads(self.store.get('chat_continuation'))['state'], 'ended')
        self.profile['continuation_max'] = 3
        self.arm()
        self.store.set('chat_continuation', json.dumps({**json.loads(self.store.get('chat_continuation')), 'sent_count': 1}))
        self.assertEqual(self.decide()['sent_count'], 1)
        self.assertIn('DATA_FOLLOWUPS_LEFT: 2', self.model.call_args.args[0])
        self.profile['continuation_max'] = 0
        self.arm()
        self.assertIsNone(self.decide())
        self.assertEqual(self.model.call_count, 2)

    def test_being_ignored_repeatedly_becomes_a_mood_until_the_friend_is_back(self):
        # 2026-10-13 user: after `after` unanswered unsolicited rounds the character is a little let down
        self.store.set('active_unanswered_rounds', '2')
        self.assertEqual(main.ignored_mood_line(self.store, self.profile), '')  # off by default
        self.profile.update(ignored_mood={'enabled': True, 'after': 2}, unanswered_round_limit=5)  # not yet paused
        self.decide()
        self.assertIn('OUR_MOOD: FRIEND_IGNORED_OUR_LAST_2_MESSAGES', self.model.call_args.args[0])
        self.assertEqual(main.current_emotions(self.store)['self'], '失落')
        events = self.store.db.execute("SELECT count(*) FROM emotion_events WHERE source='连续被无视'").fetchone()[0]
        main.ignored_mood_line(self.store, self.profile)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM emotion_events WHERE source='连续被无视'").fetchone()[0], events)  # once per level
        self.store.record_inbound('3', '刚刚在忙')
        self.assertEqual(main.ignored_mood_line(self.store, self.profile), '')
        self.assertIn('FRIEND_BACK_AFTER_IGNORING_2', main.ignored_mood_line(self.store, self.profile, reply=True))
        self.assertEqual(main.ignored_mood_line(self.store, self.profile, reply=True), '')  # only the first reply

    def test_wait_is_model_decision_even_when_reply_waiting_and_proactive_disabled(self):
        result = self.decide()
        self.assertEqual(result['action'], 'wait')
        self.store.set('chat_continuation', json.dumps(result))
        self.assertIsNone(self.decide(self.now + timedelta(minutes=1)))
        self.model.assert_called_once()
        self.assertTrue(self.model.call_args.kwargs['text_only'])
        self.assertIn('ALLOW_UNSURE', self.model.call_args.args[0])

    def test_decision_includes_context_and_timed_chat_once(self):
        self.profile['style'] = '唯一人设标记'
        self.store.record_inbound('context-test', '唯一近期聊天标记')
        self.arm()
        self.decide()
        prompt = self.model.call_args.args[0]
        self.assertEqual(prompt.count('唯一人设标记'), 1)
        self.assertEqual(prompt.count('唯一近期聊天标记'), 1)
        self.assertEqual(json.loads(self.store.get('last_generation_input'))['prompt'], prompt)

    def test_model_can_end_without_sending_farewell(self):
        self.result.update(action='end', next_check_at=None)
        result = self.decide()
        self.store.set('chat_continuation', json.dumps(result))
        self.assertEqual(result['state'], 'ended')
        self.assertIsNone(self.decide(self.now + timedelta(days=1)))

    def test_same_elapsed_time_can_yield_supplement_or_topic_change(self):
        for action in ('supplement', 'change_topic'):
            self.arm()
            self.result.update(action=action, messages=['还有个刚想起来的事'])
            self.assertEqual(self.decide()['action'], action)

    def test_new_inbound_cancels_old_plan(self):
        self.store.record_inbound('3', '等等')
        self.assertIsNone(self.decide())
        self.model.assert_not_called()

    def test_program_enforces_wait_without_messages_and_clamps_next_check(self):
        # 2026-10-10 user: hard limits are applied by the program instead of failing the whole decision
        self.result.update(action='wait', messages=['不应发送'], next_check_at=(self.now + timedelta(days=3)).isoformat())
        result = self.decide()
        self.assertEqual(result['messages'], [])
        self.assertEqual(datetime.fromisoformat(result['next_check_at']), self.now + timedelta(days=1))
        self.arm()
        self.result.update(action='supplement', messages=['一', '二', '三', '四', '五', '一'], next_check_at='不是时间')
        result = self.decide()
        self.assertEqual(result['messages'], ['一', '二', '三', '四 五'])
        self.assertEqual(datetime.fromisoformat(result['next_check_at']), self.now + timedelta(minutes=30))

    def test_invalid_decision_fails_closed_without_retry(self):
        self.result.update(action='maybe')
        with self.assertRaises(ValueError):
            self.decide()
        self.assertEqual(json.loads(self.store.get('chat_continuation'))['state'], 'failed')
        self.assertIsNone(self.decide(self.now + timedelta(hours=1)))

    def test_disabled_bridge_does_not_generate(self):
        self.profile['bridge_enabled'] = False
        self.assertIsNone(self.decide())
        self.model.assert_not_called()

    def test_three_minute_extra_check_is_once_and_survives_restart(self):
        plan = json.loads(self.store.get('chat_continuation'))
        plan.update(forced_check_at=(self.now + timedelta(minutes=3)).isoformat(), forced_check_done=False)
        self.store.set('chat_continuation', json.dumps(plan))
        result = self.decide()
        self.store.set('chat_continuation', json.dumps(result))
        self.assertIsNone(self.decide(self.now + timedelta(minutes=2, seconds=59)))
        self.store.close()
        self.store = FriendStore(self.directory)
        self.result['next_check_at'] = (self.now + timedelta(minutes=10)).isoformat()
        result = self.decide(self.now + timedelta(minutes=3))
        self.assertTrue(result['forced_check_done'])
        self.assertIn('FORCED_CHECK: 3_MIN_AFTER_REPLY', self.model.call_args.args[0])
        self.store.set('chat_continuation', json.dumps(result))
        self.assertIsNone(self.decide(self.now + timedelta(minutes=4)))
        self.assertEqual(self.model.call_count, 2)

    def test_three_minute_check_can_reconfirm_end(self):
        plan = json.loads(self.store.get('chat_continuation'))
        plan.update(forced_check_at=(self.now + timedelta(minutes=3)).isoformat(), forced_check_done=False)
        self.store.set('chat_continuation', json.dumps(plan))
        self.result.update(action='end', next_check_at=None)
        self.store.set('chat_continuation', json.dumps(self.decide()))
        self.assertIsNone(self.decide(self.now + timedelta(minutes=2)))
        result = self.decide(self.now + timedelta(minutes=3))
        self.assertEqual(result['action'], 'end')
        self.assertTrue(result['forced_check_done'])

    def test_new_inbound_cancels_three_minute_extra_check(self):
        plan = json.loads(self.store.get('chat_continuation'))
        plan.update(forced_check_at=(self.now + timedelta(minutes=3)).isoformat(), forced_check_done=False)
        self.store.set('chat_continuation', json.dumps(plan))
        self.store.record_inbound('3', '我回来了')
        self.assertIsNone(self.decide(self.now + timedelta(minutes=3)))
        self.model.assert_not_called()

    def bridge(self):
        bridge = main.Bridge.__new__(main.Bridge)
        bridge.self_id = '90001'
        bridge.client = Mock()
        bridge.client.next_event.return_value = None
        bridge.deferred = []
        bridge.log = Mock()
        main.dump_json(self.directory / 'friend.json', self.profile)
        def api(action, payload=None, **kwargs):
            if action == 'get_login_info':
                return {'user_id': 90001}
            if action == 'send_private_msg':
                return {'message_id': 'fake-receipt'}
            raise AssertionError(action)
        bridge.client.call.side_effect = api
        return bridge

    @staticmethod
    def sent_calls(bridge):
        return [call for call in bridge.client.call.call_args_list if call.args[0] == 'send_private_msg']

    def test_transport_sends_model_supplement_and_retains_next_judgment(self):
        bridge = self.bridge()
        self.result.update(action='supplement', messages=['刚才漏说了一点'])
        with patch.object(main, 'codex_text', self.model), patch.object(main, 'load_profile', return_value=self.profile):
            bridge.continue_chat(self.record, self.profile, self.store)
        self.assertEqual(len(self.sent_calls(bridge)), 1)
        self.assertEqual(json.loads(self.store.get('chat_continuation'))['state'], 'waiting')
        self.assertEqual(self.store.get('active_unanswered_rounds'), '1')

    def test_judgment_continues_the_friend_session_without_resending_persona(self):
        bridge = self.bridge()
        self.profile['style'] = '唯一人设标记'
        model = Mock(side_effect=lambda *a, **k: (json.dumps(self.result), 'thread-1'))
        with patch.object(main, 'codex_text', model), patch.object(main, 'load_profile', return_value=self.profile):
            bridge.continue_chat(self.record, self.profile, self.store)
            self.arm()
            bridge.continue_chat(self.record, self.profile, self.store)
        first, second = model.call_args_list
        self.assertIsNone(first.args[2])
        self.assertIn('唯一人设标记', first.args[0])
        self.assertEqual(second.args[2], 'thread-1')
        self.assertNotIn('唯一人设标记', second.args[0])
        self.assertIn('DATA_NOW: ', second.args[0])
        self.assertEqual(self.store.get('thread_id'), 'thread-1')

    def test_new_message_during_generation_prevents_stale_send(self):
        bridge = self.bridge()
        self.result.update(action='supplement', messages=['旧话题补充'])
        def generate(*a, **k):
            self.store.record_inbound('3', '换个话题')
            self.arm()
            return json.dumps(self.result), None
        with patch.object(main, 'codex_text', side_effect=generate):
            bridge.continue_chat(self.record, self.profile, self.store)
        self.assertEqual(self.sent_calls(bridge), [])
        self.assertEqual(json.loads(self.store.get('chat_continuation'))['generation'], self.store.get('inbound_generation'))

    def test_uncertain_send_is_never_repeated(self):
        bridge = self.bridge()
        self.result.update(action='supplement', messages=['补充'])
        def timeout(action, payload=None, **kwargs):
            if action == 'get_login_info':
                return {'user_id': 90001}
            if action == 'send_private_msg':
                row = self.store.db.execute("SELECT value FROM metadata WHERE key LIKE 'send_attempt:%'").fetchone()
                self.assertEqual(json.loads(row['value'])['state'], 'sending')
                raise RuntimeError('confirm')
            raise AssertionError(action)
        bridge.client.call.side_effect = timeout
        with patch.object(main, 'codex_text', self.model), patch.object(main, 'load_profile', return_value=self.profile):
            with self.assertRaises(RuntimeError):
                bridge.continue_chat(self.record, self.profile, self.store)
            bridge.continue_chat(self.record, self.profile, self.store)
        self.assertEqual(len(self.sent_calls(bridge)), 1)


if __name__ == '__main__':
    unittest.main()
