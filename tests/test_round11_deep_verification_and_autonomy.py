"""Round 11 Deep Verification & Autonomous Research Closure Tests.

Verifies the 5 fundamental pillars required in Round 11:
1. Proposition-Evidence Strong Binding:
   - SymPy 'assert True', 'assert 1 == 1', and irrelevant identities (x + 0 == x) are rejected.
   - Lean 4 irrelevant theorems (e.g. 1 = 1) and 'sorry' proof escapes are rejected.
   - Genuine matching symbolic proofs and machine-checked Lean 4 theorems pass correctly.
2. Anti-Spoofing & Independent Verification:
   - Self-declaring outputs (claim_verified=true, count=999) without structured datasets are UNTRUSTED.
   - Fabricated numbers (e.g. composite 27 in prime list, bad triples (3, 4, 6)) are caught by independent verifier and refuted.
   - Genuine verifiable datasets pass independent spot check.
3. Secure Human Approval Audit & No-Impersonation:
   - Manual approval CANNOT masquerade as FORMAL_PROOF or SYMBOLIC machine proof.
   - Unauthorized callers cannot sign approvals (PermissionError).
   - Legitimate owner/admin approval records MANUAL_AUDIT with complete audit notes.
4. Multi-Tenant & Cross-Project Isolation:
   - Cross-tenant modification raises PermissionError.
   - Cross-project hypothesis mismatch raises ValueError.
5. Autonomous Replanning on Counterexample:
   - Refuted propositions trigger REPLAN, record counterexamples, synthesize refined hypotheses, and create research branches.
"""
from __future__ import annotations

import pytest
pytest.importorskip('sympy')

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research_tracker import (
    ResearchTracker,
    ProofType,
    FindingStatus,
    ResearchDecisionAction,
    evaluate_experiment_output,
    check_formal_checker_availability,
)
from task_manager import TaskManager
from agent_memory import AgentMemoryStore


