import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
from persona_types import SendStateSnapshot
from send_validator import SendValidator, profile_revision
from store import FriendStore


class SendStateConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)
        self.store = FriendStore(self.directory)
        self.addCleanup(self.store.close)
        self.profile = main.default_profile('12345', '离线', True)
        self.profile.update(bridge_enabled=True, auto_reply=True,
            scheduled_care={'enabled': True, 'slots': {'evening': {'enabled': True, 'start': '00:00', 'end': '23:59', 'count': 2}}})
        self.profile['proactive'].update(smart_timing=False, window_start='00:00', window_end='23:59',
            rounds_min=1, rounds_max=10, bubbles_min=1, bubbles_max=5, bubble_interval_seconds=[0, 0])
        self.record = {'qq_id': '12345', 'self_id': '54321', 'directory': str(self.directory)}
        self.save_profile()
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': self.record}})
        for name, value in (('REGISTRY', self.root / 'friends.json'), ('FRIENDS_DIR', self.root / 'friends')):
            patched = patch.object(main, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.bridge = main.Bridge.__new__(main.Bridge)
        self.bridge.self_id = '54321'
        self.bridge.deferred = []
        self.bridge.log = Mock()
        self.bridge.client = Mock()
        self.events = []
        self.bridge.client.next_event.side_effect = lambda timeout: self.events.pop(0) if self.events else None
        self.bridge.client.call.side_effect = self.api
        self.submissions = []
        self.transport_error = None
        self.after_submit = None
        self.serial = 0

    def save_profile(self):
        main.dump_json(self.directory / 'friend.json', self.profile)

    def api(self, action, payload=None, **kwargs):
        if action == 'get_login_info':
            return {'user_id': 54321}
        if action != 'send_private_msg':
            raise AssertionError(action)
        attempts = [json.loads(row['value']) for row in self.store.db.execute("SELECT value FROM metadata WHERE key LIKE 'send_attempt:%'")]
        sending = [item for item in attempts if item['state'] == 'sending']
        self.assertEqual(len(sending), 1)
        attempt = sending[0]
        self.assertEqual(attempt['state'], 'sending')
        self.assertEqual(attempt['submitted_index'], len(attempt['receipts']))
        self.assertFalse(self.store.db.in_transaction)
        self.submissions.append(payload)
        if self.after_submit:
            self.after_submit()
        if isinstance(self.transport_error, Exception):
            raise self.transport_error
        if self.transport_error == 'missing':
            return {}
        return {'message_id': 'receipt-' + str(len(self.submissions))}

    def ledger(self, plan):
        return json.loads(self.store.get(self.bridge._ledger_key(plan.runtime_snapshot)))

    def make_plan(self, mode, bubbles=('一', '二'), test=False):
        self.serial += 1
        for key, value in (('reply_waiting', '0'), ('proactive_paused', '0'), ('unanswered_rounds', '0'),
                           ('active_unanswered_rounds', '0'), ('round_pause_since', ''), ('reply_waiting_since', ''),
                           ('eligible_after', '')):
            self.store.set(key, value)
        self.save_profile()
        now = datetime.now().astimezone()
        options = {'round_limit': 2}
        if mode == 'proactive':
            attempt_id = ('test_slot:' if test else 'slot:') + now.date().isoformat() + ':' + str(self.serial)
            options.update(legacy_key=attempt_id, test=test, proactive_settings=self.profile['proactive'])
            self.store.set(attempt_id, 'in_progress')
        elif mode == 'care':
            from scheduled_care import bounds, configuration
            start, end = bounds(now, configuration(self.profile)['slots']['evening'])
            attempt_id = 'care:' + start.date().isoformat() + ':evening'
            self.store.set('care_plan:' + attempt_id[5:], json.dumps({'start': start.isoformat(), 'end': end.isoformat(), 'at': now.isoformat()}))
            options.update(legacy_key=attempt_id, care_name='evening')
            self.store.set(attempt_id, 'in_progress')
        else:
            attempt_id = mode + ':' + str(self.serial)
        snapshot = self.bridge._mode_snapshot(self.record, self.profile, self.store, mode, attempt_id)
        plan = self.bridge._create_mode_attempt(self.store, snapshot, bubbles, options)
        if mode == 'continuation':
            self.store.set('chat_continuation', json.dumps({'generation': str(plan.generation),
                'state': 'processing', 'send_attempt_id': attempt_id, 'action': 'supplement'}))
        self.active = plan
        return plan

    def send(self, plan, text=None):
        index = len(self.ledger(plan)['receipts'])
        return self.bridge.send_text(self.record, self.store, text or plan.bubbles[index], mode_plan=plan)

    def terminate(self, plan, exc):
        return self.bridge._fail_mode(self.store, plan, exc)

    def dead_owner(self, plan):
        key = self.bridge._ledger_key(plan.runtime_snapshot)
        attempt = self.ledger(plan)
        attempt.update(owner_pid=99999999, owner_started=0)
        self.store.set(key, json.dumps(attempt))

    def test_unsolicited_modes_share_one_random_gap(self):
        # 2026-10-10 user: after one unsolicited round the next (any mode) waits 20–60 minutes
        plan = self.make_plan('proactive')
        for text in plan.bubbles:
            self.send(plan, text)
        self.bridge._finish_mode(self.store, self.bridge._ledger_key(plan.runtime_snapshot))
        gap = json.loads(self.store.get('unsolicited_gap'))
        wait = main.unsolicited_wait(self.store, datetime.now().astimezone())
        self.assertIsNotNone(wait)
        self.assertTrue(timedelta(minutes=19) < wait - datetime.now().astimezone() <= timedelta(minutes=60))
        plan = self.make_plan('continuation')
        with self.assertRaises(main.SendBlocked):
            self.send(plan)
        self.store.set('unsolicited_gap', json.dumps({**gap, 'generation': 'older'}))  # the friend wrote since
        self.assertIsNone(main.unsolicited_wait(self.store, datetime.now().astimezone()))

    def test_all_modes_confirm_before_finalize_and_count_once(self):
        for mode in ('proactive', 'continuation', 'care'):
            with self.subTest(mode=mode):
                self.store.set('unsolicited_gap', '')  # each mode on its own; the shared gap is tested above
                plan = self.make_plan(mode)
                for text in plan.bubbles:
                    self.send(plan, text)
                self.assertEqual(self.ledger(plan)['state'], 'confirmed')
                self.assertEqual(self.store.get('unanswered_rounds'), '0')
                key = self.bridge._ledger_key(plan.runtime_snapshot)
                self.bridge._finish_mode(self.store, key)
                counters = tuple(self.store.get(name) for name in ('unanswered_rounds', 'active_unanswered_rounds'))
                self.bridge._finish_mode(self.store, key)
                self.assertEqual(counters, tuple(self.store.get(name) for name in ('unanswered_rounds', 'active_unanswered_rounds')))
                self.assertEqual(counters, ('0', '1') if mode == 'continuation' else ('1', '1'))
                self.assertEqual(self.ledger(plan)['state'], 'sent')
                with self.assertRaises(main.SendBlocked):
                    self.send(plan, plan.bubbles[0])

    def test_timeout_or_missing_receipt_is_uncertain_for_each_mode(self):
        self.assert_failure_modes(TimeoutError('private error text'))

    def test_missing_receipt_is_uncertain_for_each_mode(self):
        self.assert_failure_modes('missing')

    def assert_failure_modes(self, failure):
        for mode in ('proactive', 'continuation', 'care'):
            with self.subTest(mode=mode):
                plan = self.make_plan(mode)
                self.transport_error = failure
                with self.assertRaises((TimeoutError, RuntimeError)) as caught:
                    self.send(plan)
                self.terminate(plan, caught.exception)
                attempt = self.ledger(plan)
                self.assertEqual(attempt['state'], 'uncertain')
                self.assertNotIn('private error text', json.dumps(attempt))
                self.assertEqual(self.store.get('unanswered_rounds'), '0')
                count = len(self.submissions)
                with self.assertRaises(main.SendBlocked):
                    self.send(plan)
                self.assertEqual(len(self.submissions), count)
        self.transport_error = None

    def test_partial_transport_failure_preserves_receipt_without_success_counters(self):
        for mode in ('proactive', 'continuation', 'care'):
            plan = self.make_plan(mode)
            self.send(plan)
            self.transport_error = TimeoutError('second timeout')
            with self.assertRaises(TimeoutError) as caught:
                self.send(plan)
            self.terminate(plan, caught.exception)
            attempt = self.ledger(plan)
            self.assertEqual(attempt['state'], 'uncertain')
            self.assertEqual(len(attempt['receipts']), 1)
            self.assertEqual(self.store.get('active_unanswered_rounds'), '0')
            if mode == 'continuation':
                self.assertEqual(self.store.get('reply_waiting'), '1')
            self.transport_error = None

    def test_new_inbound_after_first_cancels_remainder_without_recursive_reply(self):
        plan = self.make_plan('proactive')
        self.send(plan)
        self.events.append({'post_type': 'message', 'message_type': 'private', 'self_id': 54321,
            'user_id': 12345, 'message_id': 'new', 'message': [{'type': 'text', 'data': {'text': '新来信'}}]})
        with patch.object(main, 'make_reply') as model:
            with self.assertRaises(main.SendBlocked) as caught:
                self.send(plan)
            self.terminate(plan, caught.exception)
            model.assert_not_called()
        self.assertEqual(self.ledger(plan)['state'], 'uncertain')
        self.assertEqual(self.store.reply_attempt('new')['state'], 'waiting')
        self.assertEqual(len(self.submissions), 1)

    def test_profile_change_and_final_cas_change_cancel_before_transport(self):
        for at_cas in (False, True):
            plan = self.make_plan('proactive')
            original = self.bridge._submit_mode
            def changed(*args, **kwargs):
                self.profile['style'] = 'Settings' + str(self.serial)
                self.save_profile()
                return original(*args, **kwargs)
            if not at_cas:
                self.profile['style'] = 'Settings' + str(self.serial)
                self.save_profile()
            with patch.object(self.bridge, '_submit_mode', side_effect=changed if at_cas else original):
                with self.assertRaises(main.SendBlocked) as caught:
                    self.send(plan)
            self.terminate(plan, caught.exception)
            self.assertEqual(self.ledger(plan)['state'], 'cancelled')
        self.assertEqual(self.submissions, [])

    def test_cas_rejects_duplicate_submission_and_other_live_owner(self):
        plan = self.make_plan('proactive')
        with self.assertRaises(main.SendBlocked):
            snapshot = self.bridge._mode_snapshot(self.record, self.profile, self.store, 'proactive', 'slot:other')
            self.bridge._create_mode_attempt(self.store, snapshot, ('other',), {'round_limit': 2})
        values = self.bridge._mode_values(self.record, self.profile, self.store, plan)
        state = SendStateSnapshot(**values, verified_self_id='54321', identity_checked_at=values['now'])
        self.bridge._submit_mode(self.store, plan, plan.bubbles[0], state, {})
        with self.assertRaises(main.SendBlocked):
            self.bridge._submit_mode(self.store, plan, plan.bubbles[0], state, {})
        self.assertEqual(self.submissions, [])

    def test_live_identity_lookup_failure_is_definitely_unsent_for_all_modes(self):
        self.bridge.client.call.side_effect = lambda *args, **kwargs: {}
        for mode in ('proactive', 'continuation', 'care'):
            plan = self.make_plan(mode)
            with self.assertRaises(RuntimeError) as caught:
                self.send(plan)
            self.terminate(plan, caught.exception)
            attempt = self.ledger(plan)
            self.assertEqual(attempt['state'], 'failed')
            self.assertIsNone(attempt['submitted_index'])
            self.assertEqual(attempt['receipts'], [])
        self.assertEqual(self.submissions, [])

    def test_restart_prepared_partial_and_submitted_never_replay(self):
        for kind in ('prepared', 'submitted', 'partial'):
            plan = self.make_plan('proactive')
            if kind == 'submitted':
                self.transport_error = TimeoutError('timeout')
                with self.assertRaises(TimeoutError):
                    self.send(plan)
                self.transport_error = None
            elif kind == 'partial':
                self.send(plan)
            self.dead_owner(plan)
            count = len(self.submissions)
            self.bridge._recover_mode_attempts(self.store)
            self.assertEqual(self.ledger(plan)['state'], 'failed' if kind == 'prepared' else 'uncertain')
            self.assertEqual(len(self.submissions), count)

    def test_confirmed_restart_only_finalizes_and_transaction_failure_rolls_back(self):
        plan = self.make_plan('proactive', ('完整',))
        self.send(plan)
        write = self.bridge._metadata_writes
        def fail_write(store, values):
            write(store, values)
            raise RuntimeError('disk failure')
        with patch.object(self.bridge, '_metadata_writes', side_effect=fail_write):
            with self.assertRaises(RuntimeError):
                self.bridge._finish_mode(self.store, self.bridge._ledger_key(plan.runtime_snapshot))
        self.assertFalse(self.ledger(plan)['finalized'])
        self.assertEqual(self.store.get('unanswered_rounds'), '0')
        self.dead_owner(plan)
        self.bridge._recover_mode_attempts(self.store)
        self.bridge._recover_mode_attempts(self.store)
        self.assertEqual(self.ledger(plan)['state'], 'sent')
        self.assertEqual(self.store.get('unanswered_rounds'), '1')
        self.assertEqual(len(self.submissions), 1)

    def test_confirmed_receipt_survives_history_error_and_new_generation(self):
        plan = self.make_plan('proactive', ('完整',))
        with patch.object(self.store, 'add_message', side_effect=RuntimeError('history failed')):
            with self.assertRaises(RuntimeError) as caught:
                self.send(plan)
        self.store.record_inbound('new-generation', '新来信')
        self.terminate(plan, caught.exception)
        self.assertEqual(self.ledger(plan)['state'], 'sent')
        self.assertTrue(self.ledger(plan)['finalized'])
        self.assertEqual(self.store.get('unanswered_rounds'), '0')
        self.assertEqual(len(self.submissions), 1)

    def test_test_path_preserves_counter_exceptions(self):
        plan = self.make_plan('proactive', ('test',), test=True)
        self.send(plan)
        self.bridge._finish_mode(self.store, self.bridge._ledger_key(plan.runtime_snapshot))
        self.assertEqual(self.store.get('proactive_test_rounds'), '1')
        self.assertEqual(self.store.get('unanswered_rounds'), '0')
        self.assertEqual(self.store.get('reply_waiting'), '0')

    def test_legacy_and_live_owner_recovery(self):
        plan = self.make_plan('proactive')
        self.store.set('care:legacy', 'sending')
        self.bridge._recover_mode_attempts(self.store)
        self.assertEqual(self.ledger(plan)['state'], 'prepared')
        self.assertEqual(self.store.get(plan.runtime_snapshot.attempt_id), 'in_progress')
        self.assertEqual(self.store.get('care:legacy'), 'uncertain')

    def test_unverifiable_owner_is_not_recovered_or_taken_over(self):
        plan = self.make_plan('proactive')
        key = self.bridge._ledger_key(plan.runtime_snapshot)
        original = self.store.get(key)
        process = main.psutil.Process
        for error in (main.psutil.AccessDenied(pid=123), main.psutil.Error('unavailable')):
            def lookup(pid=None):
                if pid is None:
                    return process()
                raise error
            with self.subTest(error=type(error).__name__), patch.object(main.psutil, 'Process', side_effect=lookup):
                self.bridge._recover_mode_attempts(self.store)
                self.assertEqual(self.store.get(key), original)
                self.assertEqual(self.store.get(plan.attempt_id), 'in_progress')
                snapshot = self.bridge._mode_snapshot(self.record, self.profile, self.store, 'proactive', 'slot:other')
                with self.assertRaisesRegex(main.SendBlocked, 'friend_send_busy'):
                    self.bridge._create_mode_attempt(self.store, snapshot, ('items',), {'round_limit': 2})
        self.assertEqual(self.submissions, [])

    def test_no_such_process_allows_conservative_recovery(self):
        plan = self.make_plan('proactive')
        with patch.object(main.psutil, 'Process', side_effect=main.psutil.NoSuchProcess(pid=123)):
            self.bridge._recover_mode_attempts(self.store)
        self.assertEqual(self.ledger(plan)['state'], 'failed')
        self.assertEqual(self.store.get(plan.attempt_id), 'failed')
        self.assertEqual(self.submissions, [])

    def test_proactive_real_callers_use_ledger(self):
        day = datetime.now().astimezone().date().isoformat()
        with patch.object(main, 'make_round', return_value=['一', '二']), \
                patch.object(main.random, 'randint', side_effect=lambda low, high: low):
            self.bridge.send_round(self.record, self.profile, self.store, day + ':0')
        key = 'send_attempt:proactive:slot:' + day + ':0'
        self.assertEqual(json.loads(self.store.get(key))['state'], 'sent')
        self.assertEqual(len(self.submissions), 2)
        # 2026-10-13 user: a round from the model's own todos is not held back by the unanswered pauses
        self.store.set('unsolicited_gap', '')
        self.store.set('reply_waiting', '1')
        self.store.set('proactive_paused', '1')
        with patch.object(main, 'make_round', return_value=['三']), \
                patch.object(main.random, 'randint', side_effect=lambda low, high: low):
            self.bridge.send_round(self.record, self.profile, self.store, day + ':smart:a', agenda=True)
        self.assertEqual(json.loads(self.store.get('send_attempt:proactive:slot:' + day + ':smart:a'))['state'], 'sent')
        self.assertEqual(len(self.submissions), 3)

    def test_generation_during_proactive_generation_cancels_without_identity_request(self):
        def generate(*args, **kwargs):
            self.events.append({'post_type': 'message', 'message_type': 'private', 'self_id': 54321,
                'user_id': 12345, 'message_id': 'new', 'message': [{'type': 'text', 'data': {'text': '新来信'}}]})
            return ['过时']
        with patch.object(main, 'make_round', side_effect=generate), patch.object(main, 'make_reply') as reply:
            self.bridge.send_round(self.record, self.profile, self.store, 'generation-test')
            reply.assert_not_called()
        attempt = json.loads(self.store.get('send_attempt:proactive:slot:generation-test'))
        self.assertEqual(attempt['state'], 'cancelled')
        self.bridge.client.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
