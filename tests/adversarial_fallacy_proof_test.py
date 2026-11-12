"""Adversarial Mathematical Fallacy & Pseudo-Proposition Stress Tests.

Verifies system defenses against classic mathematical fallacies and pseudo-theorems:
1. "Chinese Hypothesis" False Equivalence:
   Claim: "A positive integer n is prime if and only if 2^n = 2 (mod n)".
   True for all integers up to 340! Fails at n = 341 (Fermat pseudoprime: 341 = 11 * 31, but 2^341 = 2 mod 341).
   System must catch the counterexample 341 and trigger instant REFUTED.

2. Principal Square Root Sign Fallacy:
   Claim: "For all real numbers x, sqrt(x^2) = x".
   Fails for any negative real number (e.g. x = -5 -> sqrt(25) = 5 != -5).
   Sandbox execution of symbolic assertion must fail, resulting in rejection.

3. Generalized Polyhedron Euler Characteristic Fallacy:
   Claim: "For ANY closed polyhedron, V - E + F = 2 universally holds".
   Fails for toroidal (genus 1) polyhedra where V - E + F = 0 != 2.
   System must detect the counterexample and trigger instant REFUTED.

4. Disguised Division-by-Zero Constant Fallacy:
   Claim: "1 = 2" derived via factored cancellation of (a - b) where a = b.
   System's automatic contradiction engine must detect the logical absurdity and refute immediately.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research_tracker import (
    ResearchTracker,
    ProofType,
    FindingStatus,
    evaluate_experiment_output,
)
from task_manager import TaskManager
from agent_memory import AgentMemoryStore


class AdversarialFallacyProofTest(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_data" / "adv_fallacy"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / "research_fallacy.sqlite3"
        if self.db_path.exists():
            self.db_path.unlink()
        self.rt = ResearchTracker(db_path=self.db_path)
        self.tm = TaskManager(db_path=self.test_dir / "task_fallacy.sqlite3")
        self.mem = AgentMemoryStore(db_path=self.test_dir / "mem_fallacy.sqlite3")

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass

    # =========================================================================
    # FALLACY 1: The "Chinese Hypothesis" (Ancient Prime Equivalence Fallacy)
    # =========================================================================
    def test_fallacy_01_chinese_hypothesis_fermat_pseudoprime_refutation(self):
        """Test the famous historical fallacy: 'n is prime iff 2^n = 2 (mod n)'.

        This holds for EVERY number from 2 up to 340!
        At n = 341 = 11 * 31, 2^341 = 2 (mod 341), but 341 is composite.
        """
        proj_id = "p_chinese_hypo"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="中国猜想素数判别伪命题",
            goal="检验 2^n 同余于 2 (mod n) 是否为素数的充要条件",
            task_id="t_ch_hypo",
            initial_milestones=[{"title": "同余充要性检验", "objective": "寻找同余但非素数的反例"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="充要条件伪定理：整数 n 是素数 当且仅当 2^n = 2 (mod n)",
        )

        # Counterexample search script
        search_code = """
def is_prime(k):
    if k < 2: return False
    return all(k % d != 0 for d in range(2, int(k**0.5) + 1))

# Search for composite number satisfying 2^n = 2 (mod n)
found_ce = None
for n in range(2, 500):
    if pow(2, n, n) == 2 and not is_prime(n):
        found_ce = n
        break

if found_ce:
    print(f"反例: n = {found_ce} 是合数 (因数分解: 11 * 31 = {found_ce}), 但满足 2^{found_ce} 同余于 2 (mod {found_ce}), 充要条件被彻底证伪!")
"""
        cycle = self.rt.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.tm,
            memory_store=self.mem,
            experiment_code=search_code,
            auto_schedule_next=False,
        )

        # MUST BE STRICTLY REFUTED!
        self.assertTrue(cycle["experiment_success"])
        self.assertTrue(cycle["experiment_valid"])
        self.assertFalse(cycle["claim_supported"])
        self.assertEqual(cycle["verification"]["status"], FindingStatus.REFUTED.value)
        self.assertIn("341", cycle["verification"]["counterexample"])
        self.assertEqual(cycle["decision"]["action"], "REPLAN")

        # Verified finding must NOT contain this false proposition
        proj_summary = self.rt.get_project_summary(proj_id)
        self.assertEqual(len(proj_summary["verified_findings"]), 0)
        self.assertIn("341", str(proj_summary["failed_hypotheses"]))

    # =========================================================================
    # FALLACY 2: Principal Square Root Sign Fallacy: sqrt(x^2) = x
    # =========================================================================
    def test_fallacy_02_principal_square_root_sign_fallacy(self):
        """Test the common algebra fallacy: sqrt(x^2) = x for all real x.

        A deceptive script attempts symbolic verification using SymPy,
        but tests or substitutes negative real numbers.
        """
        proj_id = "p_sqrt_fallacy"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="实数平方根符号恒等式伪命题",
            goal="验证 sqrt(x^2) = x 对全体实数的成立性",
            task_id="t_sqrt",
            initial_milestones=[{"title": "实数符号验证", "objective": "验证恒等式"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全实数恒等式伪命题：对任意实数 x，sqrt(x^2) = x 恒成立",
        )

        # Deceptive SymPy script that tests negative domain
        flawed_sympy_script = """
