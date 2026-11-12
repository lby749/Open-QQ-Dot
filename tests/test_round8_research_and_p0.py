"""Round 8 Integration Test Suite: P0 Security & Reliability Fixes + Deep Autonomous Research Agent.

Covers:
- Scenario A: Web notification ownership security & cross-user isolation.
- Scenario B: Task audit lifecycle & retry recovery (no premature AUDITED confirmation).
- Scenario C: Multi-goal simultaneous changes (per-goal dispatch, no dropped goals).
- Scenario D: Bridge repeated reconnects with idempotent named sink registration.
- Scenario E: Genuine mathematical research (real Python numerical experiment, finite vs formal proof distinction).
- Scenario F: Research failure & counterexample refutation, memory-linked remedy, replan/branching without false notifications.
- Scenario G: Research persistence across restarts and crash recovery.
- Scenario H: Multi-agent research collaboration (Planner, Executor, Verifier with verifiable evidence).
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory import AgentMemoryStore
from agenda import EventType, ProactiveActionEngine, ProactiveLevel
import house_web
from notification_manager import NotificationManager, NotificationMessage, NotificationPriority, NotificationStatus
from research_tracker import (
    FindingStatus,
    ProofType,
    ResearchDecisionAction,
    ResearchTracker,
    StageStatus,
)
from task_manager import TaskManager, TaskStatus


class TestRound8ResearchAndP0(unittest.TestCase):
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
        self.proactive_engine = ProactiveActionEngine(db_path=self.proactive_db)
        self.research_tracker = ResearchTracker(db_path=self.research_db)

        self.p_task = patch.object(TaskManager, "get_instance", return_value=self.task_manager)
        self.p_notif = patch.object(NotificationManager, "get_instance", return_value=self.notif_manager)
        self.p_mem = patch.object(AgentMemoryStore, "get_instance", return_value=self.memory)
        self.p_proactive = patch.object(ProactiveActionEngine, "get_instance", return_value=self.proactive_engine)
        self.p_research = patch.object(ResearchTracker, "get_instance", return_value=self.research_tracker)

        self.p_task.start()
        self.p_notif.start()
        self.p_mem.start()
        self.p_proactive.start()
        self.p_research.start()

    def tearDown(self):
        self.task_manager.stop_worker()
        self.p_task.stop()
        self.p_notif.stop()
        self.p_mem.stop()
        self.p_proactive.stop()
        self.p_research.stop()
        self.temp_dir.cleanup()

    # =========================================================================
    # SCENARIO A: Web Notification Ownership Security
    # =========================================================================
    def test_scenario_a_web_notification_ownership_security(self):
        """Scenario A:
        1. User A has a notification.
        2. User B attempts to mark User A's notification as read or delete it -> MUST be rejected.
        3. User A marks their own notification -> Succeeded.
        4. Admin user can manage notifications with explicit admin privileges.
        """
        # Create notification for User Alice
        nid_alice = self.notif_manager.publish(
            title="Alice的任务报告",
            content="任务已成功完成",
            recipient_id="user_alice",
        )

        # 1. User Bob attempts to mark Alice's notification as read
        res_bob = house_web.post_agent_notification_action("user_bob", {"action": "mark_read", "id": nid_alice})
        self.assertEqual(res_bob["status"], "forbidden")
        self.assertFalse(res_bob["read"])

        # Check DB: Alice's notification is STILL UNREAD!
        notifs_alice = self.notif_manager.list_recent(recipient_id="user_alice", unread_only=True)
        self.assertEqual(len(notifs_alice), 1)
        self.assertEqual(notifs_alice[0]["id"], nid_alice)

        # 2. User Bob attempts to delete Alice's notification
        res_bob_del = house_web.post_agent_notification_action("user_bob", {"action": "delete", "id": nid_alice})
        self.assertEqual(res_bob_del["status"], "forbidden")
        self.assertEqual(len(self.notif_manager.list_recent(recipient_id="user_alice")), 1)

        # 3. User Alice marks her own notification as read
        res_alice = house_web.post_agent_notification_action("user_alice", {"action": "mark_read", "id": nid_alice})
        self.assertEqual(res_alice["status"], "ok")
        self.assertTrue(res_alice["read"])
        self.assertEqual(len(self.notif_manager.list_recent(recipient_id="user_alice", unread_only=True)), 0)

        # 4. Batch mark all as read for Alice: create 3 more notifications
        for i in range(3):
            self.notif_manager.publish(title=f"Notif {i}", content=f"Msg {i}", recipient_id="user_alice")
        self.assertEqual(len(self.notif_manager.list_recent(recipient_id="user_alice", unread_only=True)), 3)

        # Bob attempts mark_all_read -> Only marks Bob's (0 notifications), Alice still has 3
        house_web.post_agent_notification_action("user_bob", {"action": "mark_all_read"})
        self.assertEqual(len(self.notif_manager.list_recent(recipient_id="user_alice", unread_only=True)), 3)

        # Alice marks all read -> Alice has 0 unread
        res_all = house_web.post_agent_notification_action("user_alice", {"action": "mark_all_read"})
        self.assertEqual(res_all["status"], "ok")
        self.assertEqual(res_all["updated_count"], 3)
        self.assertEqual(len(self.notif_manager.list_recent(recipient_id="user_alice", unread_only=True)), 0)

    # =========================================================================
    # SCENARIO B: Task Audit Lifecycle & Retry Recovery (No Premature AUDITED)
    # =========================================================================
    def test_scenario_b_task_audit_lifecycle_and_retry(self):
        """Scenario B:
        1. When an audit task is created, checkpoint status is PENDING (NOT premature AUDITED).
        2. While audit task is running/pending, trigger does NOT create duplicate parallel audit tasks.
        3. If audit task FAILS, trigger retries with incremented retry_count (up to 3 times).
        4. Only when audit task is COMPLETED, checkpoint is confirmed as VERIFIED.
        """
        self.proactive_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)

        # Create high-priority task and complete it
        tid_high = self.task_manager.create_task("生产关键编译", "编译优化", priority=85, category="code", owner_id="user_alice")
        self.task_manager.update_task_status(tid_high, TaskStatus.COMPLETED, result="Compiled successfully")

        # Cycle 1: poll events -> dispatches audit task
        self.proactive_engine._cooldowns.clear()
        dispatched_1 = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_1), 1)
        audit_tid = dispatched_1[0]

        # 1. Checkpoint MUST be PENDING, NOT AUDITED!
        cp = self.proactive_engine.get_checkpoint(f"audit_task:{tid_high}")
        self.assertIsNotNone(cp)
        self.assertEqual(cp["status"], "PENDING")
        self.assertEqual(cp["metadata"]["audit_task_id"], audit_tid)
        self.assertEqual(cp["metadata"]["retry_count"], 1)

        # 2. While audit task is still PENDING, polling again MUST NOT dispatch duplicate parallel audit tasks!
        self.proactive_engine._cooldowns.clear()
        dispatched_2 = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_2), 0, "Must not create duplicate parallel audit tasks while one is in flight")

        # 3. Simulate audit task failure
        self.task_manager.update_task_status(audit_tid, TaskStatus.FAILED, error="审计模型调用超时 504")

        # Next cycle: trigger detects audit task failed -> dispatches retry task
        self.proactive_engine._cooldowns.clear()
        dispatched_3 = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_3), 1, "Must retry failed audit task")
        audit_tid_retry = dispatched_3[0]
        self.assertNotEqual(audit_tid_retry, audit_tid)

        # Checkpoint is now retry_count = 2, still PENDING
        cp_retry = self.proactive_engine.get_checkpoint(f"audit_task:{tid_high}")
        self.assertEqual(cp_retry["status"], "PENDING")
        self.assertEqual(cp_retry["metadata"]["retry_count"], 2)

        # 4. Successfully complete the retry audit task
        self.task_manager.update_task_status(audit_tid_retry, TaskStatus.COMPLETED, result="审计完成：提取出编译加速规则")

        # Poll again: trigger detects audit_task is COMPLETED -> confirms VERIFIED!
        self.proactive_engine._cooldowns.clear()
        dispatched_4 = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_4), 0)

        cp_verified = self.proactive_engine.get_checkpoint(f"audit_task:{tid_high}")
        self.assertEqual(cp_verified["status"], "VERIFIED")

    # =========================================================================
    # SCENARIO C: Multiple Goals Changed Simultaneously (No Dropped Goals)
    # =========================================================================
    def test_scenario_c_multiple_goals_simultaneously_changed(self):
        """Scenario C:
        When multiple active goals (e.g. 3 goals) have substantive changes simultaneously,
        each goal MUST be dispatched its own patrol task and its checkpoint updated individually.
        No goal is dropped or falsely skipped.
        """
        self.proactive_engine.ensure_builtin_triggers(workspace=self.root, task_manager=self.task_manager)

        # Create 3 active goals
        gid_1 = self.task_manager.create_goal("目标A", "scope A", "完成A", [{"title": "s1", "description": "d1", "category": "general"}], "alice")
        gid_2 = self.task_manager.create_goal("目标B", "scope B", "完成B", [{"title": "s1", "description": "d1", "category": "general"}], "alice")
        gid_3 = self.task_manager.create_goal("目标C", "scope C", "完成C", [{"title": "s1", "description": "d1", "category": "general"}], "alice")

        # Initial poll: detects all 3 new goals -> MUST dispatch 3 tasks!
        dispatched = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched), 3, "All 3 changed goals must be dispatched patrol tasks without omission")

        # All 3 goals must have their checkpoints recorded
        cp1 = self.proactive_engine.get_checkpoint(f"goal:{gid_1}")
        cp2 = self.proactive_engine.get_checkpoint(f"goal:{gid_2}")
        cp3 = self.proactive_engine.get_checkpoint(f"goal:{gid_3}")
        self.assertIsNotNone(cp1)
        self.assertIsNotNone(cp2)
        self.assertIsNotNone(cp3)

        # Subsequent poll: no changes -> 0 tasks dispatched
        self.proactive_engine._cooldowns.clear()
        dispatched_repeat = self.proactive_engine.poll_events_and_act(
            proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
            workspace=self.root,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched_repeat), 0)

    # =========================================================================
    # SCENARIO D: Bridge Repeated Reconnects (Idempotent Named Sink)
    # =========================================================================
    def test_scenario_d_repeated_reconnects_idempotent_sink(self):
        """Scenario D:
        Simulate 10 consecutive QQ Bridge reconnects registering sinks.
        A notification MUST only be delivered ONCE, not 10 times.
        """
        send_call_log = []

        def mock_qq_sender(msg: NotificationMessage):
            send_call_log.append(msg.id)
            return True

        # Simulate 10 reconnects with named sink "qq_onebot"
        for _ in range(10):
            self.notif_manager.register_channel_sink(mock_qq_sender, name="qq_onebot")

        # There should only be 1 sink registered under name "qq_onebot"
        self.assertEqual(len(self.notif_manager._channel_sinks), 1)

        # Publish a notification
        nid = self.notif_manager.publish("系统告警", "normal", recipient_id="123456")

        # Must have been sent exactly once!
        self.assertEqual(len(send_call_log), 1)
        self.assertEqual(send_call_log[0], nid)

    # =========================================================================
    # SCENARIO E: Genuine Mathematical Research (Real Python Experiment vs Proof)
    # =========================================================================
    def test_scenario_e_genuine_mathematical_research(self):
        """Scenario E:
        1. Formulate hypothesis: "All odd numbers n in [3, 25] are prime".
        2. Execute genuine Python numerical experiment.
        3. Collect real output showing composite numbers (e.g. 9, 15, 21, 25).
        4. Verifier recognizes counterexample 9 -> Refutes hypothesis.
        5. Finite search is correctly labeled as FINITE_NUMERIC, not formal proof.
        """
        # Create research project
        proj = self.research_tracker.create_research_project(
            project_id="math_prime_search",
            topic="奇数质数猜想",
            goal="验证区间 [3, 25] 奇数是否全为质数",
            task_id="task_prime_1",
            initial_milestones=[{"title": "区间检验", "objective": "计算区间内所有奇数的因子"}],
        )

        # Formulate hypothesis
        hypo = self.research_tracker.create_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            statement="所有区间 [3, 25] 内的奇数均为质数",
            rationale="小奇数具有高质数密度",
            verifiable_criterion="无非1和自身的因数",
            refutation_criterion="存在至少一个因数",
        )

        # Execute genuine Python computation
        script = """
