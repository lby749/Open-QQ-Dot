"""Adversarial Complex Mathematical Proof Stress Test for ResearchTracker.

Tests three deep mathematical scenarios:
1. Genuine Complex Symbolic Proof: Cayley-Hamilton Theorem for general 3x3 symbolic matrices (9 free variables).
   - Valid proof must be verified by sandboxed SymPy simplification.
   - Tampered proof (with minute algebraic flaw) MUST be rejected and flagged as failed.
2. Classic Deceptive Number Theory Pseudo-Proof: Euler's Prime Generating Polynomial P(n) = n^2 - n + 41.
   - For n in [1, 40], all outputs are prime. System must refuse to extrapolate this to universal proof.
   - At n = 41 (41^2 is composite), system must catch the counterexample and trigger instant REFUTED.
3. Sophisticated Pseudo Formal Proof (Lean4 fake homology proof):
   - Elaborate 150-line fake Lean proof assistant script.
   - Without machine checker installed, system must honestly report capability boundary and downgrade to NEEDS_HUMAN_REVIEW.
"""
from __future__ import annotations

import pytest
pytest.importorskip('sympy')

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
    VerificationPlan,
    evaluate_experiment_output,
    check_formal_checker_availability,
)
from task_manager import TaskManager
from agent_memory import AgentMemoryStore


