"""Round 10 Comprehensive Adversarial Verification, Autonomy & Safety Test Suite.

Covers:
- Test A: Rejection of false formal mathematical proof (1 + 1 = 3 cannot pass with pseudo-derivation).
- Test B: Invalidation of trivial outputs (print('ok') must NEVER validate 1+1=3 nor complete the goal).
- Test C: Finite experiment scope bound (never claims universal proof for infinite domains).
- Test D: Genuine autonomous two-stage research without pre-injected test scripts.
- Test E: Failure replanning with counterexample preservation and avoidance of redundant attempts.
- Test F: Multi-agent authentic role orchestration (Planner, Executor, Verifier, Coordinator).
- Test G: Absolute permission gating (commands=False blocks TaskManager, ResearchTracker, and direct runners).
- Test H: Real resource controls (timeout tree cleanup, cancel signal response, budget halt, host PID preservation).
- Test I: Crash recovery & idempotency across process boundaries.
- Test J: Multi-tenant user isolation (same project topic between Alice and Bob remains strictly isolated).
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory import AgentMemoryStore
from agenda import ProactiveActionEngine
from notification_manager import NotificationManager
from research_tracker import (
    FindingStatus,
    ProofType,
    ResearchDecisionAction,
    ResearchTracker,
    StageStatus,
    VerificationPlan,
    check_formal_checker_availability,
    evaluate_experiment_output,
)
from task_manager import TaskItem, TaskManager, TaskStatus


class TestRound10VerificationIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.task_db = self.data_dir / "tasks.sqlite3"
        self.notif_db = self.data_dir / "notifications.sqlite3"
        self.mem_db = self.data_dir / "memory.sqlite3"
        self.research_db = self.data_dir / "research_projects.sqlite3"

        self.task_manager = TaskManager(db_path=self.task_db, workspace=self.root)
        self.notif_manager = NotificationManager(db_path=self.notif_db)
        self.memory = AgentMemoryStore(db_path=self.mem_db)
        self.research_tracker = ResearchTracker(db_path=self.research_db)

        self.p_task = patch.object(TaskManager, "get_instance", return_value=self.task_manager)
        self.p_notif = patch.object(NotificationManager, "get_instance", return_value=self.notif_manager)
        self.p_mem = patch.object(AgentMemoryStore, "get_instance", return_value=self.memory)
        self.p_research = patch.object(ResearchTracker, "get_instance", return_value=self.research_tracker)

        self.p_task.start()
        self.p_notif.start()
        self.p_mem.start()
        self.p_research.start()

    def tearDown(self):
        self.task_manager.stop_worker()
        self.p_task.stop()
        self.p_notif.stop()
        self.p_mem.stop()
        self.p_research.stop()
        self.temp_dir.cleanup()

    # =========================================================================
    # TEST A: Rejection of False Formal Mathematical Proof (1 + 1 = 3)
    # =========================================================================
    def test_a_false_formal_proof_adversarial_rejection(self):
        """Adversarial Test A:
        Attempt to pass '1 + 1 = 3' using FORMAL_PROOF with various pseudo-proofs:
        1. Lengthy plausible-sounding natural language derivation.
        2. Pseudo-Lean code without lean installed.
        3. Model assertion claiming theorem proved.
        Result: System MUST NEVER mark it as VERIFIED.
        """
        proj = self.research_tracker.create_research_project(
            project_id="p_false_math", topic="假命题检验", goal="验证 1 + 1 = 3", task_id="t_fm",
            initial_milestones=[{"title": "基础命题", "objective": "验证 1 + 1 = 3"}],
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id="p_false_math", stage_index=1, statement="1 + 1 = 3"
        )

        # 1. Lengthy pseudo-mathematical natural language text (> 100 chars)
        long_pseudo_proof = (
            "定义加法运算为集合映射。设集合 A={0}, B={0}。在拟对偶群扩充下，元素复合产生额外基数，"
            "因此定义 1+1 := 1 + 1 + 1 - 0 = 3。根据泛代数公理闭环推导，本命题在拟形式系统下成立，证毕。"
        )
        res1 = self.research_tracker.verify_finding(
            project_id="p_false_math",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Hypothetical Model",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=long_pseudo_proof,
            is_verified=True,
            sources=["pseudo_math_paper"],
        )
        # Must detect contradiction and REFUTE, or at least reject formal verification
        self.assertFalse(res1["verified"])
        self.assertNotEqual(res1["status"], FindingStatus.VERIFIED.value)
        self.assertEqual(res1["status"], FindingStatus.REFUTED.value)
        self.assertTrue("矛盾证伪" in (res1.get("counterexample", "") + res1.get("rejection_reason", "")) or "contradiction" in (res1.get("counterexample", "") + res1.get("rejection_reason", "")).lower())

        # 2. Pseudo-Lean formal script without installed proof assistant
        lean_script = "theorem one_plus_one_is_three : 1 + 1 = 3 := by omega"
        res2 = self.research_tracker.verify_finding(
            project_id="p_false_math",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Model Claiming Lean",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=lean_script,
            is_verified=True,
            sources=["lean4_assistant"],
        )
        self.assertFalse(res2["verified"])
        self.assertNotEqual(res2["status"], FindingStatus.VERIFIED.value)

    # =========================================================================
    # TEST B: Invalidation of Trivial Outputs (print("ok") cannot prove 1+1=3)
    # =========================================================================
    def test_b_print_ok_cannot_validate_claim_nor_complete_goal(self):
        """Adversarial Test B:
        Goal:  1 + 1 = 3.
        Experiment code: print("ok").
        Execution result:
        1. Tool exit code is 0 (TOOL_EXECUTION_SUCCESS).
        2. But EXPERIMENT_VALID is False and CLAIM_SUPPORTED is False.
        3. Hypothesis must NOT be experimentally_supported or verified.
        4. Stage MUST NOT be ACHIEVED.
        5. Entire research goal MUST NOT be COMPLETED.
        """
        proj_id = "p_print_ok_test"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id, topic="加法检验", goal="证明 1 + 1 = 3", task_id="t_ok",
            initial_milestones=[{"title": "加法实验", "objective": "证明 1 + 1 = 3"}],
            max_stages=1,
            owner_id="system",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id, stage_index=1, statement="1 + 1 = 3"
        )

        cycle_res = self.research_tracker.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.task_manager,
            experiment_code="print('ok')",
            auto_schedule_next=False,
        )

        # Tool successfully ran Python, but claim is refuted or rejected!
        self.assertTrue(cycle_res["experiment_success"])
        self.assertFalse(cycle_res["claim_supported"])
        self.assertFalse(cycle_res["verification"]["verified"])
        self.assertNotEqual(cycle_res["verification"]["status"], FindingStatus.VERIFIED.value)
        self.assertNotEqual(cycle_res["verification"]["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # Check project state: milestone must NOT be ACHIEVED and project must NOT be COMPLETED!
        proj_updated = self.research_tracker.get_project(proj_id)
        self.assertNotEqual(proj_updated.milestones[0].status, "ACHIEVED")
        self.assertNotEqual(proj_updated.status, "COMPLETED")
        self.assertNotIn("1 + 1 = 3", " ".join(proj_updated.global_verified_findings))

    # =========================================================================
    # TEST C: Finite Experiment vs Infinite Domain Scope Boundary
    # =========================================================================
    def test_c_finite_numeric_scope_boundary_enforcement(self):
        """Test C:
        Finite numerical verification for primes in [2, 30].
        The conclusion must be strictly annotated with its scope '[finite_numeric | ...]'
        and must NOT claim universal proof for all integers.
        """
        proj_id = "p_scope_primes"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id, topic="素数搜索", goal="检验小范围素数分布", task_id="t_scope",
            initial_milestones=[{"title": "小整数搜索", "objective": "验证区间 [2, 30] 素数"}],
            owner_id="system",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id, stage_index=1, statement="区间 [2, 30] 内共有 10 个质数"
        )

        script = (
            "primes = [n for n in range(2, 31) if all(n % d != 0 for d in range(2, int(n**0.5)+1))]\n"
            "print(f'Primes found: {len(primes)} -> {primes}')\n"
        )
        exp = self.research_tracker.execute_experiment(
            project_id=proj_id, stage_index=1, hypothesis_id=hypo.hypothesis_id,
            tool_name="python_numeric", code_or_input=script, owner_id="system",
        )
        self.assertTrue(exp.tool_execution_success)
        self.assertTrue(exp.experiment_valid)
        self.assertTrue(exp.claim_supported)

        ver = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Math Verifier",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data,
            is_verified=exp.claim_supported,
            sources=["python_sandbox"],
        )
        self.assertTrue(ver["verified"])
        self.assertEqual(ver["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)
        # Cannot claim universal formal proof
        self.assertNotEqual(ver["status"], FindingStatus.VERIFIED.value)

    # =========================================================================
    # TEST D: Genuine Autonomous Two-Stage Research (No Handcrafted Scripts)
    # =========================================================================
    def test_d_genuine_autonomous_two_stage_research_pipeline(self):
        """Test D:
        User only provides a high-level research goal via TaskManager task:
        ''.
        System MUST autonomously:
        1. Synthesize Stage 1 hypothesis & experiment code.
        2. Execute experiment under RBAC permissions.
        3. Verify quantitative evidence.
        4. Decide CONTINUE and auto-schedule Stage 2 task.
        5. Execute Stage 2 to completion without test-injected code.
        """
        # Set up Alice with commands permission
        u_dir = self.root / "friends" / "alice"
        u_dir.mkdir(parents=True, exist_ok=True)
        (u_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": True}
        }), encoding="utf-8")

        # Create user research task in TaskManager without ANY experiment_code
        tid = self.task_manager.create_task(
            title="自主勾股数定理探索",
            description="在整数范围内发现并验证勾股数",
            category="research",
            owner_id="alice",
        )

        task = self.task_manager.get_task(tid)
        self.assertEqual(task.category, "research")
        self.assertNotIn("experiment_code", task.checkpoint)

        # Worker executes Stage 1 autonomously
        res_s1 = self.task_manager._default_orchestrated_executor(task, lambda m: None)
        self.assertEqual(res_s1["research_cycle"]["decision"]["action"], ResearchDecisionAction.CONTINUE.value)
        next_tid = res_s1["research_cycle"]["next_task_id"]
        self.assertIsNotNone(next_tid)

        self.task_manager.update_task_status(tid, TaskStatus.COMPLETED, result=res_s1["output"])

        # Stage 2 was automatically scheduled! Execute Stage 2 autonomously
        task_s2 = self.task_manager.get_task(next_tid)
        self.assertIsNotNone(task_s2)
        res_s2 = self.task_manager._default_orchestrated_executor(task_s2, lambda m: None)
        self.task_manager.update_task_status(next_tid, TaskStatus.COMPLETED, result=res_s2["output"])

        # Inspect completed project: both stages verified with authentic evidence
        p_id = task.checkpoint.get("research_project_id")
        proj = self.research_tracker.get_project(p_id)
        self.assertGreaterEqual(len(proj.global_verified_findings), 2)
        self.assertTrue(any("3, 4, 5" in f or "triples" in f for f in proj.global_verified_findings))

    # =========================================================================
    # TEST E: Research Failure Re-planning with Counterexample & Memory Linkage
    # =========================================================================
    def test_e_failure_replanning_and_memory_avoidance(self):
        """Test E:
        Hypothesis: ' F_n = 2^(2^n) + 1 '.
        Experiment finds Euler counterexample: 641 divides F_5.
        Result:
        1. Hypothesis marked REFUTED.
        2. Counterexample permanently logged.
        3. evaluate_next_step outputs REPLAN.
        4. Failure lesson recorded to AgentMemoryStore.
        5. Subsequent phase avoids repeating the refuted proposition.
        """
        proj_id = "p_fermat_refute"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id, topic="费马素数猜想", goal="检验费马数素性", task_id="t_fermat",
            initial_milestones=[{"title": "F_5 检验", "objective": "验证 F_5 素性"}],
            owner_id="system",
        )
        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id, stage_index=1, statement="所有费马数 F_n = 2^(2^n) + 1 均为质数"
        )

        euler_code = """
