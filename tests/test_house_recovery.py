import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import house_status
import scheduler
from store import FriendStore



class FakeClient:
    def next_event(self, timeout=1):
        return None

    def call(self, action, *args, **kwargs):
        return fake_response(action)


def fake_response(action, *, history=None, send_responses=None):
    if action == 'get_login_info':
        return {'user_id': 77777}
    if action == 'send_private_msg':
        return next(send_responses) if send_responses is not None else {'message_id': 9001}
    if action == 'get_friend_msg_history':
        return {'messages': [] if history is None else history}
    if action == 'get_friend_list':
        return []
    return None


def sent_calls(mock):
    return [call for call in mock.call_args_list
            if call.args and call.args[0] == 'send_private_msg']


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        classification_enqueue = patch('message_classification.enqueue_for_database')
        classification_enqueue.start()
        self.addCleanup(classification_enqueue.stop)
        self.store = FriendStore(self.directory)
        self.profile = main.default_profile('12345', 'testfriend', False)
        self.profile['proactive']['smart_timing'] = False
        self.profile.update(bridge_enabled=True, auto_reply=True)
        self.record = {'qq_id': '12345', 'directory': str(self.directory), 'self_id': '77777'}
        main.dump_json(self.directory / 'friend.json', self.profile)
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': self.record}})
        self.event = {'post_type': 'message', 'message_type': 'private', 'self_id': 77777,
                      'user_id': 12345, 'message_id': 42,
                      'message': [{'type': 'text', 'data': {'text': '测试来信'}}]}
        self.fake = FakeClient()
        self.bridge = main.Bridge(self.fake)
        continuation = patch.object(self.bridge, 'continue_chat')
        continuation.start()
        self.addCleanup(continuation.stop)
        self.bridge.self_id = '77777'
        self.registry = patch.object(main, 'REGISTRY', self.root / 'friends.json')
        self.friends = patch.object(main, 'FRIENDS_DIR', self.root / 'friends')
        self.registry.start()
        self.friends.start()

    def tearDown(self):
        self.registry.stop()
        self.friends.stop()
        self.store.close()
        self.temp.cleanup()

    def test_failed_generation_is_retryable_without_duplicate_inbound(self):
        with patch.object(main, 'make_reply', side_effect=RuntimeError('modelfailed')):
            self.bridge.receive(self.event)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'failed')
        self.store.request_reply_retry('42')
        with patch.object(main, 'make_reply', return_value='测试回复'), patch.object(main, 'refresh_memory_if_due'), \
                patch.object(self.fake, 'call', wraps=self.fake.call) as send:
            self.bridge.tick()
            self.bridge.receive(self.event)
            self.bridge.tick()
        self.assertEqual(len(sent_calls(send)), 1)
        self.assertEqual(self.store.inbound_count(), 1)
        self.assertTrue(self.store.is_handled('42'))
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')
        with self.assertRaises(ValueError):
            self.store.request_reply_retry('42')

    def test_disabled_switches_record_and_resume_as_one_ordered_batch(self):
        for switch in ('bridge_enabled', 'auto_reply'):
            with self.subTest(switch=switch):
                self.store.clear_messages()
                self.profile[switch] = False
                main.dump_json(self.directory / 'friend.json', self.profile)
                with patch.object(main, 'make_reply') as model, patch.object(self.fake, 'call') as send:
                    self.bridge.receive(self.event)
                    self.bridge.receive(self.event)
                    self.bridge.receive(dict(self.event, message_id=43))
                    self.bridge.tick()
                model.assert_not_called()
                send.assert_not_called()
                self.assertEqual(self.store.inbound_count(), 2)
                self.assertEqual(self.store.reply_attempt('42')['state'], 'waiting')
                self.assertFalse(self.store.is_handled('42'))
                self.assertEqual(len(self.store.reply_problems()), 2)
                self.bridge.recover_requests()
                self.assertEqual(self.store.reply_attempt('42')['state'], 'waiting')
                self.profile[switch] = True
                main.dump_json(self.directory / 'friend.json', self.profile)
                with patch.object(main, 'make_reply', return_value='补回复') as model, \
                        patch.object(main, 'refresh_memory_if_due'), patch.object(self.fake, 'call', wraps=self.fake.call) as send:
                    self.bridge.tick()
                    self.bridge.tick()
                    self.bridge.tick()
                    self.bridge.receive(self.event)
                self.assertEqual(len(sent_calls(send)), 1)
                self.assertEqual([call.kwargs['target_message_id'] for call in model.call_args_list], ['43'])
                self.assertIn('consolidated_body', model.call_args.kwargs)
                self.assertTrue(self.store.is_handled('42'))
                self.assertTrue(self.store.is_handled('43'))

    def test_pause_during_generation_keeps_unsent_reply_for_resume(self):
        def pause(*args, **kwargs):
            self.profile['bridge_enabled'] = False
            main.dump_json(self.directory / 'friend.json', self.profile)
            return '尚未发送'
        with patch.object(main, 'make_reply', side_effect=pause), patch.object(self.fake, 'call') as send:
            self.bridge.receive(self.event)
        send.assert_not_called()
        self.assertEqual(self.store.reply_attempt('42')['state'], 'waiting')
        self.assertFalse(self.store.is_handled('42'))

    def test_paused_recording_still_rejects_other_accounts_and_unbound_friends(self):
        self.profile['bridge_enabled'] = False
        main.dump_json(self.directory / 'friend.json', self.profile)
        self.bridge.receive(dict(self.event, self_id=88888))
        self.bridge.receive(dict(self.event, user_id=67890))
        self.assertEqual(self.store.inbound_count(), 0)

    def test_resume_does_not_replay_sent_or_uncertain_messages(self):
        for message_id, state in [('sent', 'sent'), ('uncertain', 'uncertain')]:
            self.store.record_inbound(message_id, '旧来信')
            self.store.set_reply_attempt(message_id, state)
        self.store.mark_handled('sent')
        with patch.object(main, 'make_reply') as model, patch.object(self.fake, 'call') as send:
            self.bridge.tick()
            self.bridge.tick()
        model.assert_not_called()
        send.assert_not_called()

    def test_history_sync_restores_gap_but_does_not_reply_to_answered_history(self):
        self.store.add_message('anchor', 'me', '已有记录', '2023-11-14T22:15:00+00:00')
        rows = [dict(self.event, message_id='old', time=90),
                dict(self.event, message_id='answered', time=110),
                dict(self.event, message_id='manual', user_id=77777, post_type='message_sent', time=120),
                dict(self.event, message_id='missed', time=130),
                dict(self.event, message_id='other', user_id=67890, time=140),
                dict(self.event, message_id='wrong-account', self_id=88888, time=150)]
        for row in rows:
            row['time'] += 1700000000
        with patch.object(self.fake, 'call', side_effect=lambda action, *a, **kw:
                          fake_response(action, history=list(reversed(rows)))), \
                patch.object(main, 'make_reply') as model:
            self.bridge.sync_history()
            self.bridge.sync_history()
        model.assert_not_called()
        self.assertEqual(self.store.inbound_count(), 2)
        self.assertTrue(self.store.is_handled('answered'))
        self.assertEqual(self.store.reply_attempt('missed')['state'], 'waiting')
        self.assertFalse(self.store.is_handled('missed'))
        self.assertEqual(self.store.db.execute("SELECT created_at FROM messages WHERE qq_id='missed'").fetchone()[0],
                         datetime.fromtimestamp(1700000130).astimezone().isoformat())
        with patch.object(main, 'make_reply', return_value='补回复'), patch.object(main, 'refresh_memory_if_due'), \
                patch.object(self.fake, 'call', wraps=self.fake.call) as send:
            self.bridge.tick()
            self.bridge.tick()
        self.assertEqual(len(sent_calls(send)), 1)

    def test_history_sync_does_not_queue_prebinding_history_or_another_login(self):
        with patch.object(self.fake, 'call') as fetch:
            self.bridge.sync_history()
        fetch.assert_not_called()
        self.assertIsNotNone(self.store.get('history_sync_since'))
        self.bridge.self_id = '88888'
        with patch.object(self.fake, 'call') as fetch:
            self.bridge.sync_history()
        fetch.assert_not_called()

    def test_history_sync_does_not_treat_bridge_proactive_message_as_reply_to_missing_inbound(self):
        self.store.record_inbound('anchor', '已经回复的来信', '2023-11-14T22:15:00+00:00')
        self.store.mark_handled('anchor')
        self.store.add_message('proactive', 'me', 'proactivedays', '2023-11-14T22:16:20+00:00')
        rows = [dict(self.event, message_id='missed', time=1700000130),
                dict(self.event, message_id='proactive', user_id=77777, post_type='message_sent', time=1700000180)]
        with patch.object(self.fake, 'call', side_effect=lambda action, *a, **kw:
                          fake_response(action, history=rows)):
            self.bridge.sync_history()
        self.assertEqual(self.store.reply_attempt('missed')['state'], 'waiting')
        self.assertFalse(self.store.is_handled('missed'))

    def test_uncertain_send_is_never_retryable(self):
        def timeout_send(action, *args, **kwargs):
            if action == 'get_login_info':
                return {'user_id': 77777}
            if action == 'send_private_msg':
                raise TimeoutError('发送超时')
        with patch.object(main, 'make_reply', return_value='测试回复'), \
                patch.object(self.fake, 'call', side_effect=timeout_send):
            self.bridge.receive(self.event)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'uncertain')
        with self.assertRaises(ValueError):
            self.store.request_reply_retry('42')

    def test_restart_distinguishes_generation_and_send_and_interrupts_test(self):
        for message_id, state in [('one', 'processing'), ('two', 'sending'), ('three', 'queued')]:
            self.store.record_inbound(message_id, '临时测试')
            self.store.set_reply_attempt(message_id, state)
        self.store.set('proactive_test', json.dumps({'id': 'old', 'state': 'running'}))
        self.bridge.recover_requests()
        self.assertEqual(self.store.reply_attempt('one')['state'], 'failed')
        self.assertEqual(self.store.reply_attempt('two')['state'], 'uncertain')
        self.assertEqual(self.store.reply_attempt('three')['state'], 'failed')
        self.assertEqual(json.loads(self.store.get('proactive_test'))['state'], 'interrupted')

    def test_retry_does_not_send_if_auto_reply_is_now_off(self):
        self.store.record_inbound('42', '测试来信')
        self.store.set_reply_attempt('42', 'failed')
        self.store.request_reply_retry('42')
        self.profile['auto_reply'] = False
        main.dump_json(self.directory / 'friend.json', self.profile)
        with patch.object(main, 'make_reply') as model, patch.object(self.fake, 'call') as send:
            self.bridge.tick()
        model.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.store.reply_attempt('42')['state'], 'failed')

    def test_confirmed_send_followed_by_log_failure_cannot_be_retried(self):
        with patch.object(main, 'make_reply', return_value='测试回复'), \
                patch.object(self.bridge, 'log', side_effect=[None, OSError('日志写入失败'), None]):
            self.bridge.receive(self.event)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')
        self.assertTrue(self.store.is_handled('42'))
        with self.assertRaises(ValueError):
            self.store.request_reply_retry('42')

    def test_restart_blocks_legacy_unknown_records_and_recovers_all_active_requests(self):
        self.store.record_inbound('legacy', '旧记录')
        for index in range(35):
            key = str(index)
            self.store.record_inbound(key, '测试记录')
            self.store.set_reply_attempt(key, 'processing')
        self.bridge.recover_requests()
        self.assertEqual(self.store.reply_attempt('legacy')['state'], 'uncertain')
        with self.assertRaises(ValueError):
            self.store.request_reply_retry('legacy')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM reply_attempts WHERE state='failed'").fetchone()[0], 35)

    def test_retry_prompt_targets_original_inbound_with_later_context(self):
        self.store.record_inbound('42', '最初来信')
        self.store.record_inbound('43', '之后的来信')
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', return_value=('回复', '')) as model:
            main.make_reply(self.store, self.profile, self.directory, target_message_id='42')
        self.assertIn('OLD_MESSAGE_TEXT: "最初来信"', model.call_args.args[0])
        self.assertIn('之后的来信', model.call_args.args[0])

    def test_replan_preserves_attempted_slots_cooldown_and_unanswered(self):
        now = datetime(2026, 10, 1, 15, tzinfo=timezone(timedelta(hours=9)))
        day = '2026-10-01'
        slots = [(now + timedelta(hours=offset)).isoformat() for offset in (-3, -2, -1, 5)]
        self.store.set('plan:' + day, json.dumps(slots))
        self.store.set('slot:' + day + ':0', 'sent')
        self.store.set('slot:' + day + ':1', 'failed')
        self.store.set('slot:' + day + ':2', 'missed')
        self.store.set('eligible_after', (now + timedelta(hours=1)).isoformat())
        self.store.set('unanswered_rounds', '2')
        self.store.set('proactive_paused', '1')
        settings = {'rounds_min': 3, 'rounds_max': 3, 'window_start': '14:00', 'window_end': '18:00'}
        count = scheduler.replan_remaining(self.store, now, settings, random.Random(2))
        self.assertEqual(count, 1)
        plan = json.loads(self.store.get('plan:' + day))
        self.assertEqual(plan[:4], slots)
        self.assertEqual(self.store.get('slot:' + day + ':0'), 'sent')
        self.assertEqual(self.store.get('slot:' + day + ':1'), 'failed')
        self.assertEqual(self.store.get('slot:' + day + ':3'), 'rescheduled')
        self.assertGreater(datetime.fromisoformat(plan[-1]), now + timedelta(hours=1))
        self.assertLess(datetime.fromisoformat(plan[-1]), now.replace(hour=18))
        self.assertEqual(self.store.get('unanswered_rounds'), '2')
        self.assertEqual(self.store.get('proactive_paused'), '1')

    def test_replan_cross_midnight_retains_window_day(self):
        now = datetime(2026, 10, 2, 1, tzinfo=timezone(timedelta(hours=9)))
        settings = {'rounds_min': 2, 'rounds_max': 2, 'window_start': '22:00', 'window_end': '03:00'}
        self.assertEqual(scheduler.replan_remaining(self.store, now, settings, random.Random(2)), 2)
        slots = json.loads(self.store.get('plan:2026-10-01'))
        self.assertTrue(all(now < datetime.fromisoformat(slot) < now.replace(hour=3) for slot in slots))
        self.assertIsNone(self.store.get('plan:2026-10-02'))

    def test_replan_does_not_reset_exhausted_budget_or_other_friend(self):
        now = datetime(2026, 10, 1, 15, tzinfo=timezone(timedelta(hours=9)))
        self.store.set('plan:2026-10-01', json.dumps([(now - timedelta(hours=1)).isoformat()]))
        self.store.set('slot:2026-10-01:0', 'sent')
        settings = {'rounds_min': 1, 'rounds_max': 1, 'window_start': '14:00', 'window_end': '18:00'}
        other = FriendStore(self.root / 'friends' / '67890')
        try:
            other.set('plan:2026-10-01', '[]')
            before = other.path.read_bytes()
            self.assertEqual(scheduler.replan_remaining(self.store, now, settings, random.Random(2)), 0)
            self.assertEqual(len(json.loads(self.store.get('plan:2026-10-01'))), 1)
            self.assertEqual(other.path.read_bytes(), before)
        finally:
            other.close()

    def test_replan_without_remaining_time_leaves_plan_untouched(self):
        now = datetime(2026, 10, 1, 19, tzinfo=timezone(timedelta(hours=9)))
        self.store.set('plan:2026-10-01', '[]')
        settings = {'rounds_min': 1, 'rounds_max': 1, 'window_start': '14:00', 'window_end': '18:00'}
        with self.assertRaisesRegex(ValueError, '无可用时间'):
            scheduler.replan_remaining(self.store, now, settings)
        self.assertEqual(self.store.get('plan:2026-10-01'), '[]')

    def test_backend_replans_from_latest_saved_settings_even_when_friend_paused(self):
        self.profile['bridge_enabled'] = False
        self.profile['proactive'] = main.default_profile('12345', 'testfriend', True)['proactive']
        self.profile['proactive']['rounds_max'] = 7
        main.dump_json(self.directory / 'friend.json', self.profile)
        self.store.set('replan_request', json.dumps({'state': 'pending'}))
        with patch.object(main, 'replan_remaining', return_value=2) as replan, patch.object(self.fake, 'call') as send:
            self.bridge.tick()
        self.assertEqual(replan.call_args.args[2]['rounds_max'], 7)
        self.assertEqual(json.loads(self.store.get('replan_request'))['state'], 'done')
        send.assert_not_called()

    def test_status_explains_actual_effect_of_switches(self):
        self.profile['proactive']['enabled'] = True
        text = house_status.effective_house_status(self.profile, self.store, True)
        self.assertTrue('定时主动：' in text or 'proactive:' in text.lower())
        self.assertNotIn('手机上下文', text)
        self.profile['bridge_enabled'] = False
        self.assertIn('好友已暂停', house_status.effective_house_status(self.profile, self.store, True))
        self.assertIn('桥接未启动', house_status.effective_house_status(self.profile, self.store, False))


if __name__ == '__main__':
    unittest.main()