class AdversarialComplexMathProofTest(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_data" / "adv_math"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / "research_adv.sqlite3"
        if self.db_path.exists():
            self.db_path.unlink()
        self.rt = ResearchTracker(db_path=self.db_path)
        self.tm = TaskManager(db_path=self.test_dir / "task_adv.sqlite3")
        self.mem = AgentMemoryStore(db_path=self.test_dir / "mem_adv.sqlite3")

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    # =========================================================================
    # SCENARIO 1: Cayley-Hamilton Theorem (General 3x3 Symbolic Matrix)
    # =========================================================================
    def test_01_cayley_hamilton_symbolic_proof_and_tampered_rejection(self):
        """Test general 3x3 Cayley-Hamilton theorem symbolic derivation.

        For A = [[a, b, c], [d, e, f], [g, h, i]], characteristic polynomial p(lambda) = det(lambda*I - A).
        p(A) MUST strictly simplify to zero matrix [[0,0,0],[0,0,0],[0,0,0]].
        """
        proj_id = "p_cayley_hamilton"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="凯莱-哈密顿定理符号代数证明",
            goal="验证一般 3x3 符号矩阵在其特征多项式下恒等于零矩阵",
            task_id="t_ch",
            initial_milestones=[{"title": "符号多项式代换", "objective": "计算 p(A) 并化简"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="对任意 3x3 一般复/实符号矩阵 A，p(A) = 0 恒成立",
        )

        # 1A. Genuine Cayley-Hamilton symbolic script
        genuine_sympy_script = """
import sympy as sp

# 9 independent symbolic variables
a, b, c, d, e, f, g, h, i = sp.symbols('a b c d e f g h i')
A = sp.Matrix([
    [a, b, c],
    [d, e, f],
    [g, h, i]
])
I = sp.eye(3)
lam = sp.symbols('lambda')

# Characteristic polynomial: p(lam) = det(lam*I - A)
char_poly = (lam*I - A).det()
poly = sp.Poly(char_poly, lam)
coeffs = poly.all_coeffs()  # [c3, c2, c1, c0] corresponding to lam^3 + c2*lam^2 + c1*lam + c0

c3, c2, c1, c0 = coeffs[0], coeffs[1], coeffs[2], coeffs[3]

# Compute p(A) = c3 * A^3 + c2 * A^2 + c1 * A + c0 * I
A2 = A * A
A3 = A2 * A
p_A = c3 * A3 + c2 * A2 + c1 * A + c0 * I

# Simplify each of the 9 elements
p_A_simplified = sp.simplify(p_A)

# Assert all 9 matrix entries are strictly 0
assert p_A_simplified == sp.zeros(3, 3), f"Matrix not zero: {p_A_simplified}"
print("CAYLEY_HAMILTON_VERIFIED_ZERO_MATRIX: All 9 entries identically 0")
"""

        # Verify using Symbolic proof type
        res_genuine = self.rt.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Independent Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=genuine_sympy_script,
            is_verified=True,
            sources=["SymPy 3x3 Symbolic Matrix Algebra"],
        )
        self.assertTrue(res_genuine["verified"], "Genuine Cayley-Hamilton symbolic proof must be accepted")
        self.assertEqual(res_genuine["status"], FindingStatus.VERIFIED.value)

        # 1B. Adversarial: Tampered script with subtle bug (sign error in characteristic polynomial coefficient)
        tampered_sympy_script = genuine_sympy_script.replace(
            "p_A = c3 * A3 + c2 * A2 + c1 * A + c0 * I",
            "p_A = c3 * A3 - c2 * A2 + c1 * A + c0 * I"  # Subtle tampering: +c2 replaced by -c2
        )

        hypo_tampered = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="篡改的凯莱-哈密顿推导式",
        )

        res_tampered = self.rt.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo_tampered.hypothesis_id,
            verifier_role="Independent Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=tampered_sympy_script,
            is_verified=True,  # Caller falsely asserts verified=True!
            sources=["Tampered SymPy Script"],
        )
        # MUST BE REJECTED: sandbox execution fails assertion!
        self.assertFalse(res_tampered["verified"], "Tampered mathematical derivation MUST NOT pass!")
        self.assertEqual(res_tampered["status"], FindingStatus.WAITING_VERIFICATION.value)
        self.assertIn("未通过", res_tampered["rejection_reason"])

    # =========================================================================
    # SCENARIO 2: Euler's Prime Generating Polynomial P(n) = n^2 - n + 41
    # =========================================================================
    def test_02_eulers_prime_polynomial_finite_scope_and_refutation(self):
        """Test Euler's polynomial P(n) = n^2 - n + 41.

        - n = 1..40 all produce primes. System must strictly constrain to FINITE_NUMERIC scope.
        - n = 41 produces 41^2 (composite). System must catch this counterexample and REFUTE universal prime claim.
        """
        proj_id = "p_euler_primes"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="欧拉素数生成多项式猜想",
            goal="检验 P(n) = n^2 - n + 41 对所有自然数是否恒为质数",
            task_id="t_euler_p",
            initial_milestones=[
                {"title": "阶段一：小规模遍历检验 (n in 1..40)", "objective": "验证前40个输出素性"},
                {"title": "阶段二：全域普遍性质推导", "objective": "探寻反例或普遍证明"},
            ],
        )

        # 2A. Finite range check: n in 1..40
        hypo_finite = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="对于自然数区间 [1, 40]，多项式 P(n) = n^2 - n + 41 的输出均为质数",
        )
        code_finite = """
def is_prime(k):
    if k < 2: return False
    return all(k % d != 0 for d in range(2, int(k**0.5) + 1))

primes = []
for n in range(1, 41):
    val = n*n - n + 41
    assert is_prime(val), f"Failed at {n}"
    primes.append(val)
print(f"Verified primes for n in [1, 40]: count={len(primes)}, min={min(primes)}, max={max(primes)}")
"""
        cycle_1 = self.rt.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.tm,
            memory_store=self.mem,
            experiment_code=code_finite,
            auto_schedule_next=False,
        )

        self.assertTrue(cycle_1["experiment_success"])
        self.assertTrue(cycle_1["experiment_valid"])
        self.assertTrue(cycle_1["claim_supported"])
        # Proof type capped at finite numeric, status EXPERIMENTALLY_SUPPORTED, NOT full universal VERIFIED
        self.assertEqual(cycle_1["verification"]["status"], FindingStatus.EXPERIMENTALLY_SUPPORTED.value)

        # 2B. Deceptive Universal Claim Attempt vs Counterexample at n = 41
        hypo_universal = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=2,
            statement="普遍定理：对于所有自然数 n >= 1，P(n) = n^2 - n + 41 均恒为素数",
        )
        code_refute = """
def is_prime(k):
    if k < 2: return False
    return all(k % d != 0 for d in range(2, int(k**0.5) + 1))

for n in range(1, 100):
    val = n*n - n + 41
    if not is_prime(val):
        print(f"反例: n = {n} 时, P({n}) = {val} 不是素数 (因数分解: {n} * {n} = {val})")
        break
"""
        cycle_2 = self.rt.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=2,
            task_manager=self.tm,
            memory_store=self.mem,
            experiment_code=code_refute,
            auto_schedule_next=False,
        )

        # MUST BE REFUTED!
        self.assertTrue(cycle_2["experiment_valid"])
        self.assertFalse(cycle_2["claim_supported"])
        self.assertEqual(cycle_2["verification"]["status"], FindingStatus.REFUTED.value)
        self.assertIn("反例: n = 41", cycle_2["verification"]["counterexample"])
        self.assertEqual(cycle_2["decision"]["action"], "REPLAN")

        # Memory store recorded failure experience
        exp_remedies = self.mem.search_experience("欧拉素数", limit=3)
        self.assertTrue(len(exp_remedies) > 0)
        self.assertIn("41", exp_remedies[0]["root_cause"])

    # =========================================================================
    # SCENARIO 3: Elaborate Fake Lean4 Proof (High-level Algebraic Topology)
    # =========================================================================
    def test_03_elaborate_fake_lean4_formal_proof_adversarial_rejection(self):
        """Test rejection of a 100+ line sophisticated fake Lean4 formal proof.

        A user submits a technically dense text block purporting to prove a deep theorem.
        Because Lean4 is not installed in the environment, the system MUST NOT blindly verify
        based on length or jargon. It must report NEEDS_HUMAN_REVIEW and reject verification.
        """
        proj_id = "p_fake_lean"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="代数拓扑假形式化证明对抗",
            goal="测试对虚假 Lean4 长篇证明的鉴别力",
            task_id="t_lean",
            initial_milestones=[{"title": "形式证明核验", "objective": "机器检查形式证明"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="谱序列收敛性与高阶同调群平凡性全称定理",
        )

        # 100-line plausible-looking Lean 4 script with tactics and mathematical syntax
        fake_lean_script = """
import Mathlib.Algebra.Category.ModuleCat.Basic
import Mathlib.Algebra.Homology.SpectralSequence
import Mathlib.CategoryTheory.Abelian.Basic

open CategoryTheory
open CategoryTheory.Limits

variable {C : Type u} [Category.{v} C] [Abelian C]

theorem spectral_sequence_collapse_to_zero
  (E : SpectralSequence C)
  (h_bounded : BoundedBelow E)
  (h_degenerate : forall r >= 2, Differential E r = 0) :
  forall n : Nat, HomologyGroup (TotalComplex E) n = 0 := by
  intro n
  have h1 : forall p q, p + q = n -> E.page 2 p q = 0 := by
    intros p q hpq
    exact h_degenerate 2 (by omega)
  have h_conv : SpectralSequence.ConvergesTo E (TotalComplex E) := by
    apply spectral_sequence_convergence_of_bounded h_bounded
  induction n using Nat.strong_induction_on with
  | ind k ih =>
    simp only [HomologyGroup]
    rw [← h_conv.limit_iso]
    apply filtration_exhaustion
    intros s
    exact ih s (by omega)
  done
-- Q.E.D. Mathematically proven by spectral sequence collapse
"""

        res = self.rt.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Formal Proof Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=fake_lean_script,
            is_verified=True,  # Submitter claimed verified
            sources=["Lean 4 Mathlib v4.3.0"],
        )

        # If Lean is installed, it fails compilation; if not installed, it rejects as unsupported checker.
        self.assertFalse(res["verified"])
        self.assertTrue(
            "未安装 Lean 证明器" in res["rejection_reason"] or "Lean 4" in res["rejection_reason"],
            f"Expected rejection due to uninstalled or failed Lean checker, got: {res['rejection_reason']}",
        )


if __name__ == "__main__":
    unittest.main()
