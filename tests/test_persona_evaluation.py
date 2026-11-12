import json
import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from dialogue_pairs import DialoguePair
from persona_evaluation import (MAX_TRAINING_SOURCE_REFS, EvaluationBubble, evaluate,
                                generation_input, prepare_cases, source_locator)
from persona_types import (DataScope, FriendScope, NormalizedMessage, PrivacyView,
                           ScopeMismatch, SourceOrigin, SourceRef)
from dialogue_turns import Turn


FRIEND_ID = '123456'
STICKER = '[[LOCAL_STICKER:' + 'a' * 64 + ']]'


class PersonaEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.friend_dir = self.root / 'friends' / FRIEND_ID
        self.friend_dir.mkdir(parents=True)
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            FRIEND_ID: {'qq_id': FRIEND_ID, 'directory': str(self.friend_dir)}}}), encoding='utf-8')
        (self.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': FRIEND_ID}), encoding='utf-8')
        self.scope = FriendScope.load(FRIEND_ID, project_root=self.root)
        self.refs = (self.ref(1), self.ref(2), self.ref(3))
        self.pair = self.make_pair()
        self.partitions = {source_locator(ref): 'test' for ref in self.refs}

    def ref(self, seq, friend_id=FRIEND_ID, *, origin=SourceOrigin.REAL_RELATIONSHIP,
            dataset_id=None, import_id=None, record_id=None, speaker_id=None):
        return SourceRef(dataset_id=dataset_id, import_id=import_id, record_id=record_id,
            message_seq=seq, speaker_id=speaker_id or 'speaker-' + str(seq),
            origin=origin, data_scope=DataScope.PRIVATE_FRIEND,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=friend_id, event_time=None,
            recorded_at=datetime.now(timezone.utc).isoformat())

    @staticmethod
    def message(source, text, role='unmapped', kind='text', media_id=None,
                speaker_role='human'):
        locator = source.message_seq if source.message_seq is not None else source.record_id
        return NormalizedMessage(id='m' + str(locator), timestamp=source.event_time,
            speaker_id=source.speaker_id, speaker_role=speaker_role, text=text,
            message_type=kind, media_id=media_id, source=source,
            target_role=role, target_selected=(role == 'target'))

    def make_pair(self, pair_id='pair-1', *, friend_id=FRIEND_ID, refs=None,
                  target_text='TARGET-SENTINEL-DO-NOT-LEAK', target_kind='text',
                  target_origin=SourceOrigin.REAL_RELATIONSHIP):
        refs = tuple(refs if refs is not None else self.refs if hasattr(self, 'refs') else
                     (self.ref(1), self.ref(2), self.ref(3)))
        if len(refs) < 3:
            raise ValueError('fixture needs context, user and target refs')
        context_message = self.message(refs[0], 'earlier context')
        user_message = self.message(refs[1], 'current user message')
        target_ref = refs[-1]
        if target_ref.origin != target_origin:
            target_ref = SourceRef(dataset_id=target_ref.dataset_id, import_id=target_ref.import_id,
                record_id=target_ref.record_id, message_seq=target_ref.message_seq,
                speaker_id=target_ref.speaker_id, origin=target_origin,
                data_scope=target_ref.data_scope, privacy_view=target_ref.privacy_view,
                friend_id=target_ref.friend_id, event_time=target_ref.event_time,
                recorded_at=target_ref.recorded_at, pack_id=target_ref.pack_id,
                pack_version=target_ref.pack_version, license_id=target_ref.license_id,
                model_analysis_allowed=target_ref.model_analysis_allowed)
            refs = (*refs[:-1], target_ref)
        target_message = self.message(target_ref, target_text, kind=target_kind,
            media_id='target-media' if target_kind != 'text' else None)
        context_turn = Turn(id='context-turn', session_id='session-1', ordinal=0,
            speaker_id=refs[0].speaker_id, bubbles=(context_message,), previous_turn_id=None,
            event_time=None, time_quality='UNKNOWN')
        return DialoguePair(id=pair_id, friend_id=friend_id, session_id='session-1',
            input_turn_ids=('input-turn',), context_turn_ids=('context-turn',),
            target_turn_id='target-turn', target_speaker_id=target_ref.speaker_id,
            user_bubbles=(user_message,), target_bubbles=(target_message,),
            context_turns=(context_turn,), source_refs=refs, time_gap=None, time_quality='UNKNOWN')

    def prepare(self, pair=None, *, partitions=None, training=None, limit=64):
        return prepare_cases(self.scope, (pair or self.pair,),
            partition_by_source=self.partitions if partitions is None else partitions,
            training_source_refs=(self.ref(99),) if training is None else training, limit=limit)

    def test_generation_input_contains_only_prior_context_and_current_user(self):
        case = self.prepare()[0]
        value = generation_input(case)
        rendered = repr(value)
        self.assertIn('earlier context', rendered)
        self.assertIn('current user message', rendered)
        self.assertNotIn('TARGET-SENTINEL-DO-NOT-LEAK', rendered)
        self.assertNotIn('target-turn', rendered)
        self.assertNotIn('target-speaker', rendered)
        self.assertNotIn('source_refs', rendered)
        self.assertEqual(len(value.context_turns), 1)
        self.assertEqual(tuple(item.text for item in value.user_bubbles), ('current user message',))

    def test_context_cannot_contain_the_target_turn_or_target_bubble(self):
        pair = replace(self.make_pair(), context_turn_ids=('target-turn',))
        with self.assertRaisesRegex(ValueError, 'target turn'):
            self.prepare(pair)
        pair = self.make_pair()
        pair = replace(pair, user_bubbles=pair.target_bubbles)
        with self.assertRaisesRegex(ValueError, 'target bubble'):
            self.prepare(pair)

        pair = self.make_pair()
        wrong_session = replace(pair.context_turns[0], session_id='other-session')
        pair = replace(pair, context_turns=(wrong_session,))
        with self.assertRaisesRegex(ValueError, 'session and ordinal'):
            self.prepare(pair)
        pair = replace(self.make_pair(), user_bubbles=(
            self.message(self.refs[1], 'another hidden target', 'target'),))
        with self.assertRaisesRegex(ValueError, 'target bubble'):
            self.prepare(pair)
        pair = self.make_pair()
        hidden = pair.target_bubbles[0]
        context = Turn(id='other-turn', session_id='session-1', ordinal=0,
            speaker_id=hidden.speaker_id, bubbles=(hidden,), previous_turn_id=None,
            event_time=None, time_quality='UNKNOWN')
        pair = replace(pair, context_turns=(context,), context_turn_ids=('other-turn',))
        with self.assertRaisesRegex(ValueError, 'target bubble'):
            self.prepare(pair)

    def test_prior_human_reply_by_target_speaker_is_valid_context(self):
        prior = self.ref(4, speaker_id='speaker-3')
        refs = (prior, self.refs[1], self.refs[2])
        pair = self.make_pair(refs=refs)
        prior_reply = self.message(prior, 'older real reply', 'target')
        context = replace(pair.context_turns[0], speaker_id=prior.speaker_id,
                          bubbles=(prior_reply,))
        pair = replace(pair, context_turns=(context,))
        case = self.prepare(pair, partitions={source_locator(ref): 'test' for ref in refs})[0]
        self.assertEqual(generation_input(case).context_turns[0].speaker_id, pair.target_speaker_id)

    def test_scope_and_source_identity_must_match(self):
        with self.assertRaisesRegex(TypeError, 'FriendScope'):
            prepare_cases(object(), (self.pair,),
                partition_by_source=self.partitions, training_source_refs=(self.ref(99),))
        with self.assertRaisesRegex(TypeError, 'DialoguePair'):
            prepare_cases(self.scope, (object(),), partition_by_source=self.partitions,
                training_source_refs=(self.ref(99),))
        with self.assertRaisesRegex(ValueError, 'another friend scope'):
            self.prepare(self.make_pair(friend_id='654321'))
        wrong_ref = self.ref(4, friend_id='654321')
        wrong_refs = (wrong_ref, self.refs[1], self.refs[2])
        pair = self.make_pair(refs=wrong_refs)
        with self.assertRaisesRegex(ValueError, 'another friend'):
            self.prepare(pair, partitions={source_locator(ref): 'test' for ref in wrong_refs})
        self.assertEqual(source_locator(self.refs[0]), ('history', FRIEND_ID, 1))

    def test_scope_is_reloaded_and_import_records_are_not_split_by_message_seq(self):
        (self.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '654321'}), encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            self.prepare()

    def test_import_and_public_pack_locators_preserve_underlying_record_identity(self):
        imported_a = self.ref(1, dataset_id='dataset', import_id='import', record_id='record-a')
        imported_b = self.ref(2, dataset_id='dataset', import_id='import', record_id='record-a')
        imported_other = self.ref(3, dataset_id='dataset', import_id='import', record_id='record-b')
        self.assertEqual(source_locator(imported_a), source_locator(imported_b))
        self.assertNotEqual(source_locator(imported_a), source_locator(imported_other))
        partial = self.ref(4, dataset_id='dataset')
        with self.assertRaisesRegex(ValueError, 'record locator must be complete'):
            source_locator(partial)

        at = datetime.now(timezone.utc).isoformat()
        public_dataset = SourceRef(dataset_id='dataset', import_id='import', record_id='record-a',
            message_seq=None, speaker_id='public-speaker', origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.CLOUD_SAFE,
            friend_id=None, event_time=None, recorded_at=at)
        public_pack = SourceRef(dataset_id='dataset', import_id='import', record_id='record-a',
            message_seq=None, speaker_id='public-speaker', origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_PACK, privacy_view=PrivacyView.CLOUD_SAFE,
            friend_id=None, event_time=None, recorded_at=at, pack_id='pack', pack_version='v2')
        self.assertEqual(source_locator(public_dataset), source_locator(public_pack))

        refs = (imported_a, imported_other,
                self.ref(5, dataset_id='dataset', import_id='import', record_id='record-target'))
        pair = self.make_pair(refs=refs)
        partitions = {source_locator(ref): 'test' for ref in refs}
        same_record_different_seq = self.ref(99, dataset_id='dataset', import_id='import',
                                             record_id='record-a')
        with self.assertRaisesRegex(ValueError, 'overlaps training'):
            self.prepare(pair, partitions=partitions, training=(same_record_different_seq,))

    def test_missing_unknown_train_all_and_invalid_holdout_are_rejected(self):
        cases = (
            ({}, 'every pair source'),
            ({**self.partitions, source_locator(self.refs[0]): 'mystery'}, 'test, eval, or holdout'),
            ({**self.partitions, source_locator(self.refs[0]): 'train'}, 'test, eval, or holdout'),
            ({**self.partitions, source_locator(self.refs[0]): 'all'}, 'test, eval, or holdout'),
            ({**self.partitions, source_locator(self.refs[0]): {'is_holdout': 1}}, 'explicit bool'),
            ({**self.partitions, source_locator(self.refs[0]): {'split': 'train', 'is_holdout': True}},
             'test, eval, or holdout'),
            ({**self.partitions, source_locator(self.refs[0]): {'split': 'holdout', 'is_holdout': False}},
             'disagree'),
        )
        for partitions, error in cases:
            with self.subTest(partitions=partitions), self.assertRaisesRegex(ValueError, error):
                self.prepare(partitions=partitions)

    def test_pair_and_batch_cannot_mix_splits(self):
        mixed_pair = {**self.partitions, source_locator(self.refs[0]): 'eval'}
        with self.assertRaisesRegex(ValueError, 'mixed held-out'):
            self.prepare(partitions=mixed_pair)
        second_refs = (self.ref(4), self.ref(5), self.ref(6))
        second_pair = self.make_pair('pair-2', refs=second_refs)
        partitions = {**self.partitions, **{source_locator(ref): 'eval' for ref in second_refs}}
        with self.assertRaisesRegex(ValueError, 'mixes held-out'):
            prepare_cases(self.scope, (self.pair, second_pair), partition_by_source=partitions,
                          training_source_refs=(self.ref(99),))

    def test_only_explicit_test_eval_or_holdout_metadata_is_accepted(self):
        for split in ('test', 'eval', 'holdout'):
            with self.subTest(split=split):
                partitions = {key: {'split': split} for key in self.partitions}
                self.assertEqual(self.prepare(partitions=partitions)[0].split, split)
        partitions = {key: {'is_holdout': True} for key in self.partitions}
        self.assertEqual(self.prepare(partitions=partitions)[0].split, 'holdout')
        partitions = {key: {'split': 'test', 'is_holdout': True} for key in self.partitions}
        self.assertEqual(self.prepare(partitions=partitions)[0].split, 'test')
        case = self.prepare(training=())[0]
        self.assertEqual(case.leakage_status, 'unverified')
        self.assertEqual(evaluate(case, ('offline',)).leakage_status, 'unverified')

    def test_training_source_locator_overlap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'overlaps training'):
            self.prepare(training=(self.refs[-1],))
        self.assertEqual(source_locator(self.ref(3)), ('history', FRIEND_ID, 3))
        with self.assertRaisesRegex(ValueError, 'training_source_refs exceeds limit'):
            self.prepare(training=(self.ref(99),) * (MAX_TRAINING_SOURCE_REFS + 1))

    def test_synthetic_target_is_not_an_evaluation_gold(self):
        refs = (self.refs[0], self.refs[1], self.ref(3, origin=SourceOrigin.SYNTHETIC))
        pair = self.make_pair(refs=refs, target_origin=SourceOrigin.SYNTHETIC)
        partitions = {source_locator(ref): 'test' for ref in refs}
        with self.assertRaisesRegex(ValueError, 'explicit human'):
            self.prepare(pair, partitions=partitions)
        for unknown_id in ('unknown', 'unknown:speaker'):
            with self.subTest(target_speaker_id=unknown_id), self.assertRaisesRegex(
                    ValueError, 'target_speaker_id must be explicit'):
                self.prepare(replace(self.pair, target_speaker_id=unknown_id))
        synthetic_user_refs = (self.refs[0],
            self.ref(2, origin=SourceOrigin.SYNTHETIC), self.refs[2])
        synthetic_user = self.make_pair(refs=synthetic_user_refs)
        with self.assertRaisesRegex(ValueError, 'current user input'):
            self.prepare(synthetic_user,
                partitions={source_locator(ref): 'test' for ref in synthetic_user_refs})
        refs = (self.refs[0], self.refs[1], self.ref(3, origin=SourceOrigin.BOT_GENERATED))
        pair = self.make_pair(refs=refs, target_origin=SourceOrigin.BOT_GENERATED)
        target = replace(pair.target_bubbles[0], speaker_role='assistant')
        pair = replace(pair, target_bubbles=(target,))
        partitions = {source_locator(ref): 'test' for ref in refs}
        with self.assertRaisesRegex(ValueError, 'explicit human'):
            self.prepare(pair, partitions=partitions)

    def test_human_and_model_ratings_are_distinct_and_usage_stays_unknown(self):
        case = self.prepare()[0]
        result = evaluate(case, ('generated',), human_rating={'score': 4},
            model_evaluation={'model': 'fake-local', 'prompt_version': 'p1',
                'provider': 'local', 'evaluated_at': '2026-10-08T00:00:00+00:00',
                'usage_status': 'unknown', 'result': {'label': 'good'}})
        self.assertEqual(result.human_rating, {'score': 4})
        self.assertEqual(result.model_evaluation.result, {'label': 'good'})
        self.assertEqual(result.model_evaluation.model, 'fake-local')
        self.assertEqual(result.model_evaluation.prompt_version, 'p1')
        self.assertEqual(result.model_evaluation.provider, 'local')
        self.assertEqual(result.model_evaluation.evaluated_at, '2026-10-08T00:00:00+00:00')
        self.assertEqual(result.model_evaluation.usage_status, 'unknown')
        self.assertIsNot(result.human_rating, result.model_evaluation.result)
        with self.assertRaisesRegex(ValueError, 'requires model'):
            evaluate(case, ('generated',), model_evaluation={'result': 'missing metadata'})

    def test_metrics_handle_unicode_empty_output_multiple_bubbles_and_known_stickers(self):
        target_speaker = 'speaker-3'
        refs = (self.ref(1), self.ref(2), self.ref(3),
                self.ref(4, speaker_id=target_speaker), self.ref(5, speaker_id=target_speaker))
        pair = self.make_pair(refs=refs)
        target_text = 'Hello🙂'
        pair = replace(pair, target_bubbles=(self.message(refs[2], target_text),
            self.message(refs[3], STICKER, kind='sticker', media_id='asset-1'),
            self.message(refs[4], '', kind='image', media_id='unknown-image')))
        partitions = {source_locator(ref): 'eval' for ref in refs}
        case = self.prepare(pair, partitions=partitions)[0]
        result = evaluate(case, (
            EvaluationBubble('Hello🙂?'),
            EvaluationBubble(STICKER, message_type='sticker', has_media=True),
            EvaluationBubble('', message_type='image', has_media=True),
        ))
        metrics = result.metrics
        self.assertEqual(metrics.target_chars, len(target_text))
        self.assertEqual(metrics.generated_chars, len('Hello🙂?'))
        self.assertEqual(metrics.target_bubbles, 3)
        self.assertEqual(metrics.generated_bubbles, 3)
        self.assertEqual(metrics.question_bubbles, 1)
        self.assertEqual(metrics.question_marks, 1)
        self.assertAlmostEqual(metrics.question_rate, 1.0)
        self.assertEqual(metrics.source_copy_bubbles, 0)
        copied = evaluate(case, ('current user message', 'yeah', 'generated'))
        self.assertEqual(copied.metrics.source_copy_bubbles, 1)
        self.assertEqual(evaluate(case, ('haha',)).metrics.source_copy_bubbles, 0)
        self.assertEqual(metrics.target_stickers, 1)
        self.assertEqual(metrics.target_unknown_media, 1)
        self.assertEqual(metrics.generated_stickers, 1)
        self.assertEqual(metrics.generated_unknown_media, 1)
        self.assertIsNone(metrics.sticker_delta)
        self.assertEqual(metrics.over_explanation.status, 'estimated')
        self.assertEqual(evaluate(case, (target_text,)).metrics.source_copy_bubbles, 0)
        marker_only = evaluate(case, (STICKER,)).metrics
        self.assertEqual(marker_only.generated_chars, 0)
        self.assertEqual(marker_only.question_marks, 0)

        empty = evaluate(case, ()).metrics
        self.assertEqual(empty.generated_chars, 0)
        self.assertIsNone(empty.question_rate)
        self.assertIsNone(empty.source_copy_rate)

    def test_media_without_explicit_sticker_evidence_is_not_counted(self):
        case = self.prepare()[0]
        result = evaluate(case, (EvaluationBubble('', message_type='image', has_media=True),))
        self.assertEqual(result.metrics.generated_stickers, 0)
        self.assertEqual(result.metrics.generated_unknown_media, 1)
        self.assertIsNone(result.metrics.sticker_delta)

    def test_sticker_types_and_explicit_qq_markers_are_counted_as_stickers(self):
        case = self.prepare()[0]
        typed = evaluate(case, (EvaluationBubble('', message_type='face', has_media=True),))
        self.assertEqual(typed.metrics.generated_stickers, 1)
        self.assertEqual(typed.metrics.generated_unknown_media, 0)
        marked = evaluate(case, ('[[QQ表情:微笑]]', '[QQ原生表情：流泪，编号8]'))
        self.assertEqual(marked.metrics.generated_stickers, 2)
        self.assertEqual(marked.metrics.generated_unknown_media, 0)
        self.assertEqual(marked.metrics.generated_chars, 0)

    def test_case_and_output_limits_are_enforced(self):
        for limit in (0, True, 65):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.prepare(limit=limit)
        second_pair = self.make_pair('pair-2')
        with self.assertRaisesRegex(ValueError, 'exceeds limit'):
            prepare_cases(self.scope, (self.pair, second_pair), partition_by_source=self.partitions,
                          training_source_refs=(self.ref(99),), limit=1)
        case = self.prepare()[0]
        with self.assertRaisesRegex(ValueError, 'bubble limit'):
            evaluate(case, ('x',) * 65)

    def test_context_turn_and_character_limits_are_enforced(self):
        pair = self.make_pair()
        context_message = pair.context_turns[0].bubbles[0]
        extra_turns = tuple(Turn(id=f'context-{index}', session_id=pair.session_id,
            ordinal=index, speaker_id=context_message.speaker_id, bubbles=(context_message,),
            previous_turn_id=None, event_time=None, time_quality='UNKNOWN') for index in range(4))
        pair = replace(pair, context_turns=extra_turns,
                        context_turn_ids=tuple(turn.id for turn in extra_turns))
        with self.assertRaisesRegex(ValueError, 'context turn limit'):
            self.prepare(pair)

        pair = replace(self.make_pair(), user_bubbles=(self.message(self.refs[1], 'x' * 4001),))
        with self.assertRaisesRegex(ValueError, 'bubble exceeds character'):
            self.prepare(pair)

        case = self.prepare()[0]
        with self.assertRaisesRegex(ValueError, 'bubble exceeds character'):
            evaluate(case, ('x' * 4001,))

        pair = self.make_pair()
        source = self.refs[1]
        pair = replace(pair, user_bubbles=tuple(NormalizedMessage(id=f'user-{index}', timestamp=None,
            speaker_id=source.speaker_id, speaker_role='human', text='x' * 4000,
            message_type='text', media_id=None, source=source,
            target_role='input', target_selected=False) for index in range(6)))
        with self.assertRaisesRegex(ValueError, 'case exceeds character'):
            self.prepare(pair)

        case = self.prepare()[0]
        with self.assertRaisesRegex(ValueError, 'generated output exceeds character'):
            evaluate(case, ('x' * 4000,) * 6)

    def test_no_model_or_store_is_constructed_by_evaluation(self):
        case = self.prepare()[0]
        self.assertFalse((self.friend_dir / 'persona.sqlite3').exists())
        result = evaluate(case, ('offline result',))
        self.assertEqual(result.metrics.generated_chars, len('offline result'))
        self.assertEqual(result.model_evaluation.usage_status, 'unknown')
        self.assertEqual(result.leakage_status, 'locator_overlap_checked')
        self.assertFalse((self.friend_dir / 'persona.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
