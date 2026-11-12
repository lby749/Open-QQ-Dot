import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sys
import socket
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, Candidate, DataScope, PrivacyView,
                           SourceOrigin, SourceRef)
from persona_snapshot import PreviewSnapshot
from persona_pack_bindings import PackBindingStore
from persona_versions import EXAMPLE_COLUMNS
try:
    from retrieval_debug_gui import (RetrievalDebugService, RetrievalDebugWindow,
        _artifact_directory, _write_artifact)
except Exception as exc:  # Tk runtime missing on headless Linux
    raise unittest.SkipTest(f'tkinter/retrieval GUI unavailable: {exc}')
from tests import test_persona_snapshot as snapshot_fixtures


NOW = '2026-10-06T00:00:00+00:00'


def candidate(friend_id='12345'):
    source = SourceRef(dataset_id='manual:fixture', import_id='fixture-import',
        record_id='fixture-record', message_seq=None, speaker_id='operator',
        origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
        privacy_view=PrivacyView.LOCAL_FULL, friend_id=friend_id, event_time=None,
        recorded_at=NOW)
    return Candidate(id='fixture-candidate', namespace='memory', type='fixture',
        content='本地相关上下文', score=0.9, source=source, reason='fixture',
        retriever='keyword', friend_id=friend_id, timestamp=None, confidence=0.9,
        original_scope=DataScope.PRIVATE_FRIEND, pack_version=None, rank=1,
        raw_score=0.7, signals=('RRF',))


class FakeOrchestrator:
    def __init__(self, result, calls):
        self.result = result
        self.calls = calls

    def __call__(self, scope, *, embedding_provider=None):
        if embedding_provider is not None:
            raise AssertionError('embedding provider must remain absent')
        owner = self

        class Client:
            def retrieve(self, request):
                owner.calls.append(request)
                return owner.result

        return Client()