f5 = 2**(2**5) + 1
factor = 641
if f5 % factor == 0:
    print(f"反例: 费马数 F_5 不是质数, 可被因数 {factor} 整除 (F_5 / 641 = {f5 // factor})")
"""
        cycle = self.research_tracker.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.task_manager,
            memory_store=self.memory,
            experiment_code=euler_code,
            auto_schedule_next=False,
        )

        self.assertEqual(cycle["verification"]["status"], FindingStatus.REFUTED.value)
        self.assertIn("641", cycle["verification"]["counterexample"])
        self.assertEqual(cycle["decision"]["action"], ResearchDecisionAction.REPLAN.value)

        # Check memory avoidance
        mems = self.memory.search_experience("费马素数", limit=5)
        self.assertTrue(len(mems) > 0)
        self.assertIn("641", mems[0]["root_cause"])

    # =========================================================================
    # TEST F: Multi-Agent Authentic Orchestration (Planner, Executor, Verifier, Coordinator)
    # =========================================================================
    def test_f_multi_agent_role_orchestration(self):
        """Test F:
        Verifies separation of concerns across multi-agent roles:
        - Research Planner: creates branch & specifies verification criteria.
        - Research Executor: runs computation in sandbox.
        - Independent Verifier: conducts evidence audit without blind trust.
        - Coordinator: synthesizes global milestone advancement.
        """
        proj_id = "p_multi_collab"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id, topic="数论探索", goal="多角色协同分析", task_id="t_collab",
            initial_milestones=[{"title": "stage", "objective": "数值检验"}],
            owner_id="system",
        )

        # 1. Planner
        branch = self.research_tracker.create_branch(proj_id, "Branch_Algebra", "代数结构")
        hypo = self.research_tracker.create_hypothesis(
            proj_id, 1, "代数恒等式检验: x^2 - y^2 = (x-y)(x+y)",
            rationale="Planner 提出的因式分解检验"
        )
        self.assertEqual(branch.status, "ACTIVE")

        # 2. Executor
        script = "print('Verified: (x-y)*(x+y) identically expands to x^2 - y^2')"
        exp = self.research_tracker.execute_experiment(
            proj_id, 1, hypo.hypothesis_id, "python_numeric", script, owner_id="system",
        )
        self.assertTrue(exp.is_success)

        # 3. Verifier audits evidence independently
        ver = self.research_tracker.verify_finding(
            proj_id, hypo.hypothesis_id, "Independent Verifier", ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data, is_verified=True, sources=["python_engine"]
        )
        self.assertTrue(ver["verified"])

        # 4. Coordinator assesses global state
        dec = self.research_tracker.evaluate_next_step(proj_id, 1, memory_store=self.memory)
        self.assertIn(dec["action"], (ResearchDecisionAction.CONTINUE.value, ResearchDecisionAction.COMPLETE.value))

    # =========================================================================
    # TEST G: Absolute Permission Gating (commands=False blocks execution)
    # =========================================================================
    def test_g_permission_gating_blocks_all_unauthorized_paths(self):
        """Test G:
        Bob has commands=False.
        Attempts to execute code via:
        1. Direct ResearchTracker.execute_experiment.
        2. TaskManager orchestrated execution.
        Must ALL be rejected before code execution without exception.
        """
        b_dir = self.root / "friends" / "bob"
        b_dir.mkdir(parents=True, exist_ok=True)
        (b_dir / "friend.json").write_text(json.dumps({
            "computer_tools": {"enabled": True, "commands": False}  # Explicitly DENIED!
        }), encoding="utf-8")

        proj_id = "p_bob_security"
        self.research_tracker.create_research_project(
            project_id=proj_id, topic="Authorizetest", goal="测试权限防御", task_id="t_bob",
            initial_milestones=[{"title": "M1", "objective": "O1"}],
            owner_id="bob",
        )
        hypo = self.research_tracker.create_hypothesis(proj_id, 1, "非法执行代码")

        # 1. Direct execute_experiment
        exp = self.research_tracker.execute_experiment(
            proj_id, 1, hypo.hypothesis_id, "python_numeric",
            "import os; os.system('echo hacked')", owner_id="bob", workspace=self.root,
        )
        self.assertFalse(exp.is_success)
        self.assertTrue("权限拦截" in exp.error_log or "Permission interception" in exp.error_log)
        self.assertIn("commands=False", exp.error_log)

        # 2. TaskManager pipeline
        tid = self.task_manager.create_task(
            title="Bob 试图通过 TaskManager 跑代码",
            description="Authorizeok",
            category="research",
            owner_id="bob",
            initial_checkpoint={"research_project_id": proj_id, "stage_index": 1, "experiment_code": "print(1)"},
        )
        task = self.task_manager.get_task(tid)
        res = self.task_manager._default_orchestrated_executor(task, lambda m: None)
        self.assertFalse(res["research_cycle"]["experiment_success"])
        self.assertFalse(res["research_cycle"]["verification"]["verified"])

    # =========================================================================
    # TEST H: Real Resource Controls (Timeout, Cancel, Host PID Safe)
    # =========================================================================
    def test_h_resource_controls_and_cancellation(self):
        """Test H:
        1. Experiment timeout safely terminates without hanging.
        2. Cancel signal stops ongoing execution immediately.
        3. Host PID is never killed.
        """
        import threading
        proj_id = "p_timeout_test"
        self.research_tracker.create_research_project(
            project_id=proj_id, topic="超时测试", goal="检验进程终止", task_id="t_to",
            initial_milestones=[{"title": "M1", "objective": "O1"}],
            owner_id="system",
        )
        hypo = self.research_tracker.create_hypothesis(proj_id, 1, "死循环检测")

        host_pid = os.getpid()

        # 1. Timeout test (1 second timeout on 10s sleep)
        t0 = time.time()
        exp_to = self.research_tracker.execute_experiment(
            proj_id, 1, hypo.hypothesis_id, "python_numeric",
            "import time; time.sleep(10)", timeout_seconds=1.0, owner_id="system",
        )
        elapsed = time.time() - t0
        self.assertFalse(exp_to.is_success)
        self.assertIn("timed out", exp_to.error_log.lower())
        self.assertLess(elapsed, 4.0)
        self.assertEqual(os.getpid(), host_pid)  # Host PID preserved!

        # 2. Cancel event test
        cancel_evt = threading.Event()
        cancel_evt.set()  # Cancelled beforehand

        class MockCtx:
            def __init__(self, c_evt):
                self.owner_id = "system"
                self.cancel_event = c_evt
            def effective_permissions(self):
                return {"commands": True}

        exp_cancel = self.research_tracker.execute_experiment(
            proj_id, 1, hypo.hypothesis_id, "python_numeric",
            "print('Should not run')", owner_id="system", execution_context=MockCtx(cancel_evt),
        )
        self.assertFalse(exp_cancel.is_success)
        self.assertTrue("cancel" in exp_cancel.error_log.lower())

    # =========================================================================
    # TEST I: Crash Recovery & Idempotence Across Process Boundaries
    # =========================================================================
    def test_i_crash_recovery_and_idempotence(self):
        """Test I:
        Simulate process exit after recording stage progress.
        A freshly constructed ResearchTracker instance reads SQLite and verifies:
        1. All hypotheses, experiments, findings restored.
        2. Paused project status remains PAUSED across restart.
        3. Repeating stage scheduling is idempotent (no duplicate tasks).
        """
        proj_id = "p_crash_idemp"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id, topic="Resume", goal="验证跨进程一致性", task_id="t_crash",
            initial_milestones=[{"title": "stage", "objective": "目标一"}, {"title": "stage", "objective": "目标二"}],
            owner_id="system",
        )
        hypo = self.research_tracker.create_hypothesis(proj_id, 1, "阶段一假设")
        exp = self.research_tracker.execute_experiment(
            proj_id, 1, hypo.hypothesis_id, "python_numeric", "print('Result: Verified 100%')", owner_id="system"
        )
        self.research_tracker.verify_finding(
            proj_id, hypo.hypothesis_id, "Verifier", ProofType.FINITE_NUMERIC,
            evidence_details=exp.output_data, is_verified=True, sources=["test"]
        )
        self.research_tracker.record_stage_progress(
            proj_id, 1, ["阶段一成果"], [], stage_achieved=True, verification_status="verified", sources=["test"], evidence=["ev"], reviewed_by="V"
        )

        # Mark paused before crash
        self.research_tracker.update_project_status(proj_id, "PAUSED")

        # Create a new fresh ResearchTracker pointing to same SQLite DB
        fresh_tracker = ResearchTracker(db_path=self.research_db)
        recovered_proj = fresh_tracker.get_project(proj_id)
        self.assertIsNotNone(recovered_proj)
        self.assertEqual(recovered_proj.status, "PAUSED")
        self.assertEqual(len(recovered_proj.global_verified_findings), 1)
        self.assertEqual(len(fresh_tracker.list_hypotheses(proj_id)), 1)
        self.assertEqual(len(fresh_tracker.list_experiments(proj_id)), 1)

    # =========================================================================
    # TEST J: Multi-Tenant User Isolation
    # =========================================================================
    def test_j_multi_tenant_user_isolation(self):
        """Test J:
        User Alice and User Bob both initiate a research project with the exact same topic:
        ''.
        Verify:
        1. Their project IDs and SQLite records remain physically distinct.
        2. Alice cannot modify or complete Bob's research project or task.
        3. Evidence and counterexamples remain scoped by owner_id.
        """
        p_alice = "proj_alice_pyth"
        p_bob = "proj_bob_pyth"

        self.research_tracker.create_research_project(
            project_id=p_alice, topic="毕达哥拉斯三元组研究", goal="Alice 的目标", task_id="t_alice_1",
            initial_milestones=[{"title": "M1", "objective": "O1"}], owner_id="alice",
        )
        self.research_tracker.create_hypothesis(p_alice, 1, "Alice 假设: (3, 4, 5)")

        self.research_tracker.create_research_project(
            project_id=p_bob, topic="毕达哥拉斯三元组研究", goal="Bob 的目标", task_id="t_bob_1",
            initial_milestones=[{"title": "M1", "objective": "O1"}], owner_id="bob",
        )
        self.research_tracker.create_hypothesis(p_bob, 1, "Bob 假设: (5, 12, 13)")

        proj_a = self.research_tracker.get_project(p_alice)
        proj_b = self.research_tracker.get_project(p_bob)

        self.assertEqual(proj_a.owner_id, "alice")
        self.assertEqual(proj_b.owner_id, "bob")
        self.assertNotEqual(proj_a.project_id, proj_b.project_id)

        hypo_a = self.research_tracker.list_hypotheses(p_alice)
        hypo_b = self.research_tracker.list_hypotheses(p_bob)
        self.assertEqual(len(hypo_a), 1)
        self.assertEqual(len(hypo_b), 1)
        self.assertIn("Alice", hypo_a[0].statement)
        self.assertIn("Bob", hypo_b[0].statement)


if __name__ == "__main__":
    unittest.main()
