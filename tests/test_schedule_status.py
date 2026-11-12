import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import house_status
import main
from store import FriendStore


class ScheduleStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = FriendStore(self.root / 'one')
        self.now = datetime(2026, 10, 2, 22, 30, tzinfo=timezone(timedelta(hours=9)))
        self.profile = main.default_profile('10001', 'test', True)
        self.profile['proactive'].update(enabled=True, window_start='09:00', window_end='02:00', smart_timing=False)
        self.profile['scheduled_care'] = {'enabled': True, 'slots': {
            name: {'enabled': name == 'night', 'start': '23:00', 'end': '00:00'}
            for name in ('morning', 'lunch', 'evening', 'night')}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def status(self, store=None):
        with patch.object(house_status, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            return house_status.effective_house_status(self.profile, store or self.store, True)

    def plan(self, store=None, at=None):
        store = store or self.store
        at = at or self.now + timedelta(hours=1)
        store.set('plan:2026-10-02', json.dumps([at.isoformat()]))
        return at

    def test_displays_saved_next_time_without_redrawing_random_plan(self):
        at = self.plan()
        raw = self.store.get('plan:2026-10-02')
        self.assertIn('下次计划 ' + at.strftime('%m-%d %H:%M:%S'), self.status())
        self.assertEqual(self.store.get('plan:2026-10-02'), raw)

    def test_finished_expired_or_unreachable_plan_is_not_next(self):
        at = self.plan()
        self.store.set('slot:2026-10-02:0', 'sent')
        self.assertIn('本时段无剩余计划', self.status())
        self.store.db.execute("DELETE FROM metadata WHERE key='slot:2026-10-02:0'")
        self.store.db.commit()
        self.store.set('eligible_after', (at + timedelta(minutes=11)).isoformat())
        self.assertIn('本时段无剩余计划', self.status())
        self.store.set('eligible_after', self.now.isoformat())
        self.plan(at=self.now - timedelta(minutes=11))
        self.assertIn('本时段无剩余计划', self.status())

    def test_no_plan_and_outside_window_are_honest(self):
        self.assertIn('等待后台生成计划', self.status())
        self.now = self.now.replace(hour=4)
        self.assertIn('下一时段 10-02 09:00（待生成计划）', self.status())
        self.assertIsNone(self.store.get('plan:2026-10-02'))

    def test_cross_midnight_uses_previous_day_plan(self):
        self.now += timedelta(hours=2)
        at = self.plan(at=self.now + timedelta(minutes=30))
        self.assertIn('下次计划 ' + at.strftime('%m-%d %H:%M:%S'), self.status())

    def test_care_displays_actual_pick_and_after_sent_waits_for_next_day(self):
        self.store.set('care_plan:2026-10-02:night', json.dumps({
            'start': '2026-10-02T23:00:00+09:00', 'end': '2026-10-03T00:00:00+09:00',
            'at': '2026-10-02T23:50:20+09:00'}))
        self.assertIn('下次计划 10-02 23:50:20（晚安）', self.status())
        self.store.set('care:2026-10-02:night', 'sent')
        self.assertIn('下次时段 10-03 23:00（晚安，待抽取）', self.status())

    def test_changed_care_window_ignores_old_pick(self):
        self.store.set('care_plan:2026-10-02:night', json.dumps({
            'start': '2026-10-02T20:00:00+09:00', 'end': '2026-10-02T21:00:00+09:00',
            'at': '2026-10-02T20:50:00+09:00'}))
        self.assertIn('下次时段 10-02 23:00（晚安，待抽取）', self.status())
        self.assertNotIn('20:50', self.status())

    def test_two_friends_keep_separate_next_times(self):
        other = FriendStore(self.root / 'two')
        try:
            first = self.plan()
            second = self.plan(other, self.now + timedelta(minutes=20))
            self.assertIn(first.strftime('%H:%M:%S'), self.status())
            self.assertIn(second.strftime('%H:%M:%S'), self.status(other))
            self.assertNotIn(first.strftime('%H:%M:%S'), self.status(other))
        finally:
            other.close()

    def test_one_friends_scheduling_error_does_not_stop_another(self):
        records = {}
        for index, qq in enumerate(('10001', '10002')):
            d = self.root / qq
            friend = FriendStore(d)
            self.plan(friend, self.now - timedelta(minutes=1))
            friend.close()
            records[qq] = {'qq_id': qq, 'directory': str(d), 'self_id': '99999'}
        bridge = main.Bridge(Mock())
        bridge.self_id = '99999'
        with patch.object(main, 'load_registry', return_value={'friends': records}), \
                patch.object(main, 'load_profile', return_value=self.profile), \
                patch.object(main, 'datetime', wraps=datetime) as clock, \
                patch.object(bridge, 'send_round', side_effect=[RuntimeError('friendfailed'), None]) as send:
            clock.now.return_value = self.now
            bridge.tick()
        self.assertEqual([call.args[0]['qq_id'] for call in send.call_args_list], ['10001', '10002'])
        first = FriendStore(self.root / '10001')
        second = FriendStore(self.root / '10002')
        try:
            self.assertIn('friendfailed', first.get('bridge_error'))
            self.assertFalse(second.get('bridge_error'))
        finally:
            first.close()
            second.close()


if __name__ == '__main__':
    unittest.main()
