import unittest
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from dialogue_pairs import DialoguePairBuilder, PairStore
from dataset_normalizer import privacy
from dataset_registry import DatasetRecord, DatasetRegistry
from dialogue_turns import SessionSegmenter, build_turns
from persona_analysis_input import AnalysisInputBuilder
from persona_store import ScopeMismatch, Unavailable
from persona_types import (AnalysisPurpose, DataScope, InvalidSource, PrivacyViolation,
                           PrivacyView, SourceOrigin, ProviderKind)
from tests.test_dialogue_turns import DialogueStoreTests


class AnalysisInputBuilderTests(unittest.TestCase):
    def setUp(self):
        DialogueStoreTests.setUp(self)
        self.addCleanup(self.dialogues.close)
        self.pairs = PairStore(self.scope)
        self.addCleanup(self.pairs.close)
        for seq, speaker, role, origin in (
                (0, '12345', 'friend', 'REAL_RELATIONSHIP'),
                (1, 'helper', 'assistant', 'BOT_GENERATED'),
                (2, '12345', 'friend', 'REAL_RELATIONSHIP'),
                (3, 'alias', 'human', 'REAL_RELATIONSHIP'),
                (4, '12345', 'friend', 'REAL_RELATIONSHIP')):
            self.dialogues.db.execute('UPDATE imported_messages SET speaker_id=?,speaker_role=?,source_origin=? '
                'WHERE friend_id=? AND import_id=? AND message_seq=?',
                (speaker, role, origin, '12345', 'imp', seq))
        self.messages = list(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in range(5)]))
        self.session = next(SessionSegmenter(self.scope).segment(self.messages, gap_seconds=7200,
            conversation_ids={message.id: 'fixture-chat' for message in self.messages}))
        self.dialogues.save_session(self.session, self.dialogues.revision)
        assert [(message.speaker_id, message.source.origin) for message in self.session.messages] == [
            ('12345', SourceOrigin.REAL_RELATIONSHIP), ('helper', SourceOrigin.BOT_GENERATED),
            ('12345', SourceOrigin.REAL_RELATIONSHIP), ('alias', SourceOrigin.REAL_RELATIONSHIP),
            ('12345', SourceOrigin.REAL_RELATIONSHIP)], [(message.speaker_id,
                message.speaker_role, message.source.origin) for message in self.session.messages]
        candidates = tuple(DialoguePairBuilder(self.scope).build_pairs(
            build_turns(self.session), {'alias'}))
        self.pairs.save_pairs(candidates, self.pairs.revision)
        self.builder = AnalysisInputBuilder(self.scope, self.pairs)

    def test_no_target_builds_private_relationship_events_and_bot_only_context(self):
        result = self.builder.build([self.session.id])
        self.assertEqual(result.role_pairs, ())
        self.assertEqual(len(result.relationship_events), 3)
        self.assertEqual(result.sample_info.selected_friend_speaker_ids, ('12345',))
        middle = result.relationship_events[1]
        self.assertEqual(middle.inbound_message_ids, (self.messages[2].id,))
        self.assertEqual(middle.context_message_ids, (self.messages[0].id, self.messages[1].id))
        self.assertEqual(middle.explicit_spans[0].text, self.messages[2].text)
        self.assertNotIn(self.messages[1].id, middle.inbound_message_ids)
        self.assertEqual(result.validate_analysis_call(AnalysisPurpose.DISTILLATION,
                         ProviderKind.LOCAL), DataScope.PRIVATE_FRIEND)
        self.assertEqual(result.validate_analysis_call(AnalysisPurpose.DISTILLATION,
                         ProviderKind.CLOUD), DataScope.PRIVATE_FRIEND)

    def test_explicit_target_uses_verified_persisted_pair_and_private_not_cloudsafe(self):
        result = self.builder.build([self.session.id, self.session.id],
                                    target_speaker_ids=['alias'])
        self.assertEqual(len(result.role_pairs), 1)
        self.assertEqual(result.target_mapping[0].speaker_id, 'alias')
        self.assertEqual(result.sample_info.session_count, 1)
        self.assertEqual(result.sample_info.message_count, 5)
        self.assertEqual(result.validate_analysis_call(AnalysisPurpose.JUDGE, ProviderKind.CLOUD),
                         DataScope.PRIVATE_FRIEND)
        empty =self.builder.build([self.session.id], target_speaker_ids=(),
                                   friend_speaker_ids=['alias'])
        self.assertFalse(empty.role_pairs)
        self.assertEqual(len(empty.relationship_events), 1)
        self.assertEqual(empty.sample_info.selected_friend_speaker_ids, ('alias',))
        with self.assertRaises(PrivacyViolation):
            result.validate_analysis_call('legacy', ProviderKind.LOCAL)

    def test_pairable_selected_target_without_persisted_pair_is_unavailable(self):
        self.pairs.db.execute('DELETE FROM dialogue_pairs WHERE friend_id=?', ('12345',))
        with self.assertRaises(Unavailable):
            self.builder.build([self.session.id], target_speaker_ids=['alias'])

    def test_batches_are_bounded_before_loading_and_scope_is_required(self):
        class Endless:
            def __iter__(self):
                while True:
                    yield 'unused'
        with self.assertRaises(Unavailable):
            self.builder.build(Endless())
        with self.assertRaises(InvalidSource):
            self.builder.build('single-string-session')
        with self.assertRaises(Unavailable):
            self.builder.build([]).validate_analysis_call(
                AnalysisPurpose.DISTILLATION, ProviderKind.LOCAL)
        with self.assertRaises(ScopeMismatch):
            AnalysisInputBuilder(self.scope, SimpleNamespace(scope=self.scope))
        with patch.object(self.pairs.dialogues, 'load_session', return_value=SimpleNamespace(
                id='too-large', friend_id='12345', messages=(None,) * 10001)):
            with self.assertRaises(Unavailable):
                self.builder.build(['too-large'])

    def test_cross_friend_store_is_rejected(self):
        other_dir = self.root / 'friends' / '23456'
        other_dir.mkdir(parents=True)
        (other_dir / 'friend.json').write_text(json.dumps({'qq_id': '23456'}), encoding='utf-8')
        registry_path = self.root / 'friends.json'
        registry = json.loads(registry_path.read_text(encoding='utf-8'))
        registry['friends']['23456'] = {'qq_id': '23456', 'directory': str(other_dir)}
        registry_path.write_text(json.dumps(registry), encoding='utf-8')
        other_scope = type(self.scope).load('23456', project_root=self.root)
        with self.assertRaises(ScopeMismatch):
            AnalysisInputBuilder(other_scope, self.pairs)

    def test_unknown_role_origin_and_media_only_are_not_relationship_facts(self):
        self.dialogues.db.execute('UPDATE imported_messages SET speaker_role=?,source_origin=?,text=? '
            'WHERE friend_id=? AND import_id=? AND message_seq=?',
            ('unknown', None, '', '12345', 'imp', 4))
        messages = list(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in range(5)]))
        session = next(SessionSegmenter(self.scope).segment(messages, gap_seconds=7200,
            conversation_ids={message.id: 'unknown-source-chat' for message in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        result = self.builder.build([session.id])
        all_inbound = {message_id for event in result.relationship_events
                       for message_id in event.inbound_message_ids}
        self.assertNotIn(messages[4].id, all_inbound)
        self.assertEqual(messages[4].text, '')
        self.assertEqual(messages[4].media_id, 'asset-4')

    def test_media_only_turn_retains_inbound_id_without_literal_span(self):
        self.dialogues.db.execute('UPDATE imported_messages SET speaker_role=?,source_origin=?,text=? '
            'WHERE friend_id=? AND import_id=? AND message_seq=?',
            ('friend', 'REAL_RELATIONSHIP', '', '12345', 'imp', 4))
        messages = list(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in range(5)]))
        session = next(SessionSegmenter(self.scope).segment(messages, gap_seconds=7200,
            conversation_ids={message.id: 'media-only-chat' for message in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        result = self.builder.build([session.id])
        event = next(event for event in result.relationship_events
                     if messages[4].id in event.inbound_message_ids)
        self.assertEqual(event.explicit_spans, ())

    def test_source_text_tamper_and_registry_revocation_are_rejected(self):
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? AND import_id=? '
            'AND message_seq=?', ('tampered', '12345', 'imp', 0))
        with self.assertRaises(InvalidSource):
            self.builder.build([self.session.id])

        registry = DatasetRegistry(self.scope)
        self.addCleanup(registry.close)
        registry.deactivate('private', registry.revision)
        with self.assertRaises(ScopeMismatch):
            self.builder.build([self.session.id])

    def test_public_target_permission_tristate_and_mixed_private_taint(self):
        now = '2026-10-04T12:00:00+00:00'
        registry = DatasetRegistry(self.scope)
        self.addCleanup(registry.close)
        self.dialogues.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
            'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,message_type,media_id,'
            'content_fingerprint,source_origin,source_record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('12345', 'pubimp', 'public', 'pubrec2', 1, 'public-target', 'user', 'in', None, now,
             'public target', 'text', None, hashlib.sha256(b'public-target').hexdigest(),
             'PUBLIC_DATASET', '{}'))
        for permission in (True, False, None):
            dataset = registry.get('public')
            registry.update(
                DatasetRecord(**{**dataset.__dict__, 'model_analysis_allowed': permission}),
                registry.revision)
            messages = list(self.dialogues.normalizer.normalize([
                {'friend_id': '12345', 'dataset_id': 'public', 'import_id': 'pubimp',
                 'record_id': record, 'message_seq': seq}
                for record, seq in (('pubrec', 0), ('pubrec2', 1))]))
            session = next(SessionSegmenter(self.scope).segment(messages,
                conversation_ids={message.id: f'public-chat-{permission}' for message in messages}))
            self.dialogues.save_session(session, self.dialogues.revision)
            pairs = tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session),
                                                                      {'public-target'}))
            self.pairs.save_pairs(pairs, self.pairs.revision)
            result = self.builder.build([session.id], target_speaker_ids=['public-target'])
            self.assertEqual(len(result.role_pairs), 1)
            if permission is True:
                self.assertEqual(result.validate_analysis_call(AnalysisPurpose.JUDGE,
                                 ProviderKind.CLOUD), DataScope.PUBLIC_DATASET)
                mixed = self.builder.build([self.session.id, session.id],
                    target_speaker_ids=['alias', 'public-target'])
                self.assertEqual(mixed.sample_info.data_scopes,
                                 ('PRIVATE_FRIEND', 'PUBLIC_DATASET'))
                self.assertEqual(mixed.validate_analysis_call(AnalysisPurpose.JUDGE, ProviderKind.CLOUD),
                                 DataScope.PRIVATE_FRIEND)
            else:
                with self.assertRaises(PrivacyViolation):
                    result.validate_analysis_call(AnalysisPurpose.JUDGE, ProviderKind.CLOUD)

    def test_cloudsafe_private_view_stays_private(self):
        safe_messages = tuple(privacy(self.messages, PrivacyView.CLOUD_SAFE).messages)
        safe_session = next(SessionSegmenter(self.scope).segment(safe_messages, gap_seconds=7200,
            conversation_ids={message.id: 'safe-chat' for message in safe_messages}))
        self.dialogues.save_session(safe_session, self.dialogues.revision)
        result = self.builder.build([safe_session.id])
        self.assertTrue(all(ref.privacy_view == PrivacyView.CLOUD_SAFE for ref in result.source_refs))
        self.assertEqual(result.validate_analysis_call(AnalysisPurpose.DISTILLATION, ProviderKind.CLOUD),
                         DataScope.PRIVATE_FRIEND)


if __name__ == '__main__':
    unittest.main()
