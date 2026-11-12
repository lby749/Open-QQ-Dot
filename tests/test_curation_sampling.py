from dataclasses import replace
import importlib
import unittest

from curation_sampling import DiversitySampler, JudgedPair, LocalWeakLabeler
from persona_analysis_input import EvidenceSpan, RelationshipEvent
from persona_types import (DataScope, InvalidSource, PrivacyView, SourceOrigin,
                           SourceRef)


_LABELS = dict(intent='share', emotion='happy', tone='casual', initiative='proactive')


class MockClassifier:
    runtime = dict(version='fixture', model_repo='fixture/model', model_revision='rev',
        base_model='fixture', base_model_observed_revision='base', model='fixture.gguf',
        assets=[dict(name='fixture.gguf', sha256='a' * 64)])

    def __init__(self, *, fail=False, review=True):
        self.fail = fail
        self.review = review
        self.calls = []

    def classify_batch_with_review(self, texts, *, contexts=None):
        self.calls.append((tuple(texts), tuple(tuple(x) for x in contexts)))
        if self.fail:
            raise RuntimeError('mock local classifier unavailable')
        return [dict(labels=dict(_LABELS), needs_review=self.review,
                     reason='tone_ambiguity' if self.review else 'clear') for _ in texts]


class CurationSamplingTests(unittest.TestCase):
    def setUp(self):
        fixture = importlib.import_module('tests.test_dialogue_pairs').DialoguePairBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.counter = 0

    def _judge_fixture(self):
        fixture = importlib.import_module('tests.test_persona_judge').PersonaJudgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    @staticmethod
    def _judge_result(pair, *, run_status='COMPLETED', needs_review=False):
        from persona_judge import PERSONA_FIELDS, PROMPT_VERSION
        return {'run_id': 'judge-run-' + pair.id, 'status': run_status,
            'results': [{'pair_id': pair.id, 'scores': {name: 0.8 for name in PERSONA_FIELDS},
                'quality_score': 0.9, 'needs_review': needs_review, 'abstain_reason': None}],
            'model': 'mock-judge', 'prompt_version': PROMPT_VERSION, 'prompt_hash': 'a' * 64,
            'usage': {'status': 'known', 'input_tokens': 10, 'output_tokens': 2,
                      'total_tokens': 12}, 'error_code': None}

    def test_diversity_sampler_requires_completed_public_train_pairs_and_bounds_near_dupes(self):
        fixture = self._judge_fixture()
        pair = fixture.pair()

        def source_variant(sample, pair_id, prefix):
            refs = {ref: replace(ref, record_id=prefix + ref.record_id)
                    for ref in sample.source_refs}
            return replace(sample, id=pair_id,
                user_bubbles=tuple(replace(message, source=refs[message.source])
                                   for message in sample.user_bubbles),
                target_bubbles=tuple(replace(message, source=refs[message.source])
                                     for message in sample.target_bubbles),
                source_refs=tuple(refs[ref] for ref in sample.source_refs))

        # New IDs stand for distinct target turns from the same imported source.
        duplicate = replace(pair, id='pair-near', target_bubbles=(
            replace(pair.target_bubbles[0], text='还不错呀！！'),))
        other = replace(pair, id='pair-other', target_bubbles=(
            replace(pair.target_bubbles[0], text='收到啦，挺好的'),))
        reviewed = replace(pair, id='pair-reviewed')
        unknown_partition = source_variant(pair, 'pair-unknown-partition', 'unknown-')
        heldout = source_variant(pair, 'pair-heldout', 'holdout-')
        bridge_texts = (
            'che_quick_brown_fox_jumps_over_the_lazy_dog_the_quick_brown_fox_jumps_over_the_lazy_dog_',
            'jhe_quick_brown_fox_jumps_over_the_lazy_dog_the_quick_brown_fox_jumps_over_the_lazy_dog_',
            'the_quick_brown_fox_jumps_over_the_lazy_dog_the_quick_brown_fox_jumps_over_the_lazy_dog_',
        )
        bridge_pairs = [source_variant(pair, f'pair-bridge-{i}', f'bridge-{i}-')
                        for i in range(3)]
        bridge_pairs = [replace(sample, target_bubbles=(
            replace(sample.target_bubbles[0], text=text),))
            for sample, text in zip(bridge_pairs, bridge_texts)]
        private = fixture.pair(private=True)
        private = replace(private, id='pair-private')
        refs = pair.source_refs
        partitions = {(ref.dataset_id, ref.import_id, ref.record_id): {
            'split': 'train', 'is_holdout': False, 'source_hash': 'b' * 64} for ref in refs}
        for ref in heldout.source_refs:
            partitions[(ref.dataset_id, ref.import_id, ref.record_id)] = {
                'split': 'test', 'is_holdout': False}
        for sample in bridge_pairs:
            for ref in sample.source_refs:
                partitions[(ref.dataset_id, ref.import_id, ref.record_id)] = {
                    'split': 'train', 'is_holdout': False}
        tagged = JudgedPair(pair, self._judge_result(pair), scene='casual_update',
                            reaction='short_ack', tag_source_ref=pair.target_bubbles[0].source)
        second = JudgedPair(other, self._judge_result(other))
        weak, = LocalWeakLabeler().iter_label([pair])
        candidates = [
            tagged,
            second,
            JudgedPair(duplicate, self._judge_result(duplicate)),
            JudgedPair(reviewed, self._judge_result(reviewed, needs_review=True)),
            JudgedPair(unknown_partition, self._judge_result(unknown_partition)),
            JudgedPair(heldout, self._judge_result(heldout)),
            *(JudgedPair(sample, self._judge_result(sample)) for sample in bridge_pairs),
            JudgedPair(private, self._judge_result(private)),
            weak,
        ]

        result = DiversitySampler().select(candidates, partition_by_source=partitions,
                                            limit=8, candidate_limit=16)
        self.assertTrue({'pair-a', 'pair-other'}.issubset(
            {sample.pair.id for sample in result.samples}))
        self.assertEqual(result.counts['input'], len(candidates))
        self.assertEqual(result.counts['rejected_judge_needs_review'], 1)
        self.assertEqual(result.counts['rejected_partition_missing'], 1)
        self.assertEqual(result.counts['rejected_holdout_excluded'], 1)
        self.assertEqual(result.counts['rejected_source_not_cloudsafe_public'], 1)
        self.assertEqual(result.counts['rejected_candidate_type'], 1)
        self.assertEqual(result.counts['rejected_near_duplicate_cluster_cap'], 2)
        self.assertEqual(result.counts['scene_unknown'], 3)
        self.assertEqual(result.counts['reaction_unknown'], 3)
        self.assertEqual(result.counts['selected'], 3)
        self.assertTrue(any(sample.scene == 'unknown' for sample in result.samples))
        self.assertTrue(all(sample.partitions[0]['split'] == 'train' for sample in result.samples))
        bridge_selected = [sample for sample in result.samples if sample.pair.id.startswith('pair-bridge-')]
        self.assertEqual(len(bridge_selected), 1)
        self.assertEqual(len({sample.speaker_key for sample in result.samples}), 1)
        self.assertIn(pair.session_id, bridge_selected[0].speaker_key)

    def pair(self, text='Hello'):
        self.counter += 1
        index = self.counter
        turns = self.fixture.turns([
            (f'input-{index}', 'person-a', 'human', SourceOrigin.REAL_SINGLE_SPEAKER,
             '2026-10-04T11:59:00+00:00', DataScope.PUBLIC_DATASET),
            (f'target-{index}', 'person-b', 'human', SourceOrigin.PUBLIC_DATASET,
             '2026-10-04T12:00:00+00:00', DataScope.PUBLIC_DATASET)],
            session=f'session-{index}')
        pair, = self.fixture.builder.build_pairs(turns, {'person-b'})
        return replace(pair, target_bubbles=(replace(pair.target_bubbles[0], text=text),))

    def relationship_event(self, *, spans=True):
        source = SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=7,
            speaker_id='12345', origin=SourceOrigin.REAL_RELATIONSHIP,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id='12345', event_time='2026-10-04T12:00:00+00:00',
            recorded_at='2026-10-04T12:00:00+00:00')
        evidence = (EvidenceSpan(message_id='in-7', start=0, end=len('dayshappy'),
                                 text='dayshappy'),) if spans else ()
        return RelationshipEvent(event_id='event-7', friend_id='12345',
            inbound_message_ids=('in-7',), context_message_ids=(), source_refs=(source,),
            explicit_spans=evidence, time_quality='KNOWN')

    def test_pair_labels_are_provisional_and_keep_review_fingerprint_source(self):
        pair = self.pair()
        classifier = MockClassifier(review=True)
        result, = LocalWeakLabeler(classifier).iter_label([pair])
        self.assertIs(result.sample, pair)
        self.assertEqual(result.labels, _LABELS)
        self.assertEqual((result.status, result.unknown, result.needs_review),
                         ('WEAK_PROVISIONAL', False, True))
        self.assertEqual(result.reason, 'tone_ambiguity')
        self.assertEqual(result.subject, 'BOT_PERSONA')
        self.assertIsNone(result.owner_friend_id)
        self.assertIn('PUBLIC_DATASET', result.source_origins)
        self.assertEqual(len(result.model_fingerprint), 64)
        self.assertNotIn('gender', result.labels)

    def test_private_relationship_without_target_uses_only_explicit_spans(self):
        event = self.relationship_event()
        classifier = MockClassifier(review=False)
        result, = LocalWeakLabeler(classifier).iter_label([event])
        self.assertIs(result.sample, event)
        self.assertEqual((result.subject, result.owner_friend_id), ('DYAD', '12345'))
        self.assertEqual(result.status, 'WEAK_PROVISIONAL')
        self.assertEqual(classifier.calls[0][0], ('dayshappy',))
        no_span = self.relationship_event(spans=False)
        unknown, = LocalWeakLabeler(classifier).iter_label([no_span])
        self.assertEqual((unknown.status, unknown.reason, unknown.labels),
                         ('UNKNOWN', 'no_explicit_text', None))
        self.assertEqual(len(classifier.calls), 1)

    def test_missing_or_failed_classifier_stays_unknown_without_cloud_fallback(self):
        pair = self.pair()
        missing, = LocalWeakLabeler().iter_label([pair])
        failed, = LocalWeakLabeler(MockClassifier(fail=True)).iter_label([pair])
        self.assertEqual((missing.status, missing.reason, missing.labels),
                         ('UNKNOWN', 'classifier_unavailable', None))
        self.assertEqual((failed.status, failed.reason, failed.labels),
                         ('UNKNOWN', 'local_classification_failed', None))

    def test_deterministic_bounded_sample_deduplicates_and_preserves_pairs(self):
        pairs = [self.pair(f'sample_{i}') for i in range(8)]
        source = [pairs[0], pairs[0], *pairs[1:]]
        first = list(LocalWeakLabeler(MockClassifier(), batch_size=2).iter_label(source, limit=3))
        second = list(LocalWeakLabeler(MockClassifier(), batch_size=2).iter_label(source, limit=3))
        first_ids = [item.sample.id for item in first]
        self.assertEqual(first_ids, [item.sample.id for item in second])
        self.assertEqual(len(first_ids), len(set(first_ids)))
        self.assertEqual(len(first), 3)
        self.assertTrue(all(item.sample in pairs and item.owner_friend_id is None for item in first))


if __name__ == '__main__':
    unittest.main()
