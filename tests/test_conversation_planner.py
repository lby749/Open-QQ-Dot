import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from persona_store import ScopeMismatch, Unavailable
from persona_types import (Candidate, ContextBudgets, ConversationPlan, DataScope,
    PrivacyView, RetrievalFilters, RetrievalRequest, SourceOrigin, SourceRef,
    InvalidSource)
from conversation_planner import ConversationPlanner, LegacyConversationAdapter


NOW = '2026-10-07T12:00:00+00:00'


def request(plan=None):
    return RetrievalRequest(friend_id='12345', query='untrusted query',
        recent_context='untrusted recent context', scene='unknown', plan=plan,
        filters=RetrievalFilters(), budgets=ContextBudgets(),
        allowed_pack_versions=frozenset(), now=NOW)


def plan(action='reply'):
    return ConversationPlan(action=action, intent='', tone='', core_meaning='',
        ask_question=False, continue_topic=False, introduce_new_topic=False,
        desired_bubbles=0, sticker_intent=None)


def candidate(friend_id='12345'):
    source = SourceRef(dataset_id='fixture', import_id='fixture-import',
        record_id='record', message_seq=None, speaker_id='person',
        origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
        privacy_view=PrivacyView.LOCAL_FULL, friend_id=friend_id,
        event_time=None, recorded_at=NOW)
    return Candidate(id='candidate', namespace='persona', type='fixture', content='body',
        score=0.5, source=source, reason='reason', retriever='fixture',
        friend_id=friend_id, timestamp=None, confidence=1.0,
        original_scope=DataScope.PRIVATE_FRIEND, pack_version=None)


def context(**groups):
    values = {name: () for name in ('structured', 'memory', 'persona', 'interests',
        'topics', 'examples', 'style', 'stickers')}
    values.update(groups)
    return SimpleNamespace(**values)


def plan_json(action='reply'):
    return {'action': action, 'intent': '', 'tone': '', 'core_meaning': '',
        'ask_question': False, 'continue_topic': False,
        'introduce_new_topic': False, 'desired_bubbles': 0}


def encode(payload):
    return json.dumps(payload, ensure_ascii=False)


class ConversationPlannerTests(unittest.TestCase):
    def test_only_explicit_typed_request_plan_is_returned(self):
        explicit = plan()
        self.assertIs(ConversationPlanner().plan(request(explicit), None), explicit)
        with self.assertRaises(Unavailable):
            ConversationPlanner().plan(request(), None)
        with self.assertRaises(Unavailable):
            ConversationPlanner().plan(request('写一条简短回复'), None)

    def test_candidate_scope_mismatch_is_hard_error(self):
        with self.assertRaises(ScopeMismatch):
            ConversationPlanner().plan(request(), context(persona=(candidate('54321'),)))


class LegacyConversationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = LegacyConversationAdapter()

    def test_reply_accepts_fenced_json_and_preserves_message_order_and_payload(self):
        payload = {'messages': ['  第二条 ', ' 第一条 '], 'emotion': 'uncertain',
                   'lookup_needed': False, 'learned_terms': ['term'],
                   'continuation': {'keep': True}, 'plan': plan_json()}
        raw = '```json\n' + encode(payload) + '\n```'
        parsed = self.adapter.parse(raw, mode='reply')
        self.assertEqual(parsed.messages, ('第二条', '第一条'))
        self.assertEqual(parsed.legacy_payload['emotion'], 'uncertain')
        self.assertEqual(parsed.legacy_payload['learned_terms'], ['term'])
        self.assertEqual(parsed.plan.action, 'reply')
        self.assertEqual(parsed.plan_status, 'available')

    def test_reply_plain_text_and_reply_string_compatibility(self):
        plain = self.adapter.parse('  好的呀  ', mode='reply')
        self.assertEqual(plain.messages, ('好的呀',))
        self.assertEqual(plain.plan, None)
        self.assertEqual(plain.plan_status, 'plan_unavailable')
        wrapped = self.adapter.parse(encode({'reply': ' 好的 ', 'sentiment': 'neutral'}),
                                     mode='reply')
        self.assertEqual(wrapped.messages, ('好的',))
        self.assertEqual(wrapped.legacy_payload['sentiment'], 'neutral')

    def test_reply_message_validation_and_four_message_limit(self):
        self.assertEqual(self.adapter.parse(encode({'messages': ['1', '2', '3', '4']}),
                                            mode='reply').messages, ('1', '2', '3', '4'))
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': [str(i) for i in range(6)]}), mode='reply')
        for messages in ([], ['  '], ['same', ' same ']):
            with self.subTest(messages=messages), self.assertRaises(InvalidSource):
                self.adapter.parse(encode({'messages': messages}), mode='reply')
        with self.assertRaises(InvalidSource):
            self.adapter.parse('   ', mode='reply')

    def test_reply_wait_plan_requires_empty_messages_and_never_returns_sendable_text(self):
        parsed = self.adapter.parse(encode({'messages': [], 'plan': plan_json('wait')}), mode='reply')
        self.assertEqual(parsed.messages, ())
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': ['should not send'],
                'plan': plan_json('ignore')}), mode='reply')

    def test_active_requires_exact_trusted_count_and_strict_json(self):
        parsed = self.adapter.parse(encode({'messages': ['a', 'b']}), mode='active', expected_count=2)
        self.assertEqual(parsed.messages, ('a', 'b'))
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': ['a']}), mode='active', expected_count=2)
        with self.assertRaises(Unavailable):
            self.adapter.parse(encode({'messages': []}), mode='active')
        with self.assertRaises(InvalidSource):
            self.adapter.parse('not json', mode='active', expected_count=1)
        normalized = self.adapter.parse(encode({'messages': ['  line\n inner   spaces ']}),
                                        mode='active', expected_count=1)
        self.assertEqual(normalized.messages, ('line inner spaces',))
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': ['a b', 'a   b']}),
                               mode='active', expected_count=2)

    def test_care_requires_exact_count_but_allows_empty(self):
        parsed = self.adapter.parse(encode({'messages': []}), mode='care', expected_count=0)
        self.assertEqual(parsed.messages, ())
        skipped = self.adapter.parse(encode({'messages': []}), mode='care', expected_count=3)
        self.assertEqual(skipped.messages, ())
        self.assertEqual(self.adapter.parse(encode({'messages': ['a']}), mode='care',
            expected_count=1).messages, ('a',))
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': ['a', 'b']}), mode='care', expected_count=3)

    def test_continuation_actions_preserve_legacy_semantics_without_synthesized_plan(self):
        future = (datetime.fromisoformat(NOW) + timedelta(minutes=5)).isoformat()
        cases = (
            ({'action': 'wait', 'messages': [], 'next_check_at': future}, ()),
            ({'action': 'end', 'messages': [], 'next_check_at': None}, ()),
            ({'action': 'supplement', 'messages': ['补一句'], 'next_check_at': future}, ('补一句',)),
            ({'action': 'change_topic', 'messages': ['换话题'], 'next_check_at': future}, ('换话题',)),
        )
        for payload, expected in cases:
            with self.subTest(action=payload['action']):
                parsed = self.adapter.parse(encode(payload), mode='continuation', now=NOW)
                self.assertEqual(parsed.messages, expected)
                self.assertIsNone(parsed.plan)
                self.assertEqual(parsed.plan_status, 'plan_unavailable')
                self.assertEqual(parsed.continuation_action, payload['action'])
                self.assertEqual(parsed.legacy_payload.get('next_check_at'), payload['next_check_at'])

    def test_continuation_wait_and_end_reject_messages(self):
        for action, next_check in (('wait', NOW), ('end', None)):
            payload = {'action': action, 'messages': ['不应发送'], 'next_check_at': next_check}
            with self.subTest(action=action), self.assertRaises(InvalidSource):
                self.adapter.parse(encode(payload), mode='continuation', now=NOW)

    def test_continuation_messages_are_limited_to_four(self):
        future = (datetime.fromisoformat(NOW) + timedelta(minutes=5)).isoformat()
        for action in ('supplement', 'change_topic'):
            payload = {'action': action, 'messages': [str(i) for i in range(6)], 'next_check_at': future}
            with self.subTest(action=action), self.assertRaises(InvalidSource):
                self.adapter.parse(encode(payload), mode='continuation', now=NOW)

    def test_continuation_next_check_requires_timezone_and_30_seconds_to_24_hours(self):
        now = datetime.fromisoformat(NOW)
        for delta in (timedelta(seconds=30), timedelta(hours=24)):
            payload = {'action': 'wait', 'messages': [],
                       'next_check_at': (now + delta).isoformat()}
            self.adapter.parse(encode(payload), mode='continuation', now=now)
        for delta in (timedelta(seconds=29), timedelta(hours=24, seconds=1)):
            payload = {'action': 'wait', 'messages': [],
                       'next_check_at': (now + delta).isoformat()}
            with self.subTest(delta=delta), self.assertRaises(InvalidSource):
                self.adapter.parse(encode(payload), mode='continuation', now=now)
        naive = {'action': 'wait', 'messages': [],
                 'next_check_at': (now + timedelta(minutes=5)).replace(tzinfo=None).isoformat()}
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode(naive), mode='continuation', now=now)
        aware_target = {'action': 'wait', 'messages': [],
                        'next_check_at': (now + timedelta(minutes=5)).isoformat()}
        with self.assertRaises(Unavailable):
            self.adapter.parse(encode(aware_target), mode='continuation')

    def test_nested_plan_matches_old_action_and_empty_plan_has_no_messages(self):
        future = (datetime.fromisoformat(NOW) + timedelta(minutes=5)).isoformat()
        mapped = {'wait': 'wait', 'end': 'ignore', 'supplement': 'reply', 'change_topic': 'reply'}
        for old_action, new_action in mapped.items():
            payload = {'action': old_action, 'messages': [], 'plan': plan_json(new_action)}
            if old_action in ('supplement', 'change_topic'):
                payload['messages'] = ['继续']
            payload['next_check_at'] = None if old_action == 'end' else future
            parsed = self.adapter.parse(encode(payload), mode='continuation', now=NOW)
            self.assertEqual(parsed.plan.action, new_action)
        conflict = {'action': 'supplement', 'messages': ['继续'],
                    'next_check_at': future, 'plan': plan_json('wait')}
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode(conflict), mode='continuation', now=NOW)

    def test_modes_and_bad_types_are_stably_rejected(self):
        with self.assertRaises(InvalidSource):
            self.adapter.parse('x', mode=[])
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'messages': ['x']}), mode='active', expected_count=True)
        with self.assertRaises(InvalidSource):
            self.adapter.parse(encode({'action': [], 'messages': [], 'next_check_at': NOW}),
                               mode='continuation', now=NOW)


if __name__ == '__main__':
    unittest.main()
