"""Round 9 Comprehensive Test Suite: Deep Autonomous Research Agent & Execution Loop.

Covers:
- Test A: Rejection of evidence-less verification (no free passes even with is_verified=True).
- Test B: Genuine numerical experiment execution with finite-scope proof typing.
- Test C: Autonomous two-stage continuous research execution driven by TaskManager.
- Test D: Hypothesis refutation, counterexample logging, REPLAN decision & memory avoidance.
- Test E: Crash recovery & state persistence across restarts.
- Test F: Permission gating (commands=False blocks code execution; no bypass).
- Test G: Token budget exhaustion & explicit pause/stop enforcement.
- Test H: Multi-agent research collaboration (Planner, Executor, Verifier, Coordinator).
- Test I: Goal completion integrity (single sub-question does NOT falsely complete the whole goal).
- Test J: Safety & regression verification.
"""
from __future__ import annotations

import pytest
pytest.importorskip('sympy')

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory import AgentMemoryStore
from agenda import EventType, ProactiveActionEngine, ProactiveLevel
import house_web
from notification_manager import NotificationManager, NotificationPriority, NotificationStatus
from research_tracker import (
    FindingStatus,
    ProofType,
    ResearchDecisionAction,
    ResearchTracker,
    StageStatus,
)
from task_manager import TaskItem, TaskManager, TaskStatus


