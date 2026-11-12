import os
import sys
import tempfile
import time
import unittest
import subprocess
import psutil
from pathlib import Path

from task_manager import TaskManager, TaskStatus


class TestP1TaskLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        TaskManager._instance = None
        self.tm = TaskManager.get_instance(db_path=self.root / "tasks_life.sqlite3", workspace=self.root)

    def tearDown(self):
        self.tm.stop_worker()
        TaskManager._instance = None
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cancel_task_kills_process_tree(self):
        """Cancelling a task must recursively terminate child processes."""
        tid = self.tm.create_task(
            title="Long running background process",
            description="Spawns python sleep subprocess",
        )

        # Launch an actual long-running python process
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        proc = subprocess.Popen(cmd)
        child_pid = proc.pid
        self.tm.register_process(tid, child_pid)

        # Confirm child process is running
        self.assertTrue(psutil.pid_exists(child_pid))

        # Cancel the task
        ok = self.tm.cancel_task(tid, reason="User cancelled test", owner_id=self.tm.get_task(tid).owner_id)
        self.assertTrue(ok)

        # Give small margin for process tree termination
        time.sleep(0.5)

        # Assert child process has been killed
        self.assertFalse(psutil.pid_exists(child_pid), f"Child process {child_pid} should have been terminated!")

        # Assert status is CANCELLED
        task = self.tm.get_task(tid)
        self.assertEqual(task.status, TaskStatus.CANCELLED)
        self.assertIn("User cancelled test", task.error)
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass

    def test_token_hard_budget_enforcement(self):
        """Exceeding token_budget automatically interrupts the task into WAITING_RESOURCE."""
        tid = self.tm.create_task(
            title="Token Budget Enforced Job",
            description="Limited to 1000 tokens",
            token_budget=1000,
        )
        self.tm.update_task_status(tid, TaskStatus.RUNNING)

        # Consuming 600 tokens -> below budget, should remain RUNNING
        self.tm.log_progress(tid, "Step1", "Initial progress", tokens_consumed=600)
        t = self.tm.get_task(tid)
        self.assertEqual(t.status, TaskStatus.RUNNING)
        self.assertEqual(t.consumed_tokens, 600)
        self.assertTrue(t.last_heartbeat > 0.0)

        # Consuming another 500 tokens -> total 1100 > budget 1000
        # log_progress should trigger budget check and update status to WAITING_RESOURCE
        self.tm.log_progress(tid, "Step2", "Exceeding budget", tokens_consumed=500)
        t_after = self.tm.get_task(tid)
        self.assertEqual(t_after.status, TaskStatus.WAITING_RESOURCE)
        self.assertIn("Token 预算超限", t_after.error)
        self.assertEqual(t_after.consumed_tokens, 1100)

    def test_task_duration_timeout_enforcement(self):
        """Tasks exceeding max_duration_seconds raise TimeoutError and get set to FAILED."""
        tid = self.tm.create_task(
            title="Fast Timeout Job",
            description="1 second duration limit",
            max_duration_seconds=1,
        )
        self.tm.update_task_status(tid, TaskStatus.RUNNING)

        # Wait 1.2s to exceed duration
        time.sleep(1.2)

        # Logging progress after timeout should raise TimeoutError and update to FAILED
        with self.assertRaises(TimeoutError):
            self.tm.log_progress(tid, "Late Step", "Trying to progress after timeout")

        t = self.tm.get_task(tid)
        self.assertEqual(t.status, TaskStatus.FAILED)
        self.assertIn("timed out", t.error)


if __name__ == "__main__":
    unittest.main()
