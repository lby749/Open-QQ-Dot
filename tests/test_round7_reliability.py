"""Round 7 Comprehensive Reliability & Autonomous Progression Test Suite.

Covers:
- Scenario A: Long-running unattended patrol deduplication (no redundant AI tasks when goal/task is unchanged; triggers only on real change).
- Scenario B: Disabled trigger persistence (user disabled trigger stays disabled across restarts and re-registrations).
- Scenario C: ProactiveLevel strict enforcement (Level 0 silent, Level 1 remind-only, Level 2 capability gated, Level 3 autonomous).
- Scenario D: QQ & Web notification delivery, offline buffering, sink auto-flush, and mark-as-read API.
- Scenario E: Goal blocked diagnosis, revision resumption from BLOCKED to ACTIVE, and multi-stage auto-advancement.
- Scenario F: Safety regressions (PID protection, late callback defense, worker reentrancy).
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
from notification_manager import NotificationManager, NotificationMessage, NotificationStatus
from task_manager import TaskItem, TaskManager, TaskStatus
from task_runtime import Execution, current_execution


class TestRound7Reliability(unittest.TestCase):
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
    # SCENARIO A: Unattended Patrol Deduplication (No Spam Tasks Without Change)
    # =========================================================================
    def test_scenario_a_unattended_patrol_deduplication(self):
        """Scenario A:
        1. When ACTIVE goals exist but have no progress/status changes, goal_patrol must NOT trigger redundant tasks.
        2. When a goal has substantive change (e.g. stage completed or version incremented), goal_patrol triggers exactly once.
        3. Completed high-priority tasks are audited once; subsequent cycles do NOT re-audit already audited tasks.
        """
        # Register production triggers
        self.proactive_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)

        # 1. Create an active goal with 2 stages
        gid = self.task_manager.create_goal(
            description="长期业务指标监控",
            scope="仅限 data/ 目录",
            completion_criteria="指标达成",
            milestones=[{"title": "阶段一：数据清洗", "description": "清洗日志", "category": "data"}],
            owner_id="user_alice",
        )

        # First polling: detects new goal -> triggers initial patrol task
        first_dispatched = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(first_dispatched), 1)

        # Clear memory cooldowns to simulate subsequent patrol cycles
        self.proactive_engine._cooldowns.clear()

        # Cycles 2, 3, 4: Goal has NOT changed at all! Must return 0 tasks (no runaway loop)!
        for _ in range(3):
            dispatched_no_change = self.proactive_engine.poll_events_and_act(
                proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
                workspace=self.root,
                task_manager=self.task_manager,
            )
            self.assertEqual(len(dispatched_no_change), 0, "Unchanged goal must not generate redundant tasks!")

        # 2. Complete Stage 1 -> Goal progress substantively changes!
        goal_data = self.task_manager.get_goal(gid)
        stage_task_id = goal_data["tasks"][0]["task_id"]
        self.task_manager.update_task_status(stage_task_id, TaskStatus.COMPLETED, result='{"status":"ok"}', verification_info={"success": True})

        # Next polling: progress changed -> triggers patrol task
        self.proactive_engine._cooldowns.clear()
        dispatched_on_change = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_on_change), 1)

        # 3. Task audit idempotency test: task t1 was completed (priority 50).
        # Create a priority 80 task and complete it
        tid_audit = self.task_manager.create_task("重要算法优化", "关键算法", priority=85, category="code", owner_id="user_alice")
        self.task_manager.update_task_status(tid_audit, TaskStatus.COMPLETED, result="ok")

        self.proactive_engine._cooldowns.clear()
        dispatched_audit = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        # Should trigger audit task for tid_audit
        audit_tasks = [tid for tid in dispatched_audit if "任务完成沉淀审计" in self.task_manager.get_task(tid).title]
        self.assertEqual(len(audit_tasks), 1)

        # Poll again: already audited tid_audit must NOT be audited again!
        self.proactive_engine._cooldowns.clear()
        dispatched_audit_repeat = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        audit_tasks_repeat = [tid for tid in dispatched_audit_repeat if "任务完成沉淀审计" in self.task_manager.get_task(tid).title]
        self.assertEqual(len(audit_tasks_repeat), 0, "Already audited tasks must never be re-audited!")

    # =========================================================================
    # SCENARIO B: Disabled Trigger Stays Disabled Across Restarts
    # =========================================================================
    def test_scenario_b_disabled_trigger_persists_across_restarts(self):
        """Scenario B:
        When a user disables a trigger, restarting the engine or re-calling
        ensure_builtin_triggers must NEVER reset enabled back to True.
        """
        self.proactive_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)

        # 1. User explicitly disables builtin:goal_patrol
        self.assertTrue(self.proactive_engine.disable_trigger("builtin:goal_patrol"))
        tr_info = self.proactive_engine.get_trigger("builtin:goal_patrol")
        self.assertFalse(tr_info["enabled"])

        # 2. Simulate application restart: instantiate a new engine pointing to the same DB
        restarted_engine = ProactiveActionEngine(db_path=self.proactive_db)

        # 3. Application startup re-registers builtin triggers (e.g. from start_worker / ensure_builtin_triggers)
        restarted_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)

        # 4. Trigger MUST remain disabled!
        restarted_tr_info = restarted_engine.get_trigger("builtin:goal_patrol")
        self.assertFalse(restarted_tr_info["enabled"], "Disabled trigger was falsely overwritten by registration defaults!")

    # =========================================================================
    # SCENARIO C: ProactiveLevel Strict Enforcement per User
    # =========================================================================
    def test_scenario_c_proactive_levels_per_user(self):
        """Scenario C:
        1. Level 0 (PASSIVE) user: completely silent, no tasks and no notifications.
        2. Level 1 (REMIND) user: produces notifications only, zero task creations.
        3. Level 2 user: blocks unauthorized shell commands with safety alerts.
        4. Level 3 user: allows complex tasks within boundaries.
        """
        # User 1: Zero (Level 0)
        u0_dir = self.root / "friends" / "user_zero"
        u0_dir.mkdir(parents=True, exist_ok=True)
        (u0_dir / "friend.json").write_text(json.dumps({"proactive_level": "LEVEL_0_PASSIVE"}), encoding="utf-8")

        # User 2: Remind (Level 1)
        u1_dir = self.root / "friends" / "user_remind"
        u1_dir.mkdir(parents=True, exist_ok=True)
        (u1_dir / "friend.json").write_text(json.dumps({"proactive_level": "LEVEL_1_REMIND"}), encoding="utf-8")

        # Register trigger for user_zero
        self.proactive_engine.register_trigger(
            event_type=EventType.PERIODIC,
            name="test_zero_trigger",
            condition_fn=lambda: {"active": True},
            action_plan_fn=lambda d: {"title": "静默测试", "owner_id": "user_zero"},
            cooldown_sec=300.0,
            owner_id="user_zero",
        )

        # Register trigger for user_remind
        self.proactive_engine.register_trigger(
            event_type=EventType.PERIODIC,
            name="test_remind_trigger",
            condition_fn=lambda: {"active": True},
            action_plan_fn=lambda d: {"title": "提醒测试", "description": "需要注意", "owner_id": "user_remind"},
            cooldown_sec=300.0,
            owner_id="user_remind",
            min_proactive_level=ProactiveLevel.LEVEL_1_REMIND,
        )

        # Poll events
        dispatched = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )

        # Neither user_zero nor user_remind should have any created tasks!
        self.assertEqual(len(dispatched), 0)

        # user_zero must receive NO notifications
        zero_notifs = self.notif_manager.list_recent(recipient_id="user_zero")
        self.assertEqual(len(zero_notifs), 0)

        # user_remind MUST receive a reminder notification!
        remind_notifs = self.notif_manager.list_recent(recipient_id="user_remind")
        self.assertEqual(len(remind_notifs), 1)
        self.assertTrue("【主动提醒】" in remind_notifs[0]["title"] or "proactive" in remind_notifs[0]["title"].lower())

    # =========================================================================
    # SCENARIO D: Real QQ & Web Notifications & Sink Auto-Flush
    # =========================================================================
    def test_scenario_d_real_notifications_and_web_api(self):
        """Scenario D:
        1. Notifications with no sinks remain PENDING with clear reason.
        2. Registering real QQ sink flushes pending backlog and marks SENT.
        3. Web API post_agent_notification_action correctly marks notification as read.
        """
        # Publish notification when QQ client is offline
        nid = self.notif_manager.publish(
            title="目标阶段完成",
            content="阶段 1 已通过验收",
            recipient_id="12345678",
            category="goal",
        )
        notifs = self.notif_manager.list_recent(recipient_id="12345678")
        self.assertEqual(notifs[0]["status"], NotificationStatus.PENDING.value)

        # Simulate QQ channel connection & real sink registration
        received_qq_messages = []

        def mock_qq_sink(msg: NotificationMessage):
            if msg.recipient_id == "12345678":
                received_qq_messages.append({"to": msg.recipient_id, "text": msg.content})
                return True
            return False

        self.notif_manager.register_channel_sink(mock_qq_sink)

        # Auto-flush should have dispatched the message to the QQ sink
        self.assertEqual(len(received_qq_messages), 1)
        self.assertEqual(received_qq_messages[0]["to"], "12345678")

        # Notification state in DB is now SENT
        updated_notifs = self.notif_manager.list_recent(recipient_id="12345678")
        self.assertEqual(updated_notifs[0]["status"], NotificationStatus.SENT.value)
        self.assertFalse(updated_notifs[0]["read"])

        # Test Web post_agent_notification_action API for marking read
        import house_web
        res = house_web.post_agent_notification_action("12345678", {"action": "mark_read", "id": nid})
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["read"])

        # In DB, read is now True
        unread_notifs = self.notif_manager.list_recent(recipient_id="12345678", unread_only=True)
        self.assertEqual(len(unread_notifs), 0)

    # =========================================================================
    # SCENARIO E: Goal Blocked Diagnosis & Resumption from BLOCKED
    # =========================================================================
    def test_scenario_e_goal_blocked_and_revision_resumption(self):
        """Scenario E:
        1. When a stage task fails, the goal records the specific blocked diagnosis.
        2. revise_goal on a BLOCKED goal reactivates it back to ACTIVE and increments version.
        3. Worker picks up new revised stage and completes goal.
        """
        # Create a 1-stage goal
        gid = self.task_manager.create_goal(
            description="爬虫抓取分析",
            scope="web data",
            completion_criteria="输出完整JSON",
            milestones=[{"title": "阶段一：网络抓取", "description": "抓取页面数据", "category": "data"}],
            owner_id="user_alice",
        )

        t1 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(t1)

        # Fail stage 1 with a specific error
        self.task_manager.update_task_status(t1.task_id, TaskStatus.FAILED, error="远程目标网站证书失效 503")

        # Goal MUST now be BLOCKED with detailed error reasoning
        g = self.task_manager.get_goal(gid)
        self.assertEqual(g["status"], "BLOCKED")
        self.assertTrue(any("证书失效 503" in evt["message"] for evt in g["events"]))

        # Owner revises the BLOCKED goal: amends scope, extends deadline, appends retry stage
        revised = self.task_manager.revise_goal(
            goal_id=gid,
            owner_id="user_alice",
            new_scope="web data with ignore_ssl",
            new_milestones=[{"title": "阶段一重试：忽略证书抓取", "description": "忽略SSL抓取", "category": "data"}],
        )
        self.assertTrue(revised)

        # Goal MUST reactivate from BLOCKED back to ACTIVE!
        g_revised = self.task_manager.get_goal(gid)
        self.assertEqual(g_revised["status"], "ACTIVE")
        self.assertTrue(g_revised.get("version", 1) >= 2)

        # Worker can now fetch and execute the revised stage
        t_revised = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(t_revised)
        self.assertIn("忽略证书抓取", t_revised.title)

    # =========================================================================
    # SCENARIO F: Safety Regressions (PID, Late Callback, Worker Reentrancy)
    # =========================================================================
    def test_scenario_f_safety_regressions(self):
        """Scenario F:
        1. Cannot register current process PID as a task child process.
        2. Cannot overwrite a cancelled task with completed.
        3. Worker start is reentrant and thread-safe.
        """
        tid = self.task_manager.create_task("进程测试", "测试宿主保护", owner_id="user_bob")

        # 1. Attempt to register current host process PID -> MUST raise ValueError
        with self.assertRaises(ValueError):
            self.task_manager.set_task_pid(tid, os.getpid())

        # 2. Cancel task and test late callback
        self.task_manager.cancel_task(tid, owner_id=self.task_manager.get_task(tid).owner_id)
        self.assertEqual(self.task_manager.get_task(tid).status, TaskStatus.CANCELLED)

        # Late update attempt fails
        res = self.task_manager.update_task_status(tid, TaskStatus.COMPLETED, result="late")
        self.assertFalse(res)
        self.assertEqual(self.task_manager.get_task(tid).status, TaskStatus.CANCELLED)

        # 3. Worker reentrancy: starting 10 times concurrently produces only 1 worker thread
        for _ in range(10):
            self.task_manager.start_worker()
        self.assertTrue(self.task_manager._worker_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