import sympy as sp

x = sp.Symbol('x', real=True)
lhs = sp.sqrt(x**2)
rhs = x

# Check substitution at x = -5
val_lhs = lhs.subs(x, -5)  # sqrt(25) = 5
val_rhs = rhs.subs(x, -5)  # -5

assert val_lhs == val_rhs, f"Counterexample found at x=-5: sqrt((-5)^2) = {val_lhs} != {val_rhs}"
"""
        res = self.rt.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=flawed_sympy_script,
            is_verified=True,  # Submitter falsely asserted True
            sources=["SymPy Real Domain Check"],
        )

        # Sandbox execution must fail assertion at x=-5 and reject verification
        self.assertFalse(res["verified"])
        self.assertEqual(res["status"], FindingStatus.WAITING_VERIFICATION.value)
        self.assertTrue("未通过" in res["rejection_reason"] or "failed" in res["rejection_reason"].lower())

    # =========================================================================
    # FALLACY 3: Universal Polyhedron Euler Characteristic (Toroidal Counterexample)
    # =========================================================================
    def test_fallacy_03_toroidal_polyhedron_refutes_universal_euler_formula(self):
        """Test universal claim that V - E + F = 2 holds for ALL closed polyhedra.

        Euler formula V - E + F = 2 only holds for genus-0 (spherical) polyhedra.
        For a torus (genus 1, e.g. a picture frame / doughnut polyhedron), V - E + F = 2 - 2g = 0 != 2.
        """
        proj_id = "p_euler_poly_fallacy"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="任意闭多面体欧拉示性数恒为2伪命题",
            goal="检验带孔多面体欧拉示性数",
            task_id="t_poly",
            initial_milestones=[{"title": "拓扑示性数检验", "objective": "验证环面多面体"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="全称伪命题：任意封闭多面体顶点数V、棱数E、面数F均满足 V - E + F = 2",
        )

        torus_code = """
# Construct a toroidal polyhedron (genus g=1) with a square cross section
# A 4x4 grid wrapped in both directions: 16 vertices, 32 edges, 16 quad faces
V = 16
E = 32
F = 16
chi = V - E + F  # 16 - 32 + 16 = 0

if chi != 2:
    print(f"反例: 环面多面体 (Genus 1) 顶点V={V}, 棱E={E}, 面F={F}, 欧拉示性数 V-E+F = {chi} != 2, 普遍定理被反例证伪!")
"""
        cycle = self.rt.run_autonomous_stage_cycle(
            project_id=proj_id,
            stage_index=1,
            task_manager=self.tm,
            memory_store=self.mem,
            experiment_code=torus_code,
            auto_schedule_next=False,
        )

        # Counterexample triggers instant REFUTED
        self.assertTrue(cycle["experiment_valid"])
        self.assertFalse(cycle["claim_supported"])
        self.assertEqual(cycle["verification"]["status"], FindingStatus.REFUTED.value)
        self.assertIn("反例: 环面多面体", cycle["verification"]["counterexample"])
        self.assertIn("!= 2", cycle["verification"]["counterexample"])

    # =========================================================================
    # FALLACY 4: Disguised Constant Absurdity: 1 = 2
    # =========================================================================
    def test_fallacy_04_constant_absurdity_direct_rejection(self):
        """Test direct automatic contradiction detection on disguised fallacies asserting 1 = 2."""
        proj_id = "p_one_equals_two"
        proj = self.rt.create_research_project(
            project_id=proj_id,
            topic="除零谬误推导 1 = 2",
            goal="消去 (a - b) 试图得出 1 = 2",
            task_id="t_one_two",
            initial_milestones=[{"title": "谬误检验", "objective": "检测矛盾"}],
        )

        hypo = self.rt.create_hypothesis(
            project_id=proj_id,
            stage_index=1,
            statement="经过代数消元推导出 1 = 2",
        )

        # Even if a caller attempts to force is_verified=True with bogus output
        res = self.rt.verify_finding(
            project_id=proj_id,
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Verifier",
            proof_type=ProofType.FINITE_NUMERIC,
            evidence_details="Let a=b. a^2 - b^2 = a(a-b)... Therefore 1 = 2.",
            is_verified=True,
            sources=["Bogus Algebra"],
        )

        # MUST BE INSTANTLY REFUTED BY DIRECT MATHEMATICAL CONTRADICTION!
        self.assertFalse(res["verified"])
        self.assertEqual(res["status"], FindingStatus.REFUTED.value)
        self.assertIn("1 = 2", res["counterexample"])
        self.assertTrue("矛盾" in res["rejection_reason"] or "contradiction" in res["rejection_reason"].lower())


if __name__ == "__main__":
    unittest.main()
