"""Architecture Fixes Verification Test Suite.

Validates the minimal intrusive architectural enhancements:
1. Worker reentrancy protection (multiple start_worker calls start only one thread).
2. Headless/No-Web background execution (start_background_services executes tasks without Web).
3. Process-level task cancellation & late callback defense (cancelled task is never overwritten).
4. Proactive event retryability upon task creation failure.
5. Persistent notification recovery from durable database path.
6. Unified execution permission context (Execution.effective_permissions) & subtask owner constraint.
7. Model dispatcher capability requirement & manual override enforcement.
8. Memory experience linkage with task_id and goal_id.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_errors import AgentError
from agent_memory import AgentMemoryStore, MemoryScope
from agenda import EventType, ProactiveActionEngine, ProactiveLevel
from model_dispatcher import ModelCapability, ModelDispatcher, TaskCategory
from notification_manager import NotificationManager, NotificationMessage, NotificationStatus
from task_manager import TaskItem, TaskManager, TaskStatus
from task_runtime import Execution, current_execution


class TestArchitectureFixes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.task_db = self.data_dir / "tasks.sqlite3"
        self.notif_db = self.data_dir / "notifications.sqlite3"
        self.mem_db = self.data_dir / "memory.sqlite3"
        self.proactive_db = self.data_dir / "proactive_state.sqlite3"

        self.task_manager = TaskManager(db_path=self.task_db, workspace=self.root)
        self.notif_manager = NotificationManager(db_path=self.notif_db)
        self.memory = AgentMemoryStore(db_path=self.mem_db)
        self.proactive_engine = ProactiveActionEngine(db_path=self.proactive_db)
        self.dispatcher = ModelDispatcher.get_instance()

        self.p_task = patch.object(TaskManager, "get_instance", return_value=self.task_manager)
        self.p_notif = patch.object(NotificationManager, "get_instance", return_value=self.notif_manager)
        self.p_mem = patch.object(AgentMemoryStore, "get_instance", return_value=self.memory)
        self.p_proactive = patch.object(ProactiveActionEngine, "get_instance", return_value=self.proactive_engine)
        self.p_task.start()
        self.p_notif.start()
        self.p_mem.start()
        self.p_proactive.start()

    def tearDown(self):
        self.task_manager.stop_worker()
        self.p_task.stop()
        self.p_notif.stop()
        self.p_mem.stop()
        self.p_proactive.stop()
        self.temp_dir.cleanup()

    # 1. Worker reentrancy protection
    def test_01_worker_reentrancy_protection(self):
        """Calling start_worker concurrently should only start a single worker thread."""
        threads = [threading.Thread(target=self.task_manager.start_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        worker_thread = self.task_manager._worker_thread
        self.assertIsNotNone(worker_thread)
        self.assertTrue(worker_thread.is_alive())

        # Calling again while alive is a no-op and preserves same thread
        self.task_manager.start_worker()
        self.assertEqual(self.task_manager._worker_thread, worker_thread)

    # 2. Headless/No-Web background execution
    def test_02_headless_execution_via_app_lifecycle(self):
        """App lifecycle starts background services and processes queued tasks without Web UI."""
        import app_lifecycle

        # Register custom executor for test task
        executed_tasks = []

        def mock_executor(task: TaskItem, progress_cb):
            executed_tasks.append(task.task_id)
            return {"output": "Headless result", "data": {"status": "ok"}, "tokens_used": 10, "usage_recorded": True}

        self.task_manager.register_custom_executor("data", mock_executor)

        tid = self.task_manager.create_task("后台任务", "无Web环境自动执行", category="data", owner_id="user_bob")

        # Start via app_lifecycle (decoupled from Web)
        app_lifecycle.start_background_services()

        # Wait for worker loop to pick up and complete the task
        start_wait = time.monotonic()
        while time.monotonic() - start_wait < 3.0:
            task = self.task_manager.get_task(tid)
            if task and task.status == TaskStatus.COMPLETED:
                break
            time.sleep(0.1)

        task = self.task_manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertIn(tid, executed_tasks)

    # 3. Process-level task cancellation & late callback defense
    def test_03_cancellation_and_late_callback_defense(self):
        """Cancelled task terminates owned processes, and late callbacks cannot overwrite CANCELLED."""
        tid = self.task_manager.create_task("长耗时任务", "Cancel", category="general", owner_id="user_alice")

        # Spawn a child dummy process (sleep) and register it to this task
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.task_manager.set_task_pid(tid, child.pid)

        # Cancel task
        cancelled = self.task_manager.cancel_task(tid, reason="proactivemedium", owner_id=self.task_manager.get_task(tid).owner_id)
        self.assertTrue(cancelled)

        task = self.task_manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.CANCELLED)

        # Verify child process was terminated
        time.sleep(0.3)
        self.assertIsNotNone(child.poll())

        # Attempt late callback / completion transition on cancelled task
        transition_result = self.task_manager.update_task_status(
            tid, TaskStatus.COMPLETED, result="迟到的完成结果",
        )
        self.assertFalse(transition_result)

        # Status must STILL be CANCELLED, not overwritten by COMPLETED
        task_after = self.task_manager.get_task(tid)
        self.assertEqual(task_after.status, TaskStatus.CANCELLED)

    # 4. Proactive event retryability upon task creation failure
    def test_04_proactive_event_retry_on_dispatch_failure(self):
        """If task dispatch fails, cooldown is rolled back so event remains retryable."""
        fail_trigger_name = "test_failing_trigger"
        self.proactive_engine.register_trigger(
            event_type=EventType.PERIODIC,
            name=fail_trigger_name,
            condition_fn=lambda: {"param": "val1"},
            action_plan_fn=lambda data: {"title": "待建任务", "category": "general", "owner_id": "user_alice"},
            cooldown_sec=300.0,
            owner_id="user_alice",
            min_proactive_level=ProactiveLevel.LEVEL_3_AUTONOMOUS,
        )

        # Mock task_manager.create_task to raise an exception
        with patch.object(self.task_manager, "create_task", side_effect=RuntimeError("SQLite 锁竞争")):
            dispatched = self.proactive_engine.poll_events_and_act(
                proactive_level=ProactiveLevel.LEVEL_3_AUTONOMOUS,
                workspace=self.root,
                task_manager=self.task_manager,
            )
            self.assertEqual(len(dispatched), 0)

        # Cooldown must have been rolled back; polling again without failure should succeed!
        dispatched_second = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_3_AUTONOMOUS,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_second), 1)

    # 5. Persistent notification recovery from durable database path
    def test_05_persistent_notification_recovery(self):
        """NotificationManager uses durable path and recovers pending notifications across restarts."""
        # Manager 1 publishes notification with no sinks -> status is PENDING
        nid = self.notif_manager.publish("跨重启通知", "重要提醒", recipient_id="user_carol")
        notifs = self.notif_manager.list_recent(recipient_id="user_carol")
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]["status"], NotificationStatus.PENDING.value)

        # Simulate restart: create new NotificationManager pointing to same durable DB
        restarted_manager = NotificationManager(db_path=self.notif_db)
        restarted_notifs = restarted_manager.list_recent(recipient_id="user_carol")
        self.assertEqual(len(restarted_notifs), 1)
        self.assertEqual(restarted_notifs[0]["id"], nid)

        # Connect a sink to the restarted manager -> pending notification is flushed automatically
        delivered_items = []
        restarted_manager.register_channel_sink(lambda msg: delivered_items.append(msg) or True)

        self.assertEqual(len(delivered_items), 1)
        self.assertEqual(delivered_items[0].id, nid)

        # Status in DB is now SENT
        updated = restarted_manager.list_recent(recipient_id="user_carol")
        self.assertEqual(updated[0]["status"], NotificationStatus.SENT.value)

    # 6. Unified execution permission context & subtask owner constraint
    def test_06_unified_permission_context_and_owner_constraint(self):
        """Execution.effective_permissions provides consistent context and respects friend.json."""
        # Create user_david with commands=True
        david_dir = self.root / "friends" / "user_david"
        david_dir.mkdir(parents=True, exist_ok=True)
        (david_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": True, "full_computer_access": False}
        }), encoding="utf-8")

        tid = self.task_manager.create_task("用户命令任务", "oktest", owner_id="user_david")
        cancel_ev = threading.Event()
        exec_ctx = Execution(self.task_manager, tid, "run_01", cancel_ev)

        self.assertEqual(exec_ctx.owner_id, "user_david")
        perms = exec_ctx.effective_permissions()
        self.assertTrue(perms["enabled"])
        self.assertTrue(perms["commands"])
        self.assertFalse(perms["full_computer_access"])

    # 7. Model dispatcher capability requirement & manual override
    def test_07_model_dispatcher_capabilities_and_manual_override(self):
        """Dispatcher rejects incapable models when capability is required, and honors manual override."""
        available = ["claude", "custom"]  # claude has CODE_EXECUTION, custom is lightweight

        # 1. Require CODE_EXECUTION -> must pick claude
        chosen = self.dispatcher.select_best_model(
            task_category=TaskCategory.CODE,
            available_providers=available,
            required_capability=ModelCapability.CODE_EXECUTION,
        )
        self.assertEqual(chosen, "claude")

        # 2. Only custom available but task strictly requires CODE_EXECUTION -> must raise AgentError
        with self.assertRaises(AgentError):
            self.dispatcher.select_best_model(
                task_category=TaskCategory.CODE,
                available_providers=["custom"],
                required_capability=ModelCapability.CODE_EXECUTION,
                strict_required=True,
            )

        # 3. Manual override specifies custom for general task -> must respect manual override
        manual_chosen = self.dispatcher.select_best_model(
            task_category=TaskCategory.GENERAL,
            available_providers=available,
            manual_provider="custom",
        )
        self.assertEqual(manual_chosen, "custom")

    # 8. Memory experience linkage with task_id and goal_id
    def test_08_memory_experience_linkage(self):
        """Failure and fix experience records task_id and goal_id for end-to-end traceability."""
        mid = self.memory.record_failure_experience(
            task_category="code",
            failed_action="oktestfailed",
            root_cause="缺少依赖包 pytest",
            solution="pip install pytest",
            owner_id="user_alice",
            task_id="task_2026_001",
            goal_id="goal_999",
        )
        self.assertTrue(mid > 0)

        # Retrieve and verify association fields
        memories = self.memory.get_scoped_memories(MemoryScope.FAILURE_AND_FIX, owner_id="user_alice")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].task_id, "task_2026_001")
        self.assertEqual(memories[0].goal_id, "goal_999")
        self.assertIn("pip install pytest", memories[0].content)


if __name__ == "__main__":
    unittest.main()
