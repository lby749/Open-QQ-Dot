"""
Comprehensive End-to-End Test Suite for Codex App Server Native Integration & 7-Step Real World Workflow:
1. Task Creation & Agent Dispatching: Task creation with Codex App Server backend
2. Real-time Event Streaming: Telemetry mapped to unified status events (STARTED, PROGRESS, TOOL_RUNNING)
3. Dynamic In-Flight Steering: Appending instructions while running via turn/steer
4. Tool Approval Gating: Interception and approval/rejection via ApprovalManager
5. Interruption and Session Continuity: Cancelling via turn/interrupt and resuming via thread/resume
6. Completion, Artifact Diffs & Quad-Partite Memory: Facts, user info, speculations, hypotheses
7. Multi-User Tenant Isolation & Shared Subscription Compliance: Admin risk alert & friend first-use terms archive
"""

import json
import queue
import time
import pytest
from pathlib import Path

from codex_app_server import CodexAppServerClient, UnifiedTaskEvent, CodexSessionInfo, set_codex_app_server_client
from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus, create_trusted_identity
from unittest.mock import patch
import house_web
from task_manager import TaskManager, TaskStatus
from model_dispatcher import ModelDispatcher
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource


class MockProcess:
    """Thread-safe Mock process simulating official Codex App Server stdio JSON-RPC 2.0 communication."""

    def __init__(self):
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self._msg_queue = queue.Queue()
        self._is_alive = True

    def poll(self):
        return None if self._is_alive else 0

    def write(self, s: str):
        data = json.loads(s.strip())
        method = data.get("method")
        req_id = data.get("id")

        if method == "initialize":
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "capabilities": {"experimentalApi": True},
                    "serverInfo": {"name": "codex-app-server", "version": "0.162.1"},
                },
            })
        elif method == "thread/start":
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "thread": {"id": "th_test_abc123", "cwd": data.get("params", {}).get("cwd")},
                },
            })
        elif method == "thread/resume":
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "thread": {"id": data.get("params", {}).get("threadId", "th_test_abc123")},
                },
            })
        elif method == "turn/start":
            turn_id = "turn_xyz987"
            # Send turn/start result first
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "turn": {"id": turn_id, "status": "running"},
                },
            })
            # Simulate streaming server notifications
            self._queue_response({
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"threadId": "th_test_abc123", "turnId": turn_id},
            })
            self._queue_response({
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "threadId": "th_test_abc123",
                    "turnId": turn_id,
                    "item": {"id": "item_1", "type": "commandExecution", "command": "pytest tests/"},
                },
            })
            self._queue_response({
                "jsonrpc": "2.0",
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "th_test_abc123", "turnId": turn_id, "delta": "Running tests...\n"},
            })
            self._queue_response({
                "jsonrpc": "2.0",
                # Documented app-server event: aggregated unified diff of the turn's file changes.
                "method": "turn/diff/updated",
                "params": {"threadId": "th_test_abc123", "turnId": turn_id,
                           "diff": "--- a/src/feature.py\n+++ b/src/feature.py\n@@ -0,0 +1 @@\n+def feature(): pass\n"},
            })
            self._queue_response({
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "th_test_abc123",
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "completed", "usage": {"total_tokens": 1250}},
                },
            })
        elif method == "turn/steer":
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "steered"},
            })
        elif method == "turn/interrupt":
            self._queue_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"status": "interrupted"},
            })

    def flush(self):
        pass

    def readline(self):
        try:
            line = self._msg_queue.get(timeout=0.2)
            return line
        except queue.Empty:
            if not self._is_alive:
                return ""
            return "\n"

    def _queue_response(self, obj: dict):
        self._msg_queue.put(json.dumps(obj, ensure_ascii=False) + "\n")

    def terminate(self):
        self._is_alive = False

    def kill(self):
        self._is_alive = False


