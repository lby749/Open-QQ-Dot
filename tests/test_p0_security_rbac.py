import re
# -*- coding: utf-8 -*-
"""P0 Regression Test: Resource-Level Authorization & Cross-Friend Privilege Escalation Defense."""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import house_status
import house_web
from approval_manager import ApprovalCategory, ApprovalManager
from task_manager import TaskManager


class TestSecurityRBAC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.old_bridge_home = os.environ.get("QQ_BRIDGE_HOME")
        os.environ["QQ_BRIDGE_HOME"] = str(cls.root)
        cls.old_root, cls.old_registry, cls.old_port = house_status.ROOT, house_status.REGISTRY, house_web.PORT
        house_status.ROOT = cls.root
        house_status.REGISTRY = cls.root / "friends.json"

        # Mock two friends: Alice (10001) and Bob (10002), plus Admin (99999)
        cls.alice_dir = cls.root / "friends" / "10001"
        cls.bob_dir = cls.root / "friends" / "10002"
        cls.admin_dir = cls.root / "friends" / "99999"

        cls.alice_dir.mkdir(parents=True, exist_ok=True)
        cls.bob_dir.mkdir(parents=True, exist_ok=True)
        cls.admin_dir.mkdir(parents=True, exist_ok=True)

        (cls.alice_dir / "friend.json").write_text(json.dumps({"qq_id": "10001", "name": "Alice"}), encoding="utf-8")
        (cls.bob_dir / "friend.json").write_text(json.dumps({"qq_id": "10002", "name": "Bob"}), encoding="utf-8")
        (cls.admin_dir / "friend.json").write_text(json.dumps({"qq_id": "99999", "name": "Admin", "is_admin": True}), encoding="utf-8")

        registry_data = {
            "friends": {
                "10001": {"directory": str(cls.alice_dir), "display_name": "Alice"},
                "10002": {"directory": str(cls.bob_dir), "display_name": "Bob"},
                "99999": {"directory": str(cls.admin_dir), "display_name": "Admin"},
            }
        }
        house_status.REGISTRY.write_text(json.dumps(registry_data), encoding="utf-8")

        # Tokens
        tokens_data = {
            "friend_tokens": {
                "10001": "token_alice_secret_123456",
                "10002": "token_bob_secret_654321",
                "99999": "token_admin_secret_999999",
            },
            "admin_qqs": ["99999"],
        }
        (cls.root / "house_web.local.json").write_text(json.dumps(tokens_data), encoding="utf-8")

        # Start house_web server on an ephemeral port
        cls.server = house_web.ThreadingHTTPServer(("127.0.0.1", 0), house_web.Handler)
        cls.port = cls.server.server_port
        house_web.PORT = cls.port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        house_status.ROOT, house_status.REGISTRY, house_web.PORT = cls.old_root, cls.old_registry, cls.old_port
        if cls.old_bridge_home is None:
            os.environ.pop('QQ_BRIDGE_HOME', None)
        else:
            os.environ['QQ_BRIDGE_HOME'] = cls.old_bridge_home
        TaskManager._instance = None
        ApprovalManager._instance = None
        import gc
        gc.collect()
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def _request(self, method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        else:
            payload = None

        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {"raw": data}
        return resp.status, parsed

    def test_missing_or_invalid_token_returns_401(self):
        # No token
        code, data = self._request("GET", "/api/friend/10001/agent_tasks")
        self.assertEqual(code, 401)
        self.assertIn("令牌无效", data.get("error", ""))

        # Invalid token
        code, data = self._request("GET", "/api/friend/10001/agent_tasks", token="wrong_fake_token")
        self.assertEqual(code, 401)

    def test_alice_token_cannot_access_bob_friend_endpoint(self):
        # Alice tries to access Bob's endpoint directly -> 403 Forbidden
        code, data = self._request("GET", "/api/friend/10002/friend", token="token_alice_secret_123456")
        self.assertEqual(code, 403)
        self.assertIn("无权访问", data.get("error", ""))

    def test_alice_cannot_view_or_cancel_bob_task(self):
        # Create Bob's private task
        task_mgr = TaskManager.get_instance()
        bob_task_id = task_mgr.create_task(
            title="Bob Private Job",
            description="Confidential data",
            owner_id="10002",
        )

        # Alice creates a task
        alice_task_id = task_mgr.create_task(
            title="Alice Public Job",
            description="Alice work",
            owner_id="10001",
        )

        # Alice lists tasks -> Should only see her own task, NOT Bob's
        code, data = self._request("GET", "/api/friend/10001/agent_tasks", token="token_alice_secret_123456")
        self.assertEqual(code, 200)
        task_ids = [t["task_id"] for t in data.get("tasks", [])]
        self.assertIn(alice_task_id, task_ids)
        self.assertNotIn(bob_task_id, task_ids)

        # Alice tries to query Bob's task detail -> 403 Forbidden
        code, data = self._request("GET", f"/api/friend/10001/agent_task_detail?task_id={bob_task_id}", token="token_alice_secret_123456")
        self.assertEqual(code, 403)
        self.assertIn("无权查看", data.get("error", ""))

        # Alice tries to cancel Bob's task -> 403 Forbidden
        code, data = self._request(
            "POST",
            "/api/friend/10001/agent_task_action",
            token="token_alice_secret_123456",
            body={"action": "cancel", "task_id": bob_task_id},
        )
        self.assertEqual(code, 403)
        self.assertIn("无权操作", data.get("error", ""))

        # Bob's task must still be in PENDING status, unaffected
        bob_task = task_mgr.get_task(bob_task_id)
        self.assertEqual(bob_task.status.value, "PENDING")

    def test_alice_cannot_decide_bob_approval(self):
        apr_mgr = ApprovalManager.get_instance()
        ok, res = apr_mgr.check_and_request_approval(
            task_id="task_bob_1",
            category=ApprovalCategory.HIGH_RISK_COMMAND,
            description="Format partition",
            owner_id="10002",
            sync_wait=False,
        )
        bob_req_id = re.search(r"(apr_\w+)", res).group(1) if re.search(r"(apr_\w+)", res) else res.split(":")[-1].strip().rstrip(")")

        # Alice lists approvals -> Should NOT see Bob's approval request
        code, data = self._request("GET", "/api/friend/10001/agent_approvals", token="token_alice_secret_123456")
        self.assertEqual(code, 200)
        req_ids = [r["req_id"] for r in data.get("approvals", [])]
        self.assertNotIn(bob_req_id, req_ids)

        # Alice tries to approve Bob's dangerous action -> 403 Forbidden
        code, data = self._request(
            "POST",
            "/api/friend/10001/agent_approval_decision",
            token="token_alice_secret_123456",
            body={"decision": "approve", "req_id": bob_req_id},
        )
        self.assertEqual(code, 403)
        self.assertTrue("无权处理" in data.get("error", "") or "Unauthorized" in data.get("error", ""))

    def test_admin_friend_token_remains_owner_scoped(self):
        task_mgr = TaskManager.get_instance()
        t1 = task_mgr.create_task(title="Admin Test 1", description="desc", owner_id="10001")
        t2 = task_mgr.create_task(title="Admin Test 2", description="desc", owner_id="10002")

        # The friend API does not promote a friend token into a global administrator context.
        code, data = self._request("GET", "/api/friend/99999/agent_tasks", token="token_admin_secret_999999")
        self.assertEqual(code, 200)
        tids = [t["task_id"] for t in data.get("tasks", [])]
        self.assertNotIn(t1, tids)
        self.assertNotIn(t2, tids)


if __name__ == "__main__":
    unittest.main()