class RetrievalDebugGuiTests(unittest.TestCase):
    def setUp(self):
        snapshot_fixtures.SnapshotTests.setUp(self)
        self.scope = snapshot_fixtures.SnapshotTests.scope(self)
        self.result = SimpleNamespace(
            structured=(), memory=(candidate(),), persona=(), interests=(), topics=(),
            examples=(), style=(), stickers=(),
            router=SimpleNamespace(scene='daily_chat', query_terms=['q'], entities=[],
                time_range=(None, None), reason='default', mode='initial',
                retrievers=['memory']),
            trace={'snapshot_version': 'retriever-version', 'routes': [
                {'route': 'semantic', 'received': 0, 'degraded': True,
                 'error_codes': ['Unavailable']}], 'candidates': [
                {'route': 'keyword', 'raw_score': 0.7, 'rrf': 0.02,
                 'input_rank': 1, 'output_rank': 1, 'signals': ['RRF'],
                 'excluded_by': None}], 'snapshot_changed': False},
            degraded=True)
        self.calls = []
        self.service = RetrievalDebugService(self.scope,
            orchestrator_factory=FakeOrchestrator(self.result, self.calls))

    def _run(self, **overrides):
        values = {'query': '最近提到的书', 'recent_context': '手动提供的上下文',
            'scene': 'daily_chat', 'legacy_context': 'legacy 原文'}
        values.update(overrides)
        return self.service.run(**values)

    @staticmethod
    def _db_state(scope):
        paths = [scope.friend_dir / 'history.sqlite3', scope.friend_dir / 'persona.sqlite3',
                 scope.friend_dir / 'friend.json', scope.project_root / 'friends.json']
        paths += [Path(str(path) + suffix) for path in paths[:2]
                  for suffix in ('-wal', '-shm', '-journal')]
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                for path in paths}

    def test_one_retrieval_builds_context_comparison_and_private_artifact(self):
        before = self._db_state(self.scope)
        with patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
             patch.object(AnalysisCallPolicy, 'validate', side_effect=AssertionError('model')):
            result = self._run()
        self.assertEqual(len(self.calls), 1)
        request = self.calls[0]
        self.assertEqual((request.query, request.recent_context, request.scene),
                         ('最近提到的书', '手动提供的上下文', 'daily_chat'))
        self.assertIsNone(request.plan)
        self.assertEqual(request.filters.privacy_scope, DataScope.PRIVATE_FRIEND)
        self.assertEqual(result['comparison_kind'], 'context_only_not_model_reply')
        self.assertEqual(result['A']['context'], 'legacy 原文')
        self.assertIn('本地相关上下文', result['B']['context'])
        self.assertEqual(result['A']['bytes_proxy'], len('legacy 原文'.encode('utf-8')))
        self.assertEqual(result['measurement'], 'UTF-8 byte proxy; not model tokens or API usage')
        self.assertEqual(result['request']['scene_origin'], 'operator_input')
        self.assertEqual(result['unimplemented']['full_model_reply_ab'], 'not_implemented')
        self.assertEqual(result['unimplemented']['sticker_route'], 'not_connected')
        artifact = Path(result['artifact_path'])
        self.assertTrue(artifact.is_file())
        stored = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(stored['comparison_kind'], result['comparison_kind'])
        self.assertEqual(self._db_state(self.scope), before)

    def test_model_send_and_production_writer_sentinels_are_never_called(self):
        forbidden = Mock(side_effect=AssertionError('forbidden entry point'))
        main_stub = SimpleNamespace(make_reply=forbidden, make_round=forbidden,
            codex_text=forbidden, SERVER=forbidden, Bridge=forbidden,
            reply_runtime_context=forbidden, record_prompt=forbidden,
            record_input=forbidden)
        server_stub = SimpleNamespace(SERVER=forbidden, CodexAppServer=forbidden)
        with patch.dict(sys.modules, {'main': main_stub, 'app_server': server_stub}), \
             patch('persona_store.PersonaStore', side_effect=forbidden), \
             patch('dialogue_pairs.PairStore', side_effect=forbidden), \
             patch('dataset_registry.DatasetRegistry', side_effect=forbidden), \
             patch('semantic_index.SemanticIndex', side_effect=forbidden):
            self._run()
        forbidden.assert_not_called()

    def test_empty_retrieval_keeps_legacy_and_semantic_unavailability_is_visible(self):
        self.result.memory = ()
        result = self._run()
        self.assertEqual(result['B']['context'], result['A']['context'])
        self.assertTrue(result['retrieval']['degraded'])
        route = result['retrieval']['trace']['routes'][0]
        self.assertEqual((route['route'], route['error_codes']), ('semantic', ['Unavailable']))

    def test_real_unindexed_retrieval_uses_visible_degraded_fallback(self):
        service = RetrievalDebugService(self.scope)
        result = service.run(query='像之前那样', recent_context='', scene='daily_chat',
                             legacy_context='legacy')
        routes = result['retrieval']['trace']['routes']
        semantic = next(row for row in routes if row['route'] == 'semantic')
        self.assertTrue(semantic['degraded'])
        self.assertIn('Unavailable', semantic['error_codes'])
        self.assertTrue(result['retrieval']['degraded'])

    def test_public_mode_reads_exact_binding_and_retrieves_public_style_without_writes(self):
        pack_id, version = 'public-fixture', 'v1'
        directory = self.root / 'data' / 'persona_packs' / pack_id / 'versions' / version
        directory.mkdir(parents=True)
        ref = SourceRef(dataset_id='fixture', import_id='run1', record_id='target',
            message_seq=None, speaker_id='public-speaker', origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=None, event_time=None, recorded_at=NOW, license_id='CC-BY',
            model_analysis_allowed=True)
        identity = {'pack_id': pack_id, 'version': version, 'scope': 'PUBLIC_PACK',
                    'friend_id': None}
        profile = dict(identity, schema_version='persona-profile-v1', claims=[])
        style = dict(identity, schema_version='style-profile-v1',
            observed={'sample_info': {'sample_status': 'low', 'pair_count': 3}},
            manual_overrides={})
        for name, document in (('persona_profile.json', profile), ('style_profile.json', style)):
            (directory / name).write_text(json.dumps(document), encoding='utf-8')
        for name in ('persona_skill.md', 'persona_report.md'):
            (directory / name).write_text('derived public fixture', encoding='utf-8')
        bubble = {'id': 'target', 'text': '公开范例', 'source': asdict(ref)}
        example = {'id': 'pair1', 'user': [bubble], 'context': [], 'target_bubbles': [bubble],
            'scene': 'daily_chat', 'emotion': None, 'topic': None, 'reaction_type': None,
            'source_origin': 'PUBLIC_DATASET', 'friend_id': None, 'relationship_tags': None,
            'source_refs': [asdict(ref)], 'version': version}
        with closing(sqlite3.connect(directory / 'examples.db')) as db, db:
            db.execute('CREATE TABLE examples (' + ','.join(column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
            db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                tuple(json.dumps(example[column]) if column in ('user', 'context', 'target_bubbles', 'source_refs')
                      else example[column] for column in EXAMPLE_COLUMNS))
        manifest = dict(identity, source_refs=[asdict(ref)], selected_pair_ids=['pair1'],
            sources=[{'dataset_id': 'fixture', 'local_use_allowed': True,
                'derived_stats_allowed': True, 'license_data': 'CC-BY', 'raw_export_allowed': False}],
            raw_export_allowed=False, files={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir()})
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with closing(PackBindingStore(self.scope)) as bindings:
            bindings.bind(pack_id, version)
        before = self._db_state(self.scope)
        pack_before = {path.name: path.read_bytes() for path in directory.iterdir()}
        original_init, reads = PackBindingStore.__init__, []

        def track_init(store, scope, readonly=False):
            reads.append(readonly)
            original_init(store, scope, readonly=readonly)

        with patch.object(PackBindingStore, '__init__', track_init), \
             patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch.object(AnalysisCallPolicy, 'validate', side_effect=AssertionError('model')):
            result = RetrievalDebugService(self.scope).run(query='music', recent_context='',
                scene='daily_chat', legacy_context='legacy', privacy_scope=DataScope.PUBLIC_PACK)
        self.assertTrue(reads and all(reads))
        self.assertEqual(result['request']['privacy_scope'], 'PUBLIC_PACK')
        self.assertEqual(result['request']['allowed_pack_versions'], [(pack_id, version)])
        self.assertTrue('RETRIEVED_STYLE' in result['B']['context'])  # 2026-10-10 prompt tags replaced
        self.assertEqual(self._db_state(self.scope), before)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, pack_before)

    def test_public_mode_missing_sidecar_does_not_create_database(self):
        with self.assertRaises(Unavailable):
            self._run(privacy_scope=DataScope.PUBLIC_PACK)
        self.assertFalse((self.scope.friend_dir / 'persona.sqlite3').exists())
        self.assertEqual(self.calls, [])


    def test_foreign_friend_candidate_fails_closed(self):
        self.result.memory = (candidate('54321'),)
        with self.assertRaises(ScopeMismatch):
            self._run()

    def test_source_version_race_discards_before_artifact_publication(self):
        versions = iter(('before', 'before', 'after'))
        service = RetrievalDebugService(self.scope, snapshotter=lambda _scope: next(versions),
            orchestrator_factory=FakeOrchestrator(self.result, self.calls))
        with patch('retrieval_debug_gui._write_artifact') as write:
            with self.assertRaises(Conflict):
                service.run(query='q', recent_context='', scene='daily_chat', legacy_context='A')
        write.assert_not_called()
        self.assertEqual(len(self.calls), 1)

    def test_preview_snapshot_failure_does_not_publish_result(self):
        service = RetrievalDebugService(self.scope, snapshot_factory=Mock(side_effect=OSError('busy')),
            orchestrator_factory=FakeOrchestrator(self.result, self.calls))
        with patch('retrieval_debug_gui._write_artifact') as write:
            with self.assertRaises(OSError):
                service.run(query='q', recent_context='', scene='daily_chat', legacy_context='A')
        self.assertEqual(len(self.calls), 1)
        write.assert_not_called()

    def test_wal_shm_or_journal_state_fails_closed_before_retrieval(self):
        service = RetrievalDebugService(self.scope,
            orchestrator_factory=FakeOrchestrator(self.result, self.calls))
        for suffix in ('-wal', '-shm', '-journal'):
            path = Path(str(self.scope.friend_dir / 'history.sqlite3') + suffix)
            with self.subTest(suffix=suffix):
                path.write_bytes(b'fixture')
                with self.assertRaises(Conflict):
                    service.run(query='q', recent_context='', scene='daily_chat', legacy_context='A')
                self.assertEqual(self.calls, [])
                self.assertEqual(path.read_bytes(), b'fixture')
                path.unlink()

    def test_artifact_path_rejects_traversal_and_symlink(self):
        preview = PreviewSnapshot.create(self.scope)
        path = _artifact_directory(preview)
        path.mkdir()
        path_type = type(path)
        original = path_type.is_symlink
        with patch.object(path_type, 'is_symlink', autospec=True,
                          side_effect=lambda current: current == path or original(current)):
            with self.assertRaises(ScopeMismatch):
                _write_artifact(preview, {'private': True})

        forged = PreviewSnapshot.__new__(PreviewSnapshot)
        object.__setattr__(forged, 'source_friend_id', '12345')
        object.__setattr__(forged, 'scope', SimpleNamespace(friend_id='12345',
            project_root=preview.scope.project_root.parent / '..' / 'outside',
            friend_dir=preview.scope.project_root / 'friends' / '12345'))
        with self.assertRaises(ScopeMismatch):
            _artifact_directory(forged)

    def test_preexisting_temporary_hardlink_cannot_overwrite_source_file(self):
        preview = PreviewSnapshot.create(self.scope)
        directory = _artifact_directory(preview)
        directory.mkdir()
        target = self.scope.friend_dir / 'friend.json'
        before = target.read_bytes()
        temporary = directory / '.result.json.tmp'
        os.link(target, temporary)
        with self.assertRaises(FileExistsError):
            _write_artifact(preview, {'overwrite': True})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(temporary.read_bytes(), before)
        self.assertFalse((directory / 'result.json').exists())

    def test_new_temporary_hardlink_is_rejected_before_payload_write(self):
        preview = PreviewSnapshot.create(self.scope)
        directory = _artifact_directory(preview)
        temporary = directory / '.result.json.tmp'
        linked = self.root / 'concurrent-link.json'
        original_fdopen = os.fdopen

        def link_after_create(descriptor, *args, **kwargs):
            os.link(temporary, linked)
            self.assertEqual(os.fstat(descriptor).st_nlink, 2)
            return original_fdopen(descriptor, *args, **kwargs)

        before = self._db_state(self.scope)
        with patch('retrieval_debug_gui.os.fdopen', side_effect=link_after_create):
            with self.assertRaises(ScopeMismatch):
                _write_artifact(preview, {'private_payload': 'must not be written'})
        self.assertEqual(linked.read_bytes(), b'')
        self.assertFalse(temporary.exists())
        self.assertFalse((directory / 'result.json').exists())
        self.assertEqual(self._db_state(self.scope), before)

    def test_worker_drops_results_after_friend_switch_or_window_close(self):
        window = RetrievalDebugWindow.__new__(RetrievalDebugWindow)
        window.closed = False
        window.generation = 4
        window.cancel_event = threading.Event()
        window.status = Mock()
        window._show = Mock()
        window._friend_changed()
        self.assertTrue(window.cancel_event.is_set())
        self.assertEqual(window.generation, 5)
        window._deliver(3, {})
        window._show.assert_not_called()
        window._deliver(5, {'ok': True})
        window._show.assert_called_once_with({'ok': True})

        window.closed = False
        window.cancel_event = threading.Event()
        window.generation = 5
        window.executor = Mock()
        window.root = Mock()
        window.close()
        self.assertTrue(window.cancel_event.is_set())
        self.assertTrue(window.closed)
        window._deliver(5, {'late': True})
        window._show.assert_called_once_with({'ok': True})
        window.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        window.root.destroy.assert_called_once_with()

    def test_scope_cancellation_discards_worker_result_before_artifact_write(self):
        cancelled = threading.Event()
        calls = []

        class CancelAfterRetrieve(FakeOrchestrator):
            def __call__(self, scope, *, embedding_provider=None):
                client = super().__call__(scope, embedding_provider=embedding_provider)
                original = client.retrieve

                def retrieve(request):
                    result = original(request)
                    cancelled.set()
                    return result

                client.retrieve = retrieve
                return client

        service = RetrievalDebugService(self.scope,
            orchestrator_factory=CancelAfterRetrieve(self.result, calls))
        with patch('retrieval_debug_gui._write_artifact') as write:
            with self.assertRaises(InterruptedError):
                service.run(query='q', recent_context='', scene='daily_chat', legacy_context='A',
                            cancel_event=cancelled)
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
