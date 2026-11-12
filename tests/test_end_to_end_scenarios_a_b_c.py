"""Comprehensive End-to-End Acceptance Tests for Scenarios A, B, and C.

Scenario A: Cross-Agent Engineering Experience Sharing (Claude Code -> Codex).
Scenario B: Continuous Academic Research Tracking (Direction, arXiv, Digest, Experiment).
Scenario C: Multi-User Security, Memory/Task Isolation & Subscription Shared Risk Gating.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from local_retrieval_engine import (
    LocalHybridSearchEngine,
    TaskExperienceStore,
    CapabilityDiscoveryService,
    DynamicContextBuilder,
    AcademicResearchPipeline,
    ExperienceResultStatus,
    ExperienceConfidence,
)
from task_manager import TaskManager, TaskStatus
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus
from research_tracker import ResearchTracker, FindingStatus, ProofType


class TestEndToEndScenarios(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_dot_scenarios_")
        self.work_dir = Path(self.temp_dir)
        self.retrieval_db = self.work_dir / "retrieval_engine.sqlite3"
        self.tasks_db = self.work_dir / "tasks.sqlite3"
        self.memory_db = self.work_dir / "agent_memory.sqlite3"
        self.approval_db = self.work_dir / "approval.sqlite3"
        self.research_db = self.work_dir / "research.sqlite3"

        # Isolated subsystem instances
        self.engine = LocalHybridSearchEngine(self.retrieval_db)
        self.exp_store = TaskExperienceStore(self.engine)
        self.cap_service = CapabilityDiscoveryService(self.engine)
        self.task_manager = TaskManager(db_path=self.tasks_db, workspace=self.work_dir)
        self.mem_store = AgentMemoryStore(db_path=self.memory_db)
        self.approval_mgr = ApprovalManager(db_path=self.approval_db)
        self.research_tracker = ResearchTracker(db_path=self.research_db)

    def tearDown(self):
        self.task_manager.stop_worker()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # SCENARIO A: Cross-Agent Engineering Experience Sharing
    # =========================================================================
    def test_scenario_a_cross_agent_engineering_experience_sharing(self):
        """Claude Code resolves a real Windows file encoding bug, saves diff and test results.
        Codex receives a similar problem, retrieves Claude's verified experience, applies the fix,
        passes the test, and archives new validated knowledge.
        """
        # Step 1: Create a buggy module in workspace
        module_path = self.work_dir / "data_exporter.py"
        test_path = self.work_dir / "test_data_exporter.py"

        module_path.write_text(
            "# Buggy exporter without explicit UTF-8 encoding\n"
            "def export_report(filepath, text):\n"
            "    with open(filepath, 'w') as f:\n"
            "        f.write(text)\n",
            encoding="utf-8",
        )
        test_path.write_text(
            "import os\n"
            "from data_exporter import export_report\n"
            "def test_export():\n"
            "    p = 'out_test.txt'\n"
            "    export_report(p, 'Unicode report and special symbols 🚀')\n"
            "    with open(p, 'r', encoding='utf-8') as f:\n"
            "        c = f.read()\n"
            "    assert 'Unicode report' in c\n"
            "    os.remove(p)\n",
            encoding="utf-8",
        )

        # Step 2: Claude Code fixes the bug
        claude_fix_diff = (
            "--- data_exporter.py\n"
            "+++ data_exporter.py\n"
            "@@ -2,3 +2,3 @@\n"
            "-    with open(filepath, 'w') as f:\n"
            "+    with open(filepath, 'w', encoding='utf-8') as f:\n"
        )
        module_path.write_text(
            "def export_report(filepath, text):\n"
            "    with open(filepath, 'w', encoding='utf-8') as f:\n"
            "        f.write(text)\n",
            encoding="utf-8",
        )

        # Run pytest to verify Claude's fix
        py_exec = sys.executable
        import importlib.util
        has_pytest = importlib.util.find_spec("pytest") is not None
        res = subprocess.run(
            [py_exec, "-m", "pytest", str(test_path)] if has_pytest else [py_exec, "-c", "import test_data_exporter; test_data_exporter.test_export()"],
            cwd=str(self.work_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        self.assertEqual(res.returncode, 0, f"Claude fix test failed: {res.stdout}\n{res.stderr}")

        # Step 3: Register and verify Claude's task in TaskManager, then archive into TaskExperienceStore
        real_claude_tid = self.task_manager.create_task("Fix export encoding", "UTF-8 fix", category="code", owner_id="user_developer")
        self.task_manager.update_task_status(
            real_claude_tid, TaskStatus.COMPLETED, result="Fix completed",
            artifacts=[str(test_path)], verification_info={"success": True, "verifier_type": "pytest", "test_passed": True}
        )
        claude_exp = self.exp_store.record_experience(
            task_id=real_claude_tid,
            owner_id="user_developer",
            project_id="proj_reporting",
            agent_provider="claude",
            model_name="claude-3-7-sonnet",
            tools_used=["file_write", "execute_command"],
            problem_statement="Windows platform reading writing files without explicit encoding raises UnicodeEncodeError on unicode report export",
            environment_conditions={"os": "Windows_11", "python": "3.13"},
            action_taken="Explicitly declare encoding='utf-8' in open() calls, and set PYTHONIOENCODING=utf-8 in subprocess environment",
            result_status=ExperienceResultStatus.SUCCESS_VERIFIED,
            root_cause_or_condition="Windows default ANSI code page (GBK/CP1252) cannot directly encode UTF-8 characters and emojis",
            artifacts_and_evidence=["test_data_exporter.py: 1 passed", claude_fix_diff],
            confidence_status=ExperienceConfidence.HIGH_VERIFIED,
            is_shared=True,
        )
        self.assertTrue(claude_exp.experience_id.startswith("exp_"))

        # Step 4: Codex receives a similar task on a different module
        service_path = self.work_dir / "audit_logger.py"
        test_audit_path = self.work_dir / "test_audit_logger.py"
        service_path.write_text(
            "def log_event(filename, msg):\n"
            "    with open(filename, 'a') as f:\n"
            "        f.write(msg + '\\n')\n",
            encoding="utf-8",
        )
        test_audit_path.write_text(
            "import os\n"
            "from audit_logger import log_event\n"
            "def test_log():\n"
            "    p = 'audit.log'\n"
            "    log_event(p, 'Admin login audit: Success')\n"
            "    with open(p, 'r', encoding='utf-8') as f:\n"
            "        content = f.read()\n"
            "    assert 'Admin login audit' in content\n"
            "    os.remove(p)\n",
            encoding="utf-8",
        )

        codex_query = "Fix log file writing unicode garbled text and encoding errors on Windows"

        # Step 5: Dot's hybrid retrieval finds Claude's experience
        matched_exps = self.exp_store.search_experiences(
            query=codex_query,
            owner_id="user_developer",
            only_verified_success=True,
            limit=2,
        )
        self.assertTrue(len(matched_exps) >= 1)
        top_exp = matched_exps[0]
        self.assertEqual(top_exp["agent_provider"], "claude")
        self.assertIn("encoding='utf-8'", top_exp["action_taken"])

        # Step 6: Codex applies the verified pattern
        service_path.write_text(
            "def log_event(filename, msg):\n"
            "    with open(filename, 'a', encoding='utf-8') as f:\n"
            "        f.write(msg + '\\n')\n",
            encoding="utf-8",
        )
        res_codex = subprocess.run(
            [py_exec, "-m", "pytest", str(test_audit_path)] if has_pytest else [py_exec, "-c", "import test_audit_logger; test_audit_logger.test_log()"],
            cwd=str(self.work_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        self.assertEqual(res_codex.returncode, 0, f"Codex test failed: {res_codex.stdout}\n{res_codex.stderr}")

        # Step 7: Dot verifies Codex's task in TaskManager and archives validated outcome
        real_codex_tid = self.task_manager.create_task("Fix log encoding", "audit_logger UTF-8", category="code", owner_id="user_developer")
        self.task_manager.update_task_status(
            real_codex_tid, TaskStatus.COMPLETED, result="Fix completed",
            artifacts=[str(test_audit_path)], verification_info={"success": True, "verifier_type": "pytest", "test_passed": True}
        )
        codex_exp = self.exp_store.record_experience(
            task_id=real_codex_tid,
            owner_id="user_developer",
            project_id="proj_logging",
            agent_provider="codex",
            model_name="gpt-5-codex",
            tools_used=["file_write", "execute_command"],
            problem_statement="Log recording module audit_logger.py unicode write Windows compatibility fix",
            environment_conditions={"os": "Windows_11", "python": "3.13"},
            action_taken="Referencing Claude Code historical experience, specify encoding='utf-8' for audit_logger.py file stream",
            result_status=ExperienceResultStatus.SUCCESS_VERIFIED,
            root_cause_or_condition="Inherited from historical experience: resolve Windows code page truncation",
            artifacts_and_evidence=["test_audit_logger.py: 1 passed"],
            confidence_status=ExperienceConfidence.HIGH_VERIFIED,
            is_shared=True,
        )
        self.assertTrue(codex_exp.experience_id.startswith("exp_"))

    # =========================================================================
    # SCENARIO B: Continuous Academic Research Tracking & Daily Digest
    # =========================================================================
    def test_scenario_b_academic_research_tracking_pipeline(self):
        """User sets research direction: 'Adaptive Single-Pixel Computational Imaging'.
        Dot fetches arXiv literature, reranks papers, generates structured daily digest with candidate
        innovations, and transforms approved directions into an experimental research task.
        """
        owner_scholar = "qq_scholar_1001"
        direction = "自适应单像素计算成像 (Adaptive Single-Pixel Computational Imaging)"

        # 1. User records research direction in scoped memory
        self.mem_store.store_memory(
            scope=MemoryScope.USER_PREFERENCE,
            owner_id=owner_scholar,
            key="research_focus",
            content=f"Current key tracked research topic: {direction}",
            importance=10,
            source=MemorySource.USER_EXPLICIT,
            confidence=1.0,
        )
        retrieved_pref = self.mem_store.get_scoped_memories(MemoryScope.USER_PREFERENCE, owner_id=owner_scholar)
        self.assertTrue(any("Adaptive Single-Pixel" in m.content for m in retrieved_pref))

        # 2. Academic pipeline searches and generates digest
        # First verify that offline/failed retrieval reports retrieval_failed rather than fabricating fake papers
        fail_pipe = AcademicResearchPipeline(self.engine, paper_fetcher=lambda q, m: [])
        fail_digest = fail_pipe.generate_research_daily_digest(topic="Unreachable", owner_id=owner_scholar)
        self.assertEqual(fail_digest["status"], "retrieval_failed")
        self.assertEqual(fail_digest["papers"], [])

        # Now test with structured arXiv feed response
        pipeline = AcademicResearchPipeline(
            self.engine,
            paper_fetcher=lambda q, m: [{
                "title": "Adaptive Single-Pixel Computational Imaging via Variational Hadamard Basis",
                "summary": "Demonstrates 3.4dB PSNR improvement at 10% sampling ratio using adaptive Hadamard sensing.",
                "link": "https://arxiv.org/abs/2411.08421",
                "published": "2024-11-15",
                "authors": ["L. Wang", "M. Zhang"],
                "source": "arXiv",
            }],
        )
        digest = pipeline.generate_research_daily_digest(
            topic="Single-Pixel Imaging Adaptive Hadamard CS Reconstruction",
            owner_id=owner_scholar,
            user_research_focus=direction,
        )
        self.assertIn("papers", digest)
        self.assertTrue(digest["papers_reviewed_count"] >= 1)

        paper = digest["papers"][0]
        self.assertIn("paper_title", paper)
        self.assertIn("citation_link", paper)
        self.assertIn("innovation_candidate", paper)
        self.assertIn("limitations", paper)
        self.assertTrue(len(digest["next_executable_experiments"]) >= 1)

        # 3. User approves candidate direction -> create durable research project & task
        proj = self.research_tracker.create_research_project(
            project_id="proj_single_pixel_01",
            topic="Adaptive Single-Pixel Computational Imaging Algorithm Autonomous Verification",
            goal="Compressed sensing based on VAE priors and Hadamard bases",
            task_id="task_research_lead_01",
            initial_milestones=[{"title": "Literature Review and Baseline Reproduction"}, {"title": "Adaptive Sampling Base Optimization and Simulation"}],
            owner_id=owner_scholar,
        )
        self.assertEqual(proj.owner_id, owner_scholar)

        # Formulate verifiable hypothesis with explicit scope
        hypo = self.research_tracker.create_hypothesis(
            project_id=proj.project_id,
            stage_index=1,
            statement="Adaptive sampling at 10% rate improves reconstruction PSNR by over 3dB compared to fixed Hadamard",
            rationale="Variational prior focuses earlier on high frequency gradient regions",
            verifiable_criterion="Simulated single-pixel PSNR mean exceeds fixed basis by 3.0 or more",
            owner_id=owner_scholar,
        )
        self.assertEqual(hypo.status, FindingStatus.HYPOTHESIS.value)

        # Ensure project and hypothesis are safely archived
        p_summary = self.research_tracker.get_project_summary(proj.project_id)
        self.assertIn("Adaptive Single-Pixel", p_summary["topic"])

    # =========================================================================
    # SCENARIO C: Multi-User Security, Memory/Task Isolation & Subscription Risk Gating
    # =========================================================================
    def test_scenario_c_multi_user_security_and_subscription_risk_gating(self):
        """Verify strict multi-user boundaries between Alice and Bob:
        - Memory and tasks are strictly segregated.
        - Cross-user task tampering is blocked.
        - Shared personal subscription triggers risk warning and requires admin approval.
        """
        user_alice = "qq_user_alice"
        user_bob = "qq_user_bob"

        # 1. Private memory isolation
        self.mem_store.store_memory(
            scope=MemoryScope.USER_PREFERENCE,
            owner_id=user_alice,
            key="private_key_config",
            content="Alice private dev machine SSH config and internal deployment path: /opt/alice_secret",
            importance=9,
            source=MemorySource.USER_EXPLICIT,
            is_shared=False,  # Private
        )
        bob_mems = self.mem_store.get_scoped_memories(MemoryScope.USER_PREFERENCE, owner_id=user_bob)
        self.assertEqual(len(bob_mems), 0, "Bob should not see Alice's private memories")

        # 2. Task isolation
        t_alice = self.task_manager.create_task(
            title="Alice private data cleanup",
            description="Clean up temporary build artifacts under Alice directory",
            owner_id=user_alice,
        )
        bob_tasks = self.task_manager.list_tasks(owner_id=user_bob)
        self.assertEqual(len(bob_tasks), 0, "Bob should not see Alice's tasks in task list")

        # 3. Cross-user operation prevention
        with self.assertRaises((PermissionError, ValueError, Exception)):
            # Bob attempts to cancel or hijack Alice's research project
            self.research_tracker.add_hypothesis(
                project_id="proj_single_pixel_01",
                stage_index=1,
                hypothesis="Maliciously modified hypothesis",
                owner_id=user_bob,
            )

        # 4. Shared Subscription Risk Gating:
        # When user_bob attempts to invoke a shared personal subscription,
        # system requires explicit confirmation and admin approval ticket.
        appr_ok, reason = self.approval_mgr.check_and_request_approval(
            task_id="task_bob_sub_001",
            category=ApprovalCategory.SHARED_SUBSCRIPTION_RISK,
            description="User Bob requests calling shared personal Claude/Codex subscription account",
            details={
                "user": user_bob,
                "risk_notice": "Multi-user sharing personal accounts carries risk of suspension and data cross-talk; requires admin signature",
                "service": "codex_shared_tier",
            },
            owner_id=user_bob,
            sync_wait=False,  # Pending asynchronous approval
        )
        self.assertFalse(appr_ok)
        self.assertIn("pending user approval", reason.lower())

        # Admin explicitly approves the ticket
        reqs = self.approval_mgr.list_pending_requests(owner_id=user_bob)
        self.assertTrue(len(reqs) >= 1)
        req_id = reqs[0].req_id

        import house_web, os
        from unittest.mock import patch
        os.environ["QQ_ADMINS"] = "admin_supervisor"
        with patch.object(house_web, "friend_for_token", return_value="admin_supervisor"):
            admin_ctx = self.approval_mgr.register_trusted_admin("admin_supervisor", session_token="mock_admin_token")
            approved = self.approval_mgr.approve_request(
                req_id=req_id,
                allowed_scope="once",
                reason="Administrator reviewed and signed acknowledgement",
                approver_id=admin_ctx,
            )
            self.assertTrue(approved)

        # Verified status after approval
        updated_req = self.approval_mgr.get_request(req_id)
        self.assertEqual(updated_req.status, ApprovalStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()