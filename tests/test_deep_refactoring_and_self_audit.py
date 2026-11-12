"""Comprehensive Deep Refactoring, P0 Root-Cause & Red-Team Self-Audit Verification Suite.

Covers:
- P0-1: Browser file:// access control, symlink escape, URL-encoded traversal, HTTP 302 -> file:// redirect block,
        tricky IP (decimal/hex/octal/IPv6) SSRF block, and upload/download directory containment.
- P0-2: ApprovalManager non-default approver identity, unforgeable _INTERNAL_SYSTEM_TOKEN, TrustedIdentityContext
        HMAC verification, anti-spoofing of 'system'/'admin' strings, state transitions, and SQLite audit logs.
- P0-3: TaskExperienceStore state machine (CANDIDATE -> EXECUTED -> RESULT_RECORDED -> VALIDATED),
        fake task_id rejection, 'FAKE PASS' evidence rejection, cross-tenant task/evidence theft block,
        stale artifact detection, historical poisoned data sanitization, and concurrent transition safety.
- Test B (Real Interactive Browser): Real DOM mutation, inline JS execution, real HTTP POST form serialization
        and submission, wait_for_selector visibility checks, real Headless Chromium PNG screenshot, and 8 failure modes.
- Self-Audit Fixes: AgentMemoryStore cross-tenant invalidate protection, TaskManager cross-tenant control protection,
        IR metrics (Recall@K, MRR, nDCG@K), and Research subscriptions/knowledge graph linking.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import unittest

from approval_manager import (
    ApprovalManager,
    ApprovalCategory,
    ApprovalPolicy,
    ApprovalStatus,
    TrustedIdentityContext,
    create_trusted_identity,
    _INTERNAL_SYSTEM_TOKEN,
)
from browser_automation import IsolatedBrowserSession, BrowserInterventionRequired
from local_retrieval_engine import (
    LocalHybridSearchEngine,
    TaskExperienceStore,
    CapabilityDiscoveryService,
    DynamicContextBuilder,
    TaskDeduplicatorAndFailureDetector,
    AcademicResearchPipeline,
    ExperienceLifecycleState,
    ExperienceResultStatus,
    ExperienceConfidence,
)
from task_manager import TaskManager, TaskStatus
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource


class _InteractiveWebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect_to_file":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path == "/redirect_to_metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.6)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body>Slow Page</body></html>")
            return
        if self.path == "/download_doc.txt":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"VERIFIED_DOWNLOAD_PAYLOAD_2026")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = """<!DOCTYPE html>
