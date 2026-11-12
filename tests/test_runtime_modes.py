import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import main
from chat_continuation import decide
import agenda
from store import FriendStore


class RuntimeModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.store.record_inbound('inbound', '今天有点累')
        self.profile = main.default_profile('10001', 'testfriend', False)
        self.profile['proactive'].update(window_start='00:00', window_end='23:59',
                                         enabled=True, rounds_min=1, rounds_max=2,
                                         bubbles_min=1, bubbles_max=1)
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=9)))
        self.record = {'qq_id': '10001', 'directory': str(self.directory)}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def arm_continuation(self):
        self.store.set('chat_continuation', json.dumps({
            'state': 'pending', 'generation': self.store.get('inbound_generation', '0'),
            'next_check_at': self.now.isoformat()}))

    def test_runtime_helper_disabled_preserves_legacy_and_enabled_adds_context(self):
        legacy = 'LEGACY_CONTEXT'
        self.profile['feature_flags'] = dict.fromkeys(
            ('persona_enabled', 'hybrid_enabled', 'sticker_metadata_enabled'), False)
        with patch('retrieval_orchestrator.RetrievalOrchestrator') as retrieval:
            self.assertEqual(main._runtime_context_for_mode(self.store, self.profile, self.directory, legacy), legacy)
        retrieval.assert_not_called()

        seen = []
        self.profile['feature_flags'] = {'hybrid_enabled': True}
        with patch.object(main, 'reply_runtime_context', side_effect=lambda *a, **k: (seen.append((a, k)) or ('ENHANCED', {}))):
            self.assertEqual(main._runtime_context_for_mode(self.store, self.profile, self.directory, legacy), 'ENHANCED')
        args, kwargs = seen[0]
        self.assertEqual(args[2], '今天有点累')
        self.assertEqual(args[4], legacy)
        self.assertEqual(kwargs['source_message']['body'], args[2])

    def test_all_four_runtime_entry_points_use_shared_context(self):
        def enhanced(store, profile, directory, legacy, opener_query=None):
            return legacy + '\n\nRUNTIME_CONTEXT'

        continuation = {'action': 'wait', 'assessment': 'uncertain', 'reason': '稍后再判断',
                        'messages': [], 'next_check_at': (self.now + timedelta(minutes=5)).isoformat()}
        self.arm_continuation()
        with patch.object(main, '_runtime_context_for_mode', side_effect=enhanced) as context, \
                patch.object(main, 'codex_text', return_value=(json.dumps(continuation), None)):
            self.assertEqual(decide(self.store, self.profile, self.directory, self.now, Mock(return_value=(json.dumps(continuation), None))), continuation | {
                'generation': self.store.get('inbound_generation', '0'), 'decided_at': self.now.isoformat(),
                'sent_count': 0, 'forced_check_at': None, 'forced_check_done': True, 'state': 'waiting'})
        context.assert_called_once()
        self.assertIn('RUNTIME_CONTEXT', json.loads(self.store.get('last_generation_input'))['prompt'])

        response = {'send_now': False, 'reason': 'daysdays', 'topic': '刚才的话题', 'todos': []}
        model = Mock(return_value=(json.dumps(response), None))
        with patch.object(main, '_runtime_context_for_mode', side_effect=enhanced) as context, \
                patch.object(main, 'codex_text', model):
            self.assertIsNone(agenda.wake(self.store, self.profile, self.directory, self.now, model))
        context.assert_called_once()
        self.assertIn('RUNTIME_CONTEXT', model.call_args.args[0])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'slot:%'").fetchone()[0], 0)

        for care_task in ('', '午饭关心'):
            model = Mock(return_value=(json.dumps({'messages': ['收到'] if not care_task else []}), None))
            with self.subTest(care_task=care_task), \
                    patch.object(main, '_runtime_context_for_mode', side_effect=enhanced) as context, \
                    patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', model):
                result = main.make_round(self.store, self.profile, self.directory, 1, care_task=care_task)
            self.assertEqual(result, ['收到'] if not care_task else [])
            context.assert_called_once()
            self.assertIn('RUNTIME_CONTEXT', model.call_args.args[0])

    def test_wait_end_send_now_false_topic_and_empty_care_do_not_send_or_count(self):
        for action in ('wait', 'end'):
            self.arm_continuation()
            response = {'action': action, 'assessment': 'uncertain', 'reason': '安静等待', 'messages': [],
                        'next_check_at': None if action == 'end' else (self.now + timedelta(minutes=5)).isoformat()}
            with patch.object(main, 'codex_text', return_value=(json.dumps(response), None)):
                result = decide(self.store, self.profile, self.directory, self.now, Mock(return_value=(json.dumps(response), None)))
            self.assertEqual(result['action'], action)

        self.profile['proactive']['smart_timing'] = True
        response = {'send_now': False, 'reason': '不打扰', 'topic': 'medium', 'todos': []}
        model = Mock(return_value=(json.dumps(response), None))
        self.assertIsNone(agenda.wake(self.store, self.profile, self.directory, self.now, model))
        self.assertEqual(json.loads(self.store.get('smart_proactive'))['topic'], 'medium')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'slot:%'").fetchone()[0], 0)



if __name__ == '__main__':
    unittest.main()
