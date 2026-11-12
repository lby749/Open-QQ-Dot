import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import tests.test_dialogue_turns as dialogue_fixtures
import style_statistics
from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import SessionSegmenter, build_turns
from persona_store import ScopeMismatch, Unavailable
from persona_types import (DataScope, InvalidSource, PrivacyView, SourceOrigin)
from style_statistics import StyleStatistics


class StyleStatisticsTests(unittest.TestCase):
    def setUp(self):
        dialogue_fixtures.DialogueStoreTests.setUp(self)
        self.pair_store = PairStore(self.scope)
        self.addCleanup(self.pair_store.close)
        rows = (
            (0, 'input-a', 'alice', 'text', None, '2026-10-04T10:00:00+00:00'),
            (1, '好呀！', 'target', 'text', None, '2026-10-04T10:00:01+00:00'),
            (2, '嗯?🙂', 'target', 'text', None, '2026-10-04T12:00:00+00:00'),
            (3, 'input-b', 'alice', 'text', None, '2026-10-04T12:00:01+00:00'),
            (4, '好呀！', 'target', 'text', None, '2026-10-04T12:00:02+00:00'),
        )
        for seq, text, speaker, kind, media_id, timestamp in rows:
            self.dialogues.db.execute('UPDATE imported_messages SET text=?,speaker_id=?,speaker_role=?, '
                'message_type=?,media_id=?,event_time=?,source_origin=? WHERE friend_id=? AND import_id=? '
                'AND message_seq=?', (text, speaker, 'user', kind, media_id, timestamp,
                'REAL_SINGLE_SPEAKER', '12345', 'imp', seq))
        for seq, text, speaker, kind, media_id in (
                (5, 'input-c', 'alice', 'text', None),
                (6, '', 'target', 'sticker', 'asset-6'),
                (7, '', 'target', 'face', 'asset-7'),
                (8, '', 'target', 'sticker', 'asset-8'),
                (9, 'input-d', 'alice', 'text', None),
                (10, '', 'target', 'image', 'asset-10')):
            timestamp = None if seq >= 5 else '2026-10-04T12:00:03+00:00'
            self.dialogues.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
                'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,message_type,media_id,'
                'content_fingerprint,source_origin,source_record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('12345', 'imp', 'private', f'r{seq}', seq, speaker, 'user', 'in', timestamp,
                 '2026-10-04T12:00:00+00:00', text, kind, media_id,
                 hashlib.sha256(f'style-fixture-{seq}'.encode()).hexdigest(),
                 'REAL_SINGLE_SPEAKER', '{}'))
        messages = list(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in range(11)]))
        chats = {message.id: ('chat-a' if i < 3 else 'chat-b' if i < 5 else
                              'chat-c' if i < 9 else 'chat-d')
                 for i, message in enumerate(messages)}
        sessions = list(SessionSegmenter(self.scope).segment(messages, gap_seconds=7200,
                                                              conversation_ids=chats))
        self.assertEqual(len(sessions), 4)
        pairs = []
        for session in sessions:
            self.dialogues.save_session(session, self.dialogues.revision)
            rebuilt = tuple(DialoguePairBuilder(self.scope).build_pairs(
                build_turns(session), {'target'}))
            self.pair_store.save_pairs(rebuilt, self.pair_store.revision)
            pairs.extend(self.pair_store.load_pairs(session.id))
        self.pairs = tuple(pairs)

    def test_hand_calculated_codepoint_quantiles_bubbles_rates_and_provenance(self):
        result = StyleStatistics.calculate(self.pairs)
        self.assertEqual(result['message_length']['reply'], {
            'sample_count': 2, 'denominator': 'text_replies', 'mean': 4.5,
            'median': 4.5, 'p25': 3.75, 'p75': 5.25, 'p90': 5.7})
        self.assertEqual(result['message_length']['single_bubble'], {
            'sample_count': 3, 'denominator': 'nonempty_target_bubbles', 'mean': 3,
            'median': 3, 'p25': 3, 'p75': 3, 'p90': 3})
        self.assertEqual(result['sample_info']['target_bubble_count'], 7)
        self.assertEqual(result['sample_info']['target_text_codepoints'], 9)
        self.assertEqual(result['sample_info']['time_quality'], {'KNOWN': 2, 'UNKNOWN': 2})
        self.assertEqual(result['bubble_behavior']['counts'],
                         {'single': 2, 'two': 1, 'three_plus': 1})
        self.assertEqual(result['bubble_behavior']['rates'],
                         {'single': 0.5, 'two': 0.25, 'three_plus': 0.25})
        self.assertEqual(result['bubble_behavior']['short_reply_rate']['rate'], 1)
        self.assertEqual(result['question_behavior']['question_rate']['rate'], 0.5)
        self.assertEqual(result['question_behavior']['question_mark_count'], 1)
        self.assertEqual(result['punctuation']['literal_counts']['！'], 2)
        self.assertEqual(result['punctuation']['literal_counts']['?'], 1)
        self.assertEqual(result['emoji_behavior']['matched_codepoint_count'], 1)
        self.assertTrue(result['emoji_behavior']['estimated'])
        self.assertEqual(result['sticker_behavior']['pair_counts'], {
            'text_only': 2, 'text_plus_sticker': 0, 'sticker_only': 1,
            'other_media_or_unknown': 1})
        self.assertEqual(result['sticker_behavior']['unknown_media_bubble_count'], 1)
        self.assertEqual(result['common_expressions']['items'][0], {
            'text': '好呀！', 'count': 2, 'session_count': 2,
            'source_import_ids': ['imp'], 'denominator': 'nonempty_target_bubbles'})
        self.assertEqual(result['sample_info']['data_scope'], 'PRIVATE_FRIEND')
        self.assertEqual(result['sample_info']['privacy_views'], {'LOCAL_FULL': 11})
        self.assertEqual(result['interaction_patterns']['reply_delay_seconds']['rate'], None)
        self.assertEqual(result['interaction_patterns']['reply_delay_seconds']['status'],
                         'not_analyzed')
        self.assertEqual(result['sample_info']['source_import_ids'], ['imp'])
        json.dumps(result, ensure_ascii=False)

    def test_repeated_punctuation_counts_replies_and_private_literals_are_preserved(self):
        pair = self.pairs[0]
        bubbles = tuple(replace(bubble, text='打电话！！！...') for bubble in pair.target_bubbles)
        result = StyleStatistics.calculate([replace(pair, target_bubbles=bubbles)])
        self.assertEqual(result['punctuation']['exclamation_rate']['rate'], 1)
        self.assertEqual(result['punctuation']['period_rate']['rate'], 1)
        self.assertEqual(result['punctuation']['literal_counts']['！'], 6)
        self.assertEqual(result['common_expressions']['items'][0]['text'], '打电话！！！...')
        self.assertEqual(result['sample_info']['data_scope'], 'PRIVATE_FRIEND')

    def test_empty_samples_are_none_and_all_estimates_are_marked(self):
        result = StyleStatistics.calculate(())
        self.assertIsNone(result['message_length']['reply']['mean'])
        self.assertIsNone(result['message_length']['reply']['p90'])
        self.assertIsNone(result['bubble_behavior']['short_reply_rate']['rate'])
        self.assertIsNone(result['question_behavior']['question_rate']['rate'])
        self.assertIsNone(result['emoji_behavior']['reply_rate']['rate'])
        self.assertIsNone(result['fragment_usage']['short_no_terminator_rate']['rate'])
        self.assertEqual(result['sample_info']['pair_count'], 0)
        self.assertEqual(result['sample_info']['sample_status'], 'insufficient')
        self.assertTrue(result['common_words']['estimated'])
        self.assertTrue(result['fragment_usage']['short_no_terminator_rate']['estimated'])
        self.assertEqual(result['anti_patterns_status'], 'not_analyzed')
        self.assertTrue(result['generated_at'].endswith('Z'))
        json.dumps(result)

    def test_duplicate_ids_deduplicate_conflicts_fail_and_bounds_hold(self):
        pair = self.pairs[0]
        result = StyleStatistics.calculate([pair, pair])
        self.assertEqual(result['sample_info']['pair_count'], 1)
        with self.assertRaises(InvalidSource):
            StyleStatistics.calculate([pair, replace(pair, session_id='different')])
        with patch.object(style_statistics, 'MAX_SESSION_MESSAGES', 2):
            with self.assertRaises(Unavailable):
                StyleStatistics.calculate([pair, pair, pair])
            three_bubble_pair = next(item for item in self.pairs if len(item.target_bubbles) == 3)
            with self.assertRaises(Unavailable):
                StyleStatistics.calculate([three_bubble_pair])

    def test_nonpair_nonhuman_unknown_and_bot_targets_are_rejected(self):
        with self.assertRaises(InvalidSource):
            StyleStatistics.calculate([self.pairs[0].target_bubbles[0]])
        pair = self.pairs[0]
        target = pair.target_bubbles[0]
        with self.assertRaises(InvalidSource):
            StyleStatistics.calculate([replace(pair, target_speaker_id='unknown:target')])
        with self.assertRaises(InvalidSource):
            StyleStatistics.calculate([replace(pair, target_bubbles=(
                replace(target, speaker_role='unknown'), *pair.target_bubbles[1:]))])
        bot_source = replace(target.source, origin=SourceOrigin.BOT_GENERATED)
        bot_target = replace(target, source=bot_source)
        with self.assertRaises(InvalidSource):
            StyleStatistics.calculate([replace(pair, target_bubbles=(bot_target,),
                source_refs=tuple(bot_source if ref == target.source else ref
                                  for ref in pair.source_refs))])

    def test_cloudsafe_private_target_remains_private_and_public_none_scope_is_valid(self):
        from dataset_normalizer import privacy
        pair = self.pairs[0]
        safe_refs = {ref: replace(ref, privacy_view=PrivacyView.CLOUD_SAFE)
                     for ref in pair.source_refs}
        safe_bubbles = tuple(next(iter(privacy([bubble], PrivacyView.CLOUD_SAFE).messages))
                             for bubble in pair.target_bubbles)
        safe = replace(pair, target_bubbles=safe_bubbles,
                       source_refs=tuple(safe_refs[ref] for ref in pair.source_refs))
        result = StyleStatistics.calculate([safe])
        self.assertEqual(result['sample_info']['data_scope'], DataScope.PRIVATE_FRIEND.value)
        self.assertEqual(result['sample_info']['privacy_views'], {'CLOUD_SAFE': 3})

        public_refs = {ref: replace(ref, data_scope=DataScope.PUBLIC_DATASET,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=None, license_id='public-license',
            model_analysis_allowed=True) for ref in pair.source_refs}
        public_target = tuple(replace(bubble, source=public_refs[bubble.source])
                              for bubble in pair.target_bubbles)
        public = replace(pair, target_bubbles=public_target,
                         source_refs=tuple(public_refs[ref] for ref in pair.source_refs))
        public_result = StyleStatistics.calculate([public])
        self.assertIsNone(public_result['sample_info']['friend_id'])
        self.assertEqual(public_result['sample_info']['data_scope'], DataScope.PUBLIC_DATASET.value)

    def test_mixed_friend_pair_owners_are_rejected(self):
        pair = self.pairs[0]
        other_sources = {ref: replace(ref, friend_id='23456') for ref in pair.source_refs}
        other_bubbles = tuple(replace(bubble, source=other_sources[bubble.source])
                              for bubble in pair.target_bubbles)
        other = replace(pair, id='other-owner-pair', friend_id='23456', target_bubbles=other_bubbles,
                        source_refs=tuple(other_sources[ref] for ref in pair.source_refs))
        with self.assertRaises(ScopeMismatch):
            StyleStatistics.calculate([pair, other])


if __name__ == '__main__':
    unittest.main()