class TestRound11DeepVerificationAndAutonomy(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_data" / "r11_deep"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / "research_r11.sqlite3"
        if self.db_path.exists():
            self.db_path.unlink()
        self.rt = ResearchTracker(db_path=self.db_path)
        self.tm = TaskManager(db_path=self.test_dir / "task_r11.sqlite3")
        self.mem = AgentMemoryStore(db_path=self.test_dir / "mem_r11.sqlite3")

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    # =========================================================================
    # PILLAR 1: Proposition & Evidence Strong Binding (Symbolic & Formal Proof)
    # =========================================================================
    def test_01_symbolic_trivial_assertion_rejected(self):
        """Pillar 1: SymPy assert True and assert 1 == 1 cannot prove mathematical propositions."""
        proj = self.rt.create_research_project(
            project_id="p_sym_bind_1",
            topic="素数无限性探索",
            goal="证明素数无限",
            task_id="t_sym_1",
            initial_milestones=[{"title": "代数检验", "objective": "检验素数公式"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id="p_sym_bind_1",
            stage_index=1,
            statement="欧几里得素数构造命题：P = p1*p2*...*pn + 1 必包含新素因数",
            owner_id="system",
        )

        # Attack 1A: assert True
        res_true = self.rt.verify_finding(
            project_id="p_sym_bind_1",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details="import sympy as sp\nassert True",
            is_verified=True,
            sources=["SymPy Fake"],
            owner_id="system",
        )
        self.assertFalse(res_true["verified"])
        self.assertIn("TRIVIAL_ASSERTION_REJECTED", res_true["rejection_reason"])

        # Attack 1B: assert 1 == 1
        res_const = self.rt.verify_finding(
            project_id="p_sym_bind_1",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details="import sympy as sp\nassert 1 == 1",
            is_verified=True,
            sources=["SymPy Fake Constant"],
            owner_id="system",
        )
        self.assertFalse(res_const["verified"])
        self.assertIn("TRIVIAL_CONSTANT_COMPARISON", res_const["rejection_reason"])

    def test_02_symbolic_irrelevant_identity_rejected(self):
        """Pillar 1: Asserting an irrelevant mathematical identity cannot prove the proposition."""
        proj = self.rt.create_research_project(
            project_id="p_sym_bind_2",
            topic="勾股数伸缩性研究",
            goal="验证 (3k, 4k, 5k) 恒满足勾股定理",
            task_id="t_sym_2",
            initial_milestones=[{"title": "代数伸缩", "objective": "验证倍数勾股数"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id="p_sym_bind_2",
            stage_index=1,
            statement="勾股数具有倍数伸缩性: 对任意整数 k，(3k, 4k, 5k) 恒满足勾股定理 a^2 + b^2 = c^2",
            owner_id="system",
        )

        # Deceptive script proving x + 0 == x instead of Pythagorean theorem
        irrelevant_script = """
import sympy as sp
x = sp.Symbol('x')
# This is mathematically TRUE, but completely IRRELEVANT to Pythagorean triples!
assert sp.simplify(x + 0 - x) == 0
"""
        res = self.rt.verify_finding(
            project_id="p_sym_bind_2",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=irrelevant_script,
            is_verified=True,
            sources=["SymPy Irrelevant Identity"],
            owner_id="system",
        )
        self.assertFalse(res["verified"])
        self.assertIn("PROPOSITION_EVIDENCE_MISMATCH", res["rejection_reason"])

    def test_03_symbolic_genuine_matching_proof_passes(self):
        """Pillar 1: Genuine symbolic derivation bound to the proposition passes verification."""
        proj = self.rt.create_research_project(
            project_id="p_sym_bind_3",
            topic="勾股数伸缩性研究",
            goal="验证 (3k, 4k, 5k) 恒满足勾股定理",
            task_id="t_sym_3",
            initial_milestones=[{"title": "代数伸缩", "objective": "验证倍数勾股数"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id="p_sym_bind_3",
            stage_index=1,
            statement="勾股数具有倍数伸缩性: 对任意整数 k，(3k, 4k, 5k) 恒满足勾股定理",
            owner_id="system",
        )

        genuine_script = """
import sympy as sp
k = sp.Symbol('k', integer=True)
a = 3 * k
b = 4 * k
c = 5 * k
lhs = a**2 + b**2
rhs = c**2
assert sp.simplify(lhs - rhs) == 0
"""
        res = self.rt.verify_finding(
            project_id="p_sym_bind_3",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=genuine_script,
            is_verified=True,
            sources=["SymPy Algebraic Derivation"],
            owner_id="system",
        )
        self.assertTrue(res["verified"])
        self.assertEqual(res["status"], FindingStatus.VERIFIED.value)

    def test_04_lean4_irrelevant_theorem_rejected(self):
        """Pillar 1: Lean 4 theorem proving 1 = 1 cannot prove prime or collatz claims."""
        proj = self.rt.create_research_project(
            project_id="p_lean_bind_1",
            topic="素数分布定理形式化",
            goal="形式化证明素数定理",
            task_id="t_lean_1",
            initial_milestones=[{"title": "素数性质证明", "objective": "证明定理"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id="p_lean_bind_1",
            stage_index=1,
            statement="素数分布定理：正整数区间内存在特定素数",
            owner_id="system",
        )

        # Deceptive Lean 4 code proving an unrelated identity theorem foo : 1 = 1 := rfl
        irrelevant_lean = """
theorem trivial_foo : 1 = 1 := rfl
"""
        res = self.rt.verify_finding(
            project_id="p_lean_bind_1",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=irrelevant_lean,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="system",
        )
        self.assertFalse(res["verified"])
        self.assertIn("FORMAL_GOAL_PROPOSITION_MISMATCH", res["rejection_reason"])

    def test_05_lean4_genuine_theorem_checked_by_compiler(self):
        """Pillar 1: Genuine Lean 4 proof is checked and verified by Lean compiler."""
        checker_status = check_formal_checker_availability()
        if not checker_status["lean"]:
            self.skipTest("Lean 4 executable not found in test environment.")

        proj = self.rt.create_research_project(
            project_id="p_lean_genuine",
            topic="自然数加法交换律研究",
            goal="形式化核验加法交换律",
            task_id="t_lean_gen",
            initial_milestones=[{"title": "加法交换律证明", "objective": "Lean 4 形式化核验"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id="p_lean_genuine",
            stage_index=1,
            statement="自然数加法交换律：对任意自然数 n m，满足 n + m = m + n",
            owner_id="system",
        )

        genuine_lean_code = """
theorem nat_add_comm (n m : Nat) : n + m = m + n := Nat.add_comm n m
"""
        res = self.rt.verify_finding(
            project_id="p_lean_genuine",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=genuine_lean_code,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="system",
        )
        self.assertTrue(res["verified"])
        self.assertEqual(res["status"], FindingStatus.VERIFIED.value)

    # =========================================================================
    # PILLAR 2: Anti-Spoofing & Independent Verification
    # =========================================================================
    def test_06_anti_spoofing_self_declaration_rejected(self):
        """Pillar 2: claim_verified=true or count=999 self-declarations cannot be accepted."""
        stmt = "区间 [2, 30] 内共有 10 个质数"
        bogus_output = '{"claim_verified": True, "count": 999, "status": "verified"}'
        v, s, ce, reason = evaluate_experiment_output(
            statement=stmt, output_data=bogus_output, error_log="", tool_exit_success=True
        )
        self.assertFalse(v)
        self.assertFalse(s)
        self.assertIn("UNTRUSTED_SELF_DECLARATION", reason)

    def test_07_independent_verifier_catches_fabricated_prime_data(self):
        """Pillar 2: Fabricated prime list with composite numbers is refuted by independent verifier."""
        stmt = "区间 [2, 30] 内共有 10 个质数"
        # 27 is composite (3 * 9 = 27), falsely inserted by untrusted script
        fabricated_output = "Primes found: [2, 3, 5, 7, 11, 13, 17, 19, 23, 27, 29]"
        v, s, ce, reason = evaluate_experiment_output(
            statement=stmt, output_data=fabricated_output, error_log="", tool_exit_success=True
        )
        self.assertTrue(v)
        self.assertFalse(s)  # Must be refuted due to composite number!
        self.assertIn("27", ce)
        self.assertIn("伪造素数数据反例", ce)

    def test_08_independent_verifier_catches_fabricated_pythagorean_triples(self):
        """Pillar 2: Fabricated triple (3, 4, 6) is caught and refuted by independent calculation."""
        stmt = "寻找勾股数满足 a^2 + b^2 = c^2"
        # 3^2 + 4^2 = 25 != 36
        fabricated_output = "Found triples: [(3, 4, 5), (3, 4, 6)]"
        v, s, ce, reason = evaluate_experiment_output(
            statement=stmt, output_data=fabricated_output, error_log="", tool_exit_success=True
        )
        self.assertTrue(v)
        self.assertFalse(s)
        self.assertTrue("伪造勾股数数据反例" in ce or "Fabricated Pythagorean triple" in ce)
        self.assertIn("(3, 4, 6)", ce)

    def test_09_genuine_dataset_passes_independent_verification(self):
        """Pillar 2: Genuine prime list passes spot-check and is capped at EXPERIMENTALLY_SUPPORTED."""
        stmt = "区间 [2, 30] 内共有 10 个质数"
        genuine_output = "Primes found: [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]"
        v, s, ce, reason = evaluate_experiment_output(
            statement=stmt, output_data=genuine_output, error_log="", tool_exit_success=True
        )
        self.assertTrue(v)
        self.assertTrue(s)
        self.assertEqual(ce, "")

    # =========================================================================
    # PILLAR 3: Human Approval Audit & No-Impersonation
    # =========================================================================
    def test_10_manual_approval_cannot_impersonate_formal_proof(self):
        """Pillar 3: Setting is_manual_approval=True CANNOT produce FORMAL_PROOF."""
        proj = self.rt.create_research_project(
            project_id="p_appr_1", topic="形式化证明安全审计", goal="防止冒充形式化证明",
            task_id="t_appr_1", initial_milestones=[{"title": "stage", "objective": "形式化验证"}],
            owner_id="admin",
        )
        hypo = self.rt.create_hypothesis(proj.project_id, 1, "某未证明的高深数学命题", owner_id="admin")

        res = self.rt.verify_finding(
            project_id=proj.project_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Security Auditor",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details="Expert examined and manually approved.",
            is_verified=True,
            is_manual_approval=True,
            owner_id="admin",
            approver_id="admin",
            audit_notes="Expert sign-off",
        )
        # MUST BE STRICTLY REJECTED!
        self.assertFalse(res["verified"])
        self.assertEqual(res["status"], FindingStatus.WAITING_VERIFICATION.value)
        self.assertTrue("安全规范拦截" in res["rejection_reason"] or "Security rule" in res["rejection_reason"])
        self.assertTrue("严禁冒充形式化证明" in res["rejection_reason"] or "cannot pretend to be formal proof" in res["rejection_reason"])

    def test_11_unauthorized_user_cannot_sign_manual_approval(self):
        """Pillar 3: Non-owner, non-admin user cannot sign manual approvals (PermissionError)."""
        proj = self.rt.create_research_project(
            project_id="p_appr_2", topic="专有算法研究", goal="内部研究",
            task_id="t_appr_2", initial_milestones=[{"title": "stage", "objective": "目标一"}],
            owner_id="alice",
        )
        hypo = self.rt.create_hypothesis(proj.project_id, 1, "内部假说", owner_id="alice")

        # Bob (unauthorized) tries to approve Alice's research finding
        with self.assertRaises(PermissionError):
            self.rt.verify_finding(
                project_id=proj.project_id,
                hypothesis_id=hypo.hypothesis_id,
                verifier_role="Fake Approver",
                proof_type=ProofType.EMPIRICAL_DATA,
                evidence_details="Bob signed approval without permission.",
                is_verified=True,
                is_manual_approval=True,
                owner_id="bob",
                approver_id="bob",
                audit_notes="Unauthorized signature",
            )

    def test_12_authorized_manual_audit_converts_to_manual_audit_type(self):
        """Pillar 3: Legitimate owner approval records as ProofType.MANUAL_AUDIT with audit trail."""
        proj = self.rt.create_research_project(
            project_id="p_appr_3", topic="实证分析", goal="专家签署实证结果",
            task_id="t_appr_3", initial_milestones=[{"title": "stage", "objective": "目标一"}],
            owner_id="alice",
        )
        hypo = self.rt.create_hypothesis(proj.project_id, 1, "实证数据分析结果符合预期", owner_id="alice")

        res = self.rt.verify_finding(
            project_id=proj.project_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Principal Investigator",
            proof_type=ProofType.EMPIRICAL_DATA,
            evidence_details="Comprehensive laboratory empirical survey confirmed.",
            is_verified=True,
            is_manual_approval=True,
            owner_id="alice",
            approver_id="alice",
            audit_notes="Reviewed raw lab datasets and confirmed accuracy.",
        )
        self.assertTrue(res["verified"])
        self.assertEqual(res["status"], FindingStatus.VERIFIED.value)
        self.assertEqual(res["proof_type"], ProofType.MANUAL_AUDIT.value)

    # =========================================================================
    # PILLAR 4: Multi-Tenant & Cross-Project Isolation
    # =========================================================================
    def test_13_cross_tenant_access_interception(self):
        """Pillar 4: User Bob cannot verify findings in User Alice's project."""
        proj_alice = self.rt.create_research_project(
            project_id="p_alice_secret", topic="Alice的绝密项目", goal="目标",
            task_id="t_alice", initial_milestones=[{"title": "M1", "objective": "O1"}],
            owner_id="alice",
        )
        hypo_alice = self.rt.create_hypothesis(proj_alice.project_id, 1, "Alice假说", owner_id="alice")

        # Bob attempts to operate on Alice's project
        with self.assertRaises(PermissionError):
            self.rt.verify_finding(
                project_id=proj_alice.project_id,
                hypothesis_id=hypo_alice.hypothesis_id,
                verifier_role="Bob Hacker",
                proof_type=ProofType.FINITE_NUMERIC,
                evidence_details="Data",
                is_verified=True,
                owner_id="bob",
            )

    def test_14_cross_project_hypothesis_mismatch_interception(self):
        """Pillar 4: hypothesis_id belonging to Project X cannot be passed to Project Y."""
        proj_x = self.rt.create_research_project(
            project_id="p_proj_x", topic="项目X", goal="目标X",
            task_id="t_x", initial_milestones=[{"title": "M1", "objective": "O1"}],
            owner_id="system",
        )
        proj_y = self.rt.create_research_project(
            project_id="p_proj_y", topic="项目Y", goal="目标Y",
            task_id="t_y", initial_milestones=[{"title": "M1", "objective": "O1"}],
            owner_id="system",
        )
        hypo_x = self.rt.create_hypothesis(proj_x.project_id, 1, "属于项目X的假说", owner_id="system")

        # Caller calls verify_finding on Project Y with hypo_x!
        with self.assertRaises(ValueError) as cm:
            self.rt.verify_finding(
                project_id=proj_y.project_id,
                hypothesis_id=hypo_x.hypothesis_id,
                verifier_role="Verifier",
                proof_type=ProofType.FINITE_NUMERIC,
                evidence_details="Data",
                is_verified=True,
                owner_id="system",
            )
        self.assertTrue("跨项目越权拦截" in str(cm.exception) or "Cross-project access" in str(cm.exception))

    # =========================================================================
    # PILLAR 5: Autonomous Replanning on Refutation
    # =========================================================================
    def test_15_autonomous_replanning_and_refinement_branch(self):
        """Pillar 5: Counterexample triggers REPLAN, synthesizes refined hypothesis, and creates research branch."""
        proj_id = "p_replan_test"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="全实数平方根伪命题",
            goal="验证 sqrt(x^2) = x",
            task_id="t_replan",
            initial_milestones=[{"title": "实数检验", "objective": "全实数域检验"}],
            owner_id="system",
        )
        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全实数平方根恒等式伪命题：对任意实数 x，sqrt(x^2) = x 恒成立",
            owner_id="system",
        )

        refute_code = """
import sympy as sp
x = sp.Symbol('x', real=True)
val_lhs = sp.sqrt(x**2).subs(x, -5)  # 5
val_rhs = x.subs(x, -5)              # -5
if val_lhs != val_rhs:
    print(f"反例: 在 x = -5 处，sqrt((-5)^2) = {val_lhs} != {val_rhs}，命题在负数域不成立!")
"""
        cycle = self.rt.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.tm,
            memory_store=self.mem,
            experiment_code=refute_code,
            auto_schedule_next=False,
        )

        # 1. Hypothesis refuted
        self.assertEqual(cycle["verification"]["status"], FindingStatus.REFUTED.value)
        self.assertIn("反例", cycle["verification"]["counterexample"])

        # 2. Decision is REPLAN
        self.assertEqual(cycle["decision"]["action"], ResearchDecisionAction.REPLAN.value)
        self.assertTrue("修正假设" in cycle["decision"]["next_plan"] or "Refined hypothesis" in cycle["decision"]["next_plan"])

        # 3. Research Branch created autonomously
        branches = self.rt.list_branches(proj_id)
        self.assertGreaterEqual(len(branches), 1)
        refine_branch = branches[0]
        self.assertIn("refinement_stage_1", refine_branch.branch_name)
        self.assertTrue("修正假设" in refine_branch.focus_hypothesis or "Refined hypothesis" in refine_branch.focus_hypothesis)


if __name__ == "__main__":
    unittest.main()