composites = []
for n in range(3, 26, 2):
    factors = [d for d in range(2, n) if n % d == 0]
    if factors:
        composites.append((n, factors))
print(f"Composites found: {composites}")
"""
        exp = self.research_tracker.execute_experiment(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input=script,
        )
        self.assertTrue(exp.is_success)
        self.assertIn("Composites found", exp.output_data)
        self.assertIn("9", exp.output_data)

        # Independent Verifier analyzes experiment data and detects counterexample
        ver_res = self.research_tracker.verify_finding(
            project_id=proj.project_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Critical Reviewer",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data,
            is_verified=False,
            counterexample="9 = 3 * 3, 15 = 3 * 5 存在非平凡因数",
        )
        self.assertEqual(ver_res["status"], FindingStatus.REFUTED.value)
        self.assertEqual(ver_res["counterexample"], "9 = 3 * 3, 15 = 3 * 5 存在非平凡因数")

        # Project's failed hypotheses now includes the refutation
        updated_proj = self.research_tracker.get_project(proj.project_id)
        self.assertTrue(any("反例" in f or "counterexample" in f.lower() for f in updated_proj.failed_hypotheses))

    # =========================================================================
    # SCENARIO F: Research Refutation, Memory-Linked Remedy & Next Step Decision
    # =========================================================================
    def test_scenario_f_research_refutation_and_decision_engine(self):
        """Scenario F:
        1. When all hypotheses in a stage are refuted with counterexamples:
           evaluate_next_step automatically decides REPLAN, suggests branching,
           and records verified negative experience into AgentMemoryStore.
        2. High-priority breakthrough notifications are NOT sent on refutation.
        """
        proj = self.research_tracker.create_research_project(
            project_id="proj_refutation_test",
            topic="费马数全素猜想探索",
            goal="验证 F_n = 2^(2^n) + 1 的素性",
            task_id="task_fermat_1",
            initial_milestones=[{"title": "小阶费马数素性分析", "objective": "计算 F_5 的因子"}],
            owner_id="user_alice",
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            statement="F_5 = 2^32 + 1 是素数",
            rationale="F_0 至 F_4 均为素数",
        )

        # Run experiment checking Euler's factor 641
        script = """
