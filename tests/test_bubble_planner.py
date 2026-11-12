import json
from pathlib import Path
import tempfile
import unittest

from conversation_planner import ParsedConversation
from persona_store import Unavailable
from persona_types import (Candidate, ConversationPlan, DataScope, FriendScope, InvalidSource,
    PrivacyView, RuntimeSnapshot, SourceOrigin, SourceRef)
from bubble_planner import BubblePlanner
from similarity_guard import SimilarityGuard, SimilarityResult, may_rewrite_once


NOW = '2026-10-07T12:00:00+00:00'


class BubblePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = snapshot(self.root)
        self.planner = BubblePlanner()

    def tearDown(self):
        self.temp.cleanup()

    def test_identity_is_copied_from_snapshot_and_model_identity_is_ignored(self):
        result = self.planner.plan({'messages': ['好'], 'friend_id': '54321',
                                    'generation': -1, 'mode': 'care'}, None, {}, self.snapshot)
        self.assertEqual(result.friend_id, '12345')
        self.assertEqual(result.generation, 7)
        self.assertEqual(result.mode, 'reply')
        self.assertEqual(result.source_message_id, 'source-1')
        self.assertEqual(result.attempt_id, 'attempt-1')

    def test_missing_trusted_snapshot_or_active_count_is_unavailable(self):
        with self.assertRaises(Unavailable):
            self.planner.plan(['好'], None, {}, None)
        with self.assertRaises(Unavailable):
            self.planner.plan(['好'], None, {}, snapshot(self.root, mode='proactive'))

    def test_reply_and_care_are_limited_to_four_bubbles(self):
        with self.assertRaises(InvalidSource):
            self.planner.plan(['x'] * 5, None, {}, self.snapshot)
        five = ParsedConversation(messages=tuple('x%d' % i for i in range(5)),
            plan=None, legacy_payload={}, plan_status='plan_unavailable', mode='reply')
        for runtime in (self.snapshot, snapshot(self.root, mode='care')):
            with self.subTest(mode=runtime.mode), self.assertRaises(InvalidSource):
                self.planner.plan(five, None, {}, runtime)

    def test_proactive_exact_count_and_care_skip(self):
        active = snapshot(self.root, mode='proactive')
        self.assertEqual(len(self.planner.plan(['a', 'b'], None,
            {'expected_count': 2}, active).bubbles), 2)
        with self.assertRaises(InvalidSource):
            self.planner.plan(['a'], None, {'expected_count': 2}, active)
        care = snapshot(self.root, mode='care')
        self.assertEqual(self.planner.plan({'messages': []}, None,
            {'expected_count': 3}, care).bubbles, ())
        with self.assertRaises(InvalidSource):
            self.planner.plan(['a'], None, {'expected_count': 3}, care)

    def test_active_and_care_normalize_before_duplicate_check(self):
        for mode in ('proactive', 'care'):
            with self.subTest(mode=mode), self.assertRaises(InvalidSource):
                self.planner.plan(['a   b', 'a b'], None,
                    {'expected_count': 2}, snapshot(self.root, mode=mode))

    def test_wait_and_ignore_plans_cannot_emit_messages(self):
        wait = conversation_plan('wait')
        with self.assertRaises(InvalidSource):
            self.planner.plan(['send'], wait, {}, self.snapshot)
        parsed = ParsedConversation(messages=(), plan=conversation_plan('ignore'),
            legacy_payload={}, plan_status='available', mode='reply')
        self.assertEqual(self.planner.plan(parsed, None, {}, self.snapshot).bubbles, ())

    def test_raw_nested_plan_is_rejected_instead_of_ignored(self):
        for action in ('wait', 'ignore'):
            raw = {'messages': ['x'], 'plan': {'action': action}}
            with self.subTest(action=action), self.assertRaises(InvalidSource):
                self.planner.plan(raw, None, {}, self.snapshot)
        self.assertEqual(self.planner.plan({'messages': ['legacy']}, None, {},
            self.snapshot).bubbles, ('legacy',))

    def test_continuation_action_requires_consistent_plan_and_messages(self):
        cont = snapshot(self.root, mode='continuation')
        output = ParsedConversation(messages=(), plan=conversation_plan('ignore'),
            legacy_payload={'action': 'end'}, plan_status='available', mode='continuation')
        self.assertEqual(self.planner.plan(output, None, {}, cont).bubbles, ())
        bad = ParsedConversation(messages=('bye',), plan=None,
            legacy_payload={'action': 'wait'}, plan_status='plan_unavailable',
            mode='continuation')
        with self.assertRaises(InvalidSource):
            self.planner.plan(bad, None, {}, cont)

    def test_local_image_sticker_is_standalone_and_limited_to_one(self):
        marker = '[[LOCAL_STICKER:' + 'a' * 64 + ']]'
        self.assertEqual(self.planner.plan([marker], None, {}, self.snapshot).bubbles,
                         (marker,))
        with self.assertRaises(InvalidSource):
            self.planner.plan(['prefix ' + marker], None, {}, self.snapshot)
        with self.assertRaises(InvalidSource):
            self.planner.plan([marker, marker], None, {}, self.snapshot)


class SimilarityGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = SimilarityGuard()

    def test_short_common_phrases_are_exempt(self):
        result = self.guard.check(['haha', 'ok', 'what?', '?', 'lol'], [], ['haha'])
        self.assertFalse(result.flagged)
        self.assertEqual(result.reason, 'short_phrase_exempt')

    def test_similarity_threshold_is_inclusive_and_rewrite_is_only_suggested_once(self):
        result = self.guard.check(['abcdefghij'], [], ['abcdefghijx'])
        self.assertTrue(result.flagged)
        self.assertEqual(result.similarities, (0.9,))
        self.assertTrue(may_rewrite_once(result))
        self.assertFalse(may_rewrite_once(result, rewrite_attempted=True))
        self.assertFalse(may_rewrite_once(SimilarityResult(False, 'below_threshold', (0.0,))))

    def test_no_reference_does_not_block_and_candidate_example_is_read_as_text(self):
        empty = self.guard.check(['new line'], [], [])
        self.assertFalse(empty.flagged)
        self.assertEqual(empty.reason, 'no_reference')
        candidate = example_candidate({'target_bubbles': [{'text': 'abcdefghijx'}]})
        self.assertTrue(self.guard.check(['abcdefghij'], [candidate], []).flagged)

    def test_nontext_inputs_are_rejected_without_side_effects(self):
        with self.assertRaises(ValueError):
            self.guard.check(['ok'], [], [object()])
        self.assertFalse(hasattr(self.guard, 'send'))


def snapshot(root, mode='reply'):
    friend_dir = root / 'friends' / '12345'
    friend_dir.mkdir(parents=True, exist_ok=True)
    (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
        'qq_id': '12345', 'directory': str(friend_dir)}}}), encoding='utf-8')
    (friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
    scope = FriendScope.load('12345', project_root=root)
    return RuntimeSnapshot(scope=scope, bound_self_id='99999', inbound_generation=7,
        source_message_id='source-1', attempt_id='attempt-1', mode=mode,
        created_at=NOW, profile_revision=2)


def conversation_plan(action):
    return ConversationPlan(action=action, intent='', tone='', core_meaning='',
        ask_question=False, continue_topic=False, introduce_new_topic=False,
        desired_bubbles=0, sticker_intent=None)


def example_candidate(payload):
    source = SourceRef(dataset_id='fixture', import_id='fixture-import',
        record_id='record', message_seq=None, speaker_id='person',
        origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
        privacy_view=PrivacyView.LOCAL_FULL, friend_id='12345', event_time=None,
        recorded_at=NOW)
    return Candidate(id='example-1', namespace='example', type='example',
        content=json.dumps(payload, ensure_ascii=False), score=0.8, source=source,
        reason='fixture', retriever='fixture', friend_id='12345', timestamp=None,
        confidence=1.0, original_scope=DataScope.PRIVATE_FRIEND, pack_version=None)


if __name__ == '__main__':
    unittest.main()
