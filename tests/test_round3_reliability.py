import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from task_manager import TaskManager, TaskStatus
from task_runtime import current_execution, run_process
from task_verifier import VerificationResult


class Reliability(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = TaskManager(self.root/'tasks.db', self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.manager.stop_worker)

    def task(self, **kw):
        return self.manager.create_task('Synthetic task', 'No real model or account', **kw)

    def spawn(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        def clean():
            if p.poll() is None:
                p.kill()
            p.wait(timeout=3)
        self.addCleanup(clean)
        return p

    def test_host_pid_never_registered_or_terminated(self):
        tid = self.task()
        with self.assertRaises(ValueError):
            self.manager.register_process(tid, os.getpid())
        # Persisted legacy PID alone is never trusted.
        with self.manager._get_connection() as db:
            db.execute('UPDATE tasks SET pid=? WHERE task_id=?', (os.getpid(), tid))
        with patch('psutil.Process.terminate') as terminate:
            self.assertTrue(self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id))
            terminate.assert_not_called()

    def test_only_owned_process_cancelled(self):
        a, b = self.task(), self.task()
        pa, pb = self.spawn(), self.spawn()
        self.manager.register_process(a, pa.pid)
        self.manager.register_process(b, pb.pid)
        with self.assertRaises(ValueError):
            self.manager.register_process(b, pa.pid)
        self.manager.cancel_task(a, owner_id=self.manager.get_task(a).owner_id)
        pa.wait(timeout=3)
        self.assertIsNone(pb.poll())
        self.assertEqual(self.manager.get_task(a).pid, 0)

    def test_pid_reuse_does_not_kill(self):
        tid = self.task();p = self.spawn()
        self.manager.register_process(tid, p.pid)
        self.manager._active_pids[tid][p.pid] -= 100
        self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id)
        self.assertIsNone(p.poll())

    def test_early_process_exit_cleanup(self):
        tid = self.task();p = self.spawn()
        self.manager.register_process(tid, p.pid)
        p.kill();p.wait()
        self.assertTrue(self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id))
        self.assertNotIn(tid, self.manager._active_pids)

    def run_blocked(self, *, fail=False):
        tid = self.task();entered, release = threading.Event(), threading.Event()
        def executor(task, progress):
            self.manager.save_checkpoint(tid, {'completed_steps': ['synthetic step']})
            entered.set();release.wait(3)
            if fail:
                raise RuntimeError('late failure')
            return {'output':'late output'}
        self.manager.register_custom_executor('general', executor)
        th = threading.Thread(target=self.manager._process_single_task, args=(self.manager.get_task(tid),))
        th.start();self.assertTrue(entered.wait(2))
        self.addCleanup(lambda: (release.set(), th.join(3)))
        return tid, th, release

    def test_pause_late_success_never_completes(self):
        tid, th, release = self.run_blocked()
        self.assertTrue(self.manager.pause_task(tid, owner_id=self.manager.get_task(tid).owner_id))
        self.assertFalse(self.manager.resume_task(tid, owner_id=self.manager.get_task(tid).owner_id))
        release.set();th.join(3)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.PAUSED)
        self.assertEqual(self.manager.get_task(tid).checkpoint['completed_steps'], ['synthetic step'])
        self.assertTrue(self.manager.resume_task(tid, owner_id=self.manager.get_task(tid).owner_id))

    def test_cancel_late_error_never_requeues(self):
        tid, th, release = self.run_blocked(fail=True)
        self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id);release.set();th.join(3)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.CANCELLED)
        self.assertFalse(self.manager.resume_task(tid, owner_id=self.manager.get_task(tid).owner_id))

    def test_duplicate_claim_across_managers(self):
        tid, th, release = self.run_blocked()
        second = TaskManager(self.root/'tasks.db', self.root)
        self.assertEqual(second.get_task(tid).status, TaskStatus.RUNNING)
        called=[];second.register_custom_executor('general', lambda *a: called.append(True))
        second._process_single_task(second.get_task(tid))
        self.assertFalse(called)
        release.set();th.join(3)

    def test_cancel_during_verification_wins(self):
        tid = self.task();self.manager.register_custom_executor('general', lambda *a:{'output':'synthetic'})
        def verify(**kw):
            self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id)
            return VerificationResult(success=True, needs_approval=False, details='synthetic', verifier_type='test')
        with patch('task_manager.TaskVerifier.verify', side_effect=verify):
            self.manager._process_single_task(self.manager.get_task(tid))
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.CANCELLED)

    def test_cooperative_cli_cancel_binding(self):
        tid=self.task();entered=threading.Event()
        def executor(task, progress):
            entered.set()
            run_process([sys.executable,'-c','import time;time.sleep(30)'], capture_output=True)
            return {'output':'must not complete'}
        self.manager.register_custom_executor('general',executor)
        th=threading.Thread(target=self.manager._process_single_task,args=(self.manager.get_task(tid),));th.start()
        self.addCleanup(lambda: (self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id),th.join(4)))
        self.assertTrue(entered.wait(2))
        deadline=time.monotonic()+3
        while not self.manager._active_pids.get(tid) and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.manager._active_pids.get(tid))
        self.manager.cancel_task(tid, owner_id=self.manager.get_task(tid).owner_id);th.join(3)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.manager.get_task(tid).status,TaskStatus.CANCELLED)

    def test_budget_checked_before_second_call(self):
        tid=self.task(token_budget=20);calls=[]
        def executor(task, progress):
            runtime=current_execution.get()
            runtime.before_call('abc');calls.append(1)
            runtime.after_call('a'*90)
            runtime.before_call('never');calls.append(2)
            return {}
        self.manager.register_custom_executor('general',executor)
        self.manager._process_single_task(self.manager.get_task(tid))
        task=self.manager.get_task(tid)
        self.assertEqual(calls,[1]);self.assertEqual(task.call_count,1)
        self.assertEqual(task.status,TaskStatus.WAITING_RESOURCE)
        self.assertEqual(task.consumed_tokens,0)
        self.assertGreater(task.estimated_tokens,0)

    def test_cost_without_price_waits_before_call(self):
        tid=self.task(cost_budget=1);calls=[]
        def executor(task,progress):
            current_execution.get().before_call('prompt');calls.append(1)
        self.manager.register_custom_executor('general',executor)
        self.manager._process_single_task(self.manager.get_task(tid))
        self.assertFalse(calls)
        self.assertEqual(self.manager.get_task(tid).status,TaskStatus.WAITING_RESOURCE)

    def test_timeout_stops_cli(self):
        tid=self.task(max_duration_seconds=1)
        def executor(task,progress):
            run_process([sys.executable,'-c','import time;time.sleep(30)'])
            return {}
        self.manager.register_custom_executor('general',executor)
        start=time.monotonic();self.manager._process_single_task(self.manager.get_task(tid))
        self.assertLess(time.monotonic()-start,5)
        self.assertEqual(self.manager.get_task(tid).status,TaskStatus.FAILED)

    def test_legacy_blank_owner_is_not_public(self):
        tid=self.task()
        with self.manager._get_connection() as db:
            db.execute("UPDATE tasks SET owner_id='' WHERE task_id=?",(tid,))
        second=TaskManager(self.root/'tasks.db',self.root)
        self.assertEqual(second.get_task(tid).owner_id,'system:legacy')
        self.assertEqual(second.list_tasks(owner_id='10001'),[])

    def test_abnormal_executor_exit_releases_lease_and_preserves_checkpoint(self):
        tid=self.task()
        def executor(task,progress):
            self.manager.save_checkpoint(tid,{'completed_steps':['one']})
            raise SystemExit('synthetic worker crash')
        self.manager.register_custom_executor('general',executor)
        with self.assertRaises(SystemExit):
            self.manager._process_single_task(self.manager.get_task(tid))
        task=self.manager.get_task(tid)
        self.assertEqual(task.status,TaskStatus.PAUSED)
        self.assertEqual(task.run_id,'')
        self.assertNotIn(tid,self.manager._active_tasks)
        self.assertEqual(task.checkpoint['completed_steps'],['one'])


if __name__=='__main__':
    unittest.main()
