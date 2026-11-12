"""Attach to the owned local feeder, then review its complete run."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import psutil
from dataset_annotation.offline_feed import _exclusive_lock
from dataset_annotation.quota_guard import QuotaGuard
from dataset_annotation import review_uncertain


def _read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def _save(path, value):
    target = Path(path)
    temp = target.with_name(target.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(temp, target)


def _process(pid):
    try:
        p = psutil.Process(int(pid))
        return {'pid': p.pid, 'ppid': p.ppid(), 'create_time': p.create_time(), 'cmd': p.cmdline(), 'cwd': p.cwd()}
    except psutil.NoSuchProcess:
        return None


def _children(pid):
    try:
        return [_process(p.pid) for p in psutil.Process(int(pid)).children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def _launch(root):
    script = root / 'dataset_annotation' / 'start_offline_feed.ps1'
    process = subprocess.Popen(['powershell.exe', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                               cwd=root, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return process.pid


class Supervisor:
    def __init__(self, root, guard, *, process_reader=_process, children_reader=_children,
                 launcher=_launch, reviewer=review_uncertain.run, clock=time.time):
        self.root = Path(root).resolve()
        self.directory = self.root / 'data' / 'datasets'
        self.path = self.directory / 'pipeline-state.json'
        self.worker_path = self.directory / 'worker-state.json'
        self.db_path = self.directory / 'annotations.sqlite3'
        self.guard = guard
        self.process_reader, self.children_reader = process_reader, children_reader
        self.launcher, self.reviewer, self.clock = launcher, reviewer, clock
        self.state = _read(self.path) if self.path.exists() else {}
        self.state.update(supervisor_pid=os.getpid(), supervisor_create_time=psutil.Process().create_time(),
                          project_root=str(self.root), updated_at=self.clock())

    def _record(self, phase, **details):
        self.state.update(phase=phase, updated_at=self.clock(), **details)
        _save(self.path, self.state)
        (self.directory / 'pipeline-report.md').write_text(
            '# Local Dataset Pipeline\n\n```json\n' + json.dumps(self.state, ensure_ascii=False, indent=2) + '\n```\n', 'utf-8')
        return phase

    def _owned(self, process, kind):
        if not process or Path(process['cwd']).resolve() != self.root:
            return False
        target = self.root / 'dataset_annotation' / ('offline_feed.py' if kind == 'python' else 'start_offline_feed.ps1')
        cmd = process['cmd']
        if not any(Path(x).resolve() == target for x in cmd):
            return False
        if kind == 'python':
            return (Path(cmd[0]).resolve() in {self.root / '.venv' / 'Scripts' / name for name in ('python.exe', 'Open QQ Dot.exe')}
                    and '--db' in cmd and cmd.index('--db') + 1 < len(cmd)
                    and Path(cmd[cmd.index('--db') + 1]).resolve() == self.db_path)
        return Path(cmd[0]).name.lower() in ('powershell.exe', 'pwsh.exe') and '-File' in cmd

    def _worker(self, worker):
        if (Path(worker['project_root']).resolve() != self.root
                or Path(worker['database']).resolve() != self.db_path):
            raise RuntimeError('worker-state project/database ownership mismatch')
        shell_pid = worker.get('powershell_pid')
        shell = self.process_reader(shell_pid) if shell_pid else None
        remembered = self.state.get('shell')
        if shell:
            if not self._owned(shell, 'shell'):
                raise RuntimeError('shell command/root ownership mismatch')
            if remembered and remembered['pid'] == shell['pid']:
                if remembered['create_time'] != shell['create_time'] or remembered['cmd'] != shell['cmd']:
                    raise RuntimeError('shell PID was reused or command changed')
            elif self.state.get('launched_pid') == shell['pid']:
                if abs(shell['create_time'] - self.state['launch_started_at']) > 15:
                    raise RuntimeError('launched shell PID creation time mismatch')
            else:
                started = datetime.fromisoformat(worker['started_at']).timestamp()
                if abs(shell['create_time'] - started) > 15:
                    raise RuntimeError('shell creation time differs from worker start')
            self.state['shell'] = {**shell, 'cmd': list(shell['cmd'])}
        pid = worker.get('python_pid')
        process = self.process_reader(pid) if pid else None
        if process and not self._owned(process, 'python'):
            raise RuntimeError('Python command/root ownership mismatch')
        if not process and shell:
            matches = [p for p in self.children_reader(shell['pid']) if self._owned(p, 'python')]
            if len(matches) > 1:
                raise RuntimeError('multiple owned feeder children; refusing to choose')
            process = matches[0] if matches else None
        if process:
            if not shell or process['ppid'] != shell['pid'] or process['create_time'] < shell['create_time']:
                raise RuntimeError('Python parent/creation ownership mismatch')
            old = self.state.get('worker')
            if old and old['pid'] == process['pid'] and (old['create_time'] != process['create_time'] or old['cmd'] != process['cmd']):
                raise RuntimeError('Python PID was reused or command changed')
            self.state['worker'] = {**process, 'cmd': list(process['cmd'])}
            self.state.pop('launch_deadline', None)
        return process

    def _complete(self, run_id):
        marker_path = self.directory / 'COMPLETE.json'
        if not marker_path.exists():
            return False
        marker = _read(marker_path)
        if marker.get('run_id') != run_id:
            raise RuntimeError('COMPLETE marker differs from attached run')
        if (type(marker.get('total')) is not int or marker.get('processed') != marker['total']
                or Path(marker.get('db', '')).resolve() != self.db_path):
            raise RuntimeError('COMPLETE marker count/database mismatch')
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
            count, pending = db.execute('SELECT COUNT(*),SUM(intent IS NULL) FROM annotations WHERE run_id=?',
                                        (run_id,)).fetchone()
        if not row or row[0] != 'complete' or count != marker['total'] or pending:
            raise RuntimeError('COMPLETE marker disagrees with database completion')
        return True

    def tick(self):
        if self.state.get('phase') in ('COMPLETE', 'FAILED'):
            return self.state['phase']
        try:
            worker = _read(self.worker_path)
            run_id = self.state.get('run_id') or worker.get('run_id')
            if not run_id:
                raise RuntimeError('attached worker has no known run_id')
            if worker.get('run_id') and worker['run_id'] != run_id:
                raise RuntimeError('worker switched run_id')
            self.state['run_id'] = run_id
            process = self._worker(worker)
            if process:  # the feeder runs Qwen locally; Codex account quotas gate only the review below
                return self._record('FEEDING', error=None)
            if worker.get('state') == 'failed' or worker.get('exit_code', 0) != 0:
                raise RuntimeError(f"feeder failed with exit code {worker.get('exit_code')}")
            if worker.get('state') == 'running' and self.db_path.exists():
                with closing(sqlite3.connect(self.db_path)) as db:
                    run = db.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()
                if run and run[0] == 'paused':
                    # Old PowerShell tail can fail adding absent JSON properties after a successful pause.
                    worker.update(state='stopped_or_complete', exit_code=0, ended_at=self.clock(),
                                  exit_evidence='owned Python gone; matching database run is paused')
                    _save(self.worker_path, worker)
            if self.state.get('launch_deadline'):
                if worker.get('powershell_pid') == self.state.get('launched_pid') and worker.get('state') == 'stopped_or_complete':
                    self.state.pop('launch_deadline', None)
                elif self.clock() >= self.state['launch_deadline']:
                    raise RuntimeError('feeder startup deadline exceeded')
                else:
                    return self._record('STARTING', error=None)
            if self._complete(run_id):
                self._record('REVIEWING', error=None)
                result = self.reviewer(self.db_path, run_id, quota_checker=self.guard.allowed,
                                       quota_path=self.root / 'docs' / 'quota_state.json')
                self.state['review_result'] = result
                if result['paused']:
                    quota_path = self.root / 'docs' / 'quota_state.json'
                    quota = _read(quota_path)
                    if quota.get('status') == 'RUNNING':
                        snapshot = self.guard.snapshot
                        quota.update(status='PAUSED_QUOTA', pause_reason='supervisor:review_quota_failure',
                                     paused_resets_at=snapshot['primary']['resetsAt'] if snapshot else None)
                        if snapshot:
                            quota['observed_primary_used_percent'] = snapshot['primary']['usedPercent']
                        _save(quota_path, quota)
                    return self._record('WAITING_QUOTA', error=None)
                if result['counts'].get('PENDING', 0) or result['counts'].get('REVIEWING', 0):
                    raise RuntimeError('review returned with unfinished rows')
                return self._record('COMPLETE', error=None)
            # A live shell can remain after -NoExit; require a recorded natural feeder end.
            if worker.get('state') not in ('stopped_or_complete', 'stopped'):
                raise RuntimeError('feeder missing without a confirmed natural exit')
            pid = self.launcher(self.root)
            self.state.update(launched_pid=pid, launch_started_at=self.clock(), launch_deadline=self.clock() + 120)
            return self._record('STARTING', error=None)
        except Exception as exc:
            return self._record('FAILED', error=f'{type(exc).__name__}: {exc}',
                                required_action='Inspect the recorded error before explicitly restarting supervisor.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args(argv)
    if args.interval < 60:
        parser.error('interval must be at least 60 seconds')
    directory = PROJECT_ROOT / 'data' / 'datasets'
    try:
        with _exclusive_lock(directory / 'pipeline-supervisor.lock'), QuotaGuard(PROJECT_ROOT) as guard:
            supervisor = Supervisor(PROJECT_ROOT, guard)
            while True:
                phase = supervisor.tick()
                print(json.dumps(supervisor.state, ensure_ascii=False), flush=True)
                if phase in ('COMPLETE', 'FAILED'):
                    return 0 if phase == 'COMPLETE' else 2
                time.sleep(args.interval)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'pipeline_supervisor: {exc}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
