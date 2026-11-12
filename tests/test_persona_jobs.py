from dataclasses import replace
import subprocess
import json
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from persona_jobs import JobControl
from persona_worker import run_job
from persona_store import Conflict, ScopeMismatch
import hashlib


class PersonaJobTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_types import PersonaTypesTests
        fixture = PersonaTypesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.scope, self.now = fixture, fixture.scope(), 100
        from store import FriendStore
        history = FriendStore(self.scope.friend_dir)
        history.initialize(self.scope.friend_id, None)
        with history.db:
            history.db.execute("INSERT INTO messages(qq_id,direction,body,created_at,handled) VALUES('fixture','friend','fixture source','2026-10-04T10:00:00+09:00',0)")
        history.close()
        self.control = JobControl(self.scope, clock=lambda: self.now)
        self.addCleanup(self.control.close)

    def enqueue(self, public=False, session='s1'):
        source = self.fixture.public() if public else self.fixture.source()
        return self.control.enqueue('distill', {'session_ids': [session]}, [source])

    def pack_input(self):
        source = self.fixture.public()
        data = {'pack_id': 'reuse-test', 'target_spec': {'traits': ['warm'],
            'behavior_requirements': ['short'], 'anti_patterns': ['formal']},
            'session_ids': ['s1'], 'pair_ids': ['p1'], 'target_speaker_ids': [source.speaker_id],
            'source_partitions': [{'dataset_id': source.dataset_id, 'import_id': source.import_id,
                'record_id': source.record_id, 'split': 'train', 'is_holdout': False,
                'source_hash': 'a' * 64}],
            'selection_limit': 1, 'judge_batch_size': 1, 'use_local_model': False}
        return data, (source,)

    def completed_pack_fixture(self):
        # Persist a completed isolated job plus immutable, hash-checked fixture artifacts.
        from persona_pack import _digest
        data, refs = self.pack_input()
        job_id = self.control.enqueue('pack', data, refs)
        token = self.control.claim(job_id)
        usage = {'status': 'known', 'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
        self.control.checkpoint(job_id, token, 'judge-batch', 'hash',
            {'stage': 'judge', 'run_id': 'run-1', 'result': {'usage': usage}})
        self.control.finish(job_id, token)
        row = self.control.read_status(job_id)
        version = _digest({'job_id': job_id, 'input_hash': row['input_hash']})[:32]
        root = self.scope.project_root / 'data' / 'persona_packs' / data['pack_id']
        directory = root / 'versions' / version
        directory.mkdir(parents=True)
        files = {}
        for name in ('persona_profile.json', 'style_profile.json', 'persona_skill.md',
                     'persona_report.md', 'examples.db'):
            content = ('isolated-fixture:' + name).encode()
            (directory / name).write_bytes(content)
            files[name] = hashlib.sha256(content).hexdigest()
        manifest = {'pack_id': data['pack_id'], 'version': version, 'scope': 'PUBLIC_PACK',
            'friend_id': None, 'schema_version': 'persona-pack-manifest-v1',
            'job_id': job_id, 'input_hash': row['input_hash'], 'source_revision': row['source_revision'],
            'files': files, 'judge_runs': [{'run_id': 'run-1', 'usage': usage}]}
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        active = root / 'active.json'
        active.write_text(json.dumps({'pack_id': data['pack_id'], 'version': version}), encoding='utf-8')
        return data, refs, job_id, directory, active, manifest

    def test_completed_pack_is_reused_after_active_changes_without_republication(self):
        data, refs, job_id, directory, active, manifest = self.completed_pack_fixture()
        for version in (manifest['version'], 'another-valid-active-version'):
            active.write_text(json.dumps({'pack_id': data['pack_id'], 'version': version}), encoding='utf-8')
            before = active.read_bytes()
            with patch.object(self.control, 'spawn_worker') as spawn:
                self.assertEqual(self.control.enqueue('pack', data, refs), job_id)
                spawn.assert_not_called()
            self.assertEqual(active.read_bytes(), before)
        self.assertEqual(self.control.db.execute('SELECT count(*) FROM analysis_jobs').fetchone()[0], 1)

    def test_uncertain_pack_cannot_be_bypassed_with_a_new_active_or_source_revision(self):
        data, refs = self.pack_input()
        job_id = self.control.enqueue('pack', data, refs)
        token = self.control.claim(job_id)
        self.control.start_attempt(job_id, token, 'batch', 'hash')
        self.control.finish(job_id, token)
        active = self.scope.project_root / 'data' / 'persona_packs' / data['pack_id'] / 'active.json'
        active.parent.mkdir(parents=True)
        active.write_text(json.dumps({'pack_id': data['pack_id'], 'version': 'new-active'}), encoding='utf-8')
        self.control.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                (self.scope.friend_id,))
        with self.assertRaisesRegex(Conflict, 'UNCERTAIN'):
            self.control.enqueue('pack', data, refs)
        self.assertEqual(self.control.db.execute('SELECT count(*) FROM analysis_jobs').fetchone()[0], 1)

    def test_completed_pack_with_stale_source_is_not_reused(self):
        data, refs, old_id, directory, active, manifest = self.completed_pack_fixture()
        self.control.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                (self.scope.friend_id,))
        new_id = self.control.enqueue('pack', data, refs)
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.control.read_status(new_id)['state'], 'queued')
        self.assertEqual(self.control.read_status(new_id)['input']['expected_active_version'], manifest['version'])

    def test_completed_pack_bad_manifest_or_artifact_fails_closed(self):
        from persona_types import InvalidSource
        data, refs, job_id, directory, active, manifest = self.completed_pack_fixture()
        manifest_path = directory / 'manifest.json'
        for field, value in (('job_id', 'wrong-job'), ('input_hash', 'wrong-hash'),
                             ('source_revision', manifest['source_revision'] + 1)):
            manifest_path.write_text(json.dumps(dict(manifest, **{field: value})), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.control.enqueue('pack', data, refs)
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        (directory / 'persona_skill.md').write_text('changed', encoding='utf-8')
        with self.assertRaises(InvalidSource):
            self.control.enqueue('pack', data, refs)
        self.assertEqual(self.control.db.execute('SELECT count(*) FROM analysis_jobs').fetchone()[0], 1)

    def test_completed_pack_requires_explicit_known_judge_usage(self):
        data, refs, job_id, directory, active, manifest = self.completed_pack_fixture()
        manifest['judge_runs'][0]['usage']['status'] = 'unknown'
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(Conflict, 'confirmed usage'):
            self.control.enqueue('pack', data, refs)

    def test_project_quota_pause_keeps_job_resumable(self):
        from persona_worker import JobExecution
        job_id = self.enqueue()
        token = self.control.claim(job_id)
        execution = JobExecution(self.control, job_id, token)
        quota = self.scope.project_root / 'docs' / 'quota_state.json'
        quota.parent.mkdir(exist_ok=True)
        quota.write_text(json.dumps({'status': 'PAUSED_QUOTA'}), 'utf-8')
        self.assertTrue(execution.cancelled())
        self.control.finish(job_id, token)
        self.assertEqual(self.control.read_status(job_id)['state'], 'paused')
        quota.write_text(json.dumps({'status': 'RUNNING'}), 'utf-8')
        self.control.retry(job_id)
        self.assertEqual(self.control.read_status(job_id)['state'], 'queued')

    def test_controls_lease_checkpoint_replay_pause_cancel_and_stale(self):
        job_id = self.enqueue()
        self.assertEqual(job_id, self.enqueue())
        other = JobControl(self.scope, clock=lambda: self.now)
        self.addCleanup(other.close)
        token = self.control.claim(job_id, lease_seconds=10)
        self.assertIsNone(other.claim(job_id))
        self.control.checkpoint(job_id, token, 'b1', 'h1', {'count': 1})
        version = self.control.read_status(job_id)['version']
        self.control.checkpoint(job_id, token, 'b1', 'h1', {'count': 1})
        self.assertEqual(self.control.read_status(job_id)['version'], version)
        with self.assertRaises(Conflict):
            self.control.checkpoint(job_id, token, 'b1', 'changed', {'count': 1})
        self.control.pause(job_id)
        self.control.checkpoint(job_id, token, 'b2', 'h2', {'count': 2})
        self.control.finish(job_id, token)
        self.assertEqual(self.control.read_status(job_id)['state'], 'paused')
        self.control.retry(job_id)
        token = self.control.claim(job_id, lease_seconds=10)
        self.control.cancel(job_id)
        self.control.checkpoint(job_id, token, 'b3', 'h3', {'count': 3})
        self.control.finish(job_id, token)
        self.assertEqual(self.control.read_status(job_id)['state'], 'cancelled')
        with self.assertRaises(Conflict):
            self.control.retry(job_id)
        stale = self.enqueue(session='stale')
        token = self.control.claim(stale)
        self.control.db.execute("UPDATE scope_state SET revision=revision+1 WHERE friend_id='12345'")
        with self.assertRaises(Conflict):
            self.control.checkpoint(stale, token, 'late', 'hash', {'publish': True})
        self.control.finish(stale, token)
        self.assertEqual(self.control.read_status(stale)['error_code'], 'STALE_SOURCE')
        self.assertEqual(self.control.read_status(stale)['checkpoint'], {})
        with self.assertRaises(ScopeMismatch):
            self.control.enqueue('distill', {'session_ids': ['x']}, [replace(self.fixture.source(), friend_id='23456')])

    def test_list_recent_is_bounded_and_returns_decoded_owner_jobs(self):
        job_id = self.enqueue()
        rows = self.control.list_recent()
        self.assertEqual([row['id'] for row in rows], [job_id])
        self.assertEqual(rows[0]['input']['session_ids'], ['s1'])
        for limit in (0, 501, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.control.list_recent(limit)

    def test_expired_cloud_attempt_uncertain_blocks_retry_private_zero_cloud_and_hidden_spawn(self):
        public = self.enqueue(public=True)
        row = self.control.read_status(public)
        self.assertEqual(row['owner_scope'], 'friend:12345')
        self.assertIsNone(row['source_refs'][0]['friend_id'])
        self.assertEqual(row['source_refs'][0]['data_scope'], 'PUBLIC_DATASET')
        token = self.control.claim(public, lease_seconds=10)
        self.control.start_attempt(public, token, 'cloud-batch', 'request-hash')
        self.now = 111
        self.assertIsNone(self.control.claim(public))
        row = self.control.read_status(public)
        self.assertEqual((row['state'], row['attempt']['status'], row['tokens_unknown']), ('failed', 'UNCERTAIN', 1))
        with self.assertRaisesRegex(Conflict, 'UNCERTAIN'):
            self.control.retry(public)
        private = self.enqueue(session='private')
        token = self.control.claim(private)
        # 2026-10-09 user decision: private sources may use the cloud model.
        self.control.start_attempt(private, token, 'private-cloud', 'hash')
        self.assertEqual(self.control.read_status(private)['attempt']['status'], 'STARTED')
        self.control.not_sent(private, token)
        self.control.finish(private, token, error_code='NO_CALL')
        self.control.retry(private)
        with patch('persona_jobs.subprocess.Popen') as spawn:
            spawn.return_value.pid = 42
            self.assertEqual(self.control.spawn_worker(private), 42)
            self.assertIn('persona_worker.py', spawn.call_args.args[0][1])
            self.assertEqual(spawn.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        safe = self.enqueue(public=True, session='not-sent')
        token = self.control.claim(safe)
        self.control.start_attempt(safe, token, 'not-sent', 'h')
        self.control.not_sent(safe, token)
        self.control.finish(safe, token, error_code='NOT_SENT')
        self.control.retry(safe)
        self.assertEqual(self.control.read_status(safe)['state'], 'queued')

    def test_worker_default_private_relationship_without_target_and_custom_boundary(self):
        from tests.test_persona_analysis_input import AnalysisInputBuilderTests
        fixture = AnalysisInputBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        inputs = fixture.builder.build([fixture.session.id])
        control = JobControl(fixture.scope)
        self.addCleanup(control.close)
        job_id = control.enqueue('distill', {'session_ids': [fixture.session.id]}, inputs.source_refs)
        from persona_distiller import PersonaDistiller
        actual_analyze = PersonaDistiller.analyze
        with patch('persona_scenes.LocalPersonaAnalyzer', side_effect=AssertionError('no model requested')):
            with patch.object(PersonaDistiller, 'analyze', autospec=True, side_effect=actual_analyze) as analyze:
                result = run_job(control, job_id)
                self.assertIsNone(analyze.call_args.kwargs['pair_ids'])
        self.assertEqual(result['state'], 'completed')
        draft = next(iter(result['checkpoint'].values()))['result']
        self.assertEqual(draft['state'], 'DRAFT')
        self.assertFalse(draft['settings']['target_speaker_ids'])
        self.assertTrue(draft['sample_status'])
        self.assertEqual(control.db.execute('SELECT COUNT(*) FROM artifact_versions').fetchone()[0], 0)
        pair_id = fixture.pairs.db.execute('SELECT id FROM dialogue_pairs WHERE friend_id=? AND session_id=?',
                                           (fixture.scope.friend_id, fixture.session.id)).fetchone()[0]
        selected = fixture.builder.build([fixture.session.id], ['alias'], pair_ids=[pair_id])
        selected_job = control.enqueue('distill', {'session_ids': [fixture.session.id],
            'target_speaker_ids': ['alias'], 'pair_ids': [pair_id]}, selected.source_refs)
        # Spy on a real isolated hidden CLI launch; no model requested and no bridge entry point.
        processes, actual_popen = [], subprocess.Popen
        def capture(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch('persona_jobs.subprocess.Popen', side_effect=capture):
            control.spawn_worker(selected_job)
        self.assertEqual(processes[0].wait(timeout=15), 0)
        selected_status = control.read_status(selected_job)
        self.assertEqual(selected_status['state'], 'completed')
        selected_draft = next(iter(selected_status['checkpoint'].values()))['result']
        self.assertEqual(selected_draft['settings']['pair_ids'], [pair_id])
        job_id = self.enqueue(session='custom')
        def handler(job, execution):
            execution.control.pause(job['id'])
            execution.checkpoint('custom-batch', 'hash', {'count': 1})
        self.assertEqual(run_job(self.control, job_id, handler)['state'], 'paused')
        self.assertIn('custom-batch', self.control.read_status(job_id)['checkpoint'])

    def test_pack_kind_public_frozen_input_dispatch_and_current_cli_usage(self):
        from persona_judge import PERSONA_FIELDS
        from usage_accounting import record
        source = self.fixture.public()
        data = {'pack_id': 'friendly', 'target_spec': {'traits': ['warm'], 'behavior_requirements': ['short'],
                'anti_patterns': ['formal']}, 'session_ids': ['s1'], 'pair_ids': ['p1'],
                'target_speaker_ids': ['human'], 'source_partitions': [{'dataset_id': source.dataset_id,
                'import_id': source.import_id, 'record_id': source.record_id, 'split': 'train',
                'is_holdout': False, 'source_hash': 'source-hash'}], 'selection_limit': 1,
                'judge_batch_size': 4, 'use_local_model': False}
        job_id = self.control.enqueue('pack', data, [source])
        self.assertEqual(self.control.read_status(job_id)['input'], {**data, 'expected_active_version': None})
        self.assertNotIn('expected_active_version', data)
        self.assertEqual(self.control.read_status(job_id)['model_version'], 'gpt-6-luna')
        self.assertNotEqual(job_id, self.control.enqueue('pack', {**data, 'selection_limit': 2}, [source]))
        with self.assertRaises(ScopeMismatch):
            self.control.enqueue('pack', data, [self.fixture.source()])
        directory = self.scope.project_root / 'data' / 'persona_packs' / 'job_usage' / job_id
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(directory / 'history.sqlite3')):
            pass
        record(directory, 'old-model', None, {'input_tokens': 999, 'output_tokens': 99})
        response = json.dumps({'items': [{'pair_id': 'p1', 'scores': {key: .5 for key in PERSONA_FIELDS},
                    'quality_score': .8, 'needs_review': False, 'abstain_reason': None}]})
        cli_calls, pack_calls = [], []
        def codex_text(prompt, usage_directory, **kwargs):
            cli_calls.append(prompt)
            self.assertTrue(kwargs['ephemeral'] and kwargs['text_only'])
            self.assertEqual(kwargs['profile']['model_api']['codex_effort'], 'medium')
            self.assertTrue(kwargs['output_schema'].is_file())
            self.assertEqual(usage_directory, directory)
            record(usage_directory, 'gpt-6-luna', None,
                   {'input_tokens': 10, 'cached_input_tokens': 8, 'output_tokens': 3})
            return response, None
        def run_pack_job(job, execution, *, judge_call):
            nonlocal response
            pack_calls.append(job['kind'])
            self.assertIsNone(job['source_refs'][0]['friend_id'])
            execution.start_attempt('judge', 'hash')
            result = judge_call('public prompt')
            self.assertEqual(result['model'], 'gpt-6-luna')
            self.assertEqual(result['usage'], {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13})
            execution.checkpoint('judge', 'hash', result)
            response = 'invalid JSON'
            execution.start_attempt('judge-invalid', 'hash-invalid')
            invalid = judge_call('malformed public prompt')
            self.assertEqual(invalid['content'], 'invalid JSON')
            self.assertEqual(invalid['usage']['total_tokens'], 13)
            execution.checkpoint('judge-invalid', 'hash-invalid', invalid)
        with patch.dict(sys.modules, {'main': SimpleNamespace(codex_text=codex_text),
                                    'persona_pack': SimpleNamespace(run_pack_job=run_pack_job)}), \
             patch('dataset_annotation.quota_guard.QuotaGuard') as guard:
            guard.return_value.__enter__.return_value.allowed.return_value = True
            result = run_job(self.control, job_id)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(pack_calls, ['pack'])
        self.assertEqual(cli_calls, ['public prompt', 'malformed public prompt'])
        with closing(sqlite3.connect(directory / 'history.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('usage_calls',)])


if __name__ == '__main__':
    unittest.main()
