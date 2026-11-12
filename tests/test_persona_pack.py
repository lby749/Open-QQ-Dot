import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import SessionSegmenter, build_turns
from persona_jobs import JobControl
from persona_judge import PERSONA_FIELDS
from persona_pack import PersonaPackBuilder, _validate_files
from persona_store import Conflict
from persona_types import InvalidSource
from persona_worker import JobExecution, _pack_judge_call


class PackIntegrationTests(unittest.TestCase):
    def test_real_pairs_publication_costs_and_terminal_failures(self):
        from tests.test_dialogue_turns import DialogueStoreTests
        fixture = DialogueStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        scope, dialogues, root = fixture.scope, fixture.dialogues, fixture.root
        dialogues.db.execute("UPDATE dataset_registry SET derived_stats_allowed=1 WHERE dataset_id='public'")
        base = dict(dialogues.db.execute("SELECT * FROM imported_messages WHERE import_id='pubimp'").fetchone())
        pairs = PairStore(scope)
        self.addCleanup(pairs.close)
        sessions, candidates = [], []
        targets = ('还不错呀，刚听了首歌', '今天有点累，先歇会儿', '这事挺好玩的哈哈',
                   '留出样本禁止付费', '未知分区禁止付费', '分区不一致禁止付费')
        for index, target in enumerate(targets):
            for offset, body in enumerate(('最近怎么样呀？', target)):
                row = dict(base, record_id=f'pack-{index}-{offset}', message_seq=10+index*2+offset,
                    speaker_id=f'actor-{index}' if offset else f'user-{index}', speaker_role='human',
                    text=body, content_fingerprint=hashlib.sha256(f'{index}-{offset}'.encode()).hexdigest())
                dialogues.db.execute('INSERT INTO imported_messages (' + ','.join(row) + ') VALUES (' +
                    ','.join('?' for _ in row) + ')', tuple(row.values()))
            messages = tuple(dialogues.normalizer.normalize([{'friend_id': scope.friend_id,
                'dataset_id': 'public', 'import_id': 'pubimp', 'record_id': f'pack-{index}-{offset}',
                'message_seq': 10+index*2+offset} for offset in range(2)]))
            session = next(SessionSegmenter(scope).segment(messages,
                conversation_ids={message.id: f'pack-chat-{index}' for message in messages}))
            dialogues.save_session(session, dialogues.revision)
            built = tuple(DialoguePairBuilder(scope).build_pairs(build_turns(session), {f'actor-{index}'}))
            pairs.save_pairs(built, pairs.revision)
            sessions.append(session)
            candidates.extend(built)
        refs = tuple(dict.fromkeys(ref for pair in candidates for ref in pair.source_refs))
        partitions = [{'dataset_id': ref.dataset_id, 'import_id': ref.import_id,
            'record_id': ref.record_id, 'split': 'eval' if 'pack-3-' in ref.record_id else
            'unknown' if 'pack-4-' in ref.record_id else 'all' if ref.record_id == 'pack-5-1'
            else 'train', 'is_holdout': 'pack-3-' in ref.record_id,
            'source_hash': 'd'*64} for ref in refs]
        data = {'pack_id': 'pack-test', 'target_spec': {'traits': ['warm'],
            'behavior_requirements': ['short'], 'anti_patterns': ['formal']},
            'session_ids': [session.id for session in sessions], 'pair_ids': [pair.id for pair in candidates],
            'target_speaker_ids': [f'actor-{index}' for index in range(len(targets))], 'source_partitions': partitions,
            'selection_limit': 2, 'judge_batch_size': 8, 'use_local_model': False}
        control = JobControl(scope)
        self.addCleanup(control.close)
        class Quota:
            def __init__(self, root): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def allowed(self): return True
        builder = PersonaPackBuilder(scope, pairs, quota_factory=Quota)
        calls, execution_holder = [], []
        usage_mode = ['known']
        def raw_call(prompt, directory, **kwargs):
            execution = execution_holder[0]
            self.assertEqual(control.read_status(execution.job_id)['attempt']['status'], 'STARTED')
            values = json.loads(prompt.split(' Data: ', 1)[1])
            calls.append([item['pair_id'] for item in values])
            directory = Path(directory)
            if usage_mode[0] != 'unknown':
                with sqlite3.connect(directory / 'history.sqlite3') as db:
                    db.execute('CREATE TABLE IF NOT EXISTS usage_calls(seq INTEGER PRIMARY KEY, model TEXT, usage TEXT)')
                    db.execute('INSERT INTO usage_calls(model,usage) VALUES(?,?)', ('gpt-6-luna', json.dumps(
                        {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})))
            content = json.dumps({'items': [{'pair_id': item['pair_id'],
                'scores': {field: .8 for field in PERSONA_FIELDS}, 'quality_score': .9,
                'needs_review': False, 'abstain_reason': None} for item in values]})
            return ('bad-json' if usage_mode[0] == 'failed' else content), None
        def enqueue(pack='pack-test', trait='warm'):
            request = dict(data, pack_id=pack, target_spec=dict(data['target_spec'], traits=[trait]))
            return control.enqueue('pack', request, refs)
        def run(job_id, execution=None):
            if execution is None:
                execution = JobExecution(control, job_id, control.claim(job_id))
            execution_holder[:] = [execution]
            return builder.run_pack_job(control.read_status(job_id), execution,
                judge_call=lambda prompt: _pack_judge_call(prompt, execution, quota_checker=lambda: True))
        with patch.dict('sys.modules', {'main': SimpleNamespace(codex_text=raw_call)}):
            invalid_request = dict(data, pack_id='pack-invalid', source_partitions=[dict(row,
                source_hash='wrong') for row in partitions])
            invalid_id = control.enqueue('pack', invalid_request, refs)
            with self.assertRaisesRegex(InvalidSource, 'source hash differs'):
                run(invalid_id)
            self.assertEqual(calls, [])
            first_id, conflict_id = enqueue(), enqueue(trait='cute')
            self.assertIsNone(control.read_status(first_id)['input']['expected_active_version'])
            first_execution = JobExecution(control, first_id, control.claim(first_id))
            # claimed before the first Pack activates (persona_jobs.claim refuses a job whose source moved since)
            conflict_execution = JobExecution(control, conflict_id, control.claim(conflict_id))
            with patch.object(control, 'start_attempt', wraps=control.start_attempt) as start:
                result = run(first_id, first_execution)
                self.assertEqual(start.call_count, 1)
            manifest = json.loads(Path(result['manifest']).read_text('utf-8'))
            selected = set(manifest['selected_pair_ids'])
            self.assertEqual(len(selected), 2)
            self.assertTrue(selected < {pair.id for pair in candidates[:3]})
            self.assertEqual(calls, [[pair.id for pair in candidates[:3]]])
            self.assertTrue(manifest['target_cohort']['multi_speaker'])
            self.assertEqual(manifest['sticker_profile']['status'], 'unavailable')
            self.assertFalse(manifest['raw_export_allowed'])
            self.assertEqual(manifest['costs']['usage']['total_tokens'], 15)
            self.assertEqual(manifest['costs']['unique_judge_runs'], 1)
            self.assertIsNotNone(manifest['judge_runs'][0]['completed_at'])
            self.assertEqual(manifest['stage_counts']['partition']['excluded_before_judge'], 3)
            self.assertEqual(len(manifest['files']), 5)
            with sqlite3.connect(Path(result['path']) / 'examples.db') as db:
                rows = db.execute('SELECT id,target_bubbles,friend_id FROM examples').fetchall()
            self.assertEqual({row[0] for row in rows}, selected)
            for identity, bubbles, owner in rows:
                pair = next(pair for pair in candidates if pair.id == identity)
                self.assertEqual([b['text'] for b in json.loads(bubbles)], [b.text for b in pair.target_bubbles])
                self.assertIsNone(owner)
            active_path = Path(result['path']).parent.parent / 'active.json'
            old_active = active_path.read_bytes()
            artifact_hashes = {name: hashlib.sha256((Path(result['path']) / name).read_bytes()).hexdigest()
                               for name in manifest['files']}
            self.assertEqual(artifact_hashes, manifest['files'])
            # Replaying a durable run does not pay again; a deleted projection is rebuilt.
            catalog_path = active_path.parent.parent / 'catalog.sqlite3'
            catalog_path.unlink()
            replay = run(first_id, first_execution)
            self.assertEqual(replay['costs'], result['costs'])
            self.assertEqual(len(calls), 1)
            with sqlite3.connect(catalog_path) as db:
                self.assertEqual(db.execute('SELECT active_version FROM persona_packs').fetchone()[0], result['version'])
            # alone: the activation check refuses it; in test_gate_p4 the Pack is also bound, a persona write that
            # moves the source snapshot, so the job is refused as stale before its cloud attempt
            with self.assertRaisesRegex((InvalidSource, Conflict), 'active version changed|stale before cloud attempt'):
                run(conflict_id, conflict_execution)
            self.assertEqual(active_path.read_bytes(), old_active)
            for mode in ('pause', 'lease'):
                job_id = enqueue(trait=mode)
                self.assertEqual(control.read_status(job_id)['input']['expected_active_version'], result['version'])
                execution = JobExecution(control, job_id, control.claim(job_id))
                def stop_at_files(*args):
                    _validate_files(*args)
                    if mode == 'pause': control.pause(job_id)
                    else: execution.token = 'lost-token'
                with patch('persona_pack._validate_files', side_effect=stop_at_files):
                    with self.assertRaises((InvalidSource, Conflict)):
                        run(job_id, execution)
                self.assertEqual(active_path.read_bytes(), old_active)
            usage_mode[0] = 'unknown'
            unknown_id = enqueue(pack='pack-unknown')
            unknown_execution = JobExecution(control, unknown_id, control.claim(unknown_id))
            before = len(calls)
            for _ in range(2):
                with self.assertRaises(InvalidSource): run(unknown_id, unknown_execution)
            self.assertEqual(len(calls), before + 1)
            self.assertEqual(control.read_status(unknown_id)['tokens_unknown'], 1)
            self.assertFalse((root / 'data/persona_packs/pack-unknown/active.json').exists())
            control.finish(unknown_id, unknown_execution.token, error_code='unknown')
            self.assertEqual(control.read_status(unknown_id)['attempt']['status'], 'UNCERTAIN')
            usage_mode[0] = 'failed'
            failed_id = enqueue(pack='pack-failed')
            failed_execution = JobExecution(control, failed_id, control.claim(failed_id))
            before = len(calls)
            for _ in range(2):
                with self.assertRaises(InvalidSource): run(failed_id, failed_execution)
            self.assertEqual(len(calls), before + 1)
            self.assertFalse((root / 'data/persona_packs/pack-failed/active.json').exists())
            failed_row = control.db.execute("SELECT usage_json FROM dataset_curation_runs WHERE status='FAILED'").fetchone()
            self.assertEqual(json.loads(failed_row[0])['total_tokens'], 15)
            # Immutable artifacts remain unchanged after every stopped/conflicting publication.
            self.assertEqual({name: hashlib.sha256((Path(result['path']) / name).read_bytes()).hexdigest()
                              for name in manifest['files']}, artifact_hashes)
            usage_mode[0] = 'known'
            stale_id = enqueue(trait='source-stale')
            def change_revision(*args):
                _validate_files(*args)
                control.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                   (scope.friend_id,))
            with patch('persona_pack._validate_files', side_effect=change_revision):
                with self.assertRaisesRegex(InvalidSource, 'source revision changed'):
                    run(stale_id)
            self.assertEqual(active_path.read_bytes(), old_active)


if __name__ == '__main__':
    unittest.main()
