import json
import pickle
import tempfile
import unittest
from pathlib import Path

from dataset_adapters import PublicDatasetAdapter
from dataset_importer import MAX_TEXT_CHARS, DatasetImporter
from dataset_normalizer import DatasetNormalizer
from dataset_registry import DatasetRecord, DatasetRegistry
from dialogue_pairs import DialoguePairBuilder
from dialogue_turns import MAX_SESSION_MESSAGES, SessionSegmenter, build_turns
from persona_store import PersonaStore, ScopeMismatch, Unavailable
from persona_types import FriendScope, InvalidSource, PrivacyViolation


class PublicDatasetAdapterTests(unittest.TestCase):
    def setUp(self):
        from tests.test_dataset_importer import DatasetImporterTests
        DatasetImporterTests.setUp(self)
        self.adapter = PublicDatasetAdapter(self.scope, self.registry)

    def register(self, dataset_id='public', **values):
        defaults = dict(name='Public fixture', data_scope='PUBLIC_DATASET',
            usage_scope='research fixture', version='fixture-v1',
            license_data='Synthetic test-only permission evidence',
            local_use_allowed=True, model_analysis_allowed=None,
            derived_stats_allowed=None, raw_corpus_redistribution_allowed=None)
        defaults.update(values)
        self.registry.register(DatasetRecord(dataset_id=dataset_id, **defaults),
                               self.registry.revision)

    def adapt(self, record, adapter='personal_dialog', record_id='row-1', dataset='public'):
        return self.adapter.adapt_record(dataset, adapter, record, record_id)

    def test_personal_dialog_maps_only_explicit_uids_and_preserves_raw_profile(self):
        self.register()
        record = {'dialog': ['问候', 'respond', '补充'], 'uid': [7, 9, 7],
            'profile': [{'gender': 'unknown'}, {'gender': 'unknown'}],
            'responder_profile': None, 'golden_response': '未映射参考响应',
            'is_biased': False}
        original = json.loads(json.dumps(record, ensure_ascii=False))
        adapted = self.adapt(record)
        self.assertEqual(record, original)
        self.assertEqual(len(adapted.rows), 3)
        self.assertEqual(adapted.rows[0]['speaker_id'], adapted.rows[2]['speaker_id'])
        self.assertNotEqual(adapted.rows[0]['speaker_id'], adapted.rows[1]['speaker_id'])
        self.assertTrue(all(row['speaker_role'] == 'human' for row in adapted.rows))
        self.assertTrue(all(row['event_time'] is None for row in adapted.rows))
        self.assertTrue(all(row['source_origin'] == 'PUBLIC_DATASET' for row in adapted.rows))
        self.assertTrue(all(row['record_id'] == adapted.conversation_id for row in adapted.rows))
        self.assertTrue(all(row['source_record_id'] == 'row-1' and
                            row['source_version'] == 'fixture-v1' for row in adapted.rows))
        self.assertEqual(adapted.rows[0]['profile'], record['profile'])
        self.assertEqual(adapted.rows[0]['golden_response'], '未映射参考响应')
        self.assertIs(adapted.rows[0]['is_biased'], False)
        self.assertEqual(adapted.rows[0]['uid'], [7, 9, 7])
        self.assertEqual(adapted.permissions.model_analysis_allowed, None)
        self.assertIsNone(adapted.permissions.derived_stats_allowed)
        self.assertIsNone(adapted.permissions.raw_corpus_redistribution_allowed)
        self.assertNotEqual(adapted.conversation_id,
            self.adapt(record, record_id='row-2').conversation_id)
        self.assertEqual(adapted.rows, self.adapt(record).rows)
        old_record = self.registry.get('public')
        self.registry.update(DatasetRecord(**{**old_record.__dict__, 'version': 'fixture-v2'}),
                             self.registry.revision)
        updated = self.adapt(record)
        self.assertNotEqual(adapted.rows[0]['record_id'], updated.rows[0]['record_id'])
        self.assertEqual(updated.rows[0]['source_version'], 'fixture-v2')
        adapted.rows[0]['profile'][0]['gender'] = 'changed'
        self.assertEqual(record['profile'][0]['gender'], 'unknown')

    def test_missing_or_empty_uid_stays_unknown_and_does_not_guess_turns(self):
        self.register()
        missing = self.adapt({'dialog': ['甲', '乙']})
        empty = self.adapt({'dialog': ['甲', '乙'], 'uid': ['', None]})
        unknown_tokens = self.adapt({'dialog': ['甲', '乙'],
                                     'uid': [' unknown ', 'UNKNOWN:source']})
        for adapted in (missing, empty, unknown_tokens):
            self.assertEqual([row['speaker_id'] for row in adapted.rows],
                             ['unknown', 'unknown'])
            self.assertEqual([row['speaker_role'] for row in adapted.rows],
                             ['unknown', 'unknown'])

    def test_lccc_base_keeps_unknown_speakers_and_large_is_unavailable(self):
        self.register()
        adapted = self.adapt({'dialog': ['第一句', '第二句']}, adapter='lccc_base')
        self.assertEqual([row['speaker_role'] for row in adapted.rows], ['unknown', 'unknown'])
        self.assertTrue(all(row['event_time'] is None for row in adapted.rows))
        with self.assertRaises(Unavailable):
            self.adapt({'dialog': ['first']}, adapter='lccc_large')
        for future_adapter in ('cped', 'livechat', 'usticker', 'stickerconv', 'lccc_large'):
            with self.subTest(adapter=future_adapter):
                with self.assertRaises(Unavailable):
                    self.adapt({'dialog': ['first']}, adapter=future_adapter)

    def test_permission_tristate_and_missing_license_evidence(self):
        self.register(dataset_id='unknown-local', local_use_allowed=None)
        with self.assertRaises(Unavailable):
            self.adapter.adapt_record('unknown-local', 'personal_dialog',
                                      {'dialog': ['hello'], 'uid': [1]}, 'r')
        self.register(dataset_id='denied-local', local_use_allowed=False)
        with self.assertRaises(PrivacyViolation):
            self.adapter.adapt_record('denied-local', 'personal_dialog',
                                      {'dialog': ['hello'], 'uid': [1]}, 'r')
        self.register(dataset_id='unknown-license', license_code='MIT', license_data=None)
        with self.assertRaises(Unavailable):
            self.adapter.adapt_record('unknown-license', 'personal_dialog',
                                      {'dialog': ['hello'], 'uid': [1]}, 'r')
        self.register(dataset_id='unknown-version', version=None)
        with self.assertRaises(Unavailable):
            self.adapter.adapt_record('unknown-version', 'personal_dialog',
                                      {'dialog': ['hello'], 'uid': [1]}, 'r')

    def test_scope_binding_dataset_state_and_public_scope_are_rechecked(self):
        self.register()
        private = DatasetRecord(dataset_id='private-public-adapter', name='Private',
            data_scope='PRIVATE_FRIEND', usage_scope='local', version='v1',
            license_data='terms', local_use_allowed=True)
        self.registry.register(private, self.registry.revision)
        with self.assertRaises(ScopeMismatch):
            self.adapter.adapt_record(private.dataset_id, 'personal_dialog',
                                      {'dialog': ['x']}, 'r')
        self.registry.deactivate('public', self.registry.revision)
        with self.assertRaises(ScopeMismatch):
            self.adapt({'dialog': ['x']})

    def test_adapter_rejects_registry_from_another_friend(self):
        other_dir = self.root / 'friends' / '23456'
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / 'friend.json').write_text(json.dumps({'qq_id': '23456'}), encoding='utf-8')
        friends = json.loads((self.root / 'friends.json').read_text(encoding='utf-8'))
        friends['friends']['23456'] = {'qq_id': '23456', 'directory': str(other_dir)}
        (self.root / 'friends.json').write_text(json.dumps(friends), encoding='utf-8')
        other_scope = FriendScope.load('23456', project_root=self.root)
        other_store = PersonaStore('23456', other_scope.friend_dir, project_root=self.root)
        other_store.close()
        other_registry = DatasetRegistry(other_scope)
        self.addCleanup(other_registry.close)
        with self.assertRaises(ScopeMismatch):
            PublicDatasetAdapter(self.scope, other_registry)

        mapping = self.root / 'friends.json'
        mapping.write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.scope.friend_dir)}}}), encoding='utf-8')
        (self.scope.friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '99999'}),
                                                           encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            self.adapt({'dialog': ['x']})

    def test_record_shapes_uid_lengths_text_and_untrusted_input_are_bounded(self):
        self.register()
        for value in (pickle.dumps({'dialog': ['x']}), Path('record.json'), ['dialog']):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(InvalidSource):
                    self.adapt(value)
        for record in (
            {'dialog': 'not a list'}, {'dialog': []}, {'dialog': ['']},
            {'dialog': ['a', 'b'], 'uid': [1]},
            {'dialog': ['a'], 'uid': [object()]},
            {'dialog': ['a'], 'profile': [object()]},
            {'dialog': ['x' * (MAX_TEXT_CHARS + 1)]},
            {'dialog': ['x'] * (MAX_SESSION_MESSAGES + 1)},
            {'dialog': ['x'], 'uid': [True]},
        ):
            with self.subTest(record_shape=type(record.get('dialog')).__name__,
                              count=len(record.get('dialog', [])) if isinstance(record.get('dialog'), list) else None):
                with self.assertRaises(InvalidSource):
                    self.adapt(record)
        for dataset_id, record_id in (('', 'r'), ('public', ''), ('public', True)):
            with self.subTest(dataset_id=dataset_id, record_id=record_id):
                with self.assertRaises(InvalidSource):
                    self.adapter.adapt_record(dataset_id, 'personal_dialog',
                                              {'dialog': ['x']}, record_id)

    def test_jsonl_mapping_and_import_normalize_segment_pair_pipeline(self):
        self.register()
        adapted = self.adapt({'dialog': ['嗨', 'Hello'], 'uid': [10, 20]}, record_id='integrated')
        self.assertEqual(self.adapter.jsonl_mapping()['format'], 'jsonl')
        path = self.root / 'adapted.jsonl'
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                 for row in adapted.rows), encoding='utf-8')
        result = DatasetImporter(self.registry).import_records(self.scope, path,
            self.adapter.jsonl_mapping(), 'public')
        locator_rows = self.registry.db.execute('SELECT friend_id,dataset_id,import_id,record_id,' 
            'message_seq FROM imported_messages ORDER BY message_seq').fetchall()
        normalized = list(DatasetNormalizer(self.scope, self.registry).normalize(locator_rows))
        sessions = list(SessionSegmenter(self.scope).segment(normalized,
            conversation_ids={message.id: adapted.conversation_id for message in normalized}))
        self.assertEqual(result.accepted, 2)
        self.assertEqual(len(sessions), 1)
        turns = build_turns(sessions[0])
        target = adapted.rows[1]['speaker_id']
        pairs = list(DialoguePairBuilder(self.scope).build_pairs(turns, {target}))
        self.assertEqual(len(pairs), 1)
        self.assertIsNone(pairs[0].target_bubbles[0].source.friend_id)


if __name__ == '__main__':
    unittest.main()
