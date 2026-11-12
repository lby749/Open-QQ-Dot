"""Adversarial Deep Research & Proof Audit Tests.

Verifies root-cause fixes for subtle verification, proof binding, and lifecycle flaws:
1. Lean 4 single instance literal cannot prove universal claims.
2. Lean 4 deceptive theorem names cannot bypass domain validation.
3. SymPy tautology identity variants (x + 1 - 1 == x) are rejected as irrelevant.
4. Active domain boundary probe defeats cherry-picked empirical samples.
5. Finite empirical support never prematurely marks universal projects as COMPLETED.
6. Counterexample de-duplication prevents duplicate list bloat.
7. Research branching lifecycle with merge_branch and multi-tenant access control.
8. Native Lean 4 execution in sandboxed experiment runner.
"""

import unittest
import json
import tempfile
import shutil
from pathlib import Path

from research_tracker import (
    ResearchTracker,
    FindingStatus,
    ProofType,
    StageStatus,
    check_formal_checker_availability,
    _independent_spot_check_data,
)
from task_manager import TaskManager


class TestAdversarialDeepResearchFlaws(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qq_deep_audit_")
        self.db_path = Path(self.temp_dir) / "research.db"
        self.tracker = ResearchTracker(db_path=self.db_path)
        self.task_manager = TaskManager(db_path=Path(self.temp_dir) / "tasks.db")

        # Grant commands permission for test user_alice
        alice_cfg_dir = Path("friends") / "user_alice"
        alice_cfg_dir.mkdir(parents=True, exist_ok=True)
        (alice_cfg_dir / "friend.json").write_text(
            json.dumps({"computer_tools": {"commands": True, "full_computer_access": True}}),
            encoding="utf-8"
        )

        self.project = self.tracker.create_research_project(
            project_id="p_deep_test",
            topic="Universal Propositions and Formal Verification Audit",
            goal="Prove universal number theory proposition",
            task_id="t_deep_01",
            initial_milestones=[
                {"title": "Stage 1: Base Verification", "objective": "Verify numerical base"},
                {"title": "Stage 2: Theorem Proof", "objective": "Formal closure"},
            ],
            max_stages=2,
            owner_id="user_alice",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        alice_cfg = Path("friends") / "user_alice"
        if alice_cfg.exists():
            shutil.rmtree(alice_cfg, ignore_errors=True)

    def test_01_lean4_single_instance_cannot_prove_universal_claim(self):
        """Flaw 1: Ground literal instance (Nat.Prime 3) cannot prove universal claim 'all primes are odd'."""
        hypo = self.tracker.create_hypothesis(
            project_id="p_deep_test",
            stage_index=1,
            statement="All primes are odd",
            owner_id="user_alice",
        )
        deceptive_lean = """
theorem prime_three : Nat.Prime 3 := by decide
"""
        res = self.tracker.verify_finding(
            project_id="p_deep_test",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=deceptive_lean,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="user_alice",
        )
        self.assertFalse(res["verified"])
        self.assertIn("cannot serve as universal proof", res["rejection_reason"])

    def test_02_lean4_deceptive_theorem_name_rejected(self):
        """Flaw 2: Naming a theorem 'even_primes_pythagor' cannot satisfy universal claim 'all evens are greater than 10'."""
        hypo = self.tracker.create_hypothesis(
            project_id="p_deep_test",
            stage_index=1,
            statement="All evens are greater than 10",
            owner_id="user_alice",
        )
        deceptive_lean = """
theorem even_primes_pythagor : 0 < 10 := by decide
"""
        res = self.tracker.verify_finding(
            project_id="p_deep_test",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=deceptive_lean,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="user_alice",
        )
        self.assertFalse(res["verified"])
        self.assertIn("cannot serve as universal proof", res["rejection_reason"])

    def test_03_sympy_tautology_identity_variant_rejected(self):
        """Flaw 3: Tautological identity variants like x + 1 - 1 == x cannot prove unrelated propositions."""
        hypo = self.tracker.create_hypothesis(
            project_id="p_deep_test",
            stage_index=1,
            statement="Euler Characteristic Theorem and Graph Coloring Boundary",
            owner_id="user_alice",
        )
        deceptive_sym = """
import sympy as sp
x = sp.Symbol('x')
assert sp.simplify(x + 1 - 1) == x
"""
        res = self.tracker.verify_finding(
            project_id="p_deep_test",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Symbolic Verifier",
            proof_type=ProofType.SYMBOLIC,
            evidence_details=deceptive_sym,
            is_verified=True,
            sources=["SymPy Fake Tautology"],
            owner_id="user_alice",
        )
        self.assertFalse(res["verified"])
        self.assertIn("PROPOSITION_EVIDENCE_MISMATCH", res["rejection_reason"])

    def test_04_active_domain_boundary_probe_defeats_cherry_picked_samples(self):
        """Flaw 4: Cherry-picked output [12, 14, 16] for 'all evens are greater than 10' is refuted by active boundary probe."""
        stmt = "All evens are greater than 10"
        cherry_picked_out = "Through empirical sampling, obtained even list: [12, 14, 16, 18, 20]"
        valid, supported, ce, reason = _independent_spot_check_data(stmt, cherry_picked_out)
        self.assertFalse(supported)
        self.assertTrue(valid)
        self.assertIn("2", ce)
        self.assertIn("active probe", reason.lower())

    def test_05_universal_project_cannot_complete_on_pure_empirical_support(self):
        """Flaw 5: If all stages achieved finite numeric support, project cannot be set to COMPLETED."""
        # Formulate hypotheses in stages 1 and 2
        h1 = self.tracker.create_hypothesis("p_deep_test", 1, "Stage 1 finite prime sampling", owner_id="user_alice")
        h2 = self.tracker.create_hypothesis("p_deep_test", 2, "Stage 2 finite prime sampling", owner_id="user_alice")

        # Mark both as EXPERIMENTALLY_SUPPORTED (not VERIFIED)
        self.tracker.verify_finding(
            "p_deep_test", h1.hypothesis_id, "V", ProofType.FINITE_NUMERIC,
            evidence_details="[2, 3, 5, 7]", is_verified=True, sources=["test"], owner_id="user_alice"
        )
        self.tracker.record_stage_progress(
            "p_deep_test", 1, ["[finite_numeric] Stage 1 finding"], [], stage_achieved=True,
            verification_status=FindingStatus.EXPERIMENTALLY_SUPPORTED, sources=["test"], evidence=["ev"], reviewed_by="V",
            owner_id="user_alice",
        )

        self.tracker.verify_finding(
            "p_deep_test", h2.hypothesis_id, "V", ProofType.FINITE_NUMERIC,
            evidence_details="[11, 13, 17, 19]", is_verified=True, sources=["test"], owner_id="user_alice"
        )
        self.tracker.record_stage_progress(
            "p_deep_test", 2, ["[finite_numeric] Stage 2 finding"], [], stage_achieved=True,
            verification_status=FindingStatus.EXPERIMENTALLY_SUPPORTED, sources=["test"], evidence=["ev"], reviewed_by="V",
            owner_id="user_alice",
        )

        # Evaluate next step on final stage
        dec = self.tracker.evaluate_next_step("p_deep_test", 2)
        self.assertEqual(dec["action"], "WAIT")
        self.assertIn("formal or symbolic proof closure", dec["rationale"].lower())

        # Project status must NOT be COMPLETED
        proj = self.tracker.get_project("p_deep_test")
        self.assertNotEqual(proj.status, "COMPLETED")
        self.assertEqual(proj.status, "WAITING_VERIFICATION")

    def test_06_record_counterexample_de_duplication(self):
        """Flaw 6: Calling record_counterexample multiple times with same data does not bloat list."""
        self.tracker.record_counterexample(
            project_id="p_deep_test",
            stage_index=1,
            hypothesis="Hypothesis A",
            counterexample="Counterexample X",
            owner_id="user_alice",
        )
        self.tracker.record_counterexample(
            project_id="p_deep_test",
            stage_index=1,
            hypothesis="Hypothesis A",
            counterexample="Counterexample X",
            owner_id="user_alice",
        )
        proj = self.tracker.get_project("p_deep_test")
        matching = [x for x in proj.failed_hypotheses if "Hypothesis A: Counterexample X" in x]
        self.assertEqual(len(matching), 1)

    def test_07_branch_lifecycle_and_merge_branch(self):
        """Flaw 7: Research branch creation, tenant protection, and merge_branch."""
        br = self.tracker.create_branch("p_deep_test", "alt_direction", "New convergent hypothesis", owner_id="user_alice")
        self.assertEqual(br.status, "ACTIVE")

        # Non-owner cannot merge
        with self.assertRaises(PermissionError):
            self.tracker.merge_branch("p_deep_test", br.branch_id, owner_id="user_bob")

        # Owner merges successfully
        merged = self.tracker.merge_branch("p_deep_test", br.branch_id, owner_id="user_alice")
        self.assertTrue(merged)

        branches = self.tracker.list_branches("p_deep_test", owner_id="user_alice")
        self.assertEqual(branches[0].status, "MERGED")

    def test_08_execute_experiment_native_lean_execution(self):
        """Flaw 8: execute_experiment natively supports Lean 4 compiler execution."""
        checker_status = check_formal_checker_availability()
        if not checker_status["lean"]:
            self.skipTest("Lean 4 executable not installed in this environment.")

        hypo = self.tracker.create_hypothesis(
            project_id="p_deep_test",
            stage_index=1,
            statement="Natural Number Addition Properties",
            owner_id="user_alice",
        )

        valid_lean = """
theorem nat_add_zero_test (n : Nat) : n + 0 = n := by rfl
"""
        exp = self.tracker.execute_experiment(
            project_id="p_deep_test",
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="lean4",
            code_or_input=valid_lean,
            owner_id="user_alice",
        )
        self.assertTrue(exp.is_success)
        self.assertTrue(exp.tool_execution_success)

    def test_09_lean4_exit_escape_rejected(self):
        """Flaw 15: Lean 4 #exit instruction must be rejected as an unclosed escape."""
        hypo = self.tracker.create_hypothesis(
            project_id="p_deep_test",
            stage_index=1,
            statement="All natural numbers plus zero equals itself",
            owner_id="user_alice",
        )
        cheat_lean = """
theorem fake_thm (n : Nat) : n + 0 = n := by
#exit
"""
        res = self.tracker.verify_finding(
            project_id="p_deep_test",
            hypothesis_id=hypo.hypothesis_id,
            verifier_role="Lean 4 Verifier",
            proof_type=ProofType.FORMAL_PROOF,
            evidence_details=cheat_lean,
            is_verified=True,
            sources=["Lean 4"],
            owner_id="user_alice",
        )
        self.assertFalse(res["verified"])
        self.assertIn("UNFINISHED_PROOF", res["rejection_reason"])

    def test_10_active_probe_refutes_odd_universal_claim(self):
        """Flaw 16: 'all odds are greater than 10' with cherry-picked [11, 13, 15] is refuted by active odd probe 1 <= 10."""
        stmt = "All odds are greater than 10"
        cherry_picked_out = "Sample data: [11, 13, 15, 17]"
        valid, supported, ce, reason = _independent_spot_check_data(stmt, cherry_picked_out)
        self.assertFalse(supported)
        self.assertTrue(valid)
        self.assertIn("1", ce)
        self.assertIn("odd base active probe", reason.lower())

    def test_11_lean4_execution_gated_by_rbac(self):
        """Flaw 17: User without commands permission cannot execute Lean 4."""
        # Create project for user_charlie (no commands permission granted)
        self.tracker.create_research_project(
            project_id="p_charlie_test",
            topic="Charlie Research Topic",
            goal="Test unauthorized execution",
            task_id="t_charlie_01",
            initial_milestones=[{"title": "Stage 1", "objective": "Verify"}],
            owner_id="user_charlie",
        )
        hypo = self.tracker.create_hypothesis(
            project_id="p_charlie_test",
            stage_index=1,
            statement="Formal Proposition",
            owner_id="user_charlie",
        )
        # Call with unprivileged user_charlie (owner of project, but commands=False)
        exp = self.tracker.execute_experiment(
            project_id="p_charlie_test",
            stage_index=1,
            hypothesis_id=hypo.hypothesis_id,
            tool_name="lean4",
            code_or_input="theorem t : 1 = 1 := rfl",
            owner_id="user_charlie",
        )
        self.assertFalse(exp.is_success)
        self.assertIn("permission", exp.error_log.lower())

    def test_12_record_stage_progress_default_never_promotes_to_verified(self):
        """Flaw 13: Omitting verification_status in record_stage_progress never silently defaults to verified."""
        res, msg = self.tracker.record_stage_progress(
            project_id="p_deep_test",
            stage_index=1,
            new_findings=["Unverified finding"],
            negative_attempts=[],
            stage_achieved=True,
            sources=["fake_source"],
            evidence=["fake_evidence"],
            reviewed_by="fake_human",
            owner_id="user_alice",
        )
        # Finding must NOT be in global_verified_findings
        proj = self.tracker.get_project("p_deep_test")
        self.assertNotIn("Unverified finding", proj.global_verified_findings)
        # Milestone status should not be ACHIEVED without verified/supported status
        m = proj.milestones[0]
        self.assertNotEqual(m.status, "ACHIEVED")

    def test_13_merge_branch_injects_focus_hypothesis(self):
        """Flaw 19: Merging branch injects the branch's focus_hypothesis into current stage."""
        br = self.tracker.create_branch("p_deep_test", "fix_branch", "Corrected boundary hypothesis: even greater than 2", owner_id="user_alice")
        self.tracker.merge_branch("p_deep_test", br.branch_id, owner_id="user_alice")

        # Hypotheses should now include the merged hypothesis
        hypos = self.tracker.list_hypotheses("p_deep_test", 1)
        statements = [h.statement for h in hypos]
        self.assertIn("Corrected boundary hypothesis: even greater than 2", statements)

    def test_14_schedule_next_stage_max_stages_guard(self):
        """Flaw 20: Reaching max stages without rigorous formal proof leaves project in WAITING_VERIFICATION, not COMPLETED."""
        # Set milestones achieved with reviewed evidence
        proj = self.tracker.get_project("p_deep_test")
        proj.current_stage = 3  # Past max_stages=2
        for m in proj.milestones:
            m.status = "ACHIEVED"
            m.evidence = json.dumps({"sources": ["test"], "evidence": ["numeric data"], "reviewed_by": "Independent Verifier"})
        self.tracker._save_project(proj)

        with self.assertRaises(ValueError) as ctx:
            self.tracker.schedule_next_stage(self.task_manager, "p_deep_test", owner_id="user_alice")
        self.assertIn("maximum research stage limit reached", str(ctx.exception).lower())

        proj_after = self.tracker.get_project("p_deep_test")
        self.assertNotEqual(proj_after.status, "COMPLETED")
        self.assertEqual(proj_after.status, "WAITING_VERIFICATION")


if __name__ == "__main__":
    unittest.main()