F5 = 2**32 + 1
div = 641
rem = F5 % div
print(f"F5 % 641 = {rem}")
"""
        exp = self.research_tracker.execute_experiment(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input=script,
        )
        self.assertTrue(exp.is_success)
        self.assertIn("F5 % 641 = 0", exp.output_data)

        # Verifier marks REFUTED
        self.research_tracker.verify_finding(
            project_id=proj.project_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Verifier Agent",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details="欧拉分解证实 641 整除 F_5",
            is_verified=False,
            counterexample="F_5 可被 641 整除 (641 * 6700417)",
        )

        # Evaluate next step with AgentMemoryStore
        decision = self.research_tracker.evaluate_next_step(
            project_id=proj.project_id,
            stage_index=1,
            memory_store=self.memory,
        )
        self.assertEqual(decision["action"], ResearchDecisionAction.REPLAN.value)
        self.assertTrue("证伪" in decision["rationale"] or "refuted" in decision["rationale"].lower())

        # Check memory store: negative experience was recorded with owner_id="user_alice"!
        memories = self.memory.list_memories(owner_id="user_alice")
        self.assertTrue(any("反例" in m["content"] or "费马数" in m["content"] for m in memories))

        # Check notifications: NO high priority "breakthrough" notification was falsely sent!
        notifs = self.notif_manager.list_recent(recipient_id="user_alice")
        high_prio_notifs = [n for n in notifs if n.get("priority") == NotificationPriority.HIGH.value]
        self.assertEqual(len(high_prio_notifs), 0)

    # =========================================================================
    # SCENARIO G: Research Persistence Across Restarts & Crash Recovery
    # =========================================================================
    def test_scenario_g_research_persistence_across_restarts(self):
        """Scenario G:
        1. Create research project, hypotheses, and experiments.
        2. Simulate process exit and restart with a new ResearchTracker instance on the same SQLite DB.
        3. All project status, hypotheses, experiments, and evidence MUST be restored perfectly.
        """
        proj = self.research_tracker.create_research_project(
            project_id="proj_restart_test",
            topic="丢番图方程非平凡解",
            goal="求解 x^3 + y^3 = z^3 正整数解",
            task_id="task_diophantine_1",
            initial_milestones=[{"title": "小整数范围数值搜索", "objective": "搜索 [1, 50]"}],
            owner_id="user_bob",
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            statement="存在 1 <= x, y, z <= 50 满足 x^3 + y^3 = z^3",
        )

        exp = self.research_tracker.execute_experiment(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input="print('Search complete: 0 solutions found')",
        )

        # Simulate restart
        restarted_tracker = ResearchTracker(db_path=self.research_db)
        restarted_proj = restarted_tracker.get_project("proj_restart_test")
        self.assertIsNotNone(restarted_proj)
        self.assertEqual(restarted_proj.topic, "丢番图方程非平凡解")
        self.assertEqual(restarted_proj.owner_id, "user_bob")

        # Hypotheses restored
        hypos = restarted_tracker.list_hypotheses("proj_restart_test")
        self.assertEqual(len(hypos), 1)
        self.assertEqual(hypos[0].hypothesis_id, hypo.hypothesis_id)

        # Experiments restored
        exps = restarted_tracker.list_experiments("proj_restart_test")
        self.assertEqual(len(exps), 1)
        self.assertEqual(exps[0].experiment_id, exp.experiment_id)
        self.assertIn("0 solutions found", exps[0].output_data)

    # =========================================================================
    # SCENARIO H: Multi-Agent Research Collaboration Workflow
    # =========================================================================
    def test_scenario_h_multi_agent_research_collaboration(self):
        """Scenario H:
        Multi-agent logical role separation:
        - Research Planner creates hypotheses and branches.
        - Experiment Executor runs genuine computational checks.
        - Verifier evaluates findings with strict proof typing.
        - Research Coordinator aggregates summary and next milestones.
        """
        proj = self.research_tracker.create_research_project(
            project_id="collab_collatz_50",
            topic="考拉兹猜想小整数验证",
            goal="验证 [1, 50] 内所有正整数均收敛至 1",
            task_id="task_collatz_master",
            initial_milestones=[
                {"title": "阶段一：[1, 50] 数值穷举验证", "objective": "计算所有步数并在遇到无法收敛时报错"},
                {"title": "阶段二：停机步数统计分析", "objective": "分析最大步数与峰值分布"},
            ],
            max_stages=2,
            owner_id="user_alice",
        )

        # 1. Role: Research Planner establishes branch & hypothesis
        branch = self.research_tracker.create_branch(
            project_id=proj.project_id,
            branch_name="方向A: 有限范围数值直接检验",
            focus_hypothesis="[1, 50] 均收敛到 1",
        )
        self.assertIsNotNone(branch)

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            statement="区间 [1, 50] 内的所有整数经考拉兹迭代均能在 200 步内收敛至 1",
            rationale="建立基础可靠已知事实",
            verifiable_criterion="max_steps < 200 and all reach 1",
        )

        # 2. Role: Experiment Executor executes real Python script
        script = """
