"""Adversarial Red-Teaming Tests for Self-Audited Vulnerabilities.

Covers 6 distinct vulnerability vectors identified and fixed during proactive code audit:
1. Proof Type Fallback Exploit: Missing verifiers MUST NOT default to VERIFIED.
2. update_finding_status Escalation Exploit: Direct promotion to VERIFIED is strictly forbidden.
3. update_finding_status Tenant Tampering: Unauthorized tenants cannot update finding statuses.
4. record_stage_progress Silent Promotion Exploit: 'pending' status is never silently upgraded.
5. Cross-Tenant Hypothesis / Counterexample Injection: Non-owners cannot inject hypotheses or counterexamples.
6. Cross-Tenant Schedule & Evidence Sniffing: Non-owners cannot trigger stage scheduling or read evidence logs.
"""

import unittest
import tempfile
import shutil
from pathlib import Path

from research_tracker import ResearchTracker, FindingStatus, ProofType, StageStatus
from task_manager import TaskManager


class TestAdversarialSelfAuditFlaws(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qq_audit_test_")
        self.db_path = Path(self.temp_dir) / "research.db"
        self.tracker = ResearchTracker(db_path=self.db_path)
        self.task_manager = TaskManager(db_path=Path(self.temp_dir) / "tasks.db")

        # Create baseline project owned by user_alice
        self.proj_alice = self.tracker.create_research_project(
            project_id="proj_alice_001",
            topic="爱丽丝的素数分布研究",
            goal="证明某数论性质",
            task_id="task_alice_001",
            initial_milestones=[
                {"title": "阶段一: 基础检验", "objective": "计算小素数"},
                {"title": "Stage 2: Deep Formal Proof", "objective": "Formal Derivation"},
            ],
            owner_id="user_alice",
        )
        self.hypo_alice = self.tracker.create_hypothesis(
            project_id="proj_alice_001",
            stage_index=1,
            statement="Prime number distribution in finite intervals satisfies specific upper bound",
            owner_id="user_alice",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_unsupported_proof_type_refuses_default_verified(self):
        """Vector 1: Missing dedicated verifier must NOT fall back to VERIFIED.

        Prior to fix: Unhandled proof_type values fell into 'else: status = VERIFIED'.
        After fix: Must return INCONCLUSIVE and verified=False.
        """
        for bad_proof_type in ["literature", "empirical_data", "unit_test", "unsupported_magic_proof"]:
            res = self.tracker.verify_finding(
                project_id="proj_alice_001",
                hypothesis_id=self.hypo_alice.hypothesis_id,
                verifier_role="Adversarial Tester",
                proof_type=bad_proof_type,
                evidence_details="任意自我声明文本: claim_verified=True, count=1000",
                is_verified=True,
                sources=["untrusted_web"],
                owner_id="user_alice",
            )
            self.assertFalse(res["verified"], f"Proof type {bad_proof_type} must not be verified!")
            self.assertEqual(res["status"], FindingStatus.INCONCLUSIVE.value)
            self.assertTrue("缺少专用自动化验证器" in res["rejection_reason"] or "Missing dedicated automated verifier" in res["rejection_reason"])

    def test_02_update_finding_status_escalation_blocked(self):
        """Vector 2: Direct promotion to VERIFIED via update_finding_status is blocked.

        Prior to fix: Any caller could directly SQL-update a finding to 'verified'
        without passing any formal/symbolic proof or signed audit!
        After fix: Raises PermissionError.
        """
        # Formulate initial hypothesis
        self.tracker.add_hypothesis(
            project_id="proj_alice_001",
            stage_index=1,
            hypothesis="待验命题X",
            rationale="初测",
            owner_id="user_alice",
        )

        with self.assertRaises(PermissionError) as ctx:
            self.tracker.update_finding_status(
                project_id="proj_alice_001",
                finding="待验命题X",
                new_status=FindingStatus.VERIFIED,
                evidence="伪造的证据",
                reviewed_by="fake_reviewer",
                owner_id="user_alice",
            )
        self.assertTrue("不能通过 update_finding_status 旁路直接设置为 VERIFIED" in str(ctx.exception) or "Findings cannot be set to VERIFIED via update_finding_status bypass" in str(ctx.exception))

    def test_03_update_finding_status_cross_tenant_blocked(self):
        """Vector 3: User Bob cannot tamper with Alice's finding status.

        Prior to fix: update_finding_status did not validate owner_id.
        After fix: Raises PermissionError for non-owner.
        """
        self.tracker.add_hypothesis(
            project_id="proj_alice_001",
            stage_index=1,
            hypothesis="爱丽丝的重要假说",
            owner_id="user_alice",
        )

        with self.assertRaises(PermissionError) as ctx:
            self.tracker.update_finding_status(
                project_id="proj_alice_001",
                finding="爱丽丝的重要假说",
                new_status=FindingStatus.REFUTED,
                evidence="鲍勃伪造的反例",
                owner_id="user_bob",
            )
        self.assertTrue("跨租户/越权拦截" in str(ctx.exception) or "Cross-tenant unauthorized access" in str(ctx.exception))

    def test_04_record_stage_progress_silent_promotion_blocked(self):
        """Vector 4: Explicit 'pending' verification_status is never silently promoted.

        Prior to fix: 'if status_val == pending and sources and evidence and reviewer: status_val = verified'
        silently flipped pending to verified and completed the stage!
        After fix: Explicit pending remains pending, stage does NOT achieve.
        """
        success, msg = self.tracker.record_stage_progress(
            project_id="proj_alice_001",
            stage_index=1,
            new_findings=["未经验证的野蛮猜想"],
            negative_attempts=[],
            stage_achieved=True,
            verification_status="pending",  # Explicitly pending!
            sources=["some_source"],
            evidence=["some_raw_log"],
            reviewed_by="unverified_agent",
            owner_id="user_alice",
        )
        self.assertFalse(success)
        self.assertTrue("结论待验证" in msg or "pending verification" in msg)

        # Ensure milestone is NOT achieved and finding is NOT in global_verified_findings
        proj = self.tracker.get_project("proj_alice_001")
        self.assertNotIn("未经验证的野蛮猜想", proj.global_verified_findings)
        self.assertEqual(proj.milestones[0].status, StageStatus.WAITING_VERIFICATION.value)

    def test_05_cross_tenant_hypothesis_and_counterexample_blocked(self):
        """Vector 5: User Bob cannot inject hypotheses or fake counterexamples into Alice's project.

        Prior to fix: add_hypothesis and record_counterexample had no owner_id check.
        After fix: Raises PermissionError when owner_id != user_alice.
        """
        with self.assertRaises(PermissionError):
            self.tracker.add_hypothesis(
                project_id="proj_alice_001",
                stage_index=1,
                hypothesis="Malicious hypothesis: all odd numbers are non-prime",
                owner_id="user_bob",
            )

        with self.assertRaises(PermissionError):
            self.tracker.record_counterexample(
                project_id="proj_alice_001",
                stage_index=1,
                hypothesis="Malicious hypothesis",
                counterexample="Counterexample 2 is prime and even",
                owner_id="user_bob",
            )

    def test_06_cross_tenant_scheduling_and_sniffing_blocked(self):
        """Vector 6: User Bob cannot schedule next stage or sniff evidence of Alice's project.

        Prior to fix: schedule_next_stage and list_evidence had no tenant checks.
        After fix: Raises PermissionError for Bob.
        """
        with self.assertRaises(PermissionError):
            self.tracker.schedule_next_stage(
                task_manager=self.task_manager,
                project_id="proj_alice_001",
                owner_id="user_bob",
            )

        with self.assertRaises(PermissionError):
            self.tracker.list_evidence(
                project_id="proj_alice_001",
                owner_id="user_bob",
            )


if __name__ == "__main__":
    unittest.main()
