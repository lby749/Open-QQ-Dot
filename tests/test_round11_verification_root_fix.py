"""Round 11 Rigorous Research Integrity and General Autonomous Deepening Test Suite.

Verifies the complete root-cause elimination of:
- P0-1: Symbolic proof unconditional pass (e.g., 'all even numbers are > 10' + natural language text).
- P0-2: Formal proof masqueraded by ordinary Python scripts (e.g., 'import sympy; print("READY")').
- P0-3: Self-asserted experiment declarations (e.g., JSON {'claim_verified': True}) rejected as UNTRUSTED.
- P0-4: Unknown research topics fake success fallback completely eliminated (NEEDS_PLANNING).
- P0-5: Research stage evidence level wrongly upgraded (EXPERIMENTALLY_SUPPORTED must never become VERIFIED).
- Security: All script execution bypasses strictly closed via RBAC commands permission.
- General autonomy: Phased 2-stage cycle, failure replanning, and crash recovery.
"""
from __future__ import annotations

import shutil

import os
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research_tracker import (
    ResearchTracker,
    ProofType,
    FindingStatus,
    ToolExecutionStatus,
    EvidenceValidity,
    StageStatus,
    ResearchProjectStatus,
    VerificationPlan,
    VerificationContract,
    evaluate_experiment_output,
)
from task_manager import TaskManager
from agent_memory import AgentMemoryStore


