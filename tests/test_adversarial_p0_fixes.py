"""Adversarial security and verifier rigor regression tests (P0 fixes).

Verifies:
1. Irrelevant SymPy identity CANNOT verify unrelated propositions (e.g. all even > 10).
2. Inputting data [2, 4, 6] for proposition 'all even > 10' MUST be refuted with counterexamples (2, 4, 6 <= 10).
3. Proposition lacking a dedicated independent machine verifier MUST return UNVERIFIED / INCONCLUSIVE (claim_supported=False).
4. Cross-tenant project creation with identical project_id MUST NOT overwrite owner_id and MUST be rejected with PermissionError.
"""

try:
    import pytest
except ImportError:
    pytest = None
import tempfile, shutil, unittest
from pathlib import Path
from research_tracker import (
    ResearchTracker,
    FindingStatus,
    ProofType,
    _check_symbolic_proposition_binding,
    _independent_spot_check_data,
)


def test_adversarial_p0_irrelevant_sympy_identity_rejected():
    """Attack 1: Irrelevant SymPy identities (e.g. (x+1)^2 expansion) CANNOT verify 'all even > 10'."""
    stmt = "all even numbers are greater than 10"
    irrelevant_code = """
import sympy as sp
x = sp.Symbol('x')
assert sp.simplify((x + 1)**2 - (x**2 + 2*x + 1)) == 0
"""
    bound_ok, err = _check_symbolic_proposition_binding(stmt, irrelevant_code)
    assert bound_ok is False
    assert "PROPOSITION_EVIDENCE_MISMATCH" in err or "mismatch" in err.lower()


def test_adversarial_p0_even_numbers_counterexample_refuted():
    """Attack 2: Claim 'all even > 10' with output [2, 4, 6] must be refuted with concrete counterexamples."""
    stmt = "all even numbers are greater than 10"
    output_data = "Sample empirical even numbers list: [2, 4, 6]"
    valid, supp, ce, reason = _independent_spot_check_data(stmt, output_data)
    assert supp is False
    assert ce != ""
    assert any(x in ce for x in ["2", "4", "6"])
    assert "refut" in reason.lower() or "counterexample" in reason.lower() or "probe" in reason.lower()


def test_adversarial_p0_lacking_verifier_returns_unverified():
    """Attack 3: Unknown research topic with arbitrary structured metrics MUST return UNVERIFIED/INCONCLUSIVE."""
    stmt = "Research novel graphene superconductor critical magnetic field transition temperature"
    output_data = "Experimental sample measurements: [120, 135, 140, 150], min=120, max=150, samples=4"
    valid, supp, ce, reason = _independent_spot_check_data(stmt, output_data)
    assert supp is False
    assert "UNVERIFIED" in reason or "unverified" in reason.lower() or "missing" in reason.lower() or "inconclusive" in reason.lower()


def test_adversarial_p0_cross_tenant_project_takeover_prevented(tmp_path: Path):
    """Attack 4: User B attempting to create project with User A's project_id MUST be rejected."""
    tracker = ResearchTracker(db_path=tmp_path / "test_ownership.db")
    
    # User A creates project
    proj_a = tracker.create_research_project(
        project_id="shared_proj_001",
        topic="Quantum Entanglement Verification",
        goal="Verify entangled state",
        task_id="task_a",
        initial_milestones=[{"title": "Stage 1", "objective": "Initial test"}],
        owner_id="user_alice",
    )
    assert proj_a.owner_id == "user_alice"

    # User B attempts to hijack the project with same project_id
    try:
        tracker.create_research_project(
            project_id="shared_proj_001",
            topic="Tampered Topic",
            goal="Tampered Goal",
            task_id="task_b",
            initial_milestones=[{"title": "Malicious Stage", "objective": "Overwrite"}],
            owner_id="user_bob",
        )
        raise AssertionError("Expected PermissionError")
    except PermissionError as exc_val:
        assert "conflict" in str(exc_val).lower() or "unauthorized" in str(exc_val).lower() or "permission" in str(exc_val).lower() or "cannot overwrite" in str(exc_val).lower()

    # Project metadata for Alice remains untampered
    proj_after = tracker.get_project("shared_proj_001")
    assert proj_after.owner_id == "user_alice"
    assert proj_after.topic == "Quantum Entanglement Verification"


class TestAdversarialP0Fixes(unittest.TestCase):
    def test_01_irrelevant_sympy_identity_rejected(self):
        test_adversarial_p0_irrelevant_sympy_identity_rejected()

    def test_02_even_numbers_counterexample_refuted(self):
        test_adversarial_p0_even_numbers_counterexample_refuted()

    def test_03_lacking_verifier_returns_unverified(self):
        test_adversarial_p0_lacking_verifier_returns_unverified()

    def test_04_cross_tenant_project_takeover_prevented(self):
        temp_d = tempfile.mkdtemp()
        try:
            test_adversarial_p0_cross_tenant_project_takeover_prevented(Path(temp_d))
        finally:
            shutil.rmtree(temp_d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
