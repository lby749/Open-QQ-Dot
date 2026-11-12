import json
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from persona_jobs import JobControl
from persona_store import Conflict
from source_state import snapshot, source_lock, source_read
from tests.test_persona_incremental import IncrementalTests


class SourceGateTests(unittest.TestCase):
    setUp = IncrementalTests.setUp
    message = IncrementalTests.message
    def test_old_worker_rejects_clear_reset_profile_and_legacy_token(self):
        from tests.test_persona_types import PersonaTypesTests
        self.message()
        control = JobControl(self.scope)
        self.addCleanup(control.close)
        source = PersonaTypesTests().source()
        for action in (lambda: self.history.set_memory('edited'),
                       self.history.reset_memory,
                       lambda: self.history.add_local_outbound('new message')):
            job_id = control.enqueue('distill', {'session_ids': ['s']}, [source])
            lease = control.claim(job_id)
            action()
            with self.assertRaises(Conflict):
                control.checkpoint(job_id, lease, 'b', 'hash', {'data': 'late'})
            control.finish(job_id, lease)
            self.assertEqual(control.read_status(job_id)['error_code'], 'STALE_SOURCE')
            self.assertEqual(control.read_status(job_id)['checkpoint'], {})
        legacy = control.enqueue('distill', {'session_ids': ['legacy']}, [source])
        control.db.execute('UPDATE analysis_jobs SET source_token=NULL WHERE id=?', (legacy,))
        self.assertIsNone(control.claim(legacy))
        self.assertEqual(control.read_status(legacy)['error_code'], 'STALE_SOURCE')

    def test_generator_batch_materializes_before_any_item_escapes(self):
        self.message()
        history = self.history
        class Reader:
            scope = self.scope
            @source_read
            def values(self):
                yield 'first'
                history.set_memory('concurrent edit')
                yield 'second'
        with self.assertRaises(Conflict):
            Reader().values()

    def test_long_calculation_does_not_hold_source_lock(self):
        self.message()
        entered, changed = threading.Event(), threading.Event()
        directory = self.scope.friend_dir
        class Reader:
            scope = self.scope
            @source_read
            def values(self):
                entered.set()
                self.test.assertTrue(changed.wait(3))
                return ('computed',)
        reader = Reader()
        reader.test = self
        def writer():
            entered.wait(3)
            with source_lock(directory):
                changed.set()
        thread = threading.Thread(target=writer)
        thread.start()
        self.assertEqual(reader.values(), ('computed',))
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_cross_process_source_writer_blocks_during_commit_lock(self):
        self.message()
        code = "from pathlib import Path; from store import FriendStore; import sys; s=FriendStore(Path(sys.argv[1])); s.set_memory('child'); s.close()"
        with source_lock(self.scope.friend_dir):
            process = subprocess.Popen([sys.executable, '-c', code, str(self.scope.friend_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            time.sleep(.15)
            self.assertIsNone(process.poll())
        output, error = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, error.decode('utf-8', 'replace'))
        self.assertEqual(self.history.memory(), 'child')

    def test_direct_reader_detects_mutation_and_rejects_entire_batch(self):
        from tests.test_dataset_normalizer import DatasetNormalizerTests
        from persona_incremental import revoke_import
        imports = DatasetNormalizerTests()
        imports.setUp()
        self.addCleanup(imports.doCleanups)
        first = imports.add_message()
        second = imports.add_message(record_id='second', seq=1)
        actual = imports.normalizer._persisted_row
        def read(candidate):
            result = actual(candidate)
            if candidate['record_id'] == 'second':
                revoke_import(imports.scope, 'imp-1')
            return result
        with patch.object(imports.normalizer, '_persisted_row', side_effect=read):
            with self.assertRaises(Conflict):
                next(imports.normalizer.normalize([first, second]))

    def test_job_heartbeat_is_not_a_source_mutation(self):
        from tests.test_persona_types import PersonaTypesTests
        self.message()
        control = JobControl(self.scope)
        self.addCleanup(control.close)
        before = snapshot(self.scope)
        job = control.enqueue('distill', {'session_ids': ['s']}, [PersonaTypesTests().source()])
        lease = control.claim(job)
        control.renew(job, lease)
        self.assertEqual(before, snapshot(self.scope))


    def test_pending_rejects_history_sidecar_and_profile_writers_then_recovers_on_reopen(self):
        from store import FriendStore, dump_json
        self.message()
        with patch('persona_incremental._invalidate_sidecar', side_effect=RuntimeError('power loss')):
            with self.assertRaises(RuntimeError):
                self.history.clear_messages()
        for write in (lambda: self.history.set_memory('late'),
                      lambda: self.persona.write_batch(self.persona.revision, []),
                      lambda: dump_json(self.scope.friend_dir / 'friend.json', {'qq_id': self.scope.friend_id})):
            with self.assertRaises(Conflict):
                write()
        with self.assertRaises(Conflict):
            self.history.get('context_summary')
        with self.assertRaises(Conflict):
            self.history.state_summary()
        with self.assertRaises(Conflict):
            self.persona.revision
        reopened = FriendStore(self.scope.friend_dir)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.message_count(), 0)
        self.assertEqual(reopened.memory(), '')

    def test_all_frozen_tables_have_revision_triggers_and_jobs_are_excluded(self):
        from source_state import PERSONA_TABLES
        actual = {r[0] for r in self.persona.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        for table in PERSONA_TABLES:
            for operation in ('insert', 'update', 'delete'):
                self.assertIn(f'source_{table}_{operation}', actual)
        self.assertFalse(any(name.startswith('source_analysis_jobs_') for name in actual))
        before = snapshot(self.scope)
        self.persona.db.execute("UPDATE scope_state SET revision=revision+1")
        self.assertNotEqual(before, snapshot(self.scope))

    def test_reset_floor_survives_clear_and_empty_reset(self):
        self.message()
        self.history.reset_memory()
        self.history.clear_messages()
        self.history.reset_memory()
        self.assertEqual(self.history.get('activity_memory_reset_seq'), '1')

    def test_deferred_read_transaction_never_takes_source_lock(self):
        with patch('source_state.source_lock', side_effect=AssertionError('reader locked')):
            self.persona.db.execute('BEGIN')
            self.persona.db.execute('SELECT revision FROM scope_state').fetchone()
            self.persona.db.rollback()
            snapshot(self.scope)

    def test_direct_index_head_changes_reader_token_but_not_worker_source_token(self):
        from semantic_index import SemanticIndex
        from persona_types import EmbeddingMetadata
        index = SemanticIndex(self.scope)
        self.addCleanup(index.close)
        generation = index.begin_generation(0, 'fixture-source',
                                             EmbeddingMetadata(model='mock', dimension=2, version='v1'))
        before, worker = snapshot(self.scope), snapshot(self.scope, catalog=False)
        index.db.execute("UPDATE embedding_index_metadata SET state='ACTIVE',revision=1 WHERE generation=?",
                         (generation,))
        self.assertNotEqual(before, snapshot(self.scope))
        self.assertEqual(worker, snapshot(self.scope, catalog=False))
        self.assertEqual(index.metadata()['generation'], generation)

    def test_wal_is_rejected_before_sqlite_open_and_creates_no_files(self):
        self.history.close()
        path = self.scope.friend_dir / 'history.sqlite3'
        db = sqlite3.connect(path)
        db.execute('PRAGMA journal_mode=WAL')
        db.close()
        before = {p.name for p in self.scope.friend_dir.iterdir()}
        with patch('source_state.sqlite3.connect', side_effect=AssertionError('opened WAL')):
            with self.assertRaises(Conflict):
                snapshot(self.scope)
        self.assertEqual(before, {p.name for p in self.scope.friend_dir.iterdir()})

    def test_legacy_logical_rows_change_token_without_revision_table(self):
        from source_state import _database_state
        path = self.scope.friend_dir / 'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE messages(seq INTEGER PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO messages VALUES(1,"before")')
        before = _database_state(path)
        with sqlite3.connect(path) as db:
            db.execute('UPDATE messages SET body="after"')
        self.assertNotEqual(before, _database_state(path))

    def test_pending_before_source_commit_recovers_exactly_once(self):
        self.message()
        with self.history.db:
            self.history.db.execute('INSERT INTO source_operations VALUES(?,?,?,NULL)',
                                    ('crash-before', 'CLEAR_MESSAGES', 'PENDING'))
        from persona_incremental import reconcile
        reconcile(self.history)
        token = snapshot(self.scope)
        reconcile(self.history)
        self.assertEqual(token, snapshot(self.scope))
        self.assertEqual(tuple(self.history.db.execute(
            'SELECT clear_epoch FROM source_state').fetchone()), (1,))
        self.assertEqual(self.history.message_count(), 0)

    def test_reset_prevents_rebuilding_old_memory_but_preserves_manual_profile(self):
        from semantic_index import SemanticIndex
        from store import dump_json
        self.message()
        dump_json(self.scope.friend_dir / 'friend.json',
                  {'qq_id': self.scope.friend_id, 'basic_info': {'nickname': 'manual'}})
        index = SemanticIndex(self.scope)
        self.addCleanup(index.close)
        index._resolve({'kind': 'history_message', 'seq': 1})
        self.history.reset_memory()
        with self.assertRaises(Conflict):
            index._resolve({'kind': 'history_message', 'seq': 1})
        self.assertEqual(index._resolve({'kind': 'structured_manual', 'field': 'basic_info'})['namespace'],
                         'structured')
        self.message('new')
        index._resolve({'kind': 'history_message', 'seq': 2})

    def test_clear_during_summary_calculation_never_resurrects_summary(self):
        from prompt_context import refresh_summary
        for i in range(60):
            self.message(str(i), 'friend' if i % 2 == 0 else 'me')
        def generate(*args, **kwargs):
            self.history.clear_messages()
            return 'obsolete summary', ''
        # A stale summary is discarded without aborting the ongoing chat round.
        refresh_summary(self.history, {}, self.scope.friend_dir, generate)
        self.assertIsNone(self.history.get('context_summary'))
        self.assertIsNone(self.history.get('context_summary_seq'))

    def test_reset_during_memory_model_never_saves_old_changes(self):
        from memory_management import refresh
        self.message()
        def generate(*args, **kwargs):
            self.history.reset_memory()
            return json.dumps({'changes': [{'op': 'add', 'id': '', 'text': 'obsolete fact',
                'kind': 'fact', 'keywords': [], 'sources': ['one']}]}), ''
        refresh(self.history, {}, self.scope.friend_dir, generate, force=True)
        self.assertEqual(self.history.memory(), '')
        self.assertEqual(self.history.get('activity_memory_reset_seq'), '1')
        self.assertIn('Conflict', self.history.get('memory_status'))

    def test_direct_distillation_rejects_profile_change_after_calculation(self):
        from tests.test_persona_evidence import PersonaEvidenceTests
        from store import dump_json
        fixture = PersonaEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture._literal_result()
        calculate = fixture.distiller.analyzer.analyze
        def changed(*args, **kwargs):
            value = calculate(*args, **kwargs)
            dump_json(fixture.scope.friend_dir / 'friend.json',
                      {'qq_id': fixture.scope.friend_id, 'basic_info': {'nickname': 'changed'}})
            return value
        with patch.object(fixture.distiller.analyzer, 'analyze', side_effect=changed):
            with self.assertRaises(Conflict):
                fixture.distiller.analyze(result.settings['session_ids'])

    def test_normalizer_keeps_bounded_duplicate_window_between_batches(self):
        from tests.test_dataset_normalizer import DatasetNormalizerTests
        fixture = DatasetNormalizerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rows = [fixture.add_message(record_id=str(i), seq=i) for i in range(256)]
        self.assertEqual(len(list(fixture.normalizer.normalize(rows + [rows[0]]))), 256)

    def test_revoke_invalidates_real_sessions_claims_and_catalog_keeps_files(self):
        from tests.test_persona_versions import PersonaVersionTests
        from persona_incremental import revoke_import
        fixture = PersonaVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        active = fixture._publish()
        directory = fixture.scope.friend_dir / 'persona_versions' / active['manifest']['version']
        contents = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        originals = fixture.pairs.db.execute('SELECT count(*) FROM imported_messages').fetchone()[0]
        self.assertTrue(revoke_import(fixture.scope, 'imp'))
        db = fixture.pairs.db
        self.assertEqual(db.execute('SELECT count(*) FROM dialogue_sessions').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT count(*) FROM dialogue_pairs').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT state FROM persona_analysis_runs').fetchone()[0], 'STALE')
        self.assertEqual(db.execute('SELECT state FROM persona_claims').fetchone()[0], 'INVALID')
        self.assertEqual(db.execute('SELECT state FROM artifact_versions').fetchone()[0], 'INVALID')
        self.assertEqual(db.execute('SELECT count(*) FROM imported_messages').fetchone()[0], originals)
        self.assertEqual(contents, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})
        token = snapshot(fixture.scope)
        self.assertFalse(revoke_import(fixture.scope, 'imp'))
        self.assertEqual(token, snapshot(fixture.scope))

    def test_local_revoke_does_not_revoke_immutable_pack_original_import_refs(self):
        from tests.test_dataset_normalizer import DatasetNormalizerTests
        from persona_incremental import revoke_import
        from persona_pack_bindings import PackBindingStore
        fixture = DatasetNormalizerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        row = fixture.add_message(dataset_id='public', import_id='public-import',
                                  record_id='public', origin='PUBLIC_DATASET')
        ref = next(fixture.normalizer.normalize([row])).source
        bindings = PackBindingStore(fixture.scope)
        self.addCleanup(bindings.close)
        bindings.db.execute('INSERT INTO pack_bindings VALUES(?,?,?,1,1,20,1,?,?)',
                            (fixture.scope.friend_id, 'published', 'v1', 'now', 'now'))
        pack = {'manifest': {'source_refs': [asdict(ref)]}}
        revoke_import(fixture.scope, 'public-import')
        with self.assertRaises(Conflict):
            next(fixture.normalizer.normalize([row]))
        with patch.object(bindings, '_read_version', return_value=pack):
            self.assertEqual(bindings.read_pack('published', 'v1'), pack)