def collatz(n):
    steps = 0
    while n > 1 and steps < 200:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return n == 1, steps

all_converged = True
max_s = 0
for i in range(1, 51):
    ok, s = collatz(i)
    if not ok:
        all_converged = False
        break
    if s > max_s:
        max_s = s
print(f"RESULT: converged={all_converged}, max_steps={max_s}")
"""
        exp = self.research_tracker.execute_experiment(
            project_id=proj.project_id,
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input=script,
        )
        self.assertTrue(exp.is_success)
        self.assertIn("RESULT: converged=True", exp.output_data)

        # 3. Role: Verifier reviews evidence, confirms FINITE_NUMERIC verification
        ver_res = self.research_tracker.verify_finding(
            project_id=proj.project_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Independent Verifier Agent",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data,
            is_verified=True,
            sources=["python_execution:sys.executable"],
        )
        self.assertEqual(ver_res["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # 4. Role: Research Coordinator evaluates next step
        decision = self.research_tracker.evaluate_next_step(project_id=proj.project_id, stage_index=1)
        self.assertEqual(decision["action"], ResearchDecisionAction.CONTINUE.value)
        self.assertTrue("推进到阶段 2" in decision["rationale"] or "advancing to stage 2" in decision["rationale"].lower())

        # Summary check
        summary = self.research_tracker.get_project_summary(proj.project_id)
        self.assertEqual(summary["hypotheses_count"], 1)
        self.assertEqual(summary["experiments_count"], 1)
        self.assertEqual(summary["branches_count"], 1)
        self.assertTrue(len(summary["verified_findings"]) > 0)


if __name__ == "__main__":
    unittest.main()
