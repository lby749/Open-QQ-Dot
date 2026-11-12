import json
import hashlib
from datetime import datetime, timedelta
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import main
from send_validator import PrivateMediaEvidence, ReplyPlan, ReplySendState, ReplySnapshot, SendValidator, profile_revision
from persona_types import FriendScope, RuntimeSnapshot, SendPlan, SendStateSnapshot
from store import FriendStore


class SendValidatorTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = ReplySnapshot('12345', '/friend/12345', '54321', 1, 'incoming')
        self.plan = ReplyPlan(self.snapshot, ('item_1', 'item_2'))
        self.state = ReplySendState('12345', '/friend/12345', '54321', '54321', 1,
            {'qq_id': '12345', 'bridge_enabled': True, 'auto_reply': True}, 'processing', 0)

    def validate(self, *, plan=None, snapshot=None, state=None, registry=None):
        return SendValidator.validate(plan or self.plan, snapshot or self.snapshot,
                                      state or self.state, {} if registry is None else registry)

    def test_valid_reply_and_confirmed_next_bubble(self):
        self.assertTrue(self.validate().allowed)
        self.assertTrue(self.validate(state=replace(self.state, attempt_state='sent', successful_bubbles=1)).allowed)

    def test_generation_identity_and_profile_fail_closed(self):
        for state, reason in (
            (replace(self.state, current_generation=2), 'generation_changed'),
            (replace(self.state, bound_self_id='98765'), 'identity_mismatch'),
            (replace(self.state, verified_self_id='98765'), 'identity_mismatch'),
            (replace(self.state, verified_self_id=None), 'identity_unavailable'),
            (replace(self.state, friend_id='98765'), 'identity_mismatch'),
            (replace(self.state, friend_dir='/other'), 'identity_mismatch'),
            (replace(self.state, current_profile={}), 'profile_unavailable'),
            (replace(self.state, current_profile={'qq_id': '12345', 'auto_reply': False}), 'reply_disabled'),
        ):
            with self.subTest(reason=reason):
                result = self.validate(state=state)
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, reason)

    def test_unknown_phone_or_incomplete_typed_state_is_rejected(self):
        self.assertEqual(SendValidator.validate(self.plan, self.snapshot, None, {}).reason,
                         'reply_state_unavailable')
        self.assertFalse(self.validate(state=replace(self.state, current_generation=None)).allowed)
        self.assertEqual(SendValidator.validate(self.plan, self.snapshot, self.state, None).reason,
                         'media_state_unavailable')

    def test_uncertain_sent_complete_and_empty_never_allow_replay(self):
        for status, count in (('sending', 0), ('uncertain', 1), ('sent', 0), ('sent', 2)):
            self.assertFalse(self.validate(state=replace(self.state,
                attempt_state=status, successful_bubbles=count)).allowed)
        self.assertFalse(self.validate(plan=replace(self.plan, bubbles=())).allowed)

    def test_media_never_infers_permission_from_legacy_or_model_registry(self):
        plan = replace(self.plan, bubbles=('[[LOCAL_STICKER:' + 'a' * 64 + ']]',))
        result = self.validate(plan=plan, registry={'a' * 64: {'allowed': True}})
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, 'media_permission_unavailable')
        directory = str(Path(tempfile.gettempdir()) / 'friend')
        key = 'a' * 64
        snapshot = replace(self.snapshot, friend_dir=directory)
        state = replace(self.state, friend_dir=directory)
        plan = replace(plan, runtime_snapshot=snapshot)
        evidence = PrivateMediaEvidence('12345', directory, key,
            str(Path(directory) / 'media' / (key + '.png')), key,
            str(Path(tempfile.gettempdir()) / 'qq-cache.png'), key)
        self.assertTrue(self.validate(plan=plan, snapshot=snapshot, state=state, registry={key: evidence}).allowed)
        for wrong in (replace(evidence, friend_id='98765'), replace(evidence, original_sha256='b' * 64),
                      replace(evidence, transport_sha256='b' * 64),
                      replace(evidence, original_file=str(Path(directory) / (key + '.png')))):
            self.assertFalse(self.validate(plan=plan, snapshot=snapshot, state=state, registry={key: wrong}).allowed)

    def test_reply_over_four_bubbles_is_rejected(self):
        self.assertFalse(self.validate(plan=replace(self.plan, bubbles=tuple(f'bubble_{i}' for i in range(6)))).allowed)


class ReplySendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        classifier = patch('message_classification.enqueue_for_database')
        classifier.start()
        self.addCleanup(classifier.stop)
        self.root = Path(self.temp.name)
        self.friend = self.root / 'friends' / '12345'
        self.friend.mkdir(parents=True)
        self.record = {'qq_id': '12345', 'self_id': '54321', 'directory': str(self.friend)}
        self.records = {'12345': self.record}
        self.profile = {'qq_id': '12345', 'bridge_enabled': True, 'auto_reply': True}
        (self.friend / 'friend.json').write_text(json.dumps(self.profile), 'utf-8')
        self.store = FriendStore(self.friend)
        self.addCleanup(self.store.close)
        self.store.record_inbound('first', '第一条来信')
        self.bridge = main.Bridge.__new__(main.Bridge)
        self.bridge.self_id = '54321'
        self.bridge.deferred = []
        self.bridge.log = Mock()
        self.bridge.client = Mock()
        self.events = []
        self.sent = []
        self.login = {'user_id': 54321}
        self.bridge.client.next_event.side_effect = lambda timeout: self.events.pop(0) if self.events else None
        self.bridge.client.call.side_effect = self.call
        registry = patch.object(main, 'load_registry', side_effect=lambda: {
            'friends': {key: dict(value) for key, value in self.records.items()}})
        registry.start()
        self.addCleanup(registry.stop)
        memory = patch.object(main, 'refresh_memory_if_due')
        memory.start()
        self.addCleanup(memory.stop)
        delay = patch.object(main.random, 'uniform', return_value=0)
        delay.start()
        self.addCleanup(delay.stop)

    def event(self, message_id='new', friend_id='12345'):
        return {'post_type': 'message', 'message_type': 'private', 'user_id': friend_id,
                'self_id': 54321, 'message_id': message_id,
                'message': [{'type': 'text', 'data': {'text': '新来信'}}]}

    def call(self, action, payload=None, **kwargs):
        if action == 'get_login_info':
            if isinstance(self.login, Exception):
                raise self.login
            return self.login
        if action == 'send_private_msg':
            self.sent.append(payload)
            return {'message_id': str(100 + len(self.sent))}
        raise AssertionError('unexpected API: ' + action)

    def run_reply(self, callback=None):
        with patch.object(main, 'make_reply', side_effect=callback or (lambda *args, **kwargs: ['item_1', 'item_2'])) as generate:
            self.bridge.process_reply(self.record, self.profile, self.store, 'first')
        return generate

    def test_quota_uses_literal_friend_notice_once_and_recovery_clears_it(self):
        from agent_errors import AgentError, AgentErrorType
        import quota_reply
        self.profile['model_api'] = {'quota_reply': '我晚点再来陪你聊～'}
        (self.friend / 'friend.json').write_text(json.dumps(self.profile), 'utf-8')
        def limited(*args, **kwargs):
            raise AgentError(AgentErrorType.QUOTA_EXHAUSTED, message='provider diagnostic must stay local')
        generated = self.run_reply(limited)
        generated.assert_called_once()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['message'], [{'type':'text', 'data':{'text':'我晚点再来陪你聊～'}}])
        self.store.record_inbound('second', '还在吗')
        with patch.object(main, 'make_reply', side_effect=limited):
            self.bridge.process_reply(self.record, self.profile, self.store, 'second')
        self.assertEqual(len(self.sent), 1)
        self.store.record_inbound('third', '又见面啦')
        with patch.object(main, 'make_reply', return_value=['恢复正常']):
            self.bridge.process_reply(self.record, self.profile, self.store, 'third')
        self.assertIsNone(self.store.get(quota_reply.KEY))

    def test_quota_notice_keeps_the_real_send_identity_gate(self):
        from agent_errors import AgentError, AgentErrorType
        self.profile['model_api'] = {'quota_reply': '晚点聊'}
        (self.friend / 'friend.json').write_text(json.dumps(self.profile), 'utf-8')
        self.login = {'user_id': 98765}
        def limited(*args, **kwargs):
            raise AgentError(AgentErrorType.QUOTA_EXHAUSTED)
        self.run_reply(limited)
        self.assertEqual(self.sent, [])

    def test_generation_time_inbound_cancels_first_and_registers_waiting_without_recursion(self):
        def generate(*args, **kwargs):
            self.events.append(self.event())
            return ['过时回复']
        generated = self.run_reply(generate)
        generated.assert_called_once()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.get('inbound_generation'), '2')
        self.assertEqual(self.store.reply_attempt('first')['state'], 'cancelled')
        self.assertEqual(self.store.reply_attempt('new')['state'], 'waiting')
        self.bridge.client.call.assert_not_called()
        # The existing tick() query can resume the newly registered attempt.
        row = self.store.db.execute("SELECT r.message_id FROM reply_attempts r JOIN messages m "
            "ON m.qq_id=r.message_id WHERE r.state='waiting' AND m.handled=0").fetchone()
        self.assertEqual(row['message_id'], 'new')

    def test_new_inbound_after_first_stops_remaining_bubbles(self):
        def call(action, payload=None, **kwargs):
            result = self.call(action, payload, **kwargs)
            if action == 'send_private_msg':
                self.events.append(self.event())
            return result
        self.bridge.client.call.side_effect = call
        self.run_reply()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.reply_attempt('first')['state'], 'cancelled')
        self.assertEqual(self.store.get('inbound_generation'), '2')
        self.assertIn(self.store.get('chat_continuation'), (None, '{}'))

    def test_duplicate_and_unbound_other_friend_do_not_cancel(self):
        self.events.extend((self.event('first'), self.event('other', '98765')))
        self.run_reply()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.get('inbound_generation'), '1')
        self.assertEqual(self.store.reply_attempt('first')['state'], 'sent')

    def test_live_switch_empty_identity_or_lookup_failure_never_sends(self):
        for login in ({'user_id': 98765}, {}, {'user_id': None}, RuntimeError('sensitive failure')):
            with self.subTest(login=type(login).__name__):
                self.login = login
                with self.assertRaisesRegex(RuntimeError, r'(?:未发送|Send)'):
                    self.bridge.send_text(self.record, self.store, '测试文本')
        self.assertEqual(self.sent, [])

    def test_bound_other_friend_is_registered_without_recursive_reply_or_cancellation(self):
        other = self.root / 'friends' / '98765'
        other.mkdir()
        (other / 'friend.json').write_text(json.dumps({'qq_id': '98765'}), 'utf-8')
        self.records['98765'] = {'qq_id': '98765', 'self_id': '54321', 'directory': str(other)}
        self.events.append(self.event('other', '98765'))
        generate = self.run_reply()
        generate.assert_called_once()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.get('inbound_generation'), '1')
        store = FriendStore(other)
        try:
            self.assertEqual(store.get('inbound_generation'), '1')
            self.assertEqual(store.reply_attempt('other')['state'], 'waiting')
        finally:
            store.close()

    def test_inbound_during_identity_check_also_cancels_before_transport(self):
        def call(action, payload=None, **kwargs):
            result = self.call(action, payload, **kwargs)
            if action == 'get_login_info':
                self.events.append(self.event())
            return result
        self.bridge.client.call.side_effect = call
        self.run_reply()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.reply_attempt('first')['state'], 'cancelled')
        self.assertEqual(self.store.reply_attempt('new')['state'], 'waiting')

    def test_registration_failure_prevents_transport(self):
        self.events.append(self.event())
        with patch.object(FriendStore, 'record_inbound', side_effect=RuntimeError('cannot persist')):
            self.run_reply()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.reply_attempt('first')['state'], 'failed')

    def test_private_original_and_identical_cache_preserve_image_and_native_face(self):
        data = b'private fixture bytes'
        key = hashlib.sha256(data).hexdigest()
        media = self.friend / 'media'
        media.mkdir()
        original = media / (key + '.png')
        original.write_bytes(data)
        cache = self.root / 'qq-cache.png'
        cache.write_bytes(data)
        self.store.set('sticker:' + key, '{}')
        with patch('sticker_library.send_reference', return_value=cache.as_uri()), \
                patch('sticker_library.candidates', return_value=[(key, {})]):  # only offered stickers may be sent
            self.run_reply(lambda *args, **kwargs: [f'[[LOCAL_STICKER:{key}]]', '[[QQ表情:微笑]]'])
        self.assertEqual([item['message'][0]['type'] for item in self.sent], ['image', 'face'])
        self.assertEqual(self.sent[0]['message'][0]['data']['file'], cache.resolve().as_uri())
        self.assertEqual(self.store.reply_attempt('first')['state'], 'sent')

    def test_private_media_rejects_remote_missing_changed_or_unowned_transport(self):
        data = b'private fixture bytes'
        key = hashlib.sha256(data).hexdigest()
        media = self.friend / 'media'
        media.mkdir()
        original = media / (key + '.png')
        original.write_bytes(data)
        wrong = self.root / 'wrong.png'
        wrong.write_bytes(b'different bytes')
        self.store.set('sticker:' + key, '{}')
        for index, reference in enumerate(('https://example.invalid/image', (self.root / 'missing.png').as_uri(), wrong.as_uri())):
            with self.subTest(reference=reference):
                source = 'media-failure-' + str(index)
                self.store.record_inbound(source, '图片')
                with patch('sticker_library.send_reference', return_value=reference), \
                        patch.object(main, 'make_reply', return_value=[f'[[LOCAL_STICKER:{key}]]']):
                    self.bridge.process_reply(self.record, self.profile, self.store, source)
                self.assertEqual(self.sent, [])
        plan = ReplyPlan(ReplySnapshot('12345', str(self.friend.resolve()), '54321', 1, 'first'),
                         (f'[[LOCAL_STICKER:{key}]]',))
        original.write_bytes(b'changed original')
        with self.assertRaisesRegex(RuntimeError, 'media_hash_mismatch'):
            self.bridge._reply_media_registry(self.record, self.store, plan)
        original.write_bytes(data)
        with patch.object(self.store, 'get', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'media_permission_unavailable'):
                self.bridge._reply_media_registry(self.record, self.store, plan)
        with patch('sticker_library.original_path', return_value=wrong):
            with self.assertRaisesRegex(RuntimeError, 'private_media_scope_mismatch'):
                self.bridge._reply_media_registry(self.record, self.store, plan)

    def test_latest_binding_or_profile_switch_cancels(self):
        def generate(*args, **kwargs):
            self.record['self_id'] = '98765'
            return ['过时回复']
        self.run_reply(generate)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.reply_attempt('first')['state'], 'cancelled')
        self.bridge.client.call.assert_not_called()

    def test_switch_during_generation_leaves_waiting_attempt(self):
        def generate(*args, **kwargs):
            profile = dict(self.profile, auto_reply=False)
            (self.friend / 'friend.json').write_text(json.dumps(profile), 'utf-8')
            return ['过时回复']
        self.run_reply(generate)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.reply_attempt('first')['state'], 'waiting')
        self.bridge.client.call.assert_not_called()
        # A pause after process preflight must also remain waiting in its except.
        (self.friend / 'friend.json').write_text(json.dumps(self.profile), 'utf-8')
        original_send = self.bridge.send_text
        def pause_before_send(*args, **kwargs):
            (self.friend / 'friend.json').write_text(json.dumps(dict(self.profile, bridge_enabled=False)), 'utf-8')
            return original_send(*args, **kwargs)
        with patch.object(self.bridge, 'send_text', side_effect=pause_before_send):
            self.run_reply()
        self.assertEqual(self.store.reply_attempt('first')['state'], 'waiting')
        self.bridge.client.call.assert_not_called()

    def test_transport_timeout_or_missing_receipt_is_uncertain_without_retry(self):
        for failure in (TimeoutError('transport timeout'), {}):
            with self.subTest(failure=type(failure).__name__):
                source_id = 'failure-' + type(failure).__name__
                self.store.record_inbound(source_id, '新请求')
                attempts = []
                def call(action, payload=None, **kwargs):
                    if action == 'send_private_msg':
                        attempts.append(payload)
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return self.call(action, payload, **kwargs)
                self.bridge.client.call.side_effect = call
                with patch.object(main, 'make_reply', return_value=['回复']) as generate:
                    self.bridge.process_reply(self.record, self.profile, self.store, source_id)
                    self.bridge.process_reply(self.record, self.profile, self.store, source_id, retry=True)
                self.assertEqual(len(attempts), 1)
                generate.assert_called_once()
                self.assertEqual(self.store.reply_attempt(source_id)['state'], 'uncertain')


class ModeValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        self.directory.mkdir(parents=True)

    def inputs(self, mode):
        profile = {'qq_id': '12345', 'bridge_enabled': True, 'auto_reply': True,
            'proactive': {'enabled': True, 'bubbles_max': 5},
            'scheduled_care': {'enabled': True, 'slots': {'evening': {'enabled': True, 'count': 2}}}}
        main.dump_json(self.directory / 'friend.json', profile)
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': {'qq_id': '12345', 'directory': str(self.directory)}}})
        now = datetime.now().astimezone().isoformat()
        snapshot = RuntimeSnapshot(scope=FriendScope.load('12345', project_root=self.root), bound_self_id='54321',
            inbound_generation=1, source_message_id=None, attempt_id='care:2026-10-07:evening' if mode == 'care' else mode + ':one',
            mode=mode, created_at=now, profile_revision=profile_revision(profile))
        plan = SendPlan(runtime_snapshot=snapshot, bubbles=('items',), sticker_intent=None, delay_hint=None, confidence=0.0)
        state = SendStateSnapshot(friend_id='12345', verified_self_id='54321', identity_checked_at=now,
            current_generation=1, current_profile=profile, attempt_state='prepared', successful_bubble_ids=(), now=now,
            search_remaining=None, contact_attempts_remaining=1 if mode == 'proactive' else None,
            window_open=True if mode != 'continuation' else None, cooldown_until=None, reply_pause_until=None,
            unanswered_pause_until=None,
            care_slot_state='ready' if mode == 'care' else None)
        return plan, snapshot, state

    def test_content_revision_is_canonical_and_never_mtime_or_default(self):
        self.assertEqual(profile_revision({'b': [1], 'a': 'medium'}), profile_revision(json.loads('{ "a": "medium", "b": [1] }')))
        self.assertNotEqual(profile_revision({'a': 1}), profile_revision({'a': 2}))
        with self.assertRaises(ValueError):
            profile_revision({'a': float('nan')})

    def test_three_typed_modes_and_missing_relevant_state(self):
        for mode, field in (('proactive', 'contact_attempts_remaining'), ('care', 'care_slot_state'),
                            ('continuation', 'current_generation')):
            plan, snapshot, state = self.inputs(mode)
            self.assertTrue(SendValidator.validate(plan, snapshot, state, {}).allowed)
            wrong = replace(state, current_profile=dict(state.current_profile), **{field: 2 if field == 'current_generation' else None})
            self.assertFalse(SendValidator.validate(plan, snapshot, wrong, {}).allowed)
            self.assertFalse(SendValidator.validate(plan, snapshot, {}, {}).allowed)
            self.assertFalse(SendValidator.validate_local(plan, snapshot, state, {}).allowed)

    def test_changed_profile_receipts_uncertain_identity_and_pause_fail_closed(self):
        plan, snapshot, state = self.inputs('proactive')
        def changed(**values):
            return replace(state, current_profile=values.pop('current_profile', dict(state.current_profile)), **values)
        for wrong in (changed(current_profile={**dict(state.current_profile), 'style': 'changed'}),
                      changed(attempt_state='uncertain'), changed(attempt_state='sending'),
                      changed(successful_bubble_ids=('one',)), changed(verified_self_id='98765'),
                      changed(window_open=False),
                      changed(cooldown_until=(datetime.now().astimezone() + timedelta(hours=1)).isoformat()),
                      changed(identity_checked_at=(datetime.now().astimezone() - timedelta(minutes=1)).isoformat())):
            self.assertFalse(SendValidator.validate(plan, snapshot, wrong, {}).allowed)

    def test_care_and_proactive_do_not_inherit_reply_four_bubble_limit(self):
        plan, snapshot, state = self.inputs('proactive')
        self.assertTrue(SendValidator.validate(replace(plan, bubbles=tuple(str(i) for i in range(5))), snapshot, state, {}).allowed)
        plan, snapshot, state = self.inputs('care')
        self.assertFalse(SendValidator.validate(replace(plan, bubbles=('一', '二', '三')), snapshot, state, {}).allowed)


if __name__ == '__main__':
    unittest.main()
