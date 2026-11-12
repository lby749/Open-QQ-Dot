import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from dataset_annotation import pipeline_supervisor as pipeline


class Guard:
    def __init__(self):
        self.result = True
        self.error = None
        self.snapshot = {'ordinaryUsageAllowed': True, 'primary': {'usedPercent': 20, 'resetsAt': 900},
                         'weekly': {'usedPercent': 65, 'resetsAt': 1000}}

    def allowed(self):
        return self.result


class PipelineSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'data' / 'datasets'
        self.directory.mkdir(parents=True)
        (self.root / 'docs').mkdir()
        self.quota = self.root / 'docs' / 'quota_state.json'
        self.quota.write_text('{"status":"RUNNING"}', 'utf-8')
        self.worker = {'project_root': str(self.root), 'database': str(self.directory / 'annotations.sqlite3'),
                       'powershell_pid': 10, 'python_pid': 11, 'run_id': 'run', 'state': 'running',
                       'started_at': datetime.fromtimestamp(100, timezone.utc).isoformat()}
        self.processes = {
            10: {'pid': 10, 'ppid': 1, 'create_time': 98, 'cwd': str(self.root),
                 'cmd': ['powershell.exe', '-File', str(self.root / 'dataset_annotation' / 'start_offline_feed.ps1')]},
            11: {'pid': 11, 'ppid': 10, 'create_time': 101, 'cwd': str(self.root),
                 'cmd': [str(self.root / '.venv' / 'Scripts' / 'python.exe'),
                         str(self.root / 'dataset_annotation' / 'offline_feed.py'), '--db', self.worker['database']]}}
        self.save_worker()
        self.guard, self.started, self.now, self.reviewed = Guard(), [], 200, []
        def launch(_):
            self.started.append(1)
            return 20
        def review(*args, **kwargs):
            self.reviewed.append((args, kwargs))
            return {'paused': False, 'counts': {'RESOLVED': 1, 'HUMAN_REVIEW': 1}}
        self.supervisor = pipeline.Supervisor(self.root, self.guard, process_reader=self.processes.get,
            children_reader=lambda _: [], launcher=launch, reviewer=review, clock=lambda: self.now)

    def save_worker(self):
        (self.directory / 'worker-state.json').write_text(json.dumps(self.worker), 'utf-8')

    def stop_worker(self):
        self.processes.pop(11, None)
        self.worker.update(state='stopped_or_complete', exit_code=0)
        self.save_worker()

    def test_attach_local_feeder_ignores_codex_quota_and_no_duplicate_start(self):
        self.assertEqual(self.supervisor.tick(), 'FEEDING')
        self.assertEqual(self.started, [])
        self.assertEqual(self.supervisor.state['worker']['create_time'], 101)
        # the feeder runs Qwen locally: an unavailable Codex quota neither stops it nor pauses the quota state
        self.guard.error, self.guard.result = 'TimeoutError', False
        self.assertEqual(self.supervisor.tick(), 'FEEDING')
        self.assertEqual(json.loads(self.quota.read_text('utf-8')), {'status': 'RUNNING'})
        self.processes.pop(11)
        with closing(sqlite3.connect(self.worker['database'])) as db:
            db.executescript("CREATE TABLE runs(run_id TEXT,status TEXT); INSERT INTO runs VALUES('run','paused');")
        self.guard.error = None
        self.assertEqual(self.supervisor.tick(), 'STARTING')
        self.assertEqual(json.loads((self.directory / 'worker-state.json').read_text('utf-8'))['state'], 'stopped_or_complete')
        self.assertEqual(json.loads(self.quota.read_text('utf-8'))['status'], 'RUNNING')
        self.assertEqual(self.supervisor.tick(), 'STARTING')
        self.assertEqual(len(self.started), 1)
        self.now = 321
        self.assertEqual(self.supervisor.tick(), 'FAILED')
        self.assertIn('startup deadline', self.supervisor.state['error'])
        self.assertEqual(self.supervisor.tick(), 'FAILED')
        self.assertEqual(len(self.started), 1)

    def test_pid_reuse_and_failed_exit_are_terminal(self):
        self.assertEqual(self.supervisor.tick(), 'FEEDING')
        self.processes[11]['create_time'] = 102
        self.assertEqual(self.supervisor.tick(), 'FAILED')
        self.assertIn('PID was reused', self.supervisor.state['error'])
        self.assertEqual(self.started, [])
        self.supervisor.state.pop('phase')
        self.stop_worker()
        self.worker.update(state='failed', exit_code=2)
        self.save_worker()
        self.assertEqual(self.supervisor.tick(), 'FAILED')
        self.assertIn('exit code 2', self.supervisor.state['error'])
        self.assertEqual(self.reviewed, [])

    def test_matching_complete_database_gate_reviews_once(self):
        self.stop_worker()
        marker = self.directory / 'COMPLETE.json'
        marker.write_text(json.dumps({'run_id': 'run', 'db': self.worker['database'], 'processed': 1, 'total': 1}), 'utf-8')
        with closing(sqlite3.connect(self.worker['database'])) as db:
            db.executescript("CREATE TABLE runs(run_id TEXT,status TEXT); INSERT INTO runs VALUES('run','complete');"
                              "CREATE TABLE annotations(run_id TEXT,intent TEXT); INSERT INTO annotations VALUES('run','respond');")
        self.assertEqual(self.supervisor.tick(), 'COMPLETE')
        self.assertEqual(len(self.reviewed), 1)
        self.assertTrue(callable(self.reviewed[0][1]['quota_checker']))
        self.assertEqual(self.supervisor.tick(), 'COMPLETE')
        self.assertEqual(len(self.reviewed), 1)
        self.assertEqual(self.started, [])
        self.supervisor.state.pop('phase')
        marker.write_text(json.dumps({'run_id': 'wrong', 'db': self.worker['database'], 'processed': 1, 'total': 1}), 'utf-8')
        self.assertEqual(self.supervisor.tick(), 'FAILED')
        self.assertEqual(len(self.reviewed), 1)


if __name__ == '__main__':
    unittest.main()
