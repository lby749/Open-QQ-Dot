"""Comprehensive Verification & P0 Regression Tests for Browser Automation, Video Production & E-Commerce Pipelines.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from browser_automation import IsolatedBrowserSession, BrowserInterventionRequired, get_browser_session
from video_ecommerce_pipeline import VideoAutomationPipeline, EcommerceAutomationPipeline
from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus


class _LocalTestHTMLHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/404":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", "session_probe=ok_123; Path=/")
        self.end_headers()
        html = (
            "<html><head><title>Example Domain Test Page</title></head>"
            "<body><h1>Example Domain</h1>"
            "<p>Real local HTTP server content for Open QQ Dot browser verification.</p>"
            "<button id='buy_btn' class='primary-btn'>Buy Now</button>"
            "<a id='more_link' href='/more'>More Info</a>"
            "<input id='user_password' name='password' type='password' value='' />"
            "<input id='search_input' name='q' type='text' value='' />"
            "</body></html>"
        )
        self.wfile.write(html.encode("utf-8"))


class TestBrowserVideoEcommerce(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LocalTestHTMLHandler)
        cls.port = cls.httpd.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_dot_bve_")
        self.work_dir = Path(self.temp_dir)
        self.approval_db = self.work_dir / "approval.sqlite3"
        self.approval_mgr = ApprovalManager(db_path=self.approval_db)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. BROWSER AUTOMATION & P0 SECURITY TESTS
    # =========================================================================
    def test_01_browser_session_isolation_cookie_and_path_traversal_guard(self):
        """Verify per-user browser profile & cookie separation, and block owner_id path traversal."""
        session_alice = IsolatedBrowserSession("user_alice", base_dir=self.work_dir / "browser")
        session_bob = IsolatedBrowserSession("user_bob", base_dir=self.work_dir / "browser")

        # Explicit host context required: a hostless Cookie cannot be safely stored.
        session_alice.current_url = self.base_url + '/login'
        session_bob.current_url = self.base_url + '/login'
        session_alice.save_cookies({"auth_token": "token_alice_12345"})
        self.assertEqual(session_alice.load_cookies().get("auth_token"), "token_alice_12345")
        self.assertEqual(session_bob.load_cookies(), {})

        # P0 Regression: Malicious owner_id attempting directory traversal MUST raise PermissionError
        for malicious_id in ("../user_alice", "../../etc", "alice/../../bob", "user:admin", "..\\windows"):
            with self.assertRaises(PermissionError, msg=f"Should reject malicious owner_id: {malicious_id}"):
                IsolatedBrowserSession(malicious_id, base_dir=self.work_dir / "browser")

    def test_02_browser_real_navigation_error_honesty_and_dom_checks(self):
        """Verify real HTTP page navigation, honest failure on unreachable/404 pages, and real DOM element checks."""
        # This test server is explicitly allowed; private-network navigation
        # must remain blocked by default for production users.
        session = IsolatedBrowserSession("user_tester", base_dir=self.work_dir / "browser",
            allowed_local_origins=[self.base_url, "http://127.0.0.1:1"])

        # 1. Unreachable / invalid URL MUST return status='failed' (never fake success)
        bad_res = session.navigate("http://127.0.0.1:1/unreachable", timeout_sec=2.0)
        self.assertEqual(bad_res["status"], "failed")

        # 2. HTTP 404 page MUST return status='failed'
        res_404 = session.navigate(f"{self.base_url}/404", timeout_sec=5.0)
        self.assertEqual(res_404["status"], "failed")

        # 3. Real local HTTP page navigation succeeds and extracts DOM
        res = session.navigate(f"{self.base_url}/index.html", timeout_sec=5.0)
        self.assertEqual(res["status"], "success")
        self.assertIn("example", res["title"].lower())
        self.assertEqual(session.load_cookies().get("session_probe"), "ok_123")

        # 4. Clicking an existing button in DOM succeeds
        click_ok = session.click("#buy_btn")
        self.assertEqual(click_ok["status"], "clicked")

        # 5. P0 Regression: Clicking a non-existent button MUST return status='failed' (never fake clicked)
        click_bad = session.click("#non_existent_button_999")
        self.assertEqual(click_bad["status"], "failed")

        # 6. Input into existing vs non-existent form elements
        inp_ok = session.input_text("#user_password", "my_secret_123")
        self.assertEqual(inp_ok["status"], "filled")
        self.assertEqual(inp_ok["masked_value"], "***")

        inp_bad = session.input_text("#missing_input_field", "hello")
        self.assertEqual(inp_bad["status"], "failed")

        # 7. Screenshot snapshot (SVG & PNG)
        snap_path = self.work_dir / "snapshot.svg"
        saved = session.take_screenshot(snap_path)
        self.assertTrue(saved.is_file())
        self.assertTrue(saved.stat().st_size > 100)

        png_path = self.work_dir / "snapshot.png"
        saved_png = session.take_screenshot(png_path)
        self.assertTrue(saved_png.is_file())
        self.assertTrue(saved_png.stat().st_size > 100)

    def test_03_browser_captcha_barrier_and_human_takeover(self):
        """Verify detection of CAPTCHA/2FA barriers and human intervention triggering."""
        session = IsolatedBrowserSession("user_shopper", base_dir=self.work_dir / "browser")
        captcha_html = "<html><body><h1>Please enter captcha for security verification</h1><div class='g-recaptcha'></div></body></html>"

        with self.assertRaises(BrowserInterventionRequired):
            session._check_for_human_intervention(captcha_html)

        approved = session.request_human_takeover("Encountered slider captcha, requesting manual takeover", approval_manager=self.approval_mgr)
        self.assertFalse(approved, "Should pause and wait for user takeover approval")

        pending = self.approval_mgr.list_pending_requests(owner_id="user_shopper")
        self.assertEqual(len(pending), 1)
        self.assertIn("intervention", pending[0].description.lower())

    # =========================================================================
    # 2. VIDEO PRODUCTION PIPELINE & ANTI-FAKE MP4 TESTS
    # =========================================================================
    def test_04_video_script_audio_render_and_anti_fake_qa_loop(self):
        """Verify real FFmpeg rendering, stream decode QA, and rejection of missing FFmpeg or fake MP4 bytes."""
        video_pipe = VideoAutomationPipeline(workspace_dir=self.work_dir / "video")

        # 1. P0 Regression: Missing FFmpeg binary MUST raise RuntimeError, never generate fake MP4
        no_ffmpeg_pipe = VideoAutomationPipeline(workspace_dir=self.work_dir / "video_nofm", ffmpeg_bin="/nonexistent/ffmpeg")
        with self.assertRaises(RuntimeError):
            no_ffmpeg_pipe.render_video_with_ffmpeg(
                audio_path=self.work_dir / "a.wav",
                subtitle_path=self.work_dir / "s.srt",
                output_mp4_path=self.work_dir / "fake_out.mp4",
            )
        self.assertFalse((self.work_dir / "fake_out.mp4").exists())

        # 2. P0 Regression: Fake byte-padded MP4 file MUST fail inspect_video_quality!
        fake_mp4 = self.work_dir / "video" / "forged_fake.mp4"
        fake_mp4.parent.mkdir(parents=True, exist_ok=True)
        fake_mp4.write_bytes(b"ftypmp42" + b"\x00" * 4096)
        fake_qa = video_pipe.inspect_video_quality(fake_mp4)
        self.assertFalse(fake_qa["is_valid"], f"Fake MP4 must fail QA: {fake_qa}")
        self.assertEqual(fake_qa["qa_status"], "FAILED")

        # 3. Real FFmpeg end-to-end synthesis & decode QA
        if not video_pipe.ffmpeg_bin or not Path(video_pipe.ffmpeg_bin).is_file():
            self.skipTest("FFmpeg binary not installed on this host")

        script = video_pipe.generate_video_script("Open QQ Dot Multi-Agent Collaborative System", target_duration_sec=4)
        self.assertEqual(len(script["scenes"]), 2)
        self.assertIn("Multi-Agent", script["full_narration"])

        audio_path = self.work_dir / "video" / "narration.wav"
        sub_path = self.work_dir / "video" / "subtitles.srt"
        video_pipe.generate_narration_audio(script["full_narration"], audio_path)
        video_pipe.generate_srt_subtitles(script["scenes"], sub_path)
        self.assertTrue(audio_path.is_file())
        self.assertTrue(sub_path.is_file())

        mp4_out = self.work_dir / "video" / "sample_output.mp4"
        video_pipe.render_video_with_ffmpeg(
            audio_path=audio_path,
            subtitle_path=sub_path,
            output_mp4_path=mp4_out,
            duration_sec=4,
        )
        self.assertTrue(mp4_out.is_file())

        qa_res = video_pipe.inspect_video_quality(mp4_out)
        self.assertTrue(qa_res["is_valid"], f"Real FFmpeg MP4 should pass decode QA: {qa_res}")
        self.assertEqual(qa_res["qa_status"], "PASSED")
        self.assertTrue(qa_res["duration_sec"] > 0)

    # =========================================================================
    # 3. E-COMMERCE MULTI-TENANT ISOLATION & PAYMENT SAFETY TESTS
    # =========================================================================
    def test_05_ecommerce_multi_tenant_order_isolation_and_owner_immutability(self):
        """P0 Regression:
        1. Bob cannot view Alice's order via track_order_status.
        2. Bob cannot dispatch or modify Alice's order via execute_order_dispatch.
        3. Order owner_id is strictly immutable.
        """
        ecom_pipe = EcommerceAutomationPipeline(approval_manager=self.approval_mgr, db_path=self.work_dir / "ecom.sqlite3")

        # Alice creates/tracks her order
        order_alice = ecom_pipe.track_order_status("ORD20261022099", owner_id="user_alice")
        self.assertEqual(order_alice["owner_id"], "user_alice")
        self.assertEqual(order_alice["status"], "IN_TRANSIT")

        # Bob attempts to read Alice's order -> MUST raise PermissionError
        with self.assertRaises(PermissionError):
            ecom_pipe.track_order_status("ORD20261022099", owner_id="user_bob")

        # Bob attempts to dispatch Alice's order -> MUST raise PermissionError
        with self.assertRaises(PermissionError):
            ecom_pipe.execute_order_dispatch(
                order_id="ORD20261022099",
                carrier="FedEx Express",
                owner_id="user_bob",
                idempotency_key="bob_hijack_key_001",
            )

        # Verify Alice's order owner_id and status remain untouched
        order_check = ecom_pipe.track_order_status("ORD20261022099", owner_id="user_alice")
        self.assertEqual(order_check["owner_id"], "user_alice")
        self.assertEqual(order_check["status"], "IN_TRANSIT")

        # Alice dispatches her own order -> succeeds
        ok_disp, _, disp_data = ecom_pipe.execute_order_dispatch(
            order_id="ORD20261022099",
            carrier="FedEx Express",
            owner_id="user_alice",
            idempotency_key="alice_disp_001",
        )
        self.assertTrue(ok_disp)
        self.assertEqual(disp_data["status"], "DISPATCHED")
        self.assertEqual(disp_data["owner_id"], "user_alice")

    def test_06_ecommerce_payment_idempotency_and_no_fake_production_paid(self):
        """Critical Financial Invariants:
        1. Missing idempotency key is rejected immediately.
        2. Unapproved payment is held in PENDING_APPROVAL.
        3. Without real payment_gateway, sandbox_mode=False refuses to fabricate PAID_SUCCESS.
        4. In sandbox_mode=True, approved payment returns SANDBOX_SIMULATED_PAID with is_simulated=True.
        5. Replay with identical idempotency key is blocked across restarts.
        """
        db_file = self.work_dir / "ecom_pay.sqlite3"
        ecom_pipe = EcommerceAutomationPipeline(approval_manager=self.approval_mgr, db_path=db_file, sandbox_mode=True)
        user_buyer = "user_buyer_888"

        # 1. Reject payment without idempotency key
        ok_no_key, msg_no_key, _ = ecom_pipe.execute_payment(
            order_id="ORD_1001",
            amount=299.0,
            owner_id=user_buyer,
            idempotency_key="",
        )
        self.assertFalse(ok_no_key)
        self.assertIn("missing idempotency", msg_no_key.lower())

        # 2. First attempt with idempotency key -> pauses for user approval
        idem_key = "idemp_order_1001_unique"
        ok_p1, msg_p1, rec_p1 = ecom_pipe.execute_payment(
            order_id="ORD_1001",
            amount=299.0,
            owner_id=user_buyer,
            idempotency_key=idem_key,
        )
        self.assertFalse(ok_p1)
        self.assertEqual(rec_p1["status"], "PENDING_APPROVAL")

        # 3. User approves payment
        pending_reqs = self.approval_mgr.list_pending_requests(owner_id=user_buyer)
        self.assertEqual(len(pending_reqs), 1)
        req_id = pending_reqs[0].req_id
        # Emulate an authenticated Web session instead of the legacy unsafe
        # approver_id string (which production must reject).
        import sys, types
        from unittest.mock import patch
        from approval_manager import create_trusted_identity
        fake_web = types.SimpleNamespace(
            friend_for_token=lambda token: user_buyer if token == "test-session-token" else None,
            is_admin_user=lambda uid: False,
        )
        with patch.dict(sys.modules, {"house_web": fake_web}):
            approver = create_trusted_identity(user_buyer, session_token="test-session-token")
        self.approval_mgr.approve_request(
            req_id=req_id,
            allowed_scope="task",
            reason="User confirmed payment agreement",
            approver_id=approver,
        )

        # 4. P0 Check: Production mode (sandbox_mode=False) without real gateway refuses to fake PAID_SUCCESS
        prod_pipe = EcommerceAutomationPipeline(approval_manager=self.approval_mgr, db_path=db_file, sandbox_mode=False)
        ok_prod, msg_prod, rec_prod = prod_pipe.execute_payment(
            order_id="ORD_1001",
            amount=299.0,
            owner_id=user_buyer,
            idempotency_key=idem_key,
            task_id=pending_reqs[0].task_id,
        )
        self.assertFalse(ok_prod)
        self.assertEqual(rec_prod["status"], "PAYMENT_GATEWAY_NOT_CONFIGURED")

        # 5. Sandbox mode executes and explicitly labels receipt as SANDBOX_SIMULATED_PAID (is_simulated=True)
        ok_p2, msg_p2, receipt = ecom_pipe.execute_payment(
            order_id="ORD_1001",
            amount=299.0,
            owner_id=user_buyer,
            idempotency_key=idem_key,
            task_id=pending_reqs[0].task_id,
        )
        self.assertTrue(ok_p2)
        self.assertEqual(receipt["status"], "SANDBOX_SIMULATED_PAID")
        self.assertTrue(receipt["is_simulated"])

        # 6. Replay attempt with same idempotency key is strictly blocked across restarts
        ecom_restarted = EcommerceAutomationPipeline(approval_manager=self.approval_mgr, db_path=db_file, sandbox_mode=True)
        ok_replay, msg_replay, _ = ecom_restarted.process_payment(
            order_id="ORD_1001",
            amount=299.0,
            owner_id=user_buyer,
            idempotency_key=idem_key,
        )
        self.assertFalse(ok_replay)
        self.assertIn("duplicate", msg_replay.lower())

    # =========================================================================
    # 4. TOOL REGISTRY PERMISSION SEPARATION & CAPABILITY DISCOVERY
    # =========================================================================
    def test_07_tool_registry_permission_separation_and_discovery(self):
        """Verify that read-only ecommerce_query (SAFE_READ) is separated from
        write-side ecommerce_dispatch (SENSITIVE_EXEC) and ecommerce_payment (SENSITIVE_EXEC).
        """
        from tool_registry import ToolRegistry, ToolPermissionLevel
        from local_retrieval_engine import LocalHybridSearchEngine, CapabilityDiscoveryService

        reg = ToolRegistry.get_instance()
        q_tool = reg.get_tool("ecommerce_query")
        d_tool = reg.get_tool("ecommerce_dispatch")
        p_tool = reg.get_tool("ecommerce_payment")

        self.assertIsNotNone(q_tool)
        self.assertIsNotNone(d_tool)
        self.assertIsNotNone(p_tool)
        self.assertEqual(q_tool.required_permission, ToolPermissionLevel.SAFE_READ)
        self.assertEqual(d_tool.required_permission, ToolPermissionLevel.SENSITIVE_EXEC)
        self.assertEqual(p_tool.required_permission, ToolPermissionLevel.SENSITIVE_EXEC)

        # Read-only mode can call ecommerce_query, but MUST block ecommerce_dispatch
        res_q = reg.execute_tool(
            "ecommerce_query",
            {"operation": "search", "query": "4K Monitor"},
            workspace=self.work_dir,
            computer_config={"enabled": True, "mode": "read_only", "commands": False, "_owner_id": "user_alice"},
        )
        self.assertTrue(res_q.success)

        res_d = reg.execute_tool(
            "ecommerce_dispatch",
            {"order_id": "ORD_1", "idempotency_key": "k_1"},
            workspace=self.work_dir,
            computer_config={"enabled": True, "mode": "read_only", "commands": False, "_owner_id": "user_alice"},
        )
        self.assertFalse(res_d.success)
        self.assertIn('没有命令执行权限', res_d.error)
        self.assertIn('未执行', res_d.error)

        engine = LocalHybridSearchEngine(self.work_dir / "retrieval.sqlite3")
        cap_service = CapabilityDiscoveryService(engine)
        caps = cap_service.scan_environment_capabilities(self.work_dir)
        cap_names = {c.name for c in caps}
        self.assertIn("ecommerce_query", cap_names)
        self.assertIn("ecommerce_dispatch", cap_names)


if __name__ == "__main__":
    unittest.main()