def test_e2e_codex_app_server_full_7_step_workflow(tmp_path: Path):
    """Execute all 7 steps of the real-world Codex App Server user journey."""
    db_file = tmp_path / "test_e2e.db"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # STEP 1: Task Creation & Codex App Server Client Initialization
    # -------------------------------------------------------------------------
    approval_mgr = ApprovalManager.get_instance(db_path=tmp_path / "approvals.sqlite3")
    client = CodexAppServerClient(approval_manager=approval_mgr)
    client.approval_timeout = 3  # nobody answers in this test; keep the decline wait short
    set_codex_app_server_client(client)

    mock_proc = MockProcess()
    client.process = mock_proc
    client._running = True
    import threading
    client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
    client._reader_thread.start()

    init_res = client._send_request("initialize", {"clientInfo": {"name": "test"}}, timeout_sec=5.0)
    assert init_res["serverInfo"]["name"] == "codex-app-server"
    client._send_notification("initialized", {})

    thread_id = client.start_thread(
        cwd=workspace_dir,
        model="gpt-5",
        task_id="task_remote_001",
        owner_id="admin_user",
    )
    assert thread_id == "th_test_abc123"

    # -------------------------------------------------------------------------
    # STEP 2: Real-time Event Streaming & Unified Status Mapping
    # -------------------------------------------------------------------------
    captured_events: list[tuple[UnifiedTaskEvent, dict]] = []

    def on_task_event(evt: UnifiedTaskEvent, payload: dict):
        captured_events.append((evt, payload))

    turn_outcome = client.execute_turn(
        thread_id=thread_id,
        prompt="请实现加法功能并编写单测",
        task_id="task_remote_001",
        owner_id="admin_user",
        on_event=on_task_event,
        timeout_sec=5.0,
    )
    assert turn_outcome["event"] == UnifiedTaskEvent.COMPLETED.value
    assert turn_outcome["tokens_used"] == 1250
    assert "src/feature.py" in turn_outcome["modified_files"]

    event_types = [e[0] for e in captured_events]
    assert UnifiedTaskEvent.STARTED in event_types
    assert UnifiedTaskEvent.TOOL_RUNNING in event_types
    assert UnifiedTaskEvent.PROGRESS in event_types
    assert UnifiedTaskEvent.COMPLETED in event_types

    # -------------------------------------------------------------------------
    # STEP 3: Dynamic In-Flight Steering (turn/steer)
    # -------------------------------------------------------------------------
    steer_ok = client.steer_turn(
        thread_id=thread_id,
        turn_id="turn_xyz987",
        additional_instruction="请同时支持浮点数加法",
    )
    assert steer_ok is True

    task_mgr = TaskManager(db_path=db_file, workspace=workspace_dir)
    created_task_id = task_mgr.create_task(
        title="浮点数计算支持",
        description="支持浮点计算",
        category="code",
        owner_id="admin_user",
    )
    client._active_turns[created_task_id] = "turn_xyz987"
    client._active_sessions[thread_id].task_id = created_task_id

    mgr_steer_ok = task_mgr.steer_task(created_task_id, "test", owner_id=task_mgr.get_task(created_task_id).owner_id)
    assert mgr_steer_ok is True

    # -------------------------------------------------------------------------
    # STEP 4: Tool Approval Gating via ApprovalManager
    # -------------------------------------------------------------------------
    decision = client._process_approval_request(
        method="item/commandExecution/requestApproval",
        params={
            "threadId": thread_id,
            "turnId": "turn_xyz987",
            "command": "pip install numpy",
            "reason": "需要科学计算库",
        },
    )
    assert decision in ("accept", "decline")  # documented decisions: accept | acceptForSession | decline | cancel

    # -------------------------------------------------------------------------
    # STEP 5: Fast Cancellation & Session Resume
    # -------------------------------------------------------------------------
    interrupt_ok = client.interrupt_turn(thread_id, "turn_xyz987")
    assert interrupt_ok is True

    # A foreign or mismatched resume is refused (anti-hijack); the bound owner/task may resume.
    bound = client._active_sessions[thread_id]
    try:
        client.resume_thread(thread_id, cwd=workspace_dir, task_id="task_remote_001")
        raise AssertionError("resume without the bound owner must be refused")
    except PermissionError:
        pass
    resumed_thread_id = client.resume_thread(thread_id, cwd=workspace_dir, task_id=bound.task_id, owner_id=bound.owner_id)
    assert resumed_thread_id == thread_id

    # -------------------------------------------------------------------------
    # STEP 6: Completion, Diffs & Quad-Partite Memory Archiving
    # -------------------------------------------------------------------------
    assert "src/feature.py" in client._active_sessions[thread_id].modified_files

    mem_store = AgentMemoryStore(db_path=tmp_path / "memory.db")
    mem_store.store_memory(
        scope=MemoryScope.PROJECT_MEMORY,
        owner_id="admin_user",
        key="project_tech_stack",
        content="项目采用 Python 3.13 与 pytest 构建，核心模块已添加 feature.py",
        source=MemorySource.TOOL_VERIFIED,
        metadata={"category": "fact"},
    )
    mem_store.store_memory(
        scope=MemoryScope.USER_PREFERENCE,
        owner_id="admin_user",
        key="user_model_pref",
        content="用户习惯通过 QQ 远程发起编程任务，偏好使用 gpt-5 模型",
        source=MemorySource.USER_EXPLICIT,
        metadata={"category": "user_info"},
    )
    mem_store.store_memory(
        scope=MemoryScope.TOOL_EXPERIENCE,
        owner_id="admin_user",
        key="spec_matrix_calc",
        content="后续可能需要增加矩阵乘法与科学计算拓展支持",
        source=MemorySource.MODEL_INFERRED,
        confidence=0.75,
        metadata={"category": "speculation"},
    )
    mem_store.store_memory(
        scope=MemoryScope.TASK_EXECUTION,
        owner_id="admin_user",
        key="hypo_vector_throughput",
        content="如果引入向量化计算，浮点运算吞吐量可提升 3 倍以上",
        source=MemorySource.MODEL_INFERRED,
        confidence=0.85,
        metadata={"category": "hypothesis"},
    )

    memories = mem_store.list_memories(owner_id="admin_user")
    cats = set()
    for m in memories:
        raw_meta = m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", {})
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        if "category" in meta:
            cats.add(meta["category"])
    assert "fact" in cats
    assert "user_info" in cats
    assert "speculation" in cats
    assert "hypothesis" in cats

    # -------------------------------------------------------------------------
    # STEP 7: Multi-User Isolation & Shared Subscription Compliance Alert
    # -------------------------------------------------------------------------
    needs_appr, msg, req_id = approval_mgr.check_admin_multitenant_subscription_risk(
        admin_id="admin_user",
        new_user_id="friend_bob",
        provider="codex",
    )
    assert needs_appr is True
    assert "Multi-User Shared Subscription Risk Notice" in msg
    assert req_id != ""

    # Decisions need a server-issued identity; the web token lookup is the only stubbed boundary.
    def trusted(user_id, is_admin=False):
        with patch.object(house_web, "friend_for_token", return_value=user_id),                 patch.object(house_web, "is_admin_user", return_value=is_admin):
            return create_trusted_identity(user_id, is_admin=is_admin, session_token=f"token-{user_id}")

    approval_mgr.decide_request(req_id, approved=True, decision_reason="Admin authorizes friend for test",
                                approver_id=trusted("admin_user", is_admin=True))
    approval_mgr.record_terms_acknowledgement(user_id="admin_user", role="admin", provider="codex")

    needs_appr2, _, _ = approval_mgr.check_admin_multitenant_subscription_risk(
        admin_id="admin_user",
        new_user_id="friend_bob",
        provider="codex",
    )
    assert needs_appr2 is False

    f_needs, f_msg, f_req = approval_mgr.check_friend_first_use_subscription_terms(
        friend_id="friend_bob",
        provider="codex",
    )
    assert f_needs is True
    assert "Personal Shared Subscription Acknowledgement" in f_msg

    approval_mgr.decide_request(f_req, approved=True, decision_reason="Friend acknowledges sharing",
                                approver_id=trusted("friend_bob"))
    approval_mgr.record_terms_acknowledgement(user_id="friend_bob", role="friend", provider="codex")

    f_needs2, _, _ = approval_mgr.check_friend_first_use_subscription_terms(
        friend_id="friend_bob",
        provider="codex",
    )
    assert f_needs2 is False

    client.close()
