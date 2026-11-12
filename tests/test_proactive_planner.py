import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import agenda
import house_status
import main
from proactive_planner import update_activity_memory
from store import FriendStore


class AgendaTests(unittest.TestCase):
    """2026-10-13 user: proactive chat and care are the model's own todos (agenda.py)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.now = datetime(2026, 10, 2, 20, tzinfo=timezone(timedelta(hours=9)))
        self.profile = main.default_profile('10001', 'test', True)
        self.profile['proactive'].update(window_start='09:00', window_end='10:00', bubbles_min=1, bubbles_max=1,
                                         rounds_min=1, rounds_max=2)
        self.model = Mock(side_effect=lambda *a, **k: (json.dumps(self.decision), None))
        self.decision = {'send_now': False, 'reason': '她说今晚要忙', 'topic': '',
                         'todos': [{'at': (self.now + timedelta(hours=1, minutes=13)).isoformat(), 'kind': 'peek',
                                    'note': '看看忙完没'}], 'min_exception': '她说今晚要忙'}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def wake(self, now=None):
        return agenda.wake(self.store, self.profile, self.directory, now or self.now, self.model)

    def test_first_wake_plans_and_nothing_is_judged_until_a_todo_is_due(self):
        self.assertIsNone(self.wake())
        self.assertIn(' [OUR_TODOS] ', self.model.call_args.args[0])
        [todo] = agenda.todos(self.store)
        at = datetime.fromisoformat(todo['at'])
        self.assertEqual((todo['kind'], todo['note']), ('peek', '看看忙完没'))
        # 2026-10-13 user (): the program moves the model's time by a few minutes
        self.assertLessEqual(abs(at - (self.now + timedelta(hours=1, minutes=13))), timedelta(minutes=10))
        self.assertIsNone(self.wake(at - timedelta(seconds=1)))
        self.model.assert_called_once()
        self.wake(at)
        self.assertEqual(self.model.call_count, 2)
        self.assertIn('看看忙完没', self.model.call_args.args[0])  # the due todo reaches the model

    def test_program_keeps_todos_apart_inside_two_days_and_never_empty(self):
        at = lambda minutes: (self.now + timedelta(minutes=minutes)).isoformat()
        raw = [{'at': at(5), 'note': '太近'}, {'at': at(60), 'kind': 'contact', 'note': '一'},
               {'at': at(70), 'note': '离上一个太近'}, {'at': at(3 * 24 * 60), 'note': '太远'},
               {'at': 'bad'}, {'at': at(120), 'kind': 'peek', 'note': '二'}]
        kept = agenda.clean_todos(raw, self.now, timedelta(minutes=30))
        self.assertEqual([item['note'] for item in kept], ['一', '二'])
        self.assertEqual(kept[0]['kind'], 'contact')
        empty = agenda.clean_todos([], self.now, timedelta(minutes=30))
        self.assertEqual((len(empty), empty[0]['kind']), (1, 'peek'))
        self.assertEqual(empty[0]['at'], (self.now + agenda.EMPTY_RETRY).isoformat())

    def test_send_now_writes_the_round_and_the_daily_cap_holds(self):
        self.decision.update(send_now=True, topic='流星雨', messages=['看到今晚有流星雨'])
        key = self.wake()
        self.assertTrue(key.startswith('2026-10-02:smart:'))
        self.assertEqual(json.loads(self.store.get('smart_prepared')), {'key': key, 'messages': ['看到今晚有流星雨']})
        for index in range(2):
            self.store.set(f'slot:2026-10-02:smart:{index}', 'sent')
        self.store.set('agenda', json.dumps([{'at': (self.now + timedelta(hours=2)).isoformat(), 'kind': 'contact',
                                              'note': '再找她'}]))
        self.decision['messages'] = ['再说一句']
        self.assertIsNone(self.wake(self.now + timedelta(hours=2)))
        self.assertIn('daysproactive', self.store.get('smart_status'))

    def test_window_and_unanswered_pause_do_not_hold_it_but_the_phone_and_care_ideas_reach_it(self):
        # 20:00 is outside the 09:00-10:00 window, and two rounds went unanswered
        self.store.set('active_unanswered_rounds', '2')
        self.store.set('round_pause_since', self.now.isoformat())
        self.profile['scheduled_care'] = {'enabled': True}
        self.decision.update(send_now=True, messages=['在干嘛'])
        with patch.object(agenda, 'phone_look', return_value='{"foreground_app":"抖音"}'):
            self.assertIsNotNone(self.wake())
        prompt = self.model.call_args.args[0]
        # 2026-10-26: a plan todo no longer auto-reads the phone; only a peek todo does.
        self.assertNotIn('DATA_PHONE_NOW:', prompt)
        self.assertIn('CARE_IDEAS', prompt)
        self.assertIn('"unanswered_rounds": 2', prompt)

    def test_a_failed_judgment_waits_and_keeps_the_todo(self):
        self.model.side_effect = None
        self.model.return_value = ('不是 JSON', None)
        with self.assertRaises(ValueError):
            self.wake()
        self.assertIsNone(self.wake(self.now + timedelta(minutes=29)))
        self.model.assert_called_once()
        self.assertIn('30minutesRetry', self.store.get('smart_status'))

    def test_status_shows_the_next_todo(self):
        self.wake()
        with patch.object(house_status, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            text = house_status.effective_house_status(self.profile, self.store, True)
        self.assertIn('模型自己排待办', text)
        at = datetime.fromisoformat(agenda.todos(self.store)[0]['at'])
        self.assertTrue(f'下一个待办 {at:%m-%d %H:%M}（只看看：看看忙完没' in text or f'next agenda {at:%m-%d %H:%M}' in text)

    def test_daily_minimum_is_filled_unless_the_model_gives_a_reason(self):
        # 2026-10-13 user ()
        self.profile['proactive'].update(rounds_min=2)
        self.decision['min_exception'] = ''
        self.wake()
        kinds = [item['kind'] for item in agenda.todos(self.store)]
        self.assertEqual((kinds.count('contact'), kinds.count('peek')), (2, 1))
        self.assertTrue(all(datetime.fromisoformat(item['at']) < self.now.replace(hour=23, minute=46)
                            for item in agenda.todos(self.store) if item['kind'] == 'contact'))
        self.assertIn('"rounds_min_today": 2', self.model.call_args.args[0])
        self.store.set('agenda', json.dumps([{'at': self.now.isoformat(), 'kind': 'contact', 'note': '到点'}]))
        self.decision['min_exception'] = '她说今晚要通宵复习，别打扰'
        self.wake()
        self.assertEqual([item['kind'] for item in agenda.todos(self.store)], ['peek'])

    def test_sparse_data_and_single_day_cannot_establish_habit(self):
        for index in range(10):
            self.store.add_message(str(index), 'friend', 'days', self.now.isoformat())
        update_activity_memory(self.store, self.now)
        self.assertNotIn('[回复时段观察]', self.store.memory())

    def test_multi_day_time_observation_preserves_memory_has_sources_and_is_idempotent(self):
        self.store.set_memory('原有明确偏好')
        for index in range(5):
            self.store.add_message(str(index), 'friend', '真实来信', (self.now - timedelta(days=index)).isoformat())
        update_activity_memory(self.store, self.now)
        text = self.store.memory()
        self.assertIn('原有明确偏好', text)
        self.assertIn('20:00–22:00', text)
        self.assertIn('不代表真实上线', text)
        revision = self.store.memory_revision()
        update_activity_memory(self.store, self.now)
        self.assertEqual(revision, self.store.memory_revision())
        row = self.store.db.execute("SELECT * FROM memory_entries WHERE kind='observation' AND active=1").fetchone()
        self.assertEqual(set(json.loads(row['sources'])), set(str(i) for i in range(5)))

    def test_memory_reset_does_not_relearn_old_reply_patterns(self):
        for index in range(5):
            self.store.add_message(str(index), 'friend', '真实来信', (self.now - timedelta(days=index)).isoformat())
        update_activity_memory(self.store, self.now)
        self.store.reset_memory()
        update_activity_memory(self.store, self.now)
        self.assertEqual(self.store.memory(), '')

    def test_real_bridge_sends_what_the_model_chose_from_its_todos(self):
        record = {'qq_id': '10001', 'directory': str(self.directory), 'self_id': '99999'}
        bridge = main.Bridge(Mock()); bridge.self_id = '99999'
        self.decision.update(send_now=True, messages=['在吗'])
        with patch.object(main, 'load_registry', return_value={'friends': {'10001': record}}), \
                patch.object(main, 'load_profile', return_value=self.profile), \
                patch.object(main, 'datetime', wraps=datetime) as clock, \
                patch.object(main, 'codex_text', self.model), patch.object(bridge, 'send_round') as send, \
                patch.object(main, 'due_slot') as old_scheduler:
            clock.now.return_value = self.now
            bridge.tick()
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs['agenda'])
        old_scheduler.assert_not_called()

    def test_heartbeat_makes_at_most_one_unsolicited_judgment(self):
        # 2026-10-10 6stage: continuation first, then the todos, one per heartbeat
        record = {'qq_id': '10001', 'directory': str(self.directory), 'self_id': '99999'}
        bridge = main.Bridge(Mock()); bridge.self_id = '99999'
        self.decision.update(send_now=True, messages=['在吗'])
        for judged, expected_rounds in ((True, 0), (False, 1)):
            with self.subTest(judged=judged), \
                    patch.object(main, 'load_registry', return_value={'friends': {'10001': record}}), \
                    patch.object(main, 'load_profile', return_value=self.profile), \
                    patch.object(main, 'datetime', wraps=datetime) as clock, \
                    patch.object(main, 'codex_text', self.model), \
                    patch.object(bridge, 'continue_chat', return_value=judged), \
                    patch.object(bridge, 'send_round') as send:
                clock.now.return_value = self.now
                bridge.tick()
            self.assertEqual(send.call_count, expected_rounds)


if __name__ == '__main__':
    unittest.main()
