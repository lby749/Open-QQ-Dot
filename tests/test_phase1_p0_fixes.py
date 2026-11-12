"""Tests for Phase 1 P0-A (TaskManager reliability) and P0-B (ToolRegistry & Approval isolation).
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from approval_manager import ApprovalCategory, ApprovalManager, open_identity_channel, ApprovalPolicy, ApprovalStatus
from task_manager import TaskItem, TaskManager, TaskStatus
from task_runtime import Execution, current_execution
from tool_registry import ToolDefinition, ToolExecutionResult, ToolPermissionLevel, ToolRegistry


class TestPhase1P0Fixes(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_phase1_")
        self.workspace = Path(self.test_dir)
        (self.workspace / "data").mkdir(parents=True, exist_ok=True)
        self.task_manager = TaskManager(workspace=self.workspace)
        self.approval_manager = ApprovalManager(db_path=self.workspace / "data" / "approvals.sqlite3")
        # Reset approval singleton for testing
        ApprovalManager._instance = self.approval_manager

    def tearDown(self):
        self.task_manager.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # P0-A: Dependency Failure Propagation, Cycle Detection & Recovery
    # =========================================================================
    def test_01_dependency_failure_propagation_marks_blocked(self):
        """When an upstream task fails, downstream task must NOT hang in PENDING;
        it must be explicitly marked BLOCKED_BY_DEPENDENCY with detailed error rationale.
        """
        # Create task 1 (upstream)
        t1 = self.task_manager.create_task(
            title="前置任务1", description="做一些准备工作", category="general", owner_id="user_alice"
        )
        # Create task 2 (downstream depends on t1)
        t2 = self.task_manager.create_task(
            title="后置任务2", description="依赖前置任务1", category="general", owner_id="user_alice",
            depends_on=[t1]
        )

        # Fail upstream task 1
        self.task_manager.update_task_status(t1, TaskStatus.FAILED, error="数据库连接池耗尽")

        # Scheduler fetches next task
        next_task = self.task_manager._fetch_next_executable_task()
        self.assertIsNone(next_task, "No executable task should be returned")

        # Verify task 2 is explicitly BLOCKED_BY_DEPENDENCY with error details
        t2_item = self.task_manager.get_task(t2)
        self.assertEqual(t2_item.status, TaskStatus.BLOCKED_BY_DEPENDENCY)
        self.assertTrue("failed" in t2_item.error.lower() or "失败" in t2_item.error)
        self.assertIn("数据库连接池耗尽", t2_item.error)
        self.assertIn(t1, t2_item.error)

    def test_02_dependency_cancellation_and_missing_task_blocked(self):
        """Downstream tasks depending on cancelled or non-existent tasks must be blocked."""
        # Case A: Cancelled dependency
        t_up = self.task_manager.create_task("上游任务", "Cancel", "general", owner_id="user_alice")
        t_down = self.task_manager.create_task("下游任务", "依赖上游", "general", owner_id="user_alice", depends_on=[t_up])
        self.task_manager.update_task_status(t_up, TaskStatus.CANCELLED)
        self.task_manager._fetch_next_executable_task()

        t_down_item = self.task_manager.get_task(t_down)
        self.assertEqual(t_down_item.status, TaskStatus.BLOCKED_BY_DEPENDENCY)
        self.assertTrue("cancel" in t_down_item.error.lower())

        # Case B: Non-existent dependency ID
        t_orphan = self.task_manager.create_task("孤儿任务", "依赖不存在的任务", "general", depends_on=["non_existent_tid_999"])
        self.task_manager._fetch_next_executable_task()
        t_orphan_item = self.task_manager.get_task(t_orphan)
        self.assertEqual(t_orphan_item.status, TaskStatus.BLOCKED_BY_DEPENDENCY)
        self.assertTrue("前置依赖任务不存在" in t_orphan_item.error or "does not exist" in t_orphan_item.error.lower())

    def test_03_dependency_cycle_detection(self):
        """Cyclic dependencies (A -> B -> A) must be detected and marked FAILED with cycle path."""
        t_a = self.task_manager.create_task("Task A", "A depends on B", "general")
        t_b = self.task_manager.create_task("Task B", "B depends on A", "general", depends_on=[t_a])

        # Manually inject dependency A depends on B into DB
        with self.task_manager._db_lock, self.task_manager._get_connection() as conn:
            conn.execute("UPDATE tasks SET depends_on = ? WHERE task_id = ?", (json.dumps([t_b]), t_a))
            conn.commit()

        # Attempt to schedule
        self.task_manager._fetch_next_executable_task()

        t_a_item = self.task_manager.get_task(t_a)
        self.assertEqual(t_a_item.status, TaskStatus.FAILED)
        self.assertTrue("依赖死锁环" in t_a_item.error or "deadlock cycle" in t_a_item.error)
        self.assertIn(t_a, t_a_item.error)
        self.assertIn(t_b, t_a_item.error)

    def test_04_dependency_recovery_manual_and_automatic(self):
        """After resolving the dependency failure (e.g. retried upstream succeeds),
        downstream task can be unblocked automatically or via resolve_dependency_block().
        """
        t_parent = self.task_manager.create_task("父任务", "failed", "general")
        t_child = self.task_manager.create_task("子任务", "依赖父任务", "general", depends_on=[t_parent])

        # Step 1: Parent fails, child blocked
        self.task_manager.update_task_status(t_parent, TaskStatus.FAILED, error="临时网络中断")
        self.task_manager._fetch_next_executable_task()
        self.assertEqual(self.task_manager.get_task(t_child).status, TaskStatus.BLOCKED_BY_DEPENDENCY)

        # Step 2: Parent is retried and completes successfully
        self.task_manager.update_task_status(
            t_parent, TaskStatus.COMPLETED, result="Retrysuccess", expected_statuses=[TaskStatus.FAILED]
        )

        # Step 3: Child task should be automatically unblocked and restored to PENDING
        child_item = self.task_manager.get_task(t_child)
        self.assertEqual(child_item.status, TaskStatus.PENDING)
        self.assertEqual(child_item.error, "")

        # Child can now be picked up by scheduler!
        picked = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(picked)
        self.assertEqual(picked.task_id, t_child)

    def test_05_dynamic_concurrency_and_idempotency_checkpoint(self):
        """Dynamic concurrency adjustment and external action deduplication checkpoint."""
        # Dynamic concurrency
        self.task_manager.set_max_concurrent_tasks(8)
        self.assertEqual(self.task_manager.max_running_tasks, 8)
        with self.assertRaises(ValueError):
            self.task_manager.set_max_concurrent_tasks(0)

        # Checkpoint idempotency
        tid = self.task_manager.create_task("幂等性测试任务", "测试外部操作去重", "general")
        self.assertFalse(self.task_manager.is_action_executed(tid, "payment_charge_tx_123"))

        # Record action execution
        self.task_manager.record_checkpoint(tid, {"step": 1, "status": "charged"}, idempotency_key="payment_charge_tx_123")
        self.assertTrue(self.task_manager.is_action_executed(tid, "payment_charge_tx_123"))

        # Different action is still not executed
        self.assertFalse(self.task_manager.is_action_executed(tid, "email_send_tx_456"))

    # =========================================================================
    # P0-B: ExecutionContext, Tool Timeout Safety & Approval Isolation
    # =========================================================================
    def test_06_execution_context_unification(self):
        """Execution class provides unified context attributes:
        execution_id, budget, approval_scope, owner_id, cancel_event.
        """
        tid = self.task_manager.create_task("上下文测试", "详情", "general", owner_id="user_bob", token_budget=5000)
        event = threading.Event()
        exec_ctx = Execution(self.task_manager, tid, "run_test_123", event)

        self.assertEqual(exec_ctx.owner_id, "user_bob")
        self.assertEqual(exec_ctx.execution_id, f"exec_{tid}_run_test_123")
        self.assertEqual(exec_ctx.approval_scope, "task")
        self.assertEqual(exec_ctx.budget["token_budget"], 5000)
        self.assertEqual(exec_ctx.cancel_event, event)

    def test_07_tool_timeout_marks_thread_still_running_and_signals_cancel(self):
        """Tool timeout must NOT pretend operation stopped;
        it must set cancel_event, report thread_still_running=True, and clean up child processes.
        """
        registry = ToolRegistry.get_instance()

        cancelled_flag = threading.Event()
        def slow_tool_handler(args, ctx):
            cancel_evt = ctx.get("cancel_event")
            for _ in range(50):
                if cancel_evt and cancel_evt.is_set():
                    cancelled_flag.set()
                    return "Interrupted by cancel event"
                time.sleep(0.05)
            return "Finished slow tool"

        registry.register_tool(ToolDefinition(
            name="slow_tool_for_test",
            description="模拟耗时工具",
            parameters={"type": "object", "properties": {}},
            required_permission=ToolPermissionLevel.SAFE_READ,
            timeout_sec=0.2,
            handler=slow_tool_handler,
        ))

        res = registry.execute_tool("slow_tool_for_test", {}, task_id="t_timeout_test")
        self.assertFalse(res.success)
        self.assertTrue("执行超时" in res.error or "timed out" in res.error.lower())
        self.assertTrue(res.thread_still_running, "Must explicitly indicate thread may still be running")

        # Verify cancel event was triggered to cooperatively stop the worker
        time.sleep(0.15)
        self.assertTrue(cancelled_flag.is_set(), "Worker should receive cooperative cancellation signal")

    def test_08_approval_manager_strict_tenant_isolation(self):
        """User A's approval grants MUST NOT apply to User B (Strict Tenant Isolation)."""
        app_mgr = self.approval_manager

        # Set HIGH_RISK_COMMAND to ASK_ALWAYS
        app_mgr.set_category_policy(ApprovalCategory.HIGH_RISK_COMMAND, ApprovalPolicy.ASK_ALWAYS)

        # 1. User Alice requests approval for task A
        approved_alice, reason = app_mgr.check_and_request_approval(
            task_id="task_alice_1",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="执行高危命令",
            sync_wait=False,
            owner_id="alice",
        )
        self.assertFalse(approved_alice)
        reqs_alice = app_mgr.list_pending_requests(owner_id="alice")
        self.assertEqual(len(reqs_alice), 1)
        req_id_alice = reqs_alice[0].req_id

        # 2. User Bob attempts to approve Alice's request -> MUST BE REJECTED BY PERMISSION!
        with self.assertRaises(PermissionError):
            app_mgr.approve_request(req_id_alice, allowed_scope="always", approver_id="bob")

        # 3. Alice approves her own request with "always" (permanent grant for Alice)
        import uuid
        try:
            chan_alice = open_identity_channel(f"phase1-alice-{uuid.uuid4().hex[:6]}")
        except Exception:
            chan_alice = None
        ident_alice = chan_alice.identity("alice") if chan_alice else "alice"
        ok = app_mgr.approve_request(req_id_alice, allowed_scope="always", approver_id=ident_alice)
        self.assertTrue(ok)

        # 4. Now Alice's future tasks are pre-approved under her always grant
        alice_future_ok, reason = app_mgr.check_and_request_approval(
            task_id="task_alice_2",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="执行高危命令2",
            sync_wait=False,
            owner_id="alice",
        )
        self.assertTrue(alice_future_ok, f"Alice should be pre-approved: {reason}")

        # 5. User Bob MUST NOT inherit Alice's permanent grant! Bob must still require approval!
        bob_check_ok, reason_bob = app_mgr.check_and_request_approval(
            task_id="task_bob_1",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="Bob 执行相同高危命令",
            sync_wait=False,
            owner_id="bob",
        )
        self.assertFalse(bob_check_ok, "Bob MUST NOT inherit Alice's permanent grant!")
        self.assertTrue("待用户审批中" in reason_bob or "Pending user approval" in reason_bob)

        # 6. Revoke Alice's grant -> Alice is no longer pre-approved
        revoked_count = app_mgr.revoke_grant(owner_id="alice", category=ApprovalCategory.HIGH_RISK_COMMAND)
        self.assertTrue(revoked_count > 0)
        alice_after_revoke, _ = app_mgr.check_and_request_approval(
            task_id="task_alice_3",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="执行高危命令3",
            sync_wait=False,
            owner_id="alice",
        )
        self.assertFalse(alice_after_revoke, "Alice's grant should be revoked and require approval again")


if __name__ == "__main__":
    unittest.main()
