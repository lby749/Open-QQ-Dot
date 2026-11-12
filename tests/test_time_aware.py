import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
import scheduler
from scheduled_care import configuration, validate
from store import FriendStore


class TimeAwareTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.directory = self.root / 'friends' / '10001'
        self.store = FriendStore(self.directory)
        main.dump_json(self.directory / 'friend.json', {'qq_id': '10001', 'bridge_enabled': True})
        main.dump_json(self.root / 'friends.json', {'friends': {'10001': {
            'qq_id': '10001', 'directory': str(self.directory), 'self_id': '77777'}}})
        for name, value in (('REGISTRY', self.root / 'friends.json'), ('FRIENDS_DIR', self.root / 'friends')):
            setting = patch.object(main, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def test_care_task_excludes_normal_style_topics_and_can_skip(self):
        profile = {'style': 'persona', 'proactive': {'style': '普通主动风格标记', 'topic_types': ['历史主题标记']}}
        model = Mock(return_value=(json.dumps({'messages': []}), ''))
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', model):
            self.assertEqual(main.make_round(self.store, profile, self.directory, 1, care_task='午饭关心标记'), [])
        prompt = model.call_args.args[0]
        self.assertIn('午饭关心标记', prompt)
        self.assertIn('get_today_chat', prompt)
        self.assertNotIn('普通主动风格标记', prompt)
        self.assertNotIn('历史主题标记', prompt)
        self.assertIn('FIRST_BUBBLE=GREETING', prompt)
        self.assertEqual(json.loads(self.store.get('last_generation_input'))['mode'], '时段关心')

    def test_normal_plan_is_unchanged_by_legacy_care_flag(self):
        settings = {'rounds_min': 4, 'rounds_max': 4, 'window_start': '09:00', 'window_end': '02:00'}
        start = self.now.replace(hour=9)
        plain = scheduler.make_plan(start, settings, random.Random(5))
        legacy = scheduler.make_plan(start, {**settings, 'time_aware': True}, random.Random(5))
        self.assertEqual(plain, legacy)
        self.assertTrue(configuration({'proactive': {'time_aware': True}})['enabled'])
        self.assertFalse(configuration({})['enabled'])
        self.assertFalse(configuration({'proactive': {'time_aware': True}, 'scheduled_care': {'enabled': False}})['enabled'])

    def test_care_generation_checks_configured_count(self):
        # 2026-10-10: the count is an upper bound (extras merge); 2026-10-11: plus a lower bound (one rewrite, then fail)
        profile = {'proactive': {}}
        model = Mock(return_value=(json.dumps({'messages': ['days', 'casual', 'items', '早呀', '今天聊点轻松的', '你随意接话就好', '多的一条']}), ''))
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', model):
            self.assertEqual(len(main.make_round(self.store, profile, self.directory, 3, care_task='早安')), 3)
        self.assertIn('BUBBLES=3;', model.call_args.args[0])
        model.reset_mock()
        model.return_value = (json.dumps({'messages': ['items']}), '')
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', model):
            self.assertEqual(main.make_round(self.store, profile, self.directory, 3, care_task='早安'), ['items'])
            model.reset_mock()
            with self.assertRaisesRegex(RuntimeError, r'(最少 2 条| 2 items)'):
                main.make_round(self.store, profile, self.directory, 3, care_task='早安', minimum=2)
        self.assertEqual(model.call_count, 2)
        self.assertIn('BUBBLES=2..3;', model.call_args_list[0].args[0])
        self.assertIn('RETRY_TOO_FEW: AT_LEAST 2', model.call_args.args[0])
        care = configuration({'scheduled_care': {'slots': {'morning': {'count': 0}}}})
        with self.assertRaises(ValueError):
            validate(care)
        care = configuration({'scheduled_care': {'slots': {'morning': {'count': 2, 'count_min': 3}}}})
        with self.assertRaises(ValueError):
            validate(care)
