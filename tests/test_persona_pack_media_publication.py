from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import SessionSegmenter, build_turns
from persona_jobs import JobControl
from persona_pack import _activate, _digest, _json, _manifest, run_pack_media_job
from persona_pack_bindings import PackBindingStore
from persona_types import DataScope, PrivacyViolation
from persona_store import Conflict, ScopeMismatch
from persona_versions import EXAMPLE_COLUMNS, render_derived
from persona_worker import JobExecution, run_job


class PackMediaPublicationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_dialogue_turns import DialogueStoreTests
        fixture = DialogueStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.scope, self.dialogues = fixture.scope, fixture.dialogues
        self.dialogues.db.execute("UPDATE dataset_registry SET derived_stats_allowed=1 WHERE dataset_id='public'")
        original = dict(self.dialogues.db.execute("SELECT * FROM imported_messages WHERE import_id='pubimp'").fetchone())
        for index, text in enumerate(('今天怎么样', '刚回来，old-token是原句')):
            row = dict(original, record_id='media-' + str(index), message_seq=index + 10,
                speaker_id='target' if index else 'user', text=text,
                content_fingerprint=hashlib.sha256(str(index).encode()).hexdigest())
            self.dialogues.db.execute('INSERT INTO imported_messages (' + ','.join(row) + ') VALUES (' +
                ','.join('?' for _ in row) + ')', tuple(row.values()))
        messages = tuple(self.dialogues.normalizer.normalize([{'friend_id': self.scope.friend_id,
            'dataset_id': 'public', 'import_id': 'pubimp', 'record_id': 'media-' + str(i),
            'message_seq': i + 10} for i in range(2)]))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={m.id: 'media-conversation' for m in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        pairs = PairStore(self.scope)
        self.addCleanup(pairs.close)
        pair = next(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), {'target'}))
        pairs.save_pairs([pair], pairs.revision)
        self.refs = pair.source_refs
        self.control = JobControl(self.scope)
        self.addCleanup(self.control.close)
        self.bindings = PackBindingStore(self.scope)
        self.addCleanup(self.bindings.close)
        self.directory = self.scope.project_root / 'data/persona_packs/media-test/versions/old-token'
        self.directory.mkdir(parents=True)
        profile = {'scope': 'PUBLIC_PACK', 'pack_id': 'media-test', 'version': 'old-token',
                   'friend_id': None, 'claims': []}
        style = dict(profile, observed={}, manual_overrides={})
        for name, value in (('persona_profile.json', profile), ('style_profile.json', style)):
            (self.directory / name).write_text(_json(value), 'utf-8')
        for name, text in zip(('persona_skill.md', 'persona_report.md'), render_derived(profile, style)):
            (self.directory / name).write_text(text, 'utf-8')
        with closing(sqlite3.connect(self.directory / 'examples.db')) as db:
            db.execute('CREATE TABLE examples (' + ','.join(c + ' TEXT' for c in EXAMPLE_COLUMNS) + ')')
            db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                (pair.id, _json([asdict(b) for b in pair.user_bubbles]),
                 _json([asdict(t) for t in pair.context_turns]), _json([asdict(b) for b in pair.target_bubbles]),
                 'unknown', None, None, None, 'PUBLIC_DATASET', None, None,
                 _json([asdict(r) for r in self.refs]), 'old-token'))
            db.commit()
        manifest = dict(profile, schema_version='persona-pack-manifest-v1', job_id='historical-fixture',
            input_hash='e'*64, source_revision=pairs.revision, created_at='2026-10-07T00:00:00+00:00',
            sources=[{'dataset_id': 'public', 'version': None, 'license_code': None,
                'license_data': 'public-license', 'local_use_allowed': True, 'derived_stats_allowed': True,
                'model_analysis_allowed': True, 'raw_corpus_redistribution_allowed': None,
                'raw_export_allowed': False}], source_refs=[asdict(r) for r in self.refs],
            selected_pair_ids=[pair.id], selection=[{'pair_id': pair.id, 'judge_run_id': 'historical-fixture',
                'partitions': [{'dataset_id': r.dataset_id, 'import_id': r.import_id,
                    'record_id': r.record_id, 'source_hash': 'd'*64} for r in self.refs]}],
            judge_runs=[{'run_id': 'historical-fixture', 'fixture': True}],
            costs={'fixture': True}, stage_counts={'examples': 1}, raw_export_allowed=False,
            files={p.name: _digest(p.read_bytes()) for p in self.directory.iterdir()})
        (self.directory / 'manifest.json').write_text(_json(manifest), 'utf-8')
        self.active = self.directory.parent.parent / 'active.json'
        self.active.write_text(_json({'pack_id': 'media-test', 'version': 'old-token',
            'manifest_sha256': _digest((self.directory / 'manifest.json').read_bytes()), 'revision': 1}), 'utf-8')
        self.bindings.bind('media-test', 'old-token')
        self.request = {'pack_id': 'media-test', 'base_version': 'old-token',
            'base_manifest_sha256': _digest((self.directory / 'manifest.json').read_bytes()),
            'media_mode': 'EMPTY_UNKNOWN', 'media_schema_version': 'sticker-profile-v1'}

    def enqueue(self):
        return self.control.enqueue('pack_media', self.request, ())

    def test_inputs_current_public_source_and_active_are_required(self):
        for key, value in (('base_version', 'missing'), ('base_manifest_sha256', 'f'*64),
                           ('media_mode', 'ANALYZE'), ('media_schema_version', 'unknown')):
            with self.subTest(key=key), self.assertRaises(Exception):
                self.control.enqueue('pack_media', dict(self.request, **{key: value}), ())
        incomplete = dict(self.request)
        incomplete.pop('media_mode')
        with self.assertRaises(ValueError): self.control.enqueue('pack_media', incomplete, ())
        private = replace(self.refs[0], data_scope=DataScope.PRIVATE_FRIEND, friend_id=self.scope.friend_id)
        with self.assertRaises(ScopeMismatch): self.control.enqueue('pack_media', self.request, [private])
        with self.assertRaises(ValueError):
            self.control.enqueue('pack_media', self.request, (), model_version='gpt-6-luna')
        self.dialogues.db.execute("UPDATE dataset_registry SET local_use_allowed=0 WHERE dataset_id='public'")
        with self.assertRaises(PrivacyViolation): self.enqueue()
        self.dialogues.db.execute("UPDATE dataset_registry SET local_use_allowed=1 WHERE dataset_id='public'")
        self.active.write_text(_json({'version': 'other'}), 'utf-8')
        with self.assertRaises(Conflict): self.enqueue()

    def test_default_worker_publishes_six_files_without_models_or_binding_changes(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        binding = self.bindings.bindings()
        job_id = self.enqueue()
        with patch('persona_worker._pack_judge_call', side_effect=AssertionError('model called')):
            status = run_job(self.control, job_id)
        self.assertEqual(status['state'], 'completed', status.get('error_code'))
        result = status['checkpoint']['pack-media:' + status['input_hash']]['result']
        directory = Path(result['path'])
        manifest, digest = _manifest(directory, 'media-test', directory.name)
        pack = self.bindings._read_version('media-test', directory.name)
        self.assertEqual(len(manifest['files']), 6)
        self.assertEqual(manifest['parent_version'], 'old-token')
        self.assertEqual(manifest['parent_manifest_sha256'], self.request['base_manifest_sha256'])
        self.assertEqual(pack['sticker_profile']['status'], 'unknown')
        self.assertEqual(json.loads(self.active.read_text('utf-8'))['manifest_sha256'], digest)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.assertEqual(binding, self.bindings.bindings())
        base = self.bindings._read_version('media-test', 'old-token')
        self.assertEqual(pack['examples'], [dict(base['examples'][0], version=directory.name)])
        for name in ('selection', 'source_refs', 'sources', 'judge_runs', 'costs'):
            self.assertEqual(manifest[name], base['manifest'][name])
        self.assertEqual(status['attempt'], {})
        self.assertEqual(status['tokens_unknown'], 0)

    def test_terminal_publication_checks_cleanup_and_preserve_active(self):
        for fault in ('cancel', 'pause', 'lease', 'revision', 'active', 'base', 'source', 'failure'):
            with self.subTest(fault=fault):
                job_id = self.control.enqueue('pack_media', self.request, (), prompt_version=fault)
                token = self.control.claim(job_id)
                execution = JobExecution(self.control, job_id, token)
                active_bytes = self.active.read_bytes()
                base_bytes = (self.directory / 'manifest.json').read_bytes()
                revision = self.control.store.revision
                def stopped(*args, **kwargs):
                    if fault == 'cancel': self.control.cancel(job_id)
                    elif fault == 'pause': self.control.pause(job_id)
                    elif fault == 'lease': execution.token = 'lost'
                    elif fault == 'revision':
                        self.control.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                                (self.scope.friend_id,))
                    elif fault == 'active': self.active.write_text(_json({'version': 'concurrent'}), 'utf-8')
                    elif fault == 'base': (self.directory / 'manifest.json').write_text('{}', 'utf-8')
                    elif fault == 'source':
                        self.dialogues.db.execute("UPDATE dataset_registry SET derived_stats_allowed=0 WHERE dataset_id='public'")
                    else: raise RuntimeError('injected build failure')
                    return _activate(*args, **kwargs)
                try:
                    with patch('persona_pack._activate', side_effect=stopped), self.assertRaises(Exception):
                        run_pack_media_job(self.control.read_status(job_id), execution)
                    expected = _json({'version': 'concurrent'}).encode() if fault == 'active' else active_bytes
                    self.assertEqual(self.active.read_bytes(), expected)
                    self.assertEqual([p.name for p in self.directory.parent.iterdir()], ['old-token'])
                finally:
                    self.active.write_bytes(active_bytes)
                    (self.directory / 'manifest.json').write_bytes(base_bytes)
                    self.control.db.execute('UPDATE scope_state SET revision=? WHERE friend_id=?',
                                            (revision, self.scope.friend_id))
                    self.dialogues.db.execute("UPDATE dataset_registry SET derived_stats_allowed=1 WHERE dataset_id='public'")

    def test_worker_failure_and_durable_builder_replay(self):
        job_id = self.enqueue()
        before = self.active.read_bytes()
        with patch('persona_pack._activate', side_effect=RuntimeError('injected activation failure')):
            status = run_job(self.control, job_id)
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['error_code'], 'RuntimeError')
        self.assertEqual(self.active.read_bytes(), before)
        self.assertEqual([p.name for p in self.directory.parent.iterdir()], ['old-token'])
        self.control.retry(job_id)
        execution = JobExecution(self.control, job_id, self.control.claim(job_id))
        first = run_pack_media_job(self.control.read_status(job_id), execution)
        published = self.active.read_bytes()
        artifact_bytes = {p.name: p.read_bytes() for p in Path(first['path']).iterdir()}
        replay = run_pack_media_job(self.control.read_status(job_id), execution)
        self.assertEqual(first, replay)
        self.assertEqual(self.active.read_bytes(), published)
        self.assertEqual(artifact_bytes, {p.name: p.read_bytes() for p in Path(first['path']).iterdir()})
