import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from task_manager import TaskManager, TaskStatus
from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus
from tool_registry import ToolRegistry
from research_tracker import ResearchTracker
from agent_memory import AgentMemoryStore


class TestDeadlockAndConcurrency(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # Reset singletons before each test
        TaskManager._instance = None
        ApprovalManager._instance = None
        ToolRegistry._instance = None
        ResearchTracker._instance = None
        AgentMemoryStore._instance = None

    def tearDown(self):
        TaskManager._instance = None
        ApprovalManager._instance = None
        ToolRegistry._instance = None
        ResearchTracker._instance = None
        AgentMemoryStore._instance = None
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_concurrent_get_instance_no_deadlock(self):
        """20 concurrent threads calling get_instance() simultaneously."""
        db_path = self.root / "tasks_concurrent.sqlite3"
        instances = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                tm = TaskManager.get_instance(db_path=db_path)
                am = ApprovalManager.get_instance(db_path=self.root / "approvals_concurrent.sqlite3")
                tr = ToolRegistry.get_instance()
                rt = ResearchTracker.get_instance(db_path=self.root / "research.sqlite3")
                mem = AgentMemoryStore.get_instance(db_path=self.root / "memories.sqlite3")
                with lock:
                    instances.append((tm, am, tr, rt, mem))
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), "线程获取单例超时死锁！")

        self.assertEqual(errors, [])
        self.assertEqual(len(instances), 20)
        # All returned instances must be identical singletons
        first_tm, first_am, first_tr, first_rt, first_mem = instances[0]
        for tm, am, tr, rt, mem in instances:
            self.assertIs(tm, first_tm)
            self.assertIs(am, first_am)
            self.assertIs(tr, first_tr)
            self.assertIs(rt, first_rt)
            self.assertIs(mem, first_mem)

    def test_interrupted_tasks_recovery_no_deadlock(self):
        """Recovering interrupted tasks while concurrent queries are running."""
        db_path = self.root / "recovery.sqlite3"

        # Pre-seed tasks in RUNNING and PLANNING status
        tm_pre = TaskManager(db_path=db_path)
        tid1 = tm_pre.create_task(title="Interrupted Running", description="Job 1")
        tid2 = tm_pre.create_task(title="Interrupted Planning", description="Job 2")
        tm_pre.update_task_status(tid1, TaskStatus.RUNNING)
        tm_pre.update_task_status(tid2, TaskStatus.PLANNING)
        del tm_pre

        # Now instantiate TaskManager again (which triggers _recover_interrupted_tasks)
        tm_recovered = TaskManager.get_instance(db_path=db_path)

        t1 = tm_recovered.get_task(tid1)
        t2 = tm_recovered.get_task(tid2)
        self.assertEqual(t1.status, TaskStatus.PAUSED)
        self.assertEqual(t2.status, TaskStatus.PAUSED)

        logs1 = tm_recovered.get_task_logs(tid1)
        self.assertTrue(any("interrupted" in l["message"].lower() or "中断" in l["message"] for l in logs1))

    def test_high_concurrency_read_write_task_manager(self):
        """Heavy concurrent read/write operations across TaskManager and ApprovalManager."""
        db_path = self.root / "tasks_stress.sqlite3"
        tm = TaskManager.get_instance(db_path=db_path)
        apr_db = self.root / "approvals_stress.sqlite3"
        am = ApprovalManager.get_instance(db_path=apr_db)

        created_task_ids = []
        ids_lock = threading.Lock()

        def producer_tasks(worker_id: int):
            for i in range(5):
                tid = tm.create_task(
                    title=f"Worker {worker_id} Task {i}",
                    description=f"Concurrent stress test task {i}",
                    priority=50 + i,
                    owner_id=f"user_{worker_id}",
                )
                with ids_lock:
                    created_task_ids.append(tid)
                tm.add_log(tid, "StressStep", f"Log from worker {worker_id} iter {i}")
                time.sleep(0.01)

        def reader_tasks(worker_id: int):
            for _ in range(10):
                tasks = tm.list_tasks(limit=10)
                _ = len(tasks)
                time.sleep(0.005)

        def producer_approvals(worker_id: int):
            for i in range(5):
                ok, res = am.check_and_request_approval(
                    task_id=f"stress_task_{worker_id}_{i}",
                    category=ApprovalCategory.HIGH_RISK_COMMAND,
                    description=f"Action {i} from worker {worker_id}",
                    owner_id=f"user_{worker_id}",
                    sync_wait=False,
                )
                time.sleep(0.01)

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = []
            for w in range(4):
                futures.append(executor.submit(producer_tasks, w))
                futures.append(executor.submit(reader_tasks, w))
                futures.append(executor.submit(producer_approvals, w))

            for f in as_completed(futures):
                # If any deadlocked or raised, this will raise or block
                f.result(timeout=15.0)

        self.assertEqual(len(created_task_ids), 20)
        all_tasks = tm.list_tasks(limit=100)
        self.assertEqual(len(all_tasks), 20)


if __name__ == "__main__":
    unittest.main()