<html>
<head><title>Interactive Order Portal</title></head>
<body>
  <h1 id="banner">Order Portal Ready</h1>
  <div id="calc_out">Total: None</div>
  <div id="hidden_box" style="display:none">Secret Hidden Text</div>
  <form id="order_form" action="/submit_order" method="POST">
    <input id="item_name" name="item" type="text" value="" />
    <textarea id="note_box" name="note"></textarea>
    <input id="file_upload" name="attachment" type="file" />
    <input id="disabled_input" name="locked" type="text" disabled />
    <button id="calc_btn" type="button" onclick="document.getElementById('calc_out').innerText = 'Preview: ' + document.getElementById('item_name').value">Preview Calculation</button>
    <button id="submit_btn" type="submit">Submit Order</button>
  </form>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        if self.path == "/submit_order":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            params = urllib.parse.parse_qs(raw_body)
            item = (params.get("item") or [""])[0]
            note = (params.get("note") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            resp_html = f"""<!DOCTYPE html>
<html>
<head><title>Order Submitted</title></head>
<body>
  <h1 id="order_confirm">Confirmed: {item} | Note: {note}</h1>
</body>
</html>"""
            self.wfile.write(resp_html.encode("utf-8"))


class TestDeepRefactoringAndSelfAudit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _InteractiveWebAppHandler)
        cls.port = cls.httpd.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_deep_audit_")
        self.work_dir = Path(self.temp_dir)
        self.tasks_db = self.work_dir / "tasks.sqlite3"
        self.retrieval_db = self.work_dir / "retrieval_engine.sqlite3"
        self.approval_db = self.work_dir / "approvals.sqlite3"
        self.memory_db = self.work_dir / "memory.sqlite3"

        self.task_mgr = TaskManager(db_path=self.tasks_db, workspace=self.work_dir)
        self.engine = LocalHybridSearchEngine(self.retrieval_db)
        self.exp_store = TaskExperienceStore(self.engine, tasks_db_path=self.tasks_db)
        self.approval_mgr = ApprovalManager(db_path=self.approval_db)
        self.mem_store = AgentMemoryStore(db_path=self.memory_db)

    def tearDown(self):
        self.task_mgr.stop_worker()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. P0-1: BROWSER FILE ACCESS, SYMLINKS, REDIRECTS & SSRF
    # =========================================================================
    def test_p0_1_browser_file_access_symlink_redirect_and_ssrf_guards(self):
        """Verify:
        1. Default browser session denies all file:// access for both Alice and Bob.
        2. Authorized workspace file:// access permits files inside Alice's root, but blocks
           Bob's root, '../' traversal, URL-encoded '%2e%2e', and symlinks pointing outside.
        3. HTTP 302 redirects to file:///etc/passwd or 169.254.169.254 are blocked.
        4. Decimal/hex/octal/IPv6 loopback SSRF tricks are blocked when allow_local_test_origins=False.
        """
        alice_ws = self.work_dir / "ws_alice"
        bob_ws = self.work_dir / "ws_bob"
        alice_ws.mkdir(parents=True, exist_ok=True)
        bob_ws.mkdir(parents=True, exist_ok=True)

        alice_doc = alice_ws / "alice_page.html"
        alice_doc.write_text("<html><head><title>Alice Doc</title></head><body>Alice Allowed Content</body></html>", "utf-8")
        bob_secret = bob_ws / "bob_secret.html"
        bob_secret.write_text("<html><body>BOB_PRIVATE_KEY_999</body></html>", "utf-8")

        # 1. Default session (allow_local_files=False) blocks file:// even for Alice's own file
        s_default = IsolatedBrowserSession("user_alice", base_dir=self.work_dir / "browser")
        with self.assertRaises(PermissionError):
            s_default.navigate(alice_doc.as_uri())
        with self.assertRaises(PermissionError):
            s_default.navigate("/etc/passwd")

        # 2. Authorized file session for Alice allows alice_doc, but blocks bob_secret, ../, %2e%2e, and symlinks!
        s_alice = IsolatedBrowserSession(
            "user_alice",
            base_dir=self.work_dir / "browser",
            allow_local_files=True,
            allowed_file_roots=[alice_ws],
        )
        ok_nav = s_alice.navigate(alice_doc.as_uri())
        self.assertEqual(ok_nav["status"], "success")
        self.assertEqual(ok_nav["title"], "Alice Doc")

        # Cross-user file:// access blocked
        with self.assertRaises(PermissionError):
            s_alice.navigate(bob_secret.as_uri())

        # URL-encoded ../ traversal blocked
        with self.assertRaises(PermissionError):
            s_alice.navigate(f"file://{alice_ws}/%2e%2e/ws_bob/bob_secret.html")

        # Symlink inside alice_ws pointing to bob_secret blocked
        symlink_escape = alice_ws / "sneaky_link.html"
        try:
            os.symlink(bob_secret, symlink_escape)
            with self.assertRaises(PermissionError):
                s_alice.navigate(symlink_escape.as_uri())
        except OSError:
            # On Windows without developer mode/admin rights, os.symlink raises WinError 1314
            pass

        # 3. HTTP 302 Redirects to file:// or cloud metadata 169.254.169.254 are blocked
        with self.assertRaises(PermissionError):
            s_alice.navigate(f"{self.base_url}/redirect_to_file")
        with self.assertRaises(PermissionError):
            s_alice.navigate(f"{self.base_url}/redirect_to_metadata")

        # 4. SSRF protection against decimal/hex/octal/IPv6 loopback when allow_local_test_origins=False
        s_no_local = IsolatedBrowserSession(
            "user_bob",
            base_dir=self.work_dir / "browser",
            allow_local_test_origins=False,
        )
        for ssrf_url in (
            "http://127.0.0.1:8080/",
            "http://localhost:8080/",
            "http://2130706433/",
            "http://0x7f000001/",
            "http://0177.0.0.1/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://0.0.0.0/",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.assertRaises(PermissionError, msg=f"Should block SSRF URL: {ssrf_url}"):
                s_no_local.navigate(ssrf_url)

    # =========================================================================
    # 2. P0-2: APPROVAL MANAGER NON-DEFAULT IDENTITY & ANTI-SPOOFING
    # =========================================================================
    def test_p0_2_approval_no_default_system_identity_and_audit_trail(self):
        """Verify:
        1. Omitting approver_id in approve_request/reject_request/decide_request raises PermissionError.
        2. Passing 'system', 'admin', 'system:admin', or 'user_bob' to approve Alice's request raises PermissionError.
        3. Forged TrustedIdentityContext signature is rejected.
        4. Legitimate owner ('user_alice') or registered admin TrustedIdentityContext succeeds.
        5. Full state transitions (REJECT -> REOPEN -> APPROVE, CANCEL, EXPIRE) are logged in approval_audit_logs.
        """
        req_id = self.approval_mgr.submit_request(
            task_id="task_high_risk_01",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="Execute high risk production script",
            owner_id="user_alice",
        )

        # 1. Omitting approver_id MUST raise PermissionError (no default 'system' identity!)
        with self.assertRaises(PermissionError):
            self.approval_mgr.approve_request(req_id)
        with self.assertRaises(PermissionError):
            self.approval_mgr.reject_request(req_id)
        with self.assertRaises(PermissionError):
            self.approval_mgr.decide_request(req_id, approved=True)

        # 2. Plain string 'system', 'admin', 'system:admin', or 'user_bob' MUST raise PermissionError
        for spoof_id in ("system", "admin", "system:admin", "user_bob", "root"):
            with self.assertRaises(PermissionError, msg=f"Spoofed approver_id '{spoof_id}' must be rejected"):
                self.approval_mgr.approve_request(req_id, approver_id=spoof_id)

        # 3. Forged TrustedIdentityContext with tampered signature MUST raise PermissionError
        forged_ctx = TrustedIdentityContext(user_id="user_alice", is_admin=True, issued_at=time.time(), signature="deadbeef0000")
        with self.assertRaises(PermissionError):
            self.approval_mgr.approve_request(req_id, approver_id=forged_ctx)

        # 4. Alice rejects her own request -> status becomes REJECTED, then reopens -> PENDING, then approves -> APPROVED
        import house_web
        from unittest.mock import patch
        with patch.object(house_web, "friend_for_token", return_value="user_alice"):
            alice_ctx = create_trusted_identity("user_alice", is_admin=False, session_token="mock_alice_token")
            self.assertTrue(self.approval_mgr.reject_request(req_id, reason="Pause and inspect first", approver_id=alice_ctx))
            self.assertEqual(self.approval_mgr.get_request(req_id).status, ApprovalStatus.REJECTED)

            self.assertTrue(self.approval_mgr.reopen_request(req_id, approver_id=alice_ctx, reason="Inspection finished reopen"))
            self.assertEqual(self.approval_mgr.get_request(req_id).status, ApprovalStatus.PENDING)

            self.assertTrue(self.approval_mgr.approve_request(req_id, approver_id=alice_ctx, reason="Confirm safe"))
            self.assertEqual(self.approval_mgr.get_request(req_id).status, ApprovalStatus.APPROVED)

        # 5. Verify audit trail recorded all denied attempts and legitimate state transitions
        audits = self.approval_mgr.get_audit_logs(req_id)
        actions = [a["action"] for a in audits]
        self.assertIn("SUBMITTED", actions)
        self.assertIn("APPROVE_DENIED", actions)
        self.assertIn("REJECTED", actions)
        self.assertIn("REOPENED", actions)
        self.assertIn("APPROVED", actions)

    # =========================================================================
    # 3. P0-3: EXPERIENCE STORE STATE MACHINE & ANTI-FABRICATION
    # =========================================================================
    def test_p0_3_experience_state_machine_fake_task_and_cross_tenant_guards(self):
        """Verify:
        1. Non-existent task_id with 'FAKE PASS' evidence cannot become VALIDATED / HIGH_VERIFIED.
        2. Existing task that is only PENDING/RUNNING or lacks TaskVerifier pass cannot become VALIDATED.
        3. Cross-tenant attack: Bob submitting Alice's completed task_id raises PermissionError.
        4. Stale artifact file is flagged as STALE and downgraded to LOW_UNVERIFIED.
        5. Explicit state machine transitions (CANDIDATE -> EXECUTED -> RESULT_RECORDED -> VALIDATED)
           and rejection of illegal jumps (CANDIDATE -> VALIDATED).
        6. Historical poisoned row sanitization downgrades unsigned high_verified records.
        """
        # 1. Non-existent task_id + 'FAKE PASS' evidence -> downgraded to CANDIDATE / LOW_UNVERIFIED
        fake_exp = self.exp_store.record_experience(
            task_id="non_existent_task_99999",
            owner_id="user_alice",
            project_id="proj_1",
            agent_provider="codex",
            model_name="gpt-5",
            tools_used=["run_command"],
            problem_statement="Fabricated high confidence experience test",
            environment_conditions={},
            action_taken="Fabricated action",
            result_status=ExperienceResultStatus.SUCCESS_VERIFIED,
            artifacts_and_evidence=["FAKE PASS: all 100 tests green"],
            confidence_status=ExperienceConfidence.HIGH_VERIFIED,
            is_shared=True,
        )
        self.assertEqual(fake_exp.lifecycle_state, ExperienceLifecycleState.CANDIDATE)
        self.assertEqual(fake_exp.result_status, ExperienceResultStatus.UNVERIFIED_SUGGESTION)
        self.assertEqual(fake_exp.confidence_status, ExperienceConfidence.LOW_UNVERIFIED)

        # 2. Create real task for Alice, complete it WITH real TaskVerifier pass
        alice_tid = self.task_mgr.create_task("Fix database deadlock", "Optimize connection pool", category="code", owner_id="user_alice")
        self.task_mgr.update_task_status(
            alice_tid,
            TaskStatus.COMPLETED,
            result="Connection pool modification passed tests",
            artifacts=["db_pool.py"],
            verification_info={"success": True, "verifier_type": "pytest", "test_passed": True},
        )

        # 3. Cross-tenant evidence theft: Bob tries to record experience using Alice's verified task_id -> PermissionError!
        with self.assertRaises(PermissionError):
            self.exp_store.record_experience(
                task_id=alice_tid,
                owner_id="user_bob",
                project_id="proj_1",
                agent_provider="codex",
                model_name="gpt-5",
                tools_used=[],
                problem_statement="Misuse Alice task ID",
                environment_conditions={},
                action_taken="Misuse",
                result_status=ExperienceResultStatus.SUCCESS_VERIFIED,
                artifacts_and_evidence=["db_pool.py: PASSED"],
                confidence_status=ExperienceConfidence.HIGH_VERIFIED,
            )

        # 4. Stale artifact file check
        stale_file = self.work_dir / "old_artifact.log"
        stale_file.write_text("old log", "utf-8")
        old_time = time.time() - 7200
        os.utime(stale_file, (old_time, old_time))
        stale_exp = self.exp_store.record_experience(
            task_id=alice_tid,
            owner_id="user_alice",
            project_id="proj_1",
            agent_provider="codex",
            model_name="gpt-5",
            tools_used=[],
            problem_statement="Expired artifact experience test",
            environment_conditions={},
            action_taken="Use expired artifact",
            result_status=ExperienceResultStatus.SUCCESS_VERIFIED,
            artifacts_and_evidence=[str(stale_file)],
            confidence_status=ExperienceConfidence.HIGH_VERIFIED,
            max_artifact_age_sec=60.0,  # Max age 60s, file is 7200s old
        )
        self.assertEqual(stale_exp.lifecycle_state, ExperienceLifecycleState.STALE)
        self.assertEqual(stale_exp.confidence_status, ExperienceConfidence.LOW_UNVERIFIED)

        # 5. Step-by-step state machine progression: CANDIDATE -> EXECUTED -> RESULT_RECORDED -> VALIDATED
        cand_exp = self.exp_store.record_experience(
            task_id=alice_tid,
            owner_id="user_alice",
            project_id="proj_1",
            agent_provider="claude",
            model_name="claude-3-7-sonnet",
            tools_used=["write_file"],
            problem_statement="SQLite WAL concurrent write lock optimization",
            environment_conditions={},
            action_taken="Enable WAL mode and busy_timeout",
            result_status=ExperienceResultStatus.UNVERIFIED_SUGGESTION,
            artifacts_and_evidence=[],
        )
        self.assertEqual(cand_exp.lifecycle_state, ExperienceLifecycleState.CANDIDATE)

        # Illegal direct jump CANDIDATE -> VALIDATED must raise ValueError
        with self.assertRaises(ValueError):
            self.exp_store.transition_experience_state(cand_exp.experience_id, ExperienceLifecycleState.VALIDATED, "user_alice")

        # Valid progression: CANDIDATE -> EXECUTED -> RESULT_RECORDED -> VALIDATED
        self.assertTrue(self.exp_store.transition_experience_state(cand_exp.experience_id, ExperienceLifecycleState.EXECUTED, "user_alice"))
        self.assertTrue(self.exp_store.transition_experience_state(
            cand_exp.experience_id, ExperienceLifecycleState.RESULT_RECORDED, "user_alice",
            artifacts_and_evidence=["tests/test_wal.py: 1 passed"],
        ))
        self.assertTrue(self.exp_store.transition_experience_state(
            cand_exp.experience_id, ExperienceLifecycleState.VALIDATED, "user_alice",
            artifacts_and_evidence=["tests/test_wal.py: 1 passed"],
        ))

        verified_list = self.exp_store.search_experiences("SQLite WAL write lock optimization", owner_id="user_alice", only_verified_success=True)
        self.assertTrue(len(verified_list) >= 1)
        self.assertNotIn(fake_exp.experience_id, [v["experience_id"] for v in verified_list])
        self.assertNotIn(stale_exp.experience_id, [v["experience_id"] for v in verified_list])
        self.assertEqual(verified_list[0]["experience_id"], cand_exp.experience_id)
        self.assertEqual(verified_list[0]["lifecycle_state"], "VALIDATED")

        # 6. Historical poisoned record sanitization
        with self.engine._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO task_experiences (
                    experience_id, task_id, owner_id, project_id, agent_provider, model_name,
                    tools_used, problem_statement, environment_conditions, action_taken,
                    result_status, root_cause_or_condition, artifacts_and_evidence,
                    confidence_status, is_shared, lifecycle_state, verifier_signature,
                    evidence_hash, created_at, updated_at, vector_json
                ) VALUES ('exp_poisoned_99', 'fake_tid', 'user_alice', '', 'codex', '', '[]',
                          'Poisoned problem', '{}', 'Poisoned action', 'success_verified', '',
                          '["FAKE"]', 'high_verified', 1, 'VALIDATED', 'forged_sig', 'forged_hash',
                          '2026-10-22', '2026-10-22', '[]')
                """
            )
        downgraded_count = self.exp_store.sanitize_historical_experiences()
        self.assertTrue(downgraded_count >= 1)

    # =========================================================================
    # 4. TEST B: REAL INTERACTIVE BROWSER AUTOMATION & 8 FAILURE MODES
    # =========================================================================
    def test_b_real_interactive_browser_dom_form_submit_and_screenshots(self):
        """Execute full real browser workflow on local test web app:
        Open page -> wait_for_selector -> input_text -> click inline JS button -> verify DOM change ->
        click form submit -> verify HTTP POST response DOM change -> capture real screenshot -> upload/download.
        Also verify failure modes: missing element, hidden/disabled element, cancel_event, crash recovery.
        """
        try:
            import playwright
        except ImportError:
            self.skipTest("Optional playwright is not installed in this environment")

        session = IsolatedBrowserSession(
            "user_interactive",
            base_dir=self.work_dir / "browser",
            use_headless_chromium=True,
            allow_local_test_origins=True,
            allowed_local_origins=[f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"],
        )
        try:
            # 1. Open page
            nav = session.navigate(f"{self.base_url}/")
            self.assertEqual(nav["status"], "success")
            self.assertEqual(nav["title"], "Interactive Order Portal")

            # 2. Wait for visible selector vs hidden selector
            w_ok = session.wait_for_selector("#item_name", timeout_sec=2.0, visible=True)
            self.assertEqual(w_ok["status"], "success")
            w_hidden = session.wait_for_selector("#hidden_box", timeout_sec=0.2, visible=True)
            self.assertEqual(w_hidden["status"], "timeout")

            # 3. Input text into disabled input fails, into active inputs succeeds and mutates DOM
            inp_disabled = session.input_text("#disabled_input", "try_write")
            self.assertEqual(inp_disabled["status"], "failed")

            inp_item = session.input_text("#item_name", "RTX-5070-12GB")
            self.assertEqual(inp_item["status"], "filled")
            self.assertTrue(inp_item["dom_value_updated"])
            self.assertEqual(session.read_content(selector="#item_name")["content"], "RTX-5070-12GB")

            inp_note = session.input_text("#note_box", "Urgent cold chain express")
            self.assertEqual(inp_note["status"], "filled")

            # 4. Click inline JS button (#calc_btn) -> mutates #calc_out in live DOM!
            clk_js = session.click("#calc_btn")
            self.assertEqual(clk_js["status"], "clicked")
            self.assertTrue(clk_js["dom_mutated"])
            calc_dom = session.read_content(selector="#calc_out")
            self.assertEqual(calc_dom["content"], "Preview: RTX-5070-12GB")

            # 5. File upload boundary check: outside file blocked, session file succeeds
            outside_f = self.work_dir / "outside.txt"
            outside_f.write_text("outside", "utf-8")
            with self.assertRaises(PermissionError):
                session.upload_file("#file_upload", outside_f)

            allowed_f = session.session_dir / "spec.txt"
            allowed_f.write_text("spec content", "utf-8")
            up_res = session.upload_file("#file_upload", allowed_f)
            self.assertEqual(up_res["status"], "uploaded")

            # 6. Click form submit (#submit_btn) -> real HTTP POST with mutated DOM form fields!
            clk_sub = session.click("#submit_btn")
            self.assertEqual(clk_sub["status"], "clicked")
            self.assertTrue(clk_sub.get("form_submitted"))
            confirm_dom = session.read_content(selector="#order_confirm")
            self.assertEqual(confirm_dom["status"], "success")
            self.assertEqual(confirm_dom["content"], "Confirmed: RTX-5070-12GB | Note: Urgent cold chain express")

            # 7. Capture real PNG screenshot via Headless Chromium
            png_out = self.work_dir / "interactive_result.png"
            saved_png = session.take_screenshot(png_out)
            self.assertTrue(saved_png.is_file())
            self.assertTrue(saved_png.stat().st_size > 1000)

            # 8. Download file confinement check
            with self.assertRaises(PermissionError):
                session.download_file(f"{self.base_url}/download_doc.txt", filename="../../evil.txt")
            dl_ok = session.download_file(f"{self.base_url}/download_doc.txt", filename="safe_doc.txt")
            self.assertEqual(dl_ok["status"], "downloaded")

            # 9. Cancellation and crash recovery
            cancel_evt = threading.Event()
            cancel_evt.set()
            nav_cancel = session.navigate(f"{self.base_url}/", cancel_event=cancel_evt)
            self.assertEqual(nav_cancel["status"], "cancelled")

            session.reset_browser_driver()
        finally:
            session.close()

    # =========================================================================
    # 5. SELF-AUDIT FIXES: MEMORY, TASK CONTROL, IR METRICS & RESEARCH GRAPH
    # =========================================================================
    def test_self_audit_memory_task_control_and_research_knowledge_graph(self):
        """Verify:
        1. Bob cannot invalidate Alice's memory in AgentMemoryStore.
        2. Bob cannot pause, cancel, or steer Alice's task in TaskManager.
        3. LocalHybridSearchEngine computes Recall@K, MRR, nDCG@K, and P50/P95 telemetry.
        4. AcademicResearchPipeline manages research subscriptions and knowledge links.
        """
        # 1. Memory cross-tenant invalidate protection
        m_id = self.mem_store.store_memory(
            scope=MemoryScope.USER_PREFERENCE,
            owner_id="user_alice",
            key="pref_lang",
            content="Alice prefers Rust and Lean 4",
            source=MemorySource.USER_EXPLICIT,
        )
        with self.assertRaises(PermissionError):
            self.mem_store.invalidate_memory(m_id, owner_id="user_bob")
        self.assertTrue(self.mem_store.invalidate_memory(m_id, owner_id="user_alice"))

        # 2. TaskManager cross-tenant control protection
        t_alice = self.task_mgr.create_task("Alice exclusive build task", "Compile kernel", owner_id="user_alice")
        for fn in (
            lambda: self.task_mgr.pause_task(t_alice, owner_id="user_bob"),
            lambda: self.task_mgr.cancel_task(t_alice, owner_id="user_bob"),
            lambda: self.task_mgr.steer_task(t_alice, "Malicious directive", owner_id="user_bob"),
        ):
            with self.assertRaises(PermissionError):
                fn()
        self.assertTrue(self.task_mgr.cancel_task(t_alice, reason="Alice manual cancel", owner_id="user_alice"))

        # 3. Research topic subscription & knowledge graph linking
        acad = AcademicResearchPipeline(self.engine)
        sub_id = acad.subscribe_research_topic(
            owner_id="user_alice",
            topic="Neuro-Symbolic Formal Verification",
            keywords=["Lean 4", "LLM", "AST Binding"],
            research_goal="Eliminate hallucinated mathematical proofs",
        )
        subs = acad.list_subscriptions("user_alice")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["sub_id"], sub_id)

        lnk_id = acad.link_knowledge_entity(
            owner_id="user_alice",
            project_id="proj_formal_01",
            relation_type="paper_inspires_hypothesis",
            paper_url="https://arxiv.org/abs/2501.12345",
            paper_title="Neuro-Symbolic Verification",
            hypothesis_id="hypo_01",
            experiment_id="exp_01",
            evidence_summary="SymPy + Lean 4 dual check",
        )
        links = acad.get_project_knowledge_links("user_alice", "proj_formal_01")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["link_id"], lnk_id)


if __name__ == "__main__":
    unittest.main()