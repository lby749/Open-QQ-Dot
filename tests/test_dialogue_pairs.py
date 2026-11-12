import json
import tempfile
import unittest
from pathlib import Path

from dialogue_pairs import DialoguePairBuilder
from dialogue_turns import Session, Turn, build_turns
from persona_store import ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, NormalizedMessage,
                           PrivacyView, SourceOrigin, SourceRef)


class DialoguePairBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        directory = self.root / 'friends' / '12345'
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(directory)}}}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.builder = DialoguePairBuilder(self.scope)

    def message(self, mid, speaker, *, role='human', origin=SourceOrigin.REAL_RELATIONSHIP,
                timestamp='2026-10-04T12:00:00+00:00', data_scope=DataScope.PRIVATE_FRIEND,
                media=None, text=None):
        source = SourceRef(dataset_id='fixture', import_id='imp', record_id=mid,
            message_seq=None, speaker_id=speaker, origin=origin, data_scope=data_scope,
            privacy_view=PrivacyView.LOCAL_FULL,
            friend_id='12345' if data_scope == DataScope.PRIVATE_FRIEND else None,
            event_time=timestamp, recorded_at='2026-10-04T12:00:00+00:00',
            license_id='fixture-license' if data_scope != DataScope.PRIVATE_FRIEND else None,
            model_analysis_allowed=False)
        return NormalizedMessage(id=mid, timestamp=timestamp, speaker_id=speaker,
            speaker_role=role, text=text if text is not None else mid,
            message_type='image' if media else 'text', media_id=media,
            source=source, target_role='unmapped', target_selected=False)

    def turns(self, specs, *, session='session'):
        messages = [self.message(spec[0], spec[1], **dict(zip(
            ('role', 'origin', 'timestamp', 'data_scope', 'media'), spec[2:])))
            for spec in specs]
        value = Session(id=session, friend_id='12345', boundary_key='{}',
            speaker_set=tuple(sorted({message.speaker_id for message in messages})),
            start_time=messages[0].timestamp if messages else None,
            end_time=messages[-1].timestamp if messages else None,
            gap_seconds=1800.0, time_quality='UNKNOWN', messages=tuple(messages))
        return build_turns(value)

    def test_only_explicit_target_ids_select_targets_and_bubbles_are_preserved(self):
        turns = self.turns([('in-1', 'friend'), ('in-2', 'friend'),
                            ('out', 'persona', 'human', SourceOrigin.REAL_PERSONA_POOL)])
        self.assertEqual(list(self.builder.build_pairs(turns, [])), [])
        pair, = self.builder.build_pairs(turns, {'persona'})
        self.assertEqual(pair.input_turn_ids, (turns[0].id,))
        self.assertEqual([item.id for item in pair.user_bubbles], ['in-1', 'in-2'])
        self.assertEqual([item.id for item in pair.target_bubbles], ['out'])
        self.assertEqual(pair.target_speaker_id, 'persona')
        self.assertEqual(pair.friend_id, '12345')

    def test_media_and_source_refs_cover_context_input_target_without_scope_rewrite(self):
        turns = self.turns([('old', 'person-b', 'human', SourceOrigin.PUBLIC_DATASET,
                             '2026-10-04T11:59:00+00:00', DataScope.PUBLIC_DATASET),
                            ('input', 'person-a', 'human', SourceOrigin.REAL_SINGLE_SPEAKER,
                             '2026-10-04T11:59:55+00:00', DataScope.PUBLIC_DATASET,
                             'voice-1'),
                            ('target', 'person-b', 'human', SourceOrigin.PUBLIC_DATASET,
                             '2026-10-04T12:00:00+00:00', DataScope.PUBLIC_DATASET)])
        pair, = self.builder.build_pairs(turns, {'person-b'})
        self.assertEqual(pair.time_gap, 5.0)
        self.assertEqual(pair.time_quality, 'KNOWN')
        self.assertEqual(pair.context_turn_ids, (turns[0].id,))
        self.assertEqual(pair.user_bubbles[0].media_id, 'voice-1')
        self.assertEqual({ref.friend_id for ref in pair.source_refs}, {None})
        self.assertEqual(pair.friend_id, '12345')
        self.assertTrue(all(ref.data_scope == DataScope.PUBLIC_DATASET for ref in pair.source_refs))

    def test_context_is_at_most_three_turns_before_input_and_never_future(self):
        specs = [(f'm{i}', 'a' if i % 2 == 0 else 'b') for i in range(8)]
        turns = self.turns(specs)
        pairs = list(self.builder.build_pairs(turns, {'b'}))
        last = pairs[-1]
        self.assertEqual(last.input_turn_ids, (turns[6].id,))
        self.assertEqual(last.target_turn_id, turns[7].id)
        self.assertEqual(last.context_turn_ids, tuple(turn.id for turn in turns[3:6]))
        self.assertNotIn(turns[6].id, last.context_turn_ids)
        self.assertNotIn(turns[7].id, last.context_turn_ids)

    def test_bot_context_is_retained_but_adjacent_bot_never_becomes_input(self):
        with_bot_before_target = self.turns([
            ('a0', 'a'), ('bot', 'helper', 'assistant', SourceOrigin.BOT_GENERATED),
            ('b1', 'b')])
        self.assertEqual(list(self.builder.build_pairs(with_bot_before_target, {'b'})), [])
        turns = self.turns([
            ('a0', 'a'), ('bot', 'helper', 'assistant', SourceOrigin.BOT_GENERATED),
            ('a1', 'a'), ('b1', 'b')])
        pair, = self.builder.build_pairs(turns, {'b'})
        self.assertEqual(pair.input_turn_ids, (turns[2].id,))
        self.assertEqual(pair.context_turn_ids, (turns[0].id, turns[1].id))
        self.assertEqual(pair.context_turns[1].bubbles[0].source.origin, SourceOrigin.BOT_GENERATED)
        self.assertNotIn('helper', {pair.target_speaker_id})

    def test_bot_synthetic_manual_unknown_and_unknown_role_never_become_targets(self):
        rejected = [
            ('bot', 'target', 'assistant', SourceOrigin.BOT_GENERATED),
            ('synthetic', 'target', 'human', SourceOrigin.SYNTHETIC),
            ('manual', 'target', 'human', SourceOrigin.MANUAL),
            ('unknown-origin', 'target', 'human', None),
            ('unknown-role', 'target', 'unknown', SourceOrigin.REAL_RELATIONSHIP),
        ]
        for mid, speaker, role, origin in rejected:
            with self.subTest(mid=mid):
                turns = self.turns([('in', 'friend'), (mid, speaker, role, origin)])
                self.assertEqual(list(self.builder.build_pairs(turns, {'target'})), [])

    def test_three_humans_unknown_participant_and_same_speaker_adjacency_do_not_pair(self):
        three = self.turns([('a', 'a'), ('b', 'b'), ('c', 'c'), ('target', 'b')])
        self.assertEqual(list(self.builder.build_pairs(three, {'b'})), [])
        unknown = self.turns([('a', 'a'), ('mystery', 'x', 'unknown', None), ('b', 'b')])
        self.assertEqual(list(self.builder.build_pairs(unknown, {'b'})), [])
        same = self.turns([('a', 'a'), ('b1', 'b'), ('b2', 'b')])
        pair, = self.builder.build_pairs(same, {'b'})
        self.assertEqual([item.id for item in pair.target_bubbles], ['b1', 'b2'])

    def test_missing_or_naive_adjacent_time_stays_unknown(self):
        turns = self.turns([('in', 'a', 'human', SourceOrigin.REAL_RELATIONSHIP,
                             '2026-10-04T12:00:00'),
                            ('out', 'b', 'human', SourceOrigin.REAL_RELATIONSHIP,
                             '2026-10-04')])
        pair, = self.builder.build_pairs(turns, {'b'})
        self.assertIsNone(pair.time_gap)
        self.assertEqual(pair.time_quality, 'UNKNOWN')

    def test_session_order_scope_and_target_argument_are_validated(self):
        turns = self.turns([('a', 'a'), ('b', 'b')])
        with self.assertRaises(InvalidSource):
            list(self.builder.build_pairs(turns, 'b'))
        with self.assertRaises(InvalidSource):
            list(self.builder.build_pairs(turns, {''}))
        with self.assertRaises(InvalidSource):
            list(self.builder.build_pairs([turns[1], turns[0]], {'b'}))
        other = self.turns([('other', 'b')], session='other-session')
        with self.assertRaises(InvalidSource):
            list(self.builder.build_pairs([*turns, *other], {'b'}))
        forged = self.message('foreign', 'a')
        forged = NormalizedMessage(id=forged.id, timestamp=forged.timestamp,
            speaker_id=forged.speaker_id, speaker_role=forged.speaker_role,
            text=forged.text, message_type=forged.message_type, media_id=forged.media_id,
            source=SourceRef(dataset_id='fixture', import_id='imp', record_id='foreign',
                message_seq=None, speaker_id='a', origin=SourceOrigin.REAL_RELATIONSHIP,
                data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id='23456', event_time=forged.timestamp,
                recorded_at='2026-10-04T12:00:00+00:00'),
            target_role='unmapped', target_selected=False)
        invalid = Turn(id='foreign-turn', session_id=turns[0].session_id, ordinal=0,
            speaker_id='a', bubbles=(forged,), previous_turn_id=None,
            event_time=forged.timestamp, time_quality='KNOWN')
        with self.assertRaises(ScopeMismatch):
            list(self.builder.build_pairs([invalid], {'a'}))

    def test_lazy_builder_rechecks_friend_binding_at_start_and_each_yield(self):
        turns = self.turns([('a0', 'a'), ('b0', 'b'), ('a1', 'a'), ('b1', 'b')])
        pending = self.builder.build_pairs(turns, {'b'})
        mapping = self.root / 'friends.json'
        mapping.write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.scope.friend_dir)}}}), encoding='utf-8')
        (self.scope.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '99999'}),
                                                           encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            next(pending)

        (self.scope.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}),
                                                           encoding='utf-8')
        builder = DialoguePairBuilder(self.scope)
        pending = builder.build_pairs(turns, {'b'})
        next(pending)
        (self.scope.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '99999'}),
                                                           encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            next(pending)

    def test_hand_built_unknown_actor_turns_never_pair(self):
        input_turn, = self.turns([('in', 'friend')])
        for actor in ('unknown', 'unknown:manual'):
            with self.subTest(actor=actor):
                bubble = self.message('target-' + actor.replace(':', '-'), actor)
                target = Turn(id='manual-' + actor.replace(':', '-'),
                    session_id=input_turn.session_id, ordinal=1, speaker_id=actor,
                    bubbles=(bubble,), previous_turn_id=input_turn.id,
                    event_time=bubble.timestamp, time_quality='KNOWN')
                self.assertEqual(list(self.builder.build_pairs(
                    [input_turn, target], {actor})), [])


