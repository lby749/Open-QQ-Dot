import pytest
pytest.importorskip('ijson')
import json
import sqlite3
from decimal import Decimal
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

from dataset_importer import DatasetImporter, InvalidImport
from dataset_registry import DatasetRecord, DatasetRegistry
from persona_store import PersonaStore, ScopeMismatch
from persona_types import FriendScope


class DatasetImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friends = {}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': friend_id}), encoding='utf-8')
            friends[friend_id] = {'qq_id': friend_id, 'directory': str(directory)}
        (self.root / 'friends.json').write_text(json.dumps({'friends': friends}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        self.registry = DatasetRegistry(self.scope)
        self.addCleanup(self.registry.close)
        self.registry.register(DatasetRecord(dataset_id='fixture', name='Fixture',
            data_scope='PRIVATE_FRIEND', usage_scope='local test', local_use_allowed=True), 0)
        self.importer = DatasetImporter(self.registry)

    def write(self, name, contents):
        path = self.root / name
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode('utf-8'))
        return path

    @staticmethod
    def mapping(format, **fields):
        values = {'format': format, 'source_origin': 'REAL_SINGLE_SPEAKER',
                  'speaker_id': 'speaker', 'speaker_role': 'role', 'text': 'text'}
        values.update(fields)
        return values

    def test_json_streaming_preview_and_import_preserves_media_and_private_scope(self):
        path = self.write('sample.json', json.dumps({'messages': [
            {'id': 'm1', 'text': 'hello', 'speaker': 'human', 'role': 'user', 'kind': 'text'},
            {'id': 'm2', 'text': '', 'media': 'image-1', 'speaker': 'bot',
             'role': 'bot', 'kind': 'image', 'direction': 'me'}]}, ensure_ascii=False))
        mapping = self.mapping('json', records_path='messages.item', record_id='id',
            message_type='kind', media_id='media', direction='direction')
        preview = self.importer.inspect(path, 'json', mapping)
        self.assertEqual(preview.count_or_unknown, 2)
        self.assertIn('media', preview.detected_fields)
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected, result.duplicates), (2, 0, 0))
        rows = self.registry.db.execute('SELECT * FROM imported_messages ORDER BY record_id').fetchall()
        scope = self.registry.db.execute('SELECT data_scope FROM dataset_imports').fetchone()[0]
        self.assertEqual(scope, 'PRIVATE_FRIEND')
        media = self.registry.db.execute('SELECT text,message_type,media_id,direction,speaker_role '
            'FROM imported_messages WHERE record_id=?', ('m2',)).fetchone()
        self.assertEqual(tuple(media), ('', 'image', 'image-1', 'me', 'bot'))
        self.assertEqual(rows[0]['source_origin'], 'REAL_SINGLE_SPEAKER')

    def test_json_decimal_values_and_utf8_bom_are_supported(self):
        path = self.write('bom.json', b'\xef\xbb\xbf{"messages":[{"id":4,"text":"hello",'
            b'"speaker":5,"role":"user","score":1.5}]}')
        mapping = self.mapping('json', records_path='messages.item', record_id='id')
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual(result.accepted, 1)
        raw = self.registry.db.execute('SELECT source_record_json FROM imported_messages').fetchone()[0]
        self.assertIn('1.5', raw)

    def test_missing_record_ids_are_stable_but_unique_across_source_files(self):
        one = self.write('first.jsonl', '{"text":"first","role":"user"}\n')
        two = self.write('second.jsonl', '{"text":"second","role":"user"}\n')
        mapping = self.mapping('jsonl')
        a = self.importer.import_records(self.scope, one, mapping, 'fixture')
        b = self.importer.import_records(self.scope, two, mapping, 'fixture')
        self.assertEqual((a.accepted, b.accepted, b.duplicates), (1, 1, 0))

    def test_csv_bad_encoding_and_invalid_timestamp_are_counted_without_text_in_errors(self):
        path = self.write('bad.jsonl', b'{"id":"1","text":"ok","speaker":"s","role":"user","time":"not-time"}\n\xff\n')
        mapping = self.mapping('jsonl', record_id='id', event_time='time')
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected), (1, 1))
        stored = self.registry.db.execute('SELECT event_time FROM imported_messages').fetchone()[0]
        self.assertIsNone(stored)
        self.assertEqual(result.checkpoint['rejection_categories'], {'malformed': 1})

    def test_csv_and_txt_require_explicit_field_mapping(self):
        csv_path = self.write('sample.csv', 'who,role,body\na,user,hello\n')
        csv_map = self.mapping('csv', speaker_id='who', speaker_role='role', text='body')
        self.assertEqual(self.importer.inspect(csv_path, 'csv', csv_map).count_or_unknown, 1)
        csv_result = self.importer.import_records(self.scope, csv_path, csv_map, 'fixture')
        self.assertEqual(csv_result.accepted, 1)
        txt_path = self.write('sample.txt', 'line one\nline two\n')
        txt_map = {'format': 'txt', 'text': '__line__', 'speaker_role': 'context'}
        self.registry.register(DatasetRecord(dataset_id='txt', name='TXT',
            data_scope='PRIVATE_FRIEND', usage_scope='local test', local_use_allowed=True),
            self.registry.revision)
        txt_result = self.importer.import_records(self.scope, txt_path, txt_map, 'txt')
        self.assertEqual(txt_result.accepted, 2)

    def test_csv_preserves_quoted_multiline_records_and_rejects_oversized_record(self):
        path = self.write('multiline.csv', b'who,role,body\r\na,user,"first line\r\nsecond line"\r\n'
            + b'b,user,"' + b'x' * 262_145 + b'"\r\n'
            + b'c,user,after\r\n')
        mapping = self.mapping('csv', speaker_id='who', speaker_role='role', text='body')
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected), (2, 1))
        self.assertEqual(result.checkpoint['rejection_categories'], {'oversized': 1})
        bodies = [row[0] for row in self.registry.db.execute(
            'SELECT text FROM imported_messages ORDER BY message_seq,record_id')]
        self.assertIn('first line\r\nsecond line', bodies)
        self.assertIn('after', bodies)

    def test_invalid_mapping_types_fail_before_creating_import(self):
        path = self.write('invalid-map.jsonl', '{"text":"safe","role":"user"}\n')
        for key, value in (('speaker_id', []), ('speaker_role', {}), ('source_origin', [])):
            with self.subTest(key=key):
                mapping = self.mapping('jsonl')
                mapping[key] = value
                with self.assertRaises(InvalidImport):
                    self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM dataset_imports').fetchone()[0], 0)

    def test_cancel_checkpoint_records_only_committed_rejection_categories_and_ordinals(self):
        path = self.write('bad-many.jsonl', ''.join(
            json.dumps({'id': str(i), 'text': '', 'role': 'user'}) + '\n'
            for i in range(103)))
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] > 101

        result = self.importer.import_records(self.scope, path,
            self.mapping('jsonl', record_id='id'), 'fixture', cancel=cancel)
        self.assertEqual((result.accepted, result.rejected), (0, 100))
        self.assertEqual(result.checkpoint['next_index'], 100)
        self.assertEqual(result.checkpoint['rejection_categories'], {'empty text without media': 100})
        self.assertEqual(result.checkpoint['rejected_rows'][0],
                         {'row': 1, 'category': 'empty text without media'})
        self.assertEqual(result.checkpoint['rejected_rows'][-1]['row'], 100)

    def test_binding_change_mid_import_prevents_next_batch(self):
        path = self.write('binding.jsonl', ''.join(json.dumps(
            {'id': str(i), 'text': f'msg-{i}', 'role': 'user'}) + '\n' for i in range(150)))
        calls = [0]

        def change_binding():
            calls[0] += 1
            if calls[0] == 101:
                (self.root / 'friends' / '12345' / 'friend.json').write_text(
                    json.dumps({'qq_id': '23456'}), encoding='utf-8')
            return False

        with self.assertRaises(ScopeMismatch):
            self.importer.import_records(self.scope, path, self.mapping('jsonl', record_id='id'),
                                         'fixture', cancel=change_binding)
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 100)

    def test_failed_import_keeps_committed_checkpoint_and_can_resume(self):
        import dataset_importer as importer_module
        path = self.write('recover.jsonl', ''.join(json.dumps(
            {'id': str(i), 'text': f'msg-{i}', 'role': 'user'}) + '\n' for i in range(101)))
        mapping = self.mapping('jsonl', record_id='id')
        original_rows = importer_module._rows

        def fail_after_first_batch(*args):
            for item in original_rows(*args):
                if item[0] == 101:
                    raise OSError('controlled local read failure')
                yield item

        with patch.object(importer_module, '_rows', side_effect=fail_after_first_batch):
            with self.assertRaisesRegex(OSError, 'controlled local read failure'):
                self.importer.import_records(self.scope, path, mapping, 'fixture')
        row = self.registry.db.execute('SELECT import_id,checkpoint_json,status FROM dataset_imports').fetchone()
        self.assertEqual(row['status'], 'FAILED')
        checkpoint = json.loads(row['checkpoint_json'])
        self.assertEqual((checkpoint['accepted'], checkpoint['next_index']), (100, 100))
        resumed = self.importer.import_records(self.scope, path, mapping, 'fixture', checkpoint=checkpoint)
        self.assertEqual((resumed.accepted, resumed.checkpoint['next_index']), (101, 101))

    def test_external_revision_change_prevents_failed_status_overwrite(self):
        import dataset_importer as importer_module
        path = self.write('revision.jsonl', ''.join(json.dumps(
            {'id': str(i), 'text': f'msg-{i}', 'role': 'user'}) + '\n' for i in range(120)))
        mapping = self.mapping('jsonl', record_id='id')
        original_rows = importer_module._rows

        def fail_after_external_change(*args):
            for item in original_rows(*args):
                if item[0] == 100:
                    yield item
                    self.registry.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                             (self.scope.friend_id,))
                    raise OSError('controlled failure after external revision')
                yield item

        with patch.object(importer_module, '_rows', side_effect=fail_after_external_change):
            with self.assertRaisesRegex(OSError, 'controlled failure after external revision'):
                self.importer.import_records(self.scope, path, mapping, 'fixture')
        row = self.registry.db.execute('SELECT checkpoint_json,status FROM dataset_imports').fetchone()
        checkpoint = json.loads(row['checkpoint_json'])
        self.assertEqual(row['status'], 'RUNNING')
        self.assertEqual((checkpoint['accepted'], checkpoint['next_index']), (100, 100))
        self.assertEqual(self.registry.db.execute('SELECT revision FROM scope_state').fetchone()[0],
                         self.registry.revision)

    def test_duplicate_import_is_idempotent_and_never_writes_live_messages(self):
        path = self.write('one.jsonl', '{"id":"1","text":"same","speaker":"s","role":"user"}\n')
        mapping = self.mapping('jsonl', record_id='id')
        first = self.importer.import_records(self.scope, path, mapping, 'fixture')
        second = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual(first.accepted, 1)
        self.assertEqual((second.accepted, second.duplicates), (0, 1))
        tables = {row[0] for row in self.registry.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('messages', tables)
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 1)

    def test_cancel_keeps_only_complete_committed_batches_and_checkpoint_resume_rejects_changed_input(self):
        path = self.write('many.jsonl', ''.join(
            json.dumps({'id': str(i), 'text': f'message-{i}', 'speaker': 's', 'role': 'user'}) + '\n'
            for i in range(220)))
        mapping = self.mapping('jsonl', record_id='id')
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] > 101

        partial = self.importer.import_records(self.scope, path, mapping, 'fixture', cancel=cancel)
        self.assertEqual(partial.accepted, 100)
        self.assertEqual(partial.checkpoint['next_index'], 100)
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 100)
        changed = path.read_bytes() + b'\n'
        path.write_bytes(changed)
        with self.assertRaises(InvalidImport):
            self.importer.import_records(self.scope, path, mapping, 'fixture', checkpoint=partial.checkpoint)

    def test_cancel_checkpoint_resumes_same_source_and_completed_checkpoint_is_read_only(self):
        path = self.write('many.jsonl', ''.join(
            json.dumps({'id': str(i), 'text': f'message-{i}', 'speaker': 's', 'role': 'user'}) + '\n'
            for i in range(130)))
        mapping = self.mapping('jsonl', record_id='id')
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] > 101

        partial = self.importer.import_records(self.scope, path, mapping, 'fixture', cancel=cancel)
        resumed = self.importer.import_records(self.scope, path, mapping, 'fixture',
                                               checkpoint=partial.checkpoint)
        self.assertEqual((resumed.accepted, resumed.checkpoint['next_index']), (130, 130))
        revision = self.registry.revision
        repeated = self.importer.import_records(self.scope, path, mapping, 'fixture',
                                                checkpoint=resumed.checkpoint)
        self.assertEqual(repeated, resumed)
        self.assertEqual(self.registry.revision, revision)

    def test_same_text_from_different_speakers_is_not_deduplicated(self):
        path = self.write('speakers.jsonl', ''.join(json.dumps(row) + '\n' for row in [
            {'id': 'a', 'text': 'same', 'speaker': 'alice', 'role': 'user', 'time': '2026-01-01T00:00:00'},
            {'id': 'b', 'text': 'same', 'speaker': 'bob', 'role': 'user', 'time': '2026-01-01T00:00:00'}]))
        result = self.importer.import_records(self.scope, path,
            self.mapping('jsonl', record_id='id', event_time='time'), 'fixture')
        self.assertEqual((result.accepted, result.duplicates), (2, 0))

    def test_public_dataset_cannot_claim_private_relationship_origin(self):
        self.registry.register(DatasetRecord(dataset_id='public', name='Public',
            data_scope='PUBLIC_DATASET', usage_scope='public test', local_use_allowed=True),
            self.registry.revision)
        path = self.write('private.jsonl', '{"text":"private marker","role":"user"}\n')
        mapping = self.mapping('jsonl', source_origin='REAL_RELATIONSHIP')
        with self.assertRaises(ScopeMismatch):
            self.importer.import_records(self.scope, path, mapping, 'public')
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 0)

    def test_malformed_json_error_does_not_echo_private_source_text(self):
        path = self.write('broken.json', '{"messages":[{"secret":"PRIVATE_BODY_MARKER"},')
        mapping = self.mapping('json', records_path='messages.item')
        with self.assertRaises(InvalidImport) as caught:
            self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertNotIn('PRIVATE_BODY_MARKER', str(caught.exception))
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 0)

    def test_source_change_during_iteration_fails_instead_of_completing_old_hash(self):
        path = self.write('changing.jsonl', ''.join(json.dumps(
            {'id': str(i), 'text': f'msg-{i}', 'role': 'user'}) + '\n' for i in range(101)))
        mapping = self.mapping('jsonl', record_id='id')
        calls = [0]

        def mutate_after_commit():
            calls[0] += 1
            if calls[0] == 102:
                with path.open('ab') as stream:
                    stream.write(b'{"id":"new","text":"later","role":"user"}\n')
            return False

        with self.assertRaisesRegex(InvalidImport, 'source changed during import'):
            self.importer.import_records(self.scope, path, mapping, 'fixture',
                                         cancel=mutate_after_commit)
        row = self.registry.db.execute('SELECT checkpoint_json,status FROM dataset_imports').fetchone()
        checkpoint = json.loads(row['checkpoint_json'])
        self.assertEqual(row['status'], 'FAILED')
        self.assertEqual((checkpoint['accepted'], checkpoint['next_index']), (100, 100))

    def test_same_speaker_repeated_bubble_without_timestamps_is_preserved(self):
        path = self.write('repeated-utterance.jsonl', ''.join(json.dumps(row, ensure_ascii=False) + '\n'
            for row in [{'id': 'first', 'text': 'yeah', 'speaker': 's', 'role': 'user'},
                        {'id': 'second', 'text': 'yeah', 'speaker': 's', 'role': 'user'}]))
        result = self.importer.import_records(self.scope, path,
            self.mapping('jsonl', record_id='id'), 'fixture')
        self.assertEqual((result.accepted, result.duplicates), (2, 0))
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 2)

    def test_line_formats_reject_oversized_rows_with_bounded_reader(self):
        path = self.write('large.txt', b'a' * (262_145) + b'\nshort\n')
        mapping = {'format': 'txt', 'text': '__line__'}
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected), (1, 1))
        self.assertEqual(result.checkpoint['rejection_categories'], {'oversized': 1})

    def test_csv_literal_quotes_do_not_swallow_next_record_and_unsupported_encoding_is_early(self):
        path = self.write('quotes.csv', b'\xef\xbb\xbf' +
                          b'who,role,body\na,user,he said "hi\nb,user,"two\nlines"\n')
        mapping = {'format': 'csv', 'speaker_id': 'who', 'speaker_role': 'role', 'text': 'body'}
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected), (2, 0))
        texts = [row[0] for row in self.registry.db.execute(
            'SELECT text FROM imported_messages ORDER BY rowid')]
        self.assertEqual(texts, ['he said "hi', 'two\nlines'])
        revision = self.registry.revision
        with self.assertRaises(InvalidImport):
            self.importer.import_records(self.scope, path, dict(mapping, encoding='utf-16'), 'fixture')
        self.assertEqual(self.registry.revision, revision)

    def test_bad_origin_is_a_rejected_row_and_bad_format_creates_no_import(self):
        path = self.write('origins.jsonl', ''.join(json.dumps(row) + '\n' for row in [
            {'id': 'bad', 'text': 'bad origin', 'speaker': 's', 'role': 'user', 'origin': []},
            {'id': 'good', 'text': 'valid row', 'speaker': 's', 'role': 'user',
             'origin': 'REAL_SINGLE_SPEAKER'}]))
        mapping = self.mapping('jsonl', record_id='id', source_origin='origin')
        result = self.importer.import_records(self.scope, path, mapping, 'fixture')
        self.assertEqual((result.accepted, result.rejected), (1, 1))
        self.assertEqual(result.checkpoint['rejected_rows'],
                         [{'row': 1, 'category': 'invalid source origin'}])
        revision = self.registry.revision
        with self.assertRaises(InvalidImport):
            self.importer.inspect(path, [], mapping)
        with self.assertRaises(InvalidImport):
            self.importer.import_records(self.scope, path, dict(mapping, format=[]), 'fixture')
        self.assertEqual(self.registry.revision, revision)

    def test_tail_batch_failure_marks_failed_and_can_resume_without_partial_counts(self):
        path = self.write('tail.jsonl', '{"id":"1","text":"tail","speaker":"s","role":"user"}\n')
        mapping = self.mapping('jsonl', record_id='id')
        with patch.object(self.importer, '_commit_batch', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                self.importer.import_records(self.scope, path, mapping, 'fixture')
        row = self.registry.db.execute('SELECT checkpoint_json,status FROM dataset_imports').fetchone()
        checkpoint = json.loads(row['checkpoint_json'])
        self.assertEqual(row['status'], 'FAILED')
        self.assertEqual((checkpoint['accepted'], checkpoint['next_index']), (0, 0))
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) FROM imported_messages').fetchone()[0], 0)
        resumed = self.importer.import_records(self.scope, path, mapping, 'fixture', checkpoint=checkpoint)
        self.assertEqual((resumed.accepted, resumed.duplicates), (1, 0))

    def test_scope_and_dataset_are_verified(self):
        path = self.write('one.jsonl', '{"text":"hello","role":"user"}\n')
        mapping = self.mapping('jsonl')
        other = FriendScope.load('23456', project_root=self.root)
        with self.assertRaises(ScopeMismatch):
            self.importer.import_records(other, path, mapping, 'fixture')
        with self.assertRaises(ScopeMismatch):
            self.importer.import_records(self.scope, path, mapping, 'missing')


if __name__ == '__main__':
    unittest.main()