class TestRound11VerificationRootFix(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_data" / "round11_root_fix"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.research_db = self.test_dir / "research_round11.sqlite3"
        self.task_db = self.test_dir / "task_round11.sqlite3"
        self.memory_db = self.test_dir / "memory_round11.sqlite3"

        if self.research_db.exists():
            self.research_db.unlink()
        if self.task_db.exists():
            self.task_db.unlink()
        if self.memory_db.exists():
            self.memory_db.unlink()

        self.research_tracker = ResearchTracker(db_path=self.research_db)
        self.task_manager = TaskManager(db_path=self.task_db)
        self.memory = AgentMemoryStore(db_path=self.memory_db)

    def tearDown(self):
        if self.test_dir.exists():
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    # =========================================================================
    # TEST 1 (P0-1): Symbolic Proof Unconditional Pass Root Cause Elimination
    # =========================================================================
    def test_01_symbolic_proof_unconditional_pass_strictly_eliminated(self):
        """P0-1 Test:
        Claim: " 10" (All even numbers are greater than 10).
        Evidence: Pure Chinese descriptive text with no machine-executable SymPy script.
        Caller sets is_verified=True.

        In Round 10, this hit line 1150 'return True, ""' and got falsely VERIFIED.
        In Round 11, it MUST be strictly rejected as UNVERIFIED / WAITING_VERIFICATION.
        """
        proj_id = "p_even_gt_10"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="数论偶数性质伪命题",
            goal="验证所有偶数均大于10",
            task_id="t_even",
            initial_milestones=[{"title": "符号推导阶段", "objective": "偶数大小推导"}],
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="普遍命题：所有偶数均严格大于 10",
        )

        bogus_text_evidence = (
            "根据基础数论分析，偶数具有因子2的整除性。\n"
            "在充分展开的情况下，显而易见偶数均大于10。\n"
            "因此该命题在逻辑上自洽，证明完毕。"
        )

        res = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=bogus_text_evidence,
            is_verified=True,  # Caller falsely asserts verified
            sources=["纯自然语言文字推导"],
            is_manual_approval=False,
        )

        # MUST BE REJECTED!
        self.assertFalse(res["verified"], "Pure natural language must NOT verify symbolic proof!")
        self.assertIn(res["status"], [FindingStatus.WAITING_VERIFICATION.value, FindingStatus.UNVERIFIED.value])
        self.assertTrue("纯自然语言" in res["rejection_reason"] or "natural language" in res["rejection_reason"].lower())

    # =========================================================================
    # TEST 2 (P0-2): Formal Proof Masqueraded by Ordinary Python Scripts
    # =========================================================================
    def test_02_formal_proof_cannot_be_masqueraded_by_python_scripts(self):
        """P0-2 Test:
        Claim: "1 + 1 = 3" or any proposition claiming FORMAL_PROOF.
        Evidence: Python script that cleanly executes `import sympy; print("READY")`.
        Caller sets proof_type=ProofType.FORMAL_PROOF and is_verified=True.

        In Round 10, line 1094 executed `subprocess.run([sys.executable, "-c", ev])`,
        exited with 0, and wrongly marked it VERIFIED!
        In Round 11, FORMAL_PROOF MUST reject Python scripts and require genuine proof assistants (Lean/Coq).
        """
        proj_id = "p_sympy_masquerade"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="形式化定理证明",
            goal="检验形式化证明是否可被普通 Python 脚本冒充",
            task_id="t_form_masq",
            initial_milestones=[{"title": "形式证明核验", "objective": "核验证明产物"}],
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全称命题：任意自然数加法满足结合律与交换律",
        )

        python_script_evidence = """
import sympy
print("READY")
"""
        res = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Formal Proof Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=python_script_evidence,
            is_verified=True,
            sources=["Python Environment with SymPy"],
        )

        # MUST BE REJECTED!
        self.assertFalse(res["verified"])
        self.assertTrue("普通 Python/SymPy 脚本" in res["rejection_reason"] or "symbolic algebra" in res["rejection_reason"].lower())
        self.assertIn("ProofType.FORMAL_PROOF", res["rejection_reason"])

    # =========================================================================
    # TEST 3 (P0-3): Self-Asserted Experiment Declaration Rejected as UNTRUSTED
    # =========================================================================
    def test_03_self_asserted_experiment_declarations_rejected(self):
        """P0-3 Test:
        Experiment script merely self-declares:
        `print('{"claim_verified": True, "confidence": 1.0}')`
        or `print('Analysis result: Hypothesis verified under specified scope')`.

        The verifier MUST NOT trust the experiment's self-declaration.
        Without substantive verification facts/data, EvidenceValidity must be UNTRUSTED,
        claim_supported must be False, and stage must NOT achieve.
        """
        proj_id = "p_self_declare"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="自我声明实验检验",
            goal="验证自称验证通过的程序不被采信",
            task_id="t_self_decl",
            initial_milestones=[{"title": "stage", "objective": "验证命题"}],
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="某未知复杂命题通过自我声明",
        )

        self_asserting_code = """
import json
print(json.dumps({"claim_verified": True, "confidence": 1.0, "status": "verified"}))
"""
        cycle = self.research_tracker.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.task_manager,
            memory_store=self.memory,
            experiment_code=self_asserting_code,
            auto_schedule_next=False,
        )

        # Verification must reject self-declaration
        self.assertTrue(cycle["experiment_success"], "Tool exited 0")
        self.assertFalse(cycle["experiment_valid"], "Self-asserted data without payload is invalid")
        self.assertFalse(cycle["claim_supported"], "Claim must NOT be supported by self-declaration")
        self.assertIn("UNTRUSTED", cycle["verification"]["rejection_reason"])

        # Stage MUST NOT be ACHIEVED
        proj_check = self.research_tracker.get_project(proj_id)
        self.assertNotEqual(proj_check.milestones[0].status, "ACHIEVED")
        self.assertNotEqual(proj_check.status, "COMPLETED")

    # =========================================================================
    # TEST 4 (P0-4): Unknown Research Topics Fake Success Fallback Eliminated
    # =========================================================================
    def test_04_unknown_research_topic_fake_success_fallback_eliminated(self):
        """P0-4 Test:
        A completely unknown open-ended research topic (e.g. "").
        System has no hardcoded templates.

        In Round 10, synthesize_stage_plan_and_code() fell back to printing
        `Analysis result: Hypothesis verified under specified scope`!
        In Round 11, this fake success fallback is eliminated; it returns NEEDS_PLANNING / WAIT safely.
        """
        proj_id = "p_unknown_topic"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="图论哈密顿回路复杂拓扑动力学分析",
            goal="对未知网络拓扑进行哈密顿路径完全判别",
            task_id="t_unknown",
            initial_milestones=[{"title": "拓扑结构提取", "objective": "分析网络矩阵"}],
        )

        # Run autonomous cycle with no pre-provided experiment code
        cycle = self.research_tracker.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.task_manager,
            memory_store=self.memory,
            experiment_code="",  # No handcrafted test code!
            auto_schedule_next=False,
        )

        # Must enter NEEDS_PLANNING / WAIT safely without faking success
        self.assertFalse(cycle["experiment_success"])
        self.assertFalse(cycle["claim_supported"])
        self.assertEqual(cycle["decision"]["action"], "WAIT")
        self.assertIn("NEEDS_PLANNING", cycle["verification"]["rejection_reason"])

        proj_check = self.research_tracker.get_project(proj_id)
        self.assertNotEqual(proj_check.milestones[0].status, "ACHIEVED")
        self.assertNotEqual(proj_check.status, "COMPLETED")

    # =========================================================================
    # TEST 5 (P0-5): Evidence Level Not Wrongly Upgraded (EXPERIMENTALLY_SUPPORTED)
    # =========================================================================
    def test_05_experimentally_supported_must_never_upgrade_to_universal_verified(self):
        """P0-5 Test:
        Finite numerical verification of prime numbers in [2, 30].
        The result is valid EXPERIMENTALLY_SUPPORTED.

        In Round 10, line 2097 passed verification_status=FindingStatus.VERIFIED,
        wrongly upgrading finite empirical data to universal VERIFIED!
        In Round 11, the recorded evidence and milestone finding MUST strictly preserve
        the EXPERIMENTALLY_SUPPORTED status, and NOT write to project.global_verified_findings
        as an unconstrained universal theorem.
        """
        proj_id = "p_finite_primes"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="质数区间有限数值探索",
            goal="探索区间 [2, 30] 内的质数分布",
            task_id="t_fin_prime",
            initial_milestones=[{"title": "区间 [2, 30] 检验", "objective": "计算素数"}],
        )

        code = """
primes = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
print(f"Primes found: {len(primes)} -> {primes}")
"""
        cycle = self.research_tracker.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.task_manager,
            memory_store=self.memory,
            experiment_code=code,
            auto_schedule_next=False,
        )

        # Evidence status must be EXPERIMENTALLY_SUPPORTED
        self.assertTrue(cycle["experiment_valid"])
        self.assertTrue(cycle["claim_supported"])
        self.assertEqual(cycle["verification"]["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # Milestone recorded evidence status must NOT be 'verified'
        evidence_list = self.research_tracker.list_evidence(proj_id)
        self.assertTrue(len(evidence_list) > 0)
        self.assertEqual(evidence_list[0]["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # P0-5 Core Invariant: Evidence is NEVER escalated to universal 'verified'
        # and project status is NEVER prematurely set to 'COMPLETED'
        self.assertNotEqual(evidence_list[0]["status"], "verified")
        proj_check = self.research_tracker.get_project(proj_id)
        self.assertNotEqual(proj_check.status, "COMPLETED")
        self.assertTrue(all("[finite_numeric]" in f for f in proj_check.global_verified_findings))

    # =========================================================================
    # TEST 6: Unified RBAC Permission Gating Closes All Execution Bypasses
    # =========================================================================
    def test_06_rbac_permission_gating_blocks_verify_finding_bypasses(self):
        """Security Test:
        An unauthorized user (commands=False) calls verify_finding() with a symbolic script,
        attempting to execute python code through the verification path.
        The unified sandboxed executor MUST strictly deny execution.
        """
        proj_id = "p_unauth_exec"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="Authorizeok",
            goal="测试无权限用户无法通过 verify_finding 旁路执行代码",
            task_id="t_unauth",
            owner_id="restricted_user_no_commands",
            initial_milestones=[{"title": "stage", "objective": "目标"}],
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="普通测试假设",
        )

        script = "import sympy; x = sympy.Symbol('x'); assert x == x"
        res = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=script,
            is_verified=True,
            owner_id="restricted_user_no_commands",
        )

        # Execution must fail due to permission rejection
        self.assertFalse(res["verified"])
        self.assertTrue("权限拦截" in res["rejection_reason"] or "Permission interception" in res["rejection_reason"])

    # =========================================================================
    # TEST 7: Irrelevant Data Cannot Support Claim
    # =========================================================================
    def test_07_irrelevant_data_cannot_support_claim(self):
        """Verification Test:
        Claim: Number theory prime distribution.
        Output: System CPU memory usage statistics (totally unrelated data).
        The system must NOT consider this claim supported.
        """
        statement = "区间 [2, 30] 内共有 10 个质数"
        irrelevant_output = "System CPU usage: 24.5%, Memory free: 8192MB, Thread count: 16"
        valid, supported, ce, expl = evaluate_experiment_output(
            statement=statement,
            output_data=irrelevant_output,
            error_log="",
            tool_exit_success=True,
            verification_plan=VerificationPlan(
                hypothesis_id="h_irrelevant",
                statement=statement,
                scope="[2, 30]",
                expected_output_pattern=r"primes|primes found",
            ),
        )

        self.assertFalse(supported, "Irrelevant telemetry data must NOT support number theory hypothesis")

    # =========================================================================
    # TEST 8: Real Lean 4 Formal Proof Genuine Machine-Verification (End-to-End)
    # =========================================================================
    @unittest.skipUnless(shutil.which('lean'), 'Lean 4 is not installed')
    def test_08_real_lean4_formal_proof_verification_success(self):
        """End-to-End Formal Proof Test:
        Using real local Lean 4 (version 4.35+) compiler installed in ~/.elan/bin.
        Submitting a genuine, rigorous Lean 4 theorem proof.
        The system executes `lean` and verifies proof assistant zero exit code!
        """
        proj_id = "p_lean4_real"
        proj = self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="实机 Lean 4 形式化定理核验",
            goal="验证真正的 Lean 4 证明器端到端闭环",
            task_id="t_lean4",
            initial_milestones=[{"title": "自然数算术交换律形式化证明", "objective": "核验证明"}],
        )

        hypo = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全称定理：对于任意自然数 n, m，自然数加法满足交换律 n + m = m + n",
        )

        # Genuine Lean 4 proof script
        genuine_lean_code = """
theorem add_comm_thm (n m : Nat) : n + m = m + n := Nat.add_comm n m
"""
        res = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Formal Proof Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=genuine_lean_code,
            is_verified=True,
            sources=["Lean 4 Theorem Prover (~/.elan/bin/lean)"],
            owner_id="system",
        )

        self.assertTrue(res["verified"], f"Real Lean 4 proof should verify successfully: {res.get('rejection_reason')}")
        self.assertEqual(res["status"], FindingStatus.VERIFIED.value)
        self.assertEqual(res["rejection_reason"], "")

        # Record genuine formal finding into milestone & project
        finding_str = f"[{ProofType.FORMAL_PROOF.value}] {hypo.statement}"
        self.research_tracker.record_stage_progress(
            project_id=proj_id,
            stage_index=1,
            new_findings=[finding_str],
            negative_attempts=[],
            stage_achieved=True,
            verification_status=FindingStatus.VERIFIED,
            sources=["Lean 4"],
            evidence=[genuine_lean_code],
            reviewed_by="Lean 4 Verifier",
        )

        proj_check = self.research_tracker.get_project(proj_id)
        self.assertIn(finding_str, proj_check.global_verified_findings)

    # =========================================================================
    # TEST 9: Real Lean 4 Rejects Bogus Theorem & Incomplete 'sorry' Scripts
    # =========================================================================
    @unittest.skipUnless(shutil.which('lean'), 'Lean 4 is not installed')
    def test_09_real_lean4_formal_proof_rejection_on_mismatch_or_sorry(self):
        """End-to-End Formal Proof Security & Rigor Test:
        Case A: Theorem with type mismatch (e.g. n + m = m + n + 1) fails lean compilation.
        Case B: Theorem containing 'sorry' is intercepted before compilation as incomplete proof.
        """
        proj_id = "p_lean4_reject"
        self.research_tracker.create_research_project(
            project_id=proj_id,
            topic="Lean 4 伪证明与未完成脚本拒绝测试",
            goal="核验证明器能否准确拦截伪命题与占位符",
            task_id="t_lean_rej",
            initial_milestones=[{"title": "伪证明拦截", "objective": "拦截测试"}],
        )

        # Case A: False theorem with type mismatch
        hypo_a = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全称伪定理：n + m = m + n + 1",
        )
        fake_lean_code = """
theorem fake_add_thm (n m : Nat) : n + m = m + n + 1 := Nat.add_comm n m
"""
        res_a = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo_a.hypothesis_id,
            verifier_role="Lean 4 Formal Proof Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=fake_lean_code,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="system",
        )
        self.assertFalse(res_a["verified"])
        self.assertTrue("Lean 4 定理核验未通过" in res_a["rejection_reason"] or "theorem verification failed" in res_a["rejection_reason"].lower())

        # Case B: Incomplete theorem with 'sorry'
        hypo_b = self.research_tracker.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="未完成定理证明带 sorry 占位",
        )
        sorry_code = """
theorem incomplete_thm (n : Nat) : n = n := by
  sorry
"""
        res_b = self.research_tracker.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo_b.hypothesis_id,
            verifier_role="Lean 4 Formal Proof Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=sorry_code,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="system",
        )
        self.assertFalse(res_b["verified"])
        self.assertTrue("未完成占位标记 'sorry'" in res_b["rejection_reason"] or "incomplete placeholder 'sorry'" in res_b["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