class PairStoreTests(unittest.TestCase):
    def setUp(self):
        from tests.test_dialogue_turns import DialogueStoreTests
        DialogueStoreTests.setUp(self)
        from dialogue_pairs import PairStore
        self.pair_store = PairStore(self.scope)
        self.addCleanup(self.pair_store.close)

    def test_pair_store_rebuilds_from_session_and_idempotently_cas_saves(self):
        from dialogue_pairs import DialoguePairBuilder
        from dialogue_turns import SessionSegmenter, build_turns
        session = next(SessionSegmenter(self.scope).segment(self.messages[2:4]))
        self.dialogues.save_session(session, self.dialogues.revision)
        turns = build_turns(session)
        pairs = tuple(DialoguePairBuilder(self.scope).build_pairs(turns, {'other'}))
        self.assertEqual(len(pairs), 1)
        revision = self.pair_store.revision
        self.assertEqual(self.pair_store.save_pairs(pairs, revision), revision + 1)
        self.assertEqual(self.pair_store.load_pairs(session.id), pairs)
        self.assertEqual(self.pair_store.save_pairs(pairs, revision), revision + 1)
        self.assertEqual(self.pair_store.revision, revision + 1)
        row = self.pair_store.db.execute('SELECT * FROM dialogue_pairs WHERE friend_id=?',
                                         ('12345',)).fetchone()
        self.assertNotIn('text', row.keys())
        from itertools import repeat
        from persona_store import Unavailable
        with self.assertRaises(Unavailable):
            self.pair_store.save_pairs(repeat(pairs[0], 10001), self.pair_store.revision)
        self.assertEqual(self.pair_store.revision, revision + 1)

    def test_pair_store_rejects_stale_revision_and_forged_pair(self):
        from dataclasses import replace
        from dialogue_pairs import DialoguePairBuilder
        from dialogue_turns import SessionSegmenter, build_turns
        from persona_store import Conflict
        from persona_types import InvalidSource
        session = next(SessionSegmenter(self.scope).segment(self.messages[2:4]))
        self.dialogues.save_session(session, self.dialogues.revision)
        pairs = tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), {'other'}))
        stale = self.pair_store.revision - 1
        with self.assertRaises(Conflict):
            self.pair_store.save_pairs(pairs, stale)
        with self.assertRaises(InvalidSource):
            self.pair_store.save_pairs((replace(pairs[0], target_speaker_id='human'),),
                                       self.pair_store.revision)

    def test_empty_pair_read_still_checks_current_friend_binding(self):
        (self.scope.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '99999'}),
                                                           encoding='utf-8')
        from persona_store import ScopeMismatch
        with self.assertRaises(ScopeMismatch):
            self.pair_store.load_pairs('session-with-no-pairs')

    def test_empty_pair_read_rejects_invalid_session_id(self):
        from persona_types import InvalidSource
        for session_id in (None, ''):
            with self.subTest(session_id=session_id):
                with self.assertRaises(InvalidSource):
                    self.pair_store.load_pairs(session_id)


if __name__ == '__main__':
    unittest.main()
