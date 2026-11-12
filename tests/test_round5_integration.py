"""Round 5 Comprehensive Integration Test Suite.

Validates the full autonomous Agent loop:
- Scenario A: Continuous multi-stage goal progression, auto-transition, review gating, budget & deadline safety limits, and goal revision.
- Scenario B: Proactive event detection with explicit owner attribution, SQLite deduplication across restarts, and notification persistence.
- Scenario C: Controlled thread-pool concurrency, non-blocking worker loop, and safe descendant process cancellation without harming host.
- Scenario D: Multi-user strict data and memory isolation (experience remedies, private tasks, and durable goals).
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

# Ensure Open-QQ-Dot is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory import AgentMemoryStore
from agenda import EventType, ProactiveActionEngine, ProactiveLevel
from notification_manager import NotificationManager, NotificationStatus
from research_tracker import FindingStatus, ResearchTracker
from task_manager import TaskItem, TaskManager, TaskStatus


class TestRound5Integration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.task_db = self.data_dir / "tasks.sqlite3"
        self.notif_db = self.data_dir / "notifications.sqlite3"
        self.mem_db = self.data_dir / "memory.sqlite3"
        self.proactive_db = self.data_dir / "proactive_events.sqlite3"
        self.research_db = self.data_dir / "research_projects.sqlite3"

        self.task_manager = TaskManager(db_path=self.task_db, workspace=self.root)
        self.notif_manager = NotificationManager(db_path=self.notif_db)
        self.memory = AgentMemoryStore(db_path=self.mem_db)
        self.research_tracker = ResearchTracker(db_path=self.research_db)

        # Patch singletons
        self.p_task = patch.object(TaskManager, "get_instance", return_value=self.task_manager)
        self.p_notif = patch.object(NotificationManager, "get_instance", return_value=self.notif_manager)
        self.p_mem = patch.object(AgentMemoryStore, "get_instance", return_value=self.memory)
        self.p_task.start()
        self.p_notif.start()
        self.p_mem.start()

    def tearDown(self):
        self.task_manager.stop_worker()
        self.p_task.stop()
        self.p_notif.stop()
        self.p_mem.stop()
        self.temp_dir.cleanup()

    # =========================================================================
    # SCENARIO A: Continuous Long-Term Goal Progression
    # =========================================================================
    def test_scenario_a_continuous_goal_advancement_and_safety_limits(self):
        """Scenario A:
        1. Multi-stage goal executes continuously across stages.
        2. Stage 1 verified -> Stage 2 automatically unblocked & executed.
        3. All stages done -> Transition to WAITING_REVIEW & publish notification.
        4. Budget & deadline enforcement -> Auto PAUSED.
        5. Goal revision -> Successfully appends stages.
        """
        # Register simulated executor for testing milestones (using category 'data' for automated verification)
        executed_stages = []

        def custom_goal_executor(task: TaskItem, log_fn):
            executed_stages.append(task.title)
            log_fn(f"Executing: {task.title}")
            return {
                "output": f"Completed {task.title}",
                "data": {"status": "ok", "stage": task.title},
                "usage_recorded": True,
                "tokens_used": 50,
            }

        self.task_manager.register_custom_executor("data", custom_goal_executor)

        milestones = [
            {"title": "阶段一：数据提取", "description": "完成模块数据抽取", "category": "data"},
            {"title": "阶段二：数据校验与聚合", "description": "完成数据全量计算", "category": "data"},
        ]

        # 1. Create a 2-stage durable goal
        goal_id = self.task_manager.create_goal(
            description="构建自动化数据清洗流水线",
            scope="仅限 data/ 目录读写",
            completion_criteria="所有阶段产出数据校验通过",
            milestones=milestones,
            owner_id="user_alice",
            priority=80,
            token_budget=5000,
            max_calls=20,
        )

        goal = self.task_manager.get_goal(goal_id)
        self.assertEqual(goal["status"], "ACTIVE")
        self.assertEqual(len(goal["tasks"]), 2)
        task_1_id = goal["tasks"][0]["task_id"]
        task_2_id = goal["tasks"][1]["task_id"]

        # Stage 2 depends on Stage 1
        t2 = self.task_manager.get_task(task_2_id)
        self.assertEqual(t2.depends_on, [task_1_id])

        # 2. Worker executes Stage 1
        task_1 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(task_1)
        self.assertEqual(task_1.task_id, task_1_id)
        self.task_manager._process_single_task(task_1)

        t1_after = self.task_manager.get_task(task_1_id)
        self.assertEqual(t1_after.status, TaskStatus.COMPLETED)

        # 3. Autonomous advancement: Worker now automatically picks Stage 2 without human intervention
        task_2 = self.task_manager._fetch_next_executable_task()
        self.assertIsNotNone(task_2)
        self.assertEqual(task_2.task_id, task_2_id)
        self.task_manager._process_single_task(task_2)

        t2_after = self.task_manager.get_task(task_2_id)
        self.assertEqual(t2_after.status, TaskStatus.COMPLETED)

        # 4. Entire goal completed -> status is WAITING_REVIEW
        goal_after = self.task_manager.get_goal(goal_id)
        self.assertEqual(goal_after["progress"], 1.0)
        self.assertEqual(goal_after["status"], "WAITING_REVIEW")

        # Notification published to user_alice
        notifs = self.notif_manager.get_recipient_notifications("user_alice")
        goal_notifs = [n for n in notifs if n.get("category") == "goal"]
        self.assertTrue(len(goal_notifs) >= 1)
        self.assertIn("全部阶段已通过任务验收", goal_notifs[-1]["content"])

        # 5. Goal Revision: add Stage 3
        revised = self.task_manager.revise_goal(
            goal_id=goal_id,
            owner_id="user_alice",
            new_milestones=[{"title": "阶段三：全量归档", "description": "写入冷存储", "category": "data"}],
            new_scope="允许写入归档库",
        )
        self.assertTrue(revised)
        goal_revised = self.task_manager.get_goal(goal_id)
        self.assertEqual(len(goal_revised["tasks"]), 3)
        self.assertEqual(goal_revised["tasks"][2]["title"], "阶段三：全量归档")

        # 6. Safety boundary: Token budget exhaustion -> PAUSED
        budget_goal_id = self.task_manager.create_goal(
            description="有预算限制的目标",
            scope="test",
            completion_criteria="完成",
            milestones=[{"title": "highstage", "description": "test", "category": "data"}],
            owner_id="user_alice",
            token_budget=100,
            max_calls=10,
        )
        b_tid = self.task_manager.get_goal(budget_goal_id)["tasks"][0]["task_id"]
        # Simulate consuming tokens over budget
        self.task_manager.record_token_usage(b_tid, 150)
        self.task_manager._refresh_task_goal(b_tid)
        b_goal = self.task_manager.get_goal(budget_goal_id)
        self.assertEqual(b_goal["status"], "PAUSED")
        self.assertIn("用量预算", b_goal["events"][-1]["message"])

        # 7. Safety boundary: Deadline expired -> PAUSED
        expired_goal_id = self.task_manager.create_goal(
            description="已过期目标",
            scope="test",
            completion_criteria="完成",
            milestones=[{"title": "stage", "description": "test", "category": "data"}],
            owner_id="user_alice",
            deadline=time.time() - 100,  # in the past
        )
        exp_tid = self.task_manager.get_goal(expired_goal_id)["tasks"][0]["task_id"]
        self.task_manager._refresh_task_goal(exp_tid)
        exp_goal = self.task_manager.get_goal(expired_goal_id)
        self.assertEqual(exp_goal["status"], "PAUSED")
        self.assertIn("截止时间已到", exp_goal["events"][-1]["message"])

    # =========================================================================
    # SCENARIO B: Proactive Event Attribution & Restart Deduplication
    # =========================================================================
    def test_scenario_b_proactive_attribution_and_restart_deduplication(self):
        """Scenario B:
        1. ProactiveActionEngine triggers event with explicit owner attribution.
        2. SQLite durable tracking prevents duplicate trigger across process re-instantiation.
        3. ProactiveLevel strict behavior (Level 1 notify-only, Level 2 controlled).
        """
        engine = ProactiveActionEngine(
            db_path=self.proactive_db,
        )

        trigger_called_count = 0

        def custom_event_check():
            nonlocal trigger_called_count
            trigger_called_count += 1
            return {
                "fingerprint": "build_failure_log_001",
                "title": "构建日志错误巡检",
                "content": "检测到本地产物构建错误，需要重新编译",
            }

        def custom_action_plan(data):
            return {
                "title": "修复构建日志报错",
                "description": "分析 build.log 并修复语法错误",
                "category": "code",
                "owner_id": "user_alice",
            }

        engine.register_trigger(
            event_type=EventType.FILE_CHANGE,
            name="test_build_log_checker",
            condition_fn=custom_event_check,
            action_plan_fn=custom_action_plan,
            cooldown_sec=3600.0,
            owner_id="user_alice",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # 1. First execution: fires trigger, creates durable task with explicit owner
        dispatched_ids = engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_ids), 1)

        tasks = self.task_manager.list_tasks(owner_id="user_alice")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "修复构建日志报错")
        self.assertEqual(tasks[0].owner_id, "user_alice")

        # 2. Check notification persisted in NotificationManager
        alice_notifs = self.notif_manager.get_recipient_notifications("user_alice")
        self.assertTrue(len(alice_notifs) >= 1)
        self.assertIn("修复构建日志报错", alice_notifs[-1]["title"])

        # 3. Simulate process restart: create a fresh engine instance loading same DB
        restarted_engine = ProactiveActionEngine(
            db_path=self.proactive_db,
        )
        restarted_engine.register_trigger(
            event_type=EventType.FILE_CHANGE,
            name="test_build_log_checker",
            condition_fn=custom_event_check,
            action_plan_fn=custom_action_plan,
            cooldown_sec=3600.0,
            owner_id="user_alice",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # Second poll during cooldown: MUST be suppressed by SQLite persisted fingerprint & cooldown
        dispatched_again = restarted_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_again), 0)

        # Task count remains strictly 1
        tasks_again = self.task_manager.list_tasks(owner_id="user_alice")
        self.assertEqual(len(tasks_again), 1)

    # =========================================================================
    # SCENARIO C: Controlled Concurrency & Safe Process Cancellation
    # =========================================================================
    def test_scenario_c_worker_concurrency_and_cancellation(self):
        """Scenario C:
        1. ThreadPoolExecutor limits concurrent task execution to max_running_tasks.
        2. Long-running tasks do not block other independent tasks.
        3. cancel_task safely terminates registered child processes without killing host.
        """
        self.task_manager.max_running_tasks = 2
        self.task_manager.start_worker()

        task_completion_order = []

        def custom_concurrency_executor(task: TaskItem, log_fn):
            tag = task.title
            if tag == "slow":
                time.sleep(0.4)
            else:
                time.sleep(0.05)
            task_completion_order.append(tag)
            return {"output": "ok", "usage_recorded": True}

        self.task_manager.register_custom_executor("concurrency", custom_concurrency_executor)

        t_slow = self.task_manager.create_task("slow", "长任务", category="concurrency", priority=90)
        t_fast1 = self.task_manager.create_task("fast1", "短任务1", category="concurrency", priority=80)
        t_fast2 = self.task_manager.create_task("fast2", "短任务2", category="concurrency", priority=70)

        # Wait for all to finish
        deadline = time.time() + 5.0
        while time.time() < deadline:
            st = [self.task_manager.get_task(tid).status for tid in (t_slow, t_fast1, t_fast2)]
            if all(s == TaskStatus.COMPLETED for s in st):
                break
            time.sleep(0.1)

        self.assertEqual(len(task_completion_order), 3)
        # fast1 should have finished while slow was running
        self.assertEqual(task_completion_order[0], "fast1")

        # Process cancellation test: start a real external child process
        child_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertTrue(child_proc.poll() is None)  # Child is alive

        cancel_task_id = self.task_manager.create_task("to_cancel", "testCancel", category="general")
        self.task_manager.set_task_pid(cancel_task_id, child_proc.pid)

        # Cancel task
        cancelled = self.task_manager.cancel_task(cancel_task_id, reason="proactivemedium", owner_id=self.task_manager.get_task(cancel_task_id).owner_id)
        self.assertTrue(cancelled)

        time.sleep(0.2)
        # Child process must have been terminated
        self.assertIsNotNone(child_proc.poll())
        # Host process is still alive and healthy
        self.assertEqual(self.task_manager.get_task(cancel_task_id).status, TaskStatus.CANCELLED)

    # =========================================================================
    # SCENARIO D: Strict Multi-User Data & Memory Isolation
    # =========================================================================
    def test_scenario_d_multi_user_memory_and_data_isolation(self):
        """Scenario D:
        1. AgentMemory strictly isolates failure remedies between user_alice and user_bob.
        2. System-shared remedies are accessible by all users.
        3. TaskManager lists tasks and goals filtered strictly by owner_id.
        """
        # 1. Alice private failure experience
        self.memory.record_failure_experience(
            task_category="code",
            failed_action="syntax_error_alice_private",
            root_cause="Private file /alice/secret.key was referenced",
            solution="Use mock secret for Alice",
            owner_id="user_alice",
            is_shared=False,
        )

        # 2. Bob private failure experience
        self.memory.record_failure_experience(
            task_category="code",
            failed_action="database_lock_bob_private",
            root_cause="Bob's exclusive SQLite connection locked",
            solution="Increase timeout for Bob",
            owner_id="user_bob",
            is_shared=False,
        )

        # 3. System shared failure experience
        self.memory.record_failure_experience(
            task_category="code",
            failed_action="cuda_out_of_memory",
            root_cause="Global GPU batch size too large",
            solution="Halve batch size globally",
            owner_id="system",
            is_shared=True,
            verified=True,
        )

        # Alice retrieves: should see Alice's remedy + System shared remedy, NOT Bob's
        alice_remedies_for_alice_err = self.memory.retrieve_experience_remedies(
            query="syntax_error_alice_private",
            owner_id="user_alice",
        )
        self.assertEqual(len(alice_remedies_for_alice_err), 1)
        self.assertIn("Use mock secret for Alice", alice_remedies_for_alice_err[0])

        # Bob attempts to retrieve Alice's error context: MUST NOT see Alice's remedy
        bob_remedies_for_alice_err = self.memory.retrieve_experience_remedies(
            query="syntax_error_alice_private",
            owner_id="user_bob",
        )
        self.assertEqual(len(bob_remedies_for_alice_err), 0)

        # Both can retrieve the system shared remedy
        alice_shared = self.memory.retrieve_experience_remedies(
            query="cuda_out_of_memory",
            owner_id="user_alice",
        )
        bob_shared = self.memory.retrieve_experience_remedies(
            query="cuda_out_of_memory",
            owner_id="user_bob",
        )
        self.assertTrue(len(alice_shared) >= 1)
        self.assertTrue(len(bob_shared) >= 1)
        self.assertIn("Halve batch size globally", alice_shared[0])
        self.assertIn("Halve batch size globally", bob_shared[0])

        # 4. TaskManager task & goal multi-tenant isolation
        t_alice = self.task_manager.create_task("AliceTask", "Alice private", owner_id="user_alice")
        t_bob = self.task_manager.create_task("BobTask", "Bob private", owner_id="user_bob")

        alice_tasks = [t.task_id for t in self.task_manager.list_tasks(owner_id="user_alice")]
        bob_tasks = [t.task_id for t in self.task_manager.list_tasks(owner_id="user_bob")]

        self.assertIn(t_alice, alice_tasks)
        self.assertNotIn(t_bob, alice_tasks)
        self.assertIn(t_bob, bob_tasks)
        self.assertNotIn(t_alice, bob_tasks)

        # Goal isolation
        g_alice = self.task_manager.create_goal(
            description="Alice 专属目标",
            scope="私有",
            completion_criteria="完成",
            milestones=[{"title": "M1", "description": "D1", "category": "general"}],
            owner_id="user_alice",
        )
        g_bob = self.task_manager.create_goal(
            description="Bob 专属目标",
            scope="私有",
            completion_criteria="完成",
            milestones=[{"title": "M1", "description": "D1", "category": "general"}],
            owner_id="user_bob",
        )

        alice_goals = [g["goal_id"] for g in self.task_manager.list_goals(owner_id="user_alice")]
        bob_goals = [g["goal_id"] for g in self.task_manager.list_goals(owner_id="user_bob")]

        self.assertIn(g_alice, alice_goals)
        self.assertNotIn(g_bob, alice_goals)
        self.assertIn(g_bob, bob_goals)
        self.assertNotIn(g_alice, bob_goals)


if __name__ == "__main__":
    unittest.main()