class TestRound9ResearchDeep(unittest.TestCase):
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
    # TEST A: Evidence-less Verification Rejection
    # =========================================================================
    def test_a_no_evidence_verification_rejected(self):
        """Test A:
        1. Formulate false hypothesis: '1 + 1 = 3'.
        2. Attempt verify_finding with is_verified=True but NO evidence/sources.
        3. System MUST reject verification; status must NOT be VERIFIED.
        4. Model verbal declaration without proof steps cannot achieve formal proof.
        """
        proj = self.research_tracker.create_research_project(
            project_id="test_a_proj", topic="加法验证", goal="检验等式", task_id="t_a",
            initial_milestones=[{"title": "基础算术", "objective": "验证"}],
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="test_a_proj", stage_index=1, statement="1 + 1 = 3"
        )

        # 1. Attempt verification with is_verified=True but empty evidence
        res = self.research_tracker.verify_finding(
            project_id="test_a_proj",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Hypothetical Model",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details="",  # Empty evidence!
            is_verified=True,
            sources=[],
        )
        self.assertFalse(res["verified"])
        self.assertNotEqual(res["status"], FindingStatus.VERIFIED.value)
        self.assertEqual(res["status"], FindingStatus.WAITING_VERIFICATION.value)
        self.assertTrue("缺少客观实验证据" in res.get("rejection_reason", "") or "Missing objective experimental evidence" in res.get("rejection_reason", ""))

        # 2. Attempt with bare assertion ""
        res2 = self.research_tracker.verify_finding(
            project_id="test_a_proj",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Hypothetical Model",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details="证明完成",
            is_verified=True,
            sources=["self_assertion"],
        )
        self.assertFalse(res2["verified"])
        self.assertNotEqual(res2["status"], FindingStatus.VERIFIED.value)

        # 3. Attempt with bogus lengthy pseudo-derivation claiming 1 + 1 = 3
        # System MUST detect arithmetic contradiction and permanently REFUTE with counterexample!
        bogus_derivation = "经过深入推导：根据皮亚诺公理在非欧空间拓展，设1为基元，因此得出1+1=3，特此给出形式证明。"
        res3 = self.research_tracker.verify_finding(
            project_id="test_a_proj",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Hypothetical Model",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=bogus_derivation,
            is_verified=True,
            sources=["model_pseudo_formal"],
        )
        self.assertFalse(res3["verified"])
        self.assertEqual(res3["status"], FindingStatus.REFUTED.value)
        self.assertTrue("矛盾证伪" in (res3.get("counterexample", "") + res3.get("rejection_reason", "")) or "contradiction" in (res3.get("counterexample", "") + res3.get("rejection_reason", "")).lower())

        # 4. Pure natural language assertions without machine-checked scripts cannot pass FORMAL_PROOF
        hypo_abstract = self.research_tracker.create_hypothesis(
            project_id="test_a_proj", stage_index=1, statement="素数间隔定理伪命题"
        )
        natural_proof = "引理一：显然任意素数间隔均满足条件。引理二：由数学归纳法推演即得。证明闭环完成。"
        res4 = self.research_tracker.verify_finding(
            project_id="test_a_proj",
            hypothesis_id=hypo_abstract.hypothesis_id,
            verifier_role="Hypothetical Model",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=natural_proof,
            is_verified=True,
            sources=["paper_draft"],
        )
        self.assertFalse(res4["verified"])
        self.assertEqual(res4["status"], FindingStatus.WAITING_VERIFICATION.value)
        self.assertTrue("纯自然语言叙述不能作为形式化证明通过判据" in res4.get("rejection_reason", "") or "natural language" in res4.get("rejection_reason", "").lower())

        # 5. Genuine machine-executable symbolic proof script succeeds
        hypo_valid = self.research_tracker.create_hypothesis(
            project_id="test_a_proj", stage_index=1, statement="代数恒等式: (x+1)^2 = x^2 + 2x + 1"
        )
        valid_script = """
import sympy as sp
x = sp.Symbol('x')
assert sp.simplify((x + 1)**2 - (x**2 + 2*x + 1)) == 0
"""
        res5 = self.research_tracker.verify_finding(
            project_id="test_a_proj",
            hypothesis_id=hypo_valid.hypothesis_id,
            verifier_role="Symbolic Math Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=valid_script,
            is_verified=True,
            sources=["sympy_formal_engine"],
        )
        self.assertTrue(res5["verified"])
        self.assertEqual(res5["status"], FindingStatus.VERIFIED.value)

    # =========================================================================
    # TEST B: Genuine Numerical Experiment & Scope Distinction
    # =========================================================================
    def test_b_real_numerical_experiment_and_scope_distinction(self):
        """Test B:
        1. Compute primes in [2, 30].
        2. Execute real Python numeric tool.
        3. Verifier verifies finite range. Status is EXPERIMENTALLY_SUPPORTED,
           explicitly distinguishing finite search from universal mathematical proof.
        """
        proj = self.research_tracker.create_research_project(
            project_id="test_b_primes", topic="质数区间搜索", goal="求解 [2, 30] 质数", task_id="t_b",
            initial_milestones=[{"title": "小整数搜索", "objective": "寻找质数"}],
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="test_b_primes", stage_index=1, statement="区间 [2, 30] 内共有 10 个质数"
        )

        script = """
primes = []
for n in range(2, 31):
    if all(n % d != 0 for d in range(2, int(n**0.5) + 1)):
        primes.append(n)
print(f"Primes found: {len(primes)} -> {primes}")
"""
        exp = self.research_tracker.execute_experiment(
            project_id="test_b_primes",
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input=script,
            owner_id="system",
        )
        self.assertTrue(exp.is_success)
        self.assertIn("Primes found: 10", exp.output_data)

        # Verifier assigns FINITE_NUMERIC
        res = self.research_tracker.verify_finding(
            project_id="test_b_primes",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Math Verifier",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data,
            is_verified=True,
            sources=["python_sandbox"],
        )
        # Finite numeric search is capped at EXPERIMENTALLY_SUPPORTED, not universal VERIFIED
        self.assertEqual(res["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)
        self.assertTrue(res["verified"])

    # =========================================================================
    # TEST C: Autonomous Two-Stage Continuous Research Loop
    # =========================================================================
    def test_c_autonomous_two_stage_continuous_research_loop(self):
        """Test C:
        1. Launch Stage 1 research task through TaskManager Worker.
        2. Task executes real experiment, verifies evidence, decides CONTINUE,
           and automatically schedules Stage 2.
        3. Worker picks up Stage 2 and executes to completion.
        4. Both stages are documented with authentic intermediate evidence.
        """
        proj_id = "proj_two_stage_loop"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="毕达哥拉斯三元组搜索",
            goal="在整数范围内发现并验证勾股数",
            task_id="t_pythagoras_root",
            initial_milestones=[
                {"title": "阶段一：小范围枚举 (a, b <= 10)", "objective": "验证 a^2 + b^2 = c^2 在小范围内的解"},
                {"title": "阶段二：参数性质分析", "objective": "分析 (3, 4, 5) 比例倍数性质"},
            ],
            max_stages=2,
            owner_id="user_alice",
        )

        # Grant Alice commands permissions
        u_dir = self.root / "friends" / "user_alice"
        u_dir.mkdir(parents=True, exist_ok=True)
        (u_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": True}
        }), encoding="utf-8")

        # Stage 1 experiment code
        code_s1 = "print('Stage 1 Result: (3, 4, 5) satisfies 3^2 + 4^2 = 5^2')"

        # Create Stage 1 task in TaskManager
        tid_s1 = self.task_manager.create_task(
            title="毕达哥拉斯三元组 · 阶段一",
            description="执行阶段一勾股数验证",
            category="research",
            owner_id="user_alice",
            initial_checkpoint={"research_project_id": proj_id, "stage_index": 1, "experiment_code": code_s1},
        )

        # Execute Stage 1 via TaskManager's orchestrated executor
        task_s1 = self.task_manager.get_task(tid_s1)
        res_s1 = self.task_manager._default_orchestrated_executor(task_s1, lambda m: None)
        self.assertEqual(res_s1["research_cycle"]["decision"]["action"], ResearchDecisionAction.CONTINUE.value)
        next_tid = res_s1["research_cycle"]["next_task_id"]
        self.assertIsNotNone(next_tid)

        # Mark Stage 1 completed
        self.task_manager.update_task_status(tid_s1, TaskStatus.COMPLETED, result=res_s1["output"])

        # Stage 2 task was automatically created in TaskManager!
        task_s2 = self.task_manager.get_task(next_tid)
        self.assertIsNotNone(task_s2)
        self.assertTrue("stage" in task_s2.title.lower() or "阶段" in task_s2.title)

        # Execute Stage 2
        task_s2.checkpoint["experiment_code"] = "print('Stage 2 Result: (6, 8, 10) verified as 2x scale')"
        res_s2 = self.task_manager._default_orchestrated_executor(task_s2, lambda m: None)
        self.task_manager.update_task_status(next_tid, TaskStatus.COMPLETED, result=res_s2["output"])

        # Check project state: completed both stages!
        proj_updated = self.research_tracker.get_project(proj_id)
        self.assertEqual(proj_updated.current_stage, 2)
        self.assertEqual(len(proj_updated.global_verified_findings), 2)

    # =========================================================================
    # TEST D: Hypothesis Refutation & REPLAN Avoidance Loop
    # =========================================================================
    def test_d_hypothesis_refutation_and_replan_avoidance(self):
        """Test D:
        1. Formulate false hypothesis: F_5 is prime.
        2. Experiment yields Euler factor 641 (counterexample).
        3. Hypothesis is REFUTED; counterexample recorded.
        4. evaluate_next_step outputs REPLAN and records remedy to AgentMemoryStore.
        5. No premature breakthrough notifications are dispatched.
        """
        proj = self.research_tracker.create_research_project(
            project_id="test_d_fermat", topic="费马数素性", goal="验证F5素性", task_id="t_d",
            initial_milestones=[{"title": "整除检验", "objective": "验证因子"}],
            owner_id="user_alice",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="test_d_fermat", stage_index=1, statement="F_5 = 2^32 + 1 满足素性条件"
        )

        # Run experiment discovering 641 factor
        exp = self.research_tracker.execute_experiment(
            project_id="test_d_fermat",
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input="print('Euler discovery: 2^32 + 1 = 641 * 6700417, remainder=0')",
            owner_id="system",
        )
        self.assertTrue(exp.is_success)

        # Verifier detects counterexample 641
        ver_res = self.research_tracker.verify_finding(
            project_id="test_d_fermat",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Independent Verifier",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data,
            is_verified=False,
            counterexample="反例: 641 是非平凡因数 (641 * 6700417)",
        )
        self.assertEqual(ver_res["status"], FindingStatus.REFUTED.value)

        # Evaluate next step with AgentMemoryStore
        decision = self.research_tracker.evaluate_next_step(
            project_id="test_d_fermat", stage_index=1, memory_store=self.memory
        )
        self.assertEqual(decision["action"], ResearchDecisionAction.REPLAN.value)

        # Memory store has failure experience recorded
        memories = self.memory.list_memories(owner_id="user_alice")
        self.assertTrue(any("反例" in m["content"] for m in memories))

        # Check notifications: NO high priority breakthrough notifications sent!
        high_notifs = [n for n in self.notif_manager.list_recent(recipient_id="user_alice")
                       if n.get("priority") == NotificationPriority.HIGH.value]
        self.assertEqual(len(high_notifs), 0)

    # =========================================================================
    # TEST E: Crash Recovery & State Persistence
    # =========================================================================
    def test_e_crash_recovery_and_state_persistence(self):
        """Test E:
        1. Create research project with hypotheses, experiments, and evidence.
        2. Simulate process crash by instantiating fresh ResearchTracker pointing to same SQLite DB.
        3. Verify all states, experiments, hypotheses, and evidence are 100% recovered.
        """
        proj = self.research_tracker.create_research_project(
            project_id="crash_rec_proj", topic="丢番图方程探索", goal="寻找正整数解", task_id="t_e",
            initial_milestones=[{"title": "小范围扫描", "objective": "计算"}],
            owner_id="user_carol",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="crash_rec_proj", stage_index=1, statement="在 [1, 20] 内存在非平凡解"
        )
        exp = self.research_tracker.execute_experiment(
            project_id="crash_rec_proj", stage_index=1, hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric", code_or_input="print('Verified: 0 solutions')", owner_id="system",
        )

        # Simulate process restart
        new_tracker = ResearchTracker(db_path=self.research_db)
        recovered_proj = new_tracker.get_project("crash_rec_proj")
        self.assertIsNotNone(recovered_proj)
        self.assertEqual(recovered_proj.topic, "丢番图方程探索")

        recovered_hypos = new_tracker.list_hypotheses("crash_rec_proj")
        self.assertEqual(len(recovered_hypos), 1)
        self.assertEqual(recovered_hypos[0].statement, hypo.statement)

        recovered_exps = new_tracker.list_experiments("crash_rec_proj")
        self.assertEqual(len(recovered_exps), 1)
        self.assertIn("Verified: 0 solutions", recovered_exps[0].output_data)

    # =========================================================================
    # TEST F: Permission Gating (commands=False Blocks Execution)
    # =========================================================================
    def test_f_permission_gating_blocks_unauthorized_code_execution(self):
        """Test F:
        1. User Dave has computer_tools.commands = False.
        2. Attempting to execute python experiment code under Dave MUST be blocked.
        3. ResearchTracker records is_success=False and permission denial log,
           never bypassing security boundaries.
        """
        u_dir = self.root / "friends" / "user_dave"
        u_dir.mkdir(parents=True, exist_ok=True)
        (u_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": False}  # Commands disabled!
        }), encoding="utf-8")

        proj = self.research_tracker.create_research_project(
            project_id="dave_proj", topic="受限代码测试", goal="测试权限边界", task_id="t_f",
            initial_milestones=[{"title": "s1", "objective": "o1"}],
            owner_id="user_dave",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="dave_proj", stage_index=1, statement="Authorizetest"
        )

        # Attempt to execute experiment
        exp = self.research_tracker.execute_experiment(
            project_id="dave_proj",
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric",
            code_or_input="print('Attacking sandbox!')",
            owner_id="user_dave",
            workspace=self.root,
        )
        self.assertFalse(exp.is_success)
        self.assertTrue("权限拦截" in exp.error_log or "Permission interception" in exp.error_log)
        self.assertEqual(exp.output_data, "")

    # =========================================================================
    # TEST G: Token Budget & Stop Enforcement
    # =========================================================================
    def test_g_budget_exhaustion_and_stop_enforcement(self):
        """Test G:
        1. When used_tokens >= budget_tokens, status updates to BUDGET_EXHAUSTED.
        2. evaluate_next_step outputs action=STOP.
        3. run_autonomous_stage_cycle refuses further execution when stopped.
        """
        proj = self.research_tracker.create_research_project(
            project_id="budget_proj", topic="预算测试", goal="测试Token上限", task_id="t_g",
            initial_milestones=[{"title": "s1", "objective": "o1"}],
            budget_tokens=1000,
        )
        # Consume budget
        proj.used_tokens = 1200
        self.research_tracker._save_project(proj)

        decision = self.research_tracker.evaluate_next_step("budget_proj", 1)
        self.assertEqual(decision["action"], ResearchDecisionAction.STOP.value)
        self.assertTrue("预算已耗尽" in decision["rationale"] or "budget exhausted" in decision["rationale"].lower())

        # Running cycle on exhausted project returns STOP status immediately
        res = self.research_tracker.run_autonomous_stage_cycle("budget_proj", 1, self.task_manager)
        self.assertEqual(res["action"], "STOP")

    # =========================================================================
    # TEST H: Multi-Agent Collaboration Workflow
    # =========================================================================
    def test_h_multi_agent_collaboration_workflow(self):
        """Test H:
        Planner -> Executor -> Verifier -> Coordinator roles collaborate with verifiable trace.
        """
        proj = self.research_tracker.create_research_project(
            project_id="collab_euler", topic="欧拉多面体公式小规模检验", goal="V - E + F = 2", task_id="t_h",
            initial_milestones=[{"title": "正多面体检验", "objective": "立方体与四面体顶点面数"}],
        )

        # 1. Planner formulates branch & hypothesis
        branch = self.research_tracker.create_branch("collab_euler", "方向A: 正多面体组合验证", "立方体与四面体")
        hypo = self.research_tracker.create_hypothesis("collab_euler", 1, "凸正多面体均满足 V - E + F = 2")

        # 2. Executor executes computation
        script = """
shapes = {'tetrahedron': (4, 6, 4), 'cube': (8, 12, 6), 'octahedron': (6, 12, 8)}
all_two = all(V - E + F == 2 for V, E, F in shapes.values())
print(f"Euler polyhedra formula verified: {all_two}")
"""
        exp = self.research_tracker.execute_experiment(
            project_id="collab_euler", stage_index=1, hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric", code_or_input=script, owner_id="system",
        )
        self.assertTrue(exp.is_success)

        # 3. Verifier checks and logs evidence
        ver = self.research_tracker.verify_finding(
            project_id="collab_euler", hypothesis_id=hypo.hypothesis_id,
            verifier_role="Geometry Verifier", proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data, is_verified=True,
        )
        self.assertEqual(ver["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # 4. Coordinator evaluates next step
        decision = self.research_tracker.evaluate_next_step("collab_euler", 1)
        self.assertEqual(decision["action"], ResearchDecisionAction.CONTINUE.value)

    # =========================================================================
    # TEST I: Goal Completion Integrity (Partial Sub-Question != COMPLETE)
    # =========================================================================
    def test_i_sub_question_completion_does_not_falsely_complete_goal(self):
        """Test I:
        A project with 2 milestones. If Stage 1 achieves a verified finding,
        the project MUST NOT be prematurely marked as COMPLETED.
        """
        proj = self.research_tracker.create_research_project(
            project_id="multi_part_proj", topic="多阶段综合论证", goal="完成全部两阶段论证", task_id="t_i",
            initial_milestones=[
                {"title": "阶段一：基础引理验证", "objective": "引理A验证"},
                {"title": "stage: Export", "objective": "总体结论导出"},
            ],
            max_stages=2,
        )
        hypo_s1 = self.research_tracker.create_hypothesis("multi_part_proj", 1, "引理A成立")
        self.research_tracker.verify_finding(
            "multi_part_proj", hypo_s1.hypothesis_id, "Verifier", ProofType.FINITE_NUMERIC,
            "数值计算支撑引理A", True, sources=["test"],
        )

        # Evaluate stage 1: MUST NOT complete project!
        decision = self.research_tracker.evaluate_next_step("multi_part_proj", 1)
        self.assertEqual(decision["action"], ResearchDecisionAction.CONTINUE.value)
        self.assertNotEqual(decision["action"], ResearchDecisionAction.COMPLETE.value)

        proj_check = self.research_tracker.get_project("multi_part_proj")
        self.assertEqual(proj_check.status, "ACTIVE")
        self.assertNotEqual(proj_check.status, "COMPLETED")

    # =========================================================================
    # TEST J: Safety & Regression Verification
    # =========================================================================
    def test_j_safety_and_regressions(self):
        """Test J:
        Verify web notification security, PID protection, and task cancellation defenses remain intact.
        """
        # 1. PID protection in TaskManager
        tid = self.task_manager.create_task("进程保护测试", "PID", owner_id="user_bob")
        with self.assertRaises(ValueError):
            self.task_manager.set_task_pid(tid, os.getpid())

        # 2. Web notification ownership
        nid = self.notif_manager.publish("Alice通知", "私密", recipient_id="user_alice")
        res = house_web.post_agent_notification_action("user_bob", {"action": "mark_read", "id": nid})
        self.assertEqual(res["status"], "forbidden")


if __name__ == "__main__":
    unittest.main()
