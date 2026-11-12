import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scheduler import automatic_cooldown, automatic_cooldown_range, defer_automatically, sync_automatic_cooldown, make_plan, due_slot
from store import FriendStore


class AutomaticCooldownTests(unittest.TestCase):
    def test_density_and_overnight_window(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
            settings = {'enabled': True, 'window_start': '00:01', 'window_end': '23:59', 'rounds_min': 20, 'rounds_max': 30}
            try:
                sparse = automatic_cooldown(store, {**settings, 'rounds_min': 6, 'rounds_max': 6}, now)
                dense = automatic_cooldown(store, settings, now)
                self.assertLess(dense, sparse)
                self.assertLess(dense, 3600)
                short = automatic_cooldown(store, {**settings, 'window_start': '22:00', 'window_end': '02:00'}, now)
                self.assertLess(short, dense)
                store.set('plan:2026-10-02', json.dumps(['unused'] * 24))
                low, high = automatic_cooldown_range(store, settings, now)
                self.assertEqual(high, round(1438 * 60 / 25 * .6))
                self.assertLess(low, high)
                self.assertTrue(low <= automatic_cooldown(store, settings, now) <= high)
            finally:
                store.close()

    def test_legacy_cooldown_recalculated_from_activity_not_current_time(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
            settings = {'window_start': '00:00', 'window_end': '23:59', 'rounds_min': 24, 'rounds_max': 24}
            try:
                store.add_message('1', 'friend', '真实来信', now.isoformat())
                store.set('eligible_after', (now + timedelta(hours=2)).isoformat())
                sync_automatic_cooldown(store, settings, now)
                first = store.get('eligible_after')
                self.assertLess(datetime.fromisoformat(first), now + timedelta(hours=1))
                sync_automatic_cooldown(store, settings, now + timedelta(minutes=5))
                self.assertEqual(store.get('eligible_after'), first)
                settings['rounds_min'] = settings['rounds_max'] = 30
                sync_automatic_cooldown(store, settings, now + timedelta(minutes=5))
                self.assertLess(datetime.fromisoformat(store.get('eligible_after')), datetime.fromisoformat(first))
                store.set('cooldown_kind', 'care')
                store.set('eligible_after', first)
                sync_automatic_cooldown(store, settings, now)
                self.assertEqual(store.get('eligible_after'), first)
            finally:
                store.close()

    def test_normal_plan_not_blocked_by_its_own_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            start = datetime(2026, 10, 2, 0, 1, tzinfo=timezone(timedelta(hours=9)))
            settings = {'enabled': True, 'window_start': '00:01', 'window_end': '23:59', 'rounds_min': 24, 'rounds_max': 24}
            slots = make_plan(start, settings, random.Random(5))
            store.set('plan:2026-10-02', json.dumps(slots))
            try:
                for i, slot in enumerate(slots):
                    at = datetime.fromisoformat(slot)
                    result = due_slot(store, at, settings)
                    self.assertEqual(result[0], f'2026-10-02:{i}')
                    store.set('slot:' + result[0], 'sent')
                    defer_automatically(store, settings, at + timedelta(minutes=3))
            finally:
                store.close()

    def test_each_activity_draws_once_and_checks_and_restart_keep_the_result(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FriendStore(Path(folder))
            now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
            settings = {'window_start': '09:00', 'window_end': '02:00', 'rounds_min': 8, 'rounds_max': 8}
            try:
                with patch('scheduler.random.random', side_effect=[0.1, 0.9]) as draw:
                    defer_automatically(store, settings, now)
                    first = store.get('eligible_after')
                    sync_automatic_cooldown(store, settings, now + timedelta(minutes=5))
                    self.assertEqual(store.get('eligible_after'), first)
                    store.close(); store = FriendStore(Path(folder))
                    sync_automatic_cooldown(store, settings, now + timedelta(minutes=10))
                    self.assertEqual(store.get('eligible_after'), first)
                    self.assertEqual(draw.call_count, 1)
                    defer_automatically(store, settings, now + timedelta(minutes=15))
                    self.assertEqual(draw.call_count, 2)
                    self.assertGreater(automatic_cooldown(store, settings, now),
                                       (datetime.fromisoformat(first) - now).total_seconds())
            finally:
                store.close()

    def test_friends_draw_separately_and_settings_changes_do_not_redraw(self):
        with tempfile.TemporaryDirectory() as folder:
            one = FriendStore(Path(folder) / 'one')
            two = FriendStore(Path(folder) / 'two')
            now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
            settings = {'window_start': '09:00', 'window_end': '02:00', 'rounds_min': 6, 'rounds_max': 10}
            try:
                with patch('scheduler.random.random', side_effect=[0.1, 0.9]) as draw:
                    defer_automatically(one, settings, now)
                    defer_automatically(two, settings, now)
                    self.assertNotEqual(one.get('eligible_after'), two.get('eligible_after'))
                    fraction = one.get('cooldown_draw')
                    sync_automatic_cooldown(one, {**settings, 'rounds_min': 20, 'rounds_max': 30}, now)
                    self.assertEqual(one.get('cooldown_draw'), fraction)
                    self.assertEqual(draw.call_count, 2)
            finally:
                one.close(); two.close()
