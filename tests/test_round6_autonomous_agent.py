"""Round 6 Comprehensive Autonomous Agent Test Suite.

Validates the full Dots-style autonomous execution loop:
- Test A: Real proactive event lifecycle (trigger -> capability gate -> task execution -> notification -> restart dedup -> trigger management).
- Test B: Continuous goal auto-advancement & WAITING_REVIEW revision resumption to ACTIVE.
- Test C: Notification reliability & delivery state machine (no sinks -> PENDING, registered -> auto-flush SENT, error tracking).
- Test D: Autonomous research workflow (hypothesis -> counterexample -> evidence -> dynamic next stage -> budget exhaustion stop).
- Test E: Multi-tenant memory permission authorization & path traversal defense.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Ensure Open-QQ-Dot root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory import AgentMemoryStore, MemoryScope
from agenda import EventType, ProactiveActionEngine, ProactiveLevel
from notification_manager import NotificationManager, NotificationMessage, NotificationStatus
from research_tracker import FindingStatus, ResearchTracker
from task_manager import TaskItem, TaskManager, TaskStatus


class TestRound6AutonomousAgent(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.task_db = self.data_dir / "tasks.sqlite3"
        self.notif_db = self.data_dir / "notifications.sqlite3"
        self.mem_db = self.data_dir / "memory.sqlite3"
        self.proactive_db = self.data_dir / "proactive_state.sqlite3"
        self.research_db = self.data_dir / "research_projects.sqlite3"

        self.task_manager = TaskManager(db_path=self.task_db, workspace=self.root)
        self.notif_manager = NotificationManager(db_path=self.notif_db)
        self.memory = AgentMemoryStore(db_path=self.mem_db)
        self.research_tracker = ResearchTracker(db_path=self.research_db)
        self.proactive_engine = ProactiveActionEngine(db_path=self.proactive_db)

        # Singletons patch
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

    # =========================================================================
    # TEST A: Real Proactive Event Lifecycle & Trigger Management
    # =========================================================================
    def test_a_real_proactive_event_lifecycle(self):
        """Test A:
        1. Built-in triggers registered idempotently.
        2. Trigger enable/disable/list APIs work accurately.
        3. Authorized file change triggers task creation and execution.
        4. Across process restart, cooldown fingerprint prevents redundant runs.
        """
        # 1. Register built-in triggers
        self.proactive_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)
        triggers = self.proactive_engine.list_triggers()
        trigger_names = [t["name"] for t in triggers]
        self.assertIn("builtin:timer_health", trigger_names)
        self.assertIn("builtin:goal_patrol", trigger_names)
        self.assertIn("builtin:task_completed_auditor", trigger_names)

        # 2. Trigger lifecycle management: disable and enable
        self.assertTrue(self.proactive_engine.disable_trigger("builtin:timer_health"))
        t_health = self.proactive_engine.get_trigger("builtin:timer_health")
        self.assertFalse(t_health["enabled"])
        self.assertTrue(self.proactive_engine.enable_trigger("builtin:timer_health"))
        t_health_again = self.proactive_engine.get_trigger("builtin:timer_health")
        self.assertTrue(t_health_again["enabled"])

        # 3. File change trigger with authorized owner
        test_file = self.root / "monitored_data.txt"
        test_file.write_text("initial content", encoding="utf-8")

        self.proactive_engine.register_file_watcher(
            target_path=test_file,
            on_change_task_title="监控文件数据变更",
            on_change_description="处理数据文件的增量变动",
            owner_id="user_alice",
            cooldown_sec=1800.0,
        )

        # Modify file to trigger change
        time.sleep(0.05)
        test_file.write_text("updated content with new rows", encoding="utf-8")

        dispatched = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched), 1)
        created_tid = dispatched[0]

        # Verify task is queued with explicit owner_id
        task = self.task_manager.get_task(created_tid)
        self.assertEqual(task.owner_id, "user_alice")
        self.assertEqual(task.status, TaskStatus.PENDING)

        # 4. Restart simulation: create a new engine instance pointing to the same SQLite
        restarted_engine = ProactiveActionEngine(db_path=self.proactive_db)
        restarted_engine.register_file_watcher(
            target_path=test_file,
            on_change_task_title="监控文件数据变更",
            on_change_description="处理数据文件的增量变动",
            owner_id="user_alice",
            cooldown_sec=1800.0,
        )
        # Without modifying the file, polling again must be suppressed
        second_poll = restarted_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(second_poll), 0)

    # =========================================================================
    # TEST B: Goal Auto-Advancement & WAITING_REVIEW Revision Resume
    # =========================================================================
    def test_b_goal_auto_advancement_and_revision_resumption(self):
        """Test B:
        1. 2-stage goal runs to completion -> reaches WAITING_REVIEW.
        2. Owner calls revise_goal to append Stage 3.
        3. Goal automatically resumes ACTIVE status and increments version.
        4. Worker executes the newly appended Stage 3 to completion.
        """
        def custom_data_executor(task: TaskItem, log_fn):
            return {
                "output": f"Done {task.title}",
                "data": {"status": "ok", "task_id": task.task_id},
                "tokens_used": 60,
                "usage_recorded": True,
            }

        self.task_manager.register_custom_executor("data", custom_data_executor)

        milestones = [
            {"title": "阶段一：特征工程", "description": "提取有效特征", "category": "data"},
            {"title": "stage: model", "description": "计算指标表现", "category": "data"},
        ]

        goal_id = self.task_manager.create_goal(
            description="客户流失预测分析",
            scope="仅限 data/ 目录分析",
            completion_criteria="评估指标AUC达到0.85以上",
            milestones=milestones,
            owner_id="user_alice",
            priority=85,
        )

        # Execute Stage 1
        t1 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(t1)
        self.task_manager._process_single_task(t1)
        self.assertEqual(self.task_manager.get_task(t1.task_id).status, TaskStatus.COMPLETED)

        # Worker picks Stage 2 automatically
        t2 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(t2)
        self.task_manager._process_single_task(t2)
        self.assertEqual(self.task_manager.get_task(t2.task_id).status, TaskStatus.COMPLETED)

        # All initial stages completed -> status must be WAITING_REVIEW
        g = self.task_manager.get_goal(goal_id)
        self.assertEqual(g["status"], "WAITING_REVIEW")
        self.assertEqual(g["progress"], 1.0)

        # User reviews and appends Stage 3 via revise_goal
        revised = self.task_manager.revise_goal(
            goal_id=goal_id,
            owner_id="user_alice",
            new_milestones=[{"title": "阶段三：部署监控探针", "description": "接入监控指标并告警", "category": "data"}],
            new_scope="允许写入探针配置",
        )
        self.assertTrue(revised)

        # Goal MUST automatically reactivate from WAITING_REVIEW back to ACTIVE
        g_revised = self.task_manager.get_goal(goal_id)
        self.assertEqual(g_revised["status"], "ACTIVE")
        self.assertEqual(len(g_revised["tasks"]), 3)
        self.assertTrue(g_revised.get("version", 1) >= 2)

        # Stage 3 must now be executable and unblocked
        t3 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(t3)
        self.assertEqual(t3.title, "阶段三：部署监控探针")
        self.task_manager._process_single_task(t3)
        self.assertEqual(self.task_manager.get_task(t3.task_id).status, TaskStatus.COMPLETED)

        # Now all 3 stages completed
        g_final = self.task_manager.get_goal(goal_id)
        self.assertEqual(g_final["progress"], 1.0)
        self.assertEqual(g_final["status"], "WAITING_REVIEW")

    # =========================================================================
    # TEST C: Notification Reliability & Delivery State Machine
    # =========================================================================
    def test_c_notification_reliability_and_real_sinks(self):
        """Test C:
        1. Without sinks registered, status remains PENDING and reason is recorded (NOT falsely marked SENT).
        2. When sink registers, pending notifications are automatically flushed.
        3. Failed sink dispatches track errors and retry counts.
        4. mark_as_read functions properly.
        """
        # Publish notification when NO sinks are registered
        nid = self.notif_manager.publish(
            title="队列任务就绪",
            content="请处理待办审批",
            recipient_id="user_bob",
            category="task",
        )

        notifs = self.notif_manager.list_recent(recipient_id="user_bob")
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0]["status"], NotificationStatus.PENDING.value)
        self.assertIn("暂无可用通知渠道", notifs[0]["error_reason"])

        # Now register a delivery sink (simulating QQ channel connection)
        delivered_msgs = []

        def qq_mock_sink(msg: NotificationMessage):
            delivered_msgs.append(msg)
            return True

        self.notif_manager.register_channel_sink(qq_mock_sink)

        # Sink registration automatically flushes the pending backlog
        self.assertEqual(len(delivered_msgs), 1)
        self.assertEqual(delivered_msgs[0].id, nid)

        # Database state must now be SENT
        updated_notifs = self.notif_manager.list_recent(recipient_id="user_bob")
        self.assertEqual(updated_notifs[0]["status"], NotificationStatus.SENT.value)
        self.assertTrue(len(updated_notifs[0]["sent_at"]) > 0)

        # Mark as read
        self.notif_manager.mark_as_read(nid)
        read_notifs = self.notif_manager.list_recent(recipient_id="user_bob", unread_only=True)
        self.assertEqual(len(read_notifs), 0)

        # Test failure tracking with faulty sink
        faulty_manager = NotificationManager()  # isolated transient instance

        def faulty_sink(msg: NotificationMessage):
            raise ConnectionError("QQ 网关连接超时")

        faulty_manager.register_channel_sink(faulty_sink)
        fid = faulty_manager.publish("网络告警", "failed", recipient_id="user_bob")

        f_records = faulty_manager.list_recent(recipient_id="user_bob")
        self.assertEqual(len(f_records), 1)
        self.assertEqual(f_records[0]["retry_count"], 1)
        self.assertIn("QQ 网关连接超时", f_records[0]["error_reason"])

    # =========================================================================
    # TEST D: Autonomous Research Workflow & Safety Limits
    # =========================================================================
    def test_d_autonomous_research_workflow(self):
        """Test D:
        1. Formulate hypotheses and record counterexample refutations.
        2. Advance verified stage and dynamically formulate next stage.
        3. Token budget enforcement stops runaway research cycles (BUDGET_EXHAUSTED).
        """
        # Ensure real task exists before creating research project
        tid_main = self.task_manager.create_task("主研究任务", "总课题", category="research", owner_id="user_alice")
        proj = self.research_tracker.create_research_project(
            project_id="res_collatz_01",
            topic="Collatz 猜想小范围数值实验",
            goal="验证 $2^{60}$ 范围内奇数变换步数收敛性",
            task_id=tid_main,
            initial_milestones=[{"title": "阶段一：奇数路径统计", "objective": "统计 1-10000 间停时分布"}],
            max_stages=3,
            budget_tokens=1000,
        )

        # 1. Hypotheses & Counterexample
        self.research_tracker.add_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis="假设所有素数路径不超过 100 步",
            rationale="经验试算假设",
        )
        self.research_tracker.record_counterexample(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis="假设所有素数路径不超过 100 步",
            counterexample="数字 27 经历 111 步才到达 1",
            conditions="n = 27",
        )

        # Verify evidence table reflects refutation
        evidence_list = self.research_tracker.list_evidence(proj.project_id)
        refuted_items = [e for e in evidence_list if e["status"] == FindingStatus.REFUTED.value]
        self.assertTrue(len(refuted_items) >= 1)

        # 2. Complete Stage 1 with verified evidence
        success, msg = self.research_tracker.record_stage_progress(
            project_id=proj.project_id,
            stage_index=1,
            new_findings=["已验证 1-10000 内所有奇数均在 261 步内收敛至 1"],
            negative_attempts=["尝试线性拟合步数失败，分布非单调"],
            tokens_consumed=400,
            stage_achieved=True,
            verification_status=FindingStatus.VERIFIED,
            sources=["collatz_sim.py"],
            evidence=["运行结果日志 checksum_ok"],
            reviewed_by="auditor_dr_chen",
        )
        self.assertTrue(success)

        # 3. Dynamic Next Stage Formulation via schedule_next_stage
        next_tid = self.research_tracker.schedule_next_stage(self.task_manager, proj.project_id)
        self.assertIsNotNone(next_tid)

        t_next = self.task_manager.get_task(next_tid)
        self.assertEqual(t_next.category, "research")
        self.assertIn("深入推导与实验扩展", t_next.title)

        # 4. Budget limit enforcement: consume tokens past budget
        self.research_tracker.record_stage_progress(
            project_id=proj.project_id,
            stage_index=2,
            new_findings=["阶段二初步结论"],
            negative_attempts=[],
            tokens_consumed=800,  # 400 + 800 = 1200 > 1000 budget
            stage_achieved=False,
            verification_status=FindingStatus.PRELIMINARY,
        )
        p_exhausted = self.research_tracker.get_project(proj.project_id)
        self.assertEqual(p_exhausted.status, "BUDGET_EXHAUSTED")

        # Scheduling next stage must now raise ValueError due to budget exhaustion
        with self.assertRaises(ValueError):
            self.research_tracker.schedule_next_stage(self.task_manager, proj.project_id)

    # =========================================================================
    # TEST E: Multi-Tenant Memory Authorization & Path Traversal Defense
    # =========================================================================
    def test_e_memory_authorization_and_path_defense(self):
        """Test E:
        1. System memories cannot be deleted or modified by regular users (PermissionError).
        2. Regular users cannot delete or modify other users' memories.
        3. Admins / System can update system memories.
        4. Malicious owner_id with path traversal (e.g. '../../etc') is rejected in task_runtime.
        5. ProactiveLevel 2 blocks shell actions when user disabled commands.
        """
        # Store system memory
        sys_mid = self.memory.store_memory(
            scope=MemoryScope.USER_PREFERENCE,
            owner_id="system",
            key="sys:security_policy",
            content="全站基准密码策略要求最少12位",
            importance=10,
        )

        # Store Alice private memory
        alice_mid = self.memory.store_memory(
            scope=MemoryScope.FRIEND_PRIVATE,
            owner_id="user_alice",
            key="profile:email",
            content="alice@corp.internal",
            importance=7,
        )

        # 1. Bob attempts to delete system memory -> MUST raise PermissionError
        with self.assertRaises(PermissionError):
            self.memory.delete_memory(sys_mid, owner_id="user_bob")

        # 2. Bob attempts to modify system memory -> MUST raise PermissionError
        with self.assertRaises(PermissionError):
            self.memory.update_memory_content(sys_mid, owner_id="user_bob", new_content="篡改后的密码策略")

        # 3. Bob attempts to delete Alice's private memory -> MUST raise PermissionError
        with self.assertRaises(PermissionError):
            self.memory.delete_memory(alice_mid, owner_id="user_bob")

        # 4. Alice modifies her own memory -> Success
        self.assertTrue(self.memory.update_memory_content(alice_mid, owner_id="user_alice", new_content="alice_new@corp.internal"))
        alice_entry = self.memory.get_scoped_memories(scope=MemoryScope.FRIEND_PRIVATE, owner_id="user_alice")
        self.assertEqual(alice_entry[0].content, "alice_new@corp.internal")

        # 5. Admin updates system memory -> Success
        self.assertTrue(self.memory.update_memory_content(sys_mid, owner_id="admin", new_content="管理员更新系统策略", is_admin=True))

        # 6. Path Traversal Defense in task_runtime
        from task_runtime import Execution
        from threading import Event
        malicious_task_id = self.task_manager.create_task(
            title="黑客任务",
            description="尝试路径穿越",
            owner_id="../../windows/system32",
        )
        exec_ctx = Execution(self.task_manager, malicious_task_id, "run_01", Event())
        with self.assertRaises(PermissionError):
            exec_ctx.friend_directory()

        # 7. Level 2 blocks shell commands when commands disabled
        # Create user_carol with commands disabled in friend.json
        carol_dir = self.root / "friends" / "user_carol"
        carol_dir.mkdir(parents=True, exist_ok=True)
        (carol_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": False}
        }), encoding="utf-8")

        self.proactive_engine.register_trigger(
            event_type=EventType.PERIODIC,
            name="test_shell_trigger",
            condition_fn=lambda: {"action": "clean_tmp"},
            action_plan_fn=lambda data: {
                "title": "执行 Shell 清理脚本",
                "description": "rm -rf /tmp/cache",
                "category": "code",
                "owner_id": "user_carol",
                "requires_shell": True,
            },
            cooldown_sec=300.0,
            owner_id="user_carol",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # Polling under Level 2 must block the shell task execution
        blocked_ids = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(blocked_ids), 0)

        # But warning notification must have been dispatched
        carol_notifs = self.notif_manager.list_recent(recipient_id="user_carol")
        self.assertTrue(len(carol_notifs) >= 1)
        self.assertTrue("Authorize Shell " in carol_notifs[0]["content"] or "未授权 Shell" in carol_notifs[0]["content"])


if __name__ == "__main__":
    unittest.main()
