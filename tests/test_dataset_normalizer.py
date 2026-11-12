import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dataset_normalizer import DatasetNormalizer, privacy
from dataset_registry import DatasetRecord, DatasetRegistry
from persona_store import PersonaStore, ScopeMismatch
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope, FriendScope,
                            InvalidSource, PrivacyView, ProviderKind)


class DatasetNormalizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friend_dir = self.root / 'friends' / '12345'
        friend_dir.mkdir(parents=True)
        (friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(friend_dir)}}}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        self.registry = DatasetRegistry(self.scope)
        self.addCleanup(self.registry.close)
        self.registry.register(DatasetRecord(dataset_id='private', name='Private fixture',
            data_scope='PRIVATE_FRIEND', usage_scope='local fixture', local_use_allowed=True,
            model_analysis_allowed=True, license_data='private-license'), 0)
        self.registry.register(DatasetRecord(dataset_id='public', name='Public fixture',
            data_scope='PUBLIC_DATASET', usage_scope='local fixture', local_use_allowed=True,
            model_analysis_allowed=True, license_data='public-license'), self.registry.revision)
        self.normalizer = DatasetNormalizer(self.scope, self.registry)

    def add_import(self, dataset_id, import_id):
        exists = self.registry.db.execute('SELECT 1 FROM dataset_imports WHERE friend_id=? AND import_id=?',
                                          (self.scope.friend_id, import_id)).fetchone()
        if exists:
            return
        record = self.registry.get(dataset_id)
        now = '2026-10-04T12:00:00+00:00'
        self.registry.db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,source_origin,'
            'data_scope,source_hash,mapping_hash,settings_hash,checkpoint_json,status,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (self.scope.friend_id, import_id, dataset_id,
            None, record.data_scope, 'a' * 64, 'b' * 64, 'c' * 64, '{}', 'COMPLETED', now, now))

    def add_message(self, *, dataset_id='private', import_id='imp-1', record_id='r1', seq=0,
                    speaker='human', role='user', text='hello', kind='text', media=None,
                    origin='REAL_SINGLE_SPEAKER', event_time=None):
        self.add_import(dataset_id, import_id)
        fingerprint = hashlib.sha256(f'{import_id}:{record_id}:{seq}'.encode()).hexdigest()
        self.registry.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
            'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,message_type,media_id,'
            'content_fingerprint,source_origin,source_record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (self.scope.friend_id, import_id, dataset_id, record_id, seq, speaker, role, None,
             event_time, '2026-10-04T12:00:00+00:00', text, kind, media, fingerprint, origin, '{}'))
        return self.registry.db.execute('SELECT * FROM imported_messages WHERE friend_id=? AND import_id=? '
            'AND record_id=? AND message_seq=?',
            (self.scope.friend_id, import_id, record_id, seq)).fetchone()

    def test_normalize_uses_persisted_scope_locator_and_import_ordinal(self):
        row = self.add_message(event_time='2026-10-01')
        forged = dict(row)
        forged['data_scope'] = 'PUBLIC_DATASET'
        forged['privacy_view'] = 'CLOUD_SAFE'
        forged['speaker_role'] = 'bot'
        forged['source_origin'] = 'BOT_GENERATED'
        result = list(self.normalizer.normalize([forged]))[0]
        self.assertEqual(result.source.data_scope, DataScope.PRIVATE_FRIEND)
        self.assertEqual(result.source.origin.value, 'REAL_SINGLE_SPEAKER')
        self.assertEqual(result.source.friend_id, '12345')
        self.assertIsNone(result.source.message_seq)
        self.assertTrue(result.id.endswith(',0]'))
        self.assertEqual(result.timestamp, '2026-10-01')
        self.assertEqual(result.target_role, 'unmapped')
        self.assertFalse(result.target_selected)

    def test_registry_from_another_project_with_same_friend_id_is_rejected(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = Path(other.name).resolve()
        friend_dir = root / 'friends' / '12345'
        friend_dir.mkdir(parents=True)
        (friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(friend_dir)}}}), encoding='utf-8')
        scope = FriendScope.load('12345', project_root=root)
        store = PersonaStore('12345', scope.friend_dir, project_root=root)
        store.close()
        registry = DatasetRegistry(scope)
        self.addCleanup(registry.close)
        with self.assertRaises(ScopeMismatch):
            DatasetNormalizer(self.scope, registry)

    def test_binding_change_after_construction_is_rechecked_when_consumed(self):
        row = self.add_message()
        pending = self.normalizer.normalize([row])
        (self.root / 'friends' / '12345' / 'friend.json').write_text(
            json.dumps({'qq_id': '99999'}), encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            next(pending)

    def test_forged_friend_or_unpersisted_locator_is_rejected(self):
        row = dict(self.add_message())
        row['friend_id'] = '23456'
        with self.assertRaises(ScopeMismatch):
            list(self.normalizer.normalize([row]))
        row = dict(self.add_message(record_id='other'))
        row['import_id'] = 'forged'
        with self.assertRaises(InvalidSource):
            list(self.normalizer.normalize([row]))

    def test_unknown_origin_and_missing_speakers_do_not_create_targets_or_merge(self):
        one = self.add_message(record_id='unknown-a', speaker=None, origin=None)
        two = self.add_message(record_id='unknown-b', speaker=None, origin=None)
        first, second = list(self.normalizer.normalize([one, two]))
        self.assertIsNone(first.source.origin)
        self.assertNotEqual(first.speaker_id, second.speaker_id)
        self.assertEqual((first.speaker_role, first.target_role, first.target_selected),
                         ('unknown', 'unmapped', False))
        with self.assertRaises(InvalidSource):
            AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION, ProviderKind.LOCAL,
                                        [first.source])

    def test_bot_is_context_and_explicit_system_transfer_ad_and_revoke_are_filtered(self):
        bot = self.add_message(record_id='bot', role='assistant', text='context')
        rows = [self.add_message(record_id='sys', role='system'),
                self.add_message(record_id='pay', kind='transfer'),
                self.add_message(record_id='ad', kind='advertisement'),
                self.add_message(record_id='rev', kind='revoke'),
                self.add_message(record_id='ordinary', text='红包、转账、广告只是正文')]
        values = list(self.normalizer.normalize([bot, *rows]))
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0].target_role, 'context')
        self.assertEqual(values[0].target_selected, False)
        self.assertEqual(values[1].text, '红包、转账、广告只是正文')

    def test_only_same_locator_is_deduplicated_and_media_is_preserved(self):
        first = self.add_message(record_id='same-a', text='yeah')
        second = self.add_message(record_id='same-b', text='yeah')
        media = self.add_message(record_id='sticker', text='', kind='sticker', media='asset-7')
        values = list(self.normalizer.normalize([first, first, second, media]))
        self.assertEqual([item.text for item in values], ['yeah', 'yeah', ''])
        self.assertEqual((values[-1].message_type, values[-1].media_id), ('sticker', 'asset-7'))

    def test_normalized_ids_do_not_collide_when_locator_fields_contain_delimiters(self):
        for dataset_id in ('left:right', 'left'):
            self.registry.register(DatasetRecord(dataset_id=dataset_id, name=dataset_id,
                data_scope='PRIVATE_FRIEND', usage_scope='local fixture', local_use_allowed=True),
                self.registry.revision)
        first = self.add_message(dataset_id='left:right', import_id='mid', record_id='r', speaker=None)
        second = self.add_message(dataset_id='left', import_id='right:mid', record_id='r', speaker=None)
        values = list(self.normalizer.normalize([first, second]))
        self.assertNotEqual(values[0].id, values[1].id)
        self.assertNotEqual(values[0].speaker_id, values[1].speaker_id)
        self.assertIn('["left:right","mid","r",0]', values[0].id)

    def test_garbled_caption_does_not_drop_media_reference(self):
        row = self.add_message(record_id='broken-caption', text='caption\ufffd',
                               kind='image', media='image-9')
        value = list(self.normalizer.normalize([row]))[0]
        self.assertEqual((value.text, value.message_type, value.media_id),
                         ('caption\ufffd', 'image', 'image-9'))

    def test_public_license_permission_comes_from_registry_and_can_be_cloud_checked(self):
        row = self.add_message(dataset_id='public', import_id='imp-public', record_id='public-1',
                               origin='PUBLIC_DATASET')
        message = list(self.normalizer.normalize([row]))[0]
        self.assertEqual(message.source.data_scope, DataScope.PUBLIC_DATASET)
        self.assertIsNone(message.source.friend_id)
        self.assertEqual(message.source.license_id, 'public-license')
        self.assertTrue(message.source.model_analysis_allowed)
        safe = list(privacy([message], PrivacyView.CLOUD_SAFE).messages)[0]
        self.assertEqual(AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION,
                         ProviderKind.CLOUD, [safe.source]), DataScope.PUBLIC_DATASET)

    def test_cloud_safe_private_remains_private_scope(self):
        row = self.add_message()
        message = list(self.normalizer.normalize([row]))[0]
        safe = list(privacy([message], PrivacyView.CLOUD_SAFE).messages)[0]
        self.assertEqual(safe.source.data_scope, DataScope.PRIVATE_FRIEND)
        self.assertEqual(safe.source.privacy_view, PrivacyView.CLOUD_SAFE)
        self.assertEqual(AnalysisCallPolicy.validate(AnalysisPurpose.DISTILLATION,
                         ProviderKind.CLOUD, [safe.source]), DataScope.PRIVATE_FRIEND)

    def test_cloud_safe_redacts_marked_pii_and_sensitive_url_parameters_without_mutating_input(self):
        row = self.add_message(record_id='pii', text=(
            '13800138000 a.person@example.com 11010519491231002X '
            'https://example.test/?token=secret 地址：北京市海淀区中关村1号，'
            '账号：wxid_private [REDACTED]'))
        message = list(self.normalizer.normalize([row]))[0]
        original = message.text
        result = privacy([message], PrivacyView.CLOUD_SAFE)
        safe = list(result.messages)[0]
        self.assertEqual(message.text, original)
        self.assertEqual(safe.source.privacy_view, PrivacyView.CLOUD_SAFE)
        for secret in ('13800138000',
        'a.person@example.com',
        '11010519491231002X',
        'token=secret',
        'medium1',
        'wxid_private',
        '北京市海淀区中关村1号'):
            self.assertNotIn(secret, safe.text)
        self.assertIn('[REDACTED]', safe.text)
        self.assertEqual(result.redaction_counts,
            {'url_secret': 1, 'address': 1, 'account': 1, 'id_number': 1, 'phone': 1, 'email': 1})

    def test_formatted_pii_and_encoded_secret_keys_are_redacted_idempotently(self):
        row = self.add_message(record_id='formatted', text=(
            '+86 138-0013-8000 110105 19491231 002X '
            'https://example.test/?access%5Ftoken=private&topic=music&password=%5BREDACTED%5D'))
        message = list(self.normalizer.normalize([row]))[0]
        result = privacy([message], PrivacyView.CLOUD_SAFE)
        safe = list(result.messages)[0]
        for secret in ('138-0013-8000', '110105 19491231 002X', 'token=private'):
            self.assertNotIn(secret, safe.text)
        self.assertIn('topic=music', safe.text)
        self.assertIn('password=%5BREDACTED%5D', safe.text)
        self.assertEqual((result.redaction_counts['phone'], result.redaction_counts['id_number'],
                          result.redaction_counts['url_secret']), (1, 1, 1))
        second = privacy([safe], PrivacyView.CLOUD_SAFE)
        self.assertEqual(list(second.messages)[0].text, safe.text)
        self.assertEqual(sum(second.redaction_counts.values()), 0)

    def test_local_full_is_an_unchanged_copy_and_placeholders_are_stable(self):
        row = self.add_message(record_id='local', text='Call [REDACTED] or token=[REDACTED]')
        message = list(self.normalizer.normalize([row]))[0]
        result = privacy([message], PrivacyView.LOCAL_FULL)
        clone = list(result.messages)[0]
        self.assertIsNot(clone, message)
        self.assertIsNot(clone.source, message.source)
        self.assertEqual(clone.text, message.text)
        self.assertEqual(clone.source.privacy_view, PrivacyView.LOCAL_FULL)
        self.assertEqual(sum(result.redaction_counts.values()), 0)

    def test_cloud_safe_copy_cannot_be_relabelled_as_local_full(self):
        row = self.add_message(record_id='one-way', text='Call 13800138000')
        message = list(self.normalizer.normalize([row]))[0]
        safe = list(privacy([message], PrivacyView.CLOUD_SAFE).messages)[0]
        with self.assertRaises(InvalidSource):
            list(privacy([safe], PrivacyView.LOCAL_FULL).messages)

    def test_normalizer_does_not_write_registry_or_staging_database(self):
        row = self.add_message()
        revision = self.registry.revision
        list(self.normalizer.normalize([row]))
        self.assertEqual(self.registry.revision, revision)


if __name__ == '__main__':
    unittest.main()
