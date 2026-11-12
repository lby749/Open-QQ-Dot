import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from store import FriendStore
from scheduler import due_slot


class ReplyPauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.start = datetime(2026, 10, 2, 8, tzinfo=timezone(timedelta(hours=9)))
        self.store.set('reply_waiting', '1')
        self.store.set('reply_waiting_since', self.start.isoformat())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_exact_timeout_restores_both_schedulers(self):
        settings = {'enabled': True, 'window_start': '06:00', 'window_end': '23:00'}
        at = self.start + timedelta(minutes=30)
        self.store.set('plan:2026-10-02', '["' + at.isoformat() + '"]')
        self.assertIsNotNone(self.store.refresh_reply_pause(30, at - timedelta(seconds=1)))
        self.assertIsNone(due_slot(self.store, at, settings))
        self.assertIsNone(self.store.refresh_reply_pause(30, at))
        self.assertIsNotNone(due_slot(self.store, at, settings))

    def test_new_message_releases_early_but_duplicate_does_not(self):
        self.store.record_inbound('1', '新消息')
        self.assertIsNone(self.store.refresh_reply_pause(60, self.start))
        self.store.begin_reply_wait()
        self.store.record_inbound('1', '新消息')
        self.assertEqual(self.store.get('reply_waiting'), '1')

    def test_duration_change_recalculates_from_original_send(self):
        now = self.start + timedelta(minutes=40)
        self.assertIsNotNone(self.store.refresh_reply_pause(60, now))
        self.assertIsNone(self.store.refresh_reply_pause(30, now))

    def test_restart_midnight_and_friend_isolation(self):
        self.start = self.start.replace(hour=23, minute=40)
        self.store.set('reply_waiting_since', self.start.isoformat())
        self.store.close()
        self.store = FriendStore(self.directory)
        at = self.start + timedelta(minutes=60)
        self.assertEqual(self.store.refresh_reply_pause(60, at - timedelta(seconds=1)), at)
        other = FriendStore(self.directory / 'other')
        try:
            self.assertIsNone(other.refresh_reply_pause(60, self.start))
        finally:
            other.close()
        self.assertIsNone(self.store.refresh_reply_pause(60, at))

    def test_legacy_two_round_pause_expires_without_clearing_other_guards(self):
        self.store.set('reply_waiting', '0')
        self.store.set('reply_waiting_since', '')
        self.store.set('proactive_paused', '1')
        self.store.set('unanswered_rounds', '2')
        self.store.set('paused_reason', 'Pause')
        self.store.add_message('out', 'me', '以前的回复', self.start.isoformat())
        self.store.refresh_reply_pause(60, self.start + timedelta(minutes=60))
        self.assertEqual(self.store.get('proactive_paused'), '0')
        self.assertEqual(self.store.get('unanswered_rounds'), '0')
        self.assertEqual(self.store.get('paused_reason'), 'Pause')

    def test_short_wait_expiry_keeps_round_count_and_second_round_pauses_five_hours(self):
        self.store.complete_active_round(2, self.start)
        second = self.start + timedelta(minutes=60)
        self.store.refresh_reply_pause(60, second)
        self.assertEqual(self.store.get('active_unanswered_rounds'), '1')
        self.store.complete_active_round(2, second)
        until = second + timedelta(minutes=300)
        self.assertEqual(self.store.refresh_round_pause(2, 300, until - timedelta(seconds=1)), until)
        self.assertIsNone(self.store.refresh_round_pause(2, 300, until))
        self.assertEqual(self.store.get('active_unanswered_rounds'), '0')

    def test_new_reply_clears_long_pause_and_round_count(self):
        self.store.complete_active_round(1, self.start)
        self.assertIsNotNone(self.store.refresh_round_pause(1, 300, self.start))
        self.store.record_inbound('new', '回复')
        self.assertIsNone(self.store.refresh_round_pause(1, 300, self.start))
        self.assertEqual(self.store.get('active_unanswered_rounds'), '0')

    def test_round_pause_duration_edit_and_restart(self):
        self.store.complete_active_round(1, self.start)
        self.store.close()
        self.store = FriendStore(self.directory)
        now = self.start + timedelta(minutes=90)
        self.assertIsNotNone(self.store.refresh_round_pause(1, 300, now))
        self.assertIsNone(self.store.refresh_round_pause(1, 60, now))
