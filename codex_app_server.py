"""
Codex App Server Backend Client (JSON-RPC 2.0 over stdio).

Provides native agent control using OpenAI Codex CLI's official App Server protocol:
- Subprocess management: `codex app-server --listen stdio://`
- Standard JSON-RPC 2.0 bidirectional protocol handshake (`initialize`, `initialized`)
- Thread and Turn lifecycle management (`thread/start`, `thread/resume`, `turn/start`)
- Dynamic runtime intervention (`turn/steer`) for appending instructions during execution
- Fast cancellation (`turn/interrupt`) for immediate task abort
- Streaming event telemetry (tool execution, file modification patches, command output deltas)
- Unified task status events: STARTED, PROGRESS, APPROVAL_REQUIRED, TOOL_RUNNING, COMPLETED, FAILED, CANCELLED
- Native integration with ApprovalManager for interactive command & file execution gating
- Token and quota tracking across turns and task sessions
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("codex_app_server")


class UnifiedTaskEvent(str, Enum):
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    TOOL_RUNNING = "TOOL_RUNNING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


APPROVAL_METHODS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")


def _paths_in_unified_diff(diff: str) -> list[str]:
    """File paths named by '+++ b/<path>' headers of a unified diff (deleted files use the '--- a/' side)."""
    paths = []
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            target = line[4:].strip()
            if target == "/dev/null":
                continue
            target = target[2:] if target[:2] in ("a/", "b/") else target
            if target not in paths:
                paths.append(target)
    return paths


@dataclass
class CodexSessionInfo:
    thread_id: str
    task_id: str
    owner_id: str
    cwd: str
    model: str
    created_at: float = field(default_factory=time.time)
    last_turn_id: str = ""
    total_tokens_used: int = 0
    modified_files: list[str] = field(default_factory=list)
    latest_diff: str = ""  # aggregated unified diff of the current turn (turn/diff/updated)


class CodexAppServerClient:
    """Production client for OpenAI Codex CLI official app-server mode."""

    def __init__(
        self,
        custom_codex_path: str | None = None,
        approval_manager=None,
        default_model: str = "gpt-5",
    ):
        self.custom_codex_path = custom_codex_path
        self.approval_manager = approval_manager
        # How long a Codex approval request waits for the owner before it is declined; tests shorten it.
        self.approval_timeout = 120
        self.default_model = default_model

        self.process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._startup_lock = threading.Lock()
        self._handshake_complete = False
        self._next_request_id = 0
        self._pending_requests: dict[int | str, Future] = {}
        self._active_listeners: dict[str, list[Callable[[UnifiedTaskEvent, dict[str, Any]], None]]] = {}
        self._active_sessions: dict[str, CodexSessionInfo] = {}
        self._active_turns: dict[str, str] = {}  # task_id -> turn_id
        self._completed_turns: dict[str, tuple[UnifiedTaskEvent, dict[str, Any]]] = {}
        self._thread_usage_totals: dict[str, int] = {}
        self._turn_usage_baselines: dict[str, int] = {}
        self._reader_thread: threading.Thread | None = None
        self._running = False

    def _resolve_codex_executable(self) -> str:
        if self.custom_codex_path and os.path.isfile(self.custom_codex_path):
            return self.custom_codex_path
        try:
            from codex_settings import cli_path
            found = cli_path(self.custom_codex_path)
            if found:
                return found
        except Exception:
            pass
        installed = shutil.which("codex") or shutil.which("codex.cmd")
        if installed:
            return installed
        default_npm = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
        if default_npm.is_file():
            return str(default_npm)
        return "codex"

    def start(self, timeout_sec: float = 15.0):
        """Start the real process and handshake without locking out its reader.

        A separate startup lock serializes concurrent callers. Holding _lock
        while waiting for initialize would deadlock the JSON-RPC reader thread.
        """
        with self._startup_lock:
            with self._lock:
                if self.process and self.process.poll() is None and self._handshake_complete:
                    return
                if self.process and self.process.poll() is not None:
                    self.process = None
                if self.process is None:
                    codex_bin = self._resolve_codex_executable()
                    cmd = [codex_bin, "app-server", "--listen", "stdio://"]
                    logger.info("Starting Codex App Server from configured CLI")
                    try:
                        self.process = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except Exception as ex:
                        raise RuntimeError(f"Unable to start Codex App Server ({codex_bin}): {ex}") from ex
                    self._running = True
                    self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True,
                                                           name="CodexAppServerReader")
                    self._reader_thread.start()
                    # Prevent a blocked child process if stderr output accumulates.
                    threading.Thread(target=self._drain_stderr, daemon=True,
                                     name="CodexAppServerStderr").start()
            try:
                # _send_request waits while the reader acquires _lock. Never
                # hold that lock across this network/process round-trip.
                self._send_request("initialize", {
                    "clientInfo": {"name": "open_qq_dot", "title": "Open QQ Dot", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                }, timeout_sec=timeout_sec)
                self._send_notification("initialized", {})
                with self._lock:
                    self._handshake_complete = True
            except BaseException:
                self.close()
                raise

    def _drain_stderr(self):
        """Drain stderr without putting potentially sensitive process logs in chats."""
        stream = self.process.stderr if self.process else None
        try:
            while stream and stream.readline():
                pass
        except (OSError, ValueError):
            pass

    def list_models(self) -> dict[str, list[str]]:
        """Read the CLI's real catalogue without creating a thread or running a model turn."""
        self.start()
        models, cursor, seen = {}, None, set()
        for _ in range(10):
            page = self._send_request('model/list', {'limit': 100, 'cursor': cursor}, timeout_sec=15)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise RuntimeError('Codex 未返回有效模型目录')
            for item in page['data']:
                if not isinstance(item, dict) or item.get('hidden') is True:
                    continue
                key = item.get('model') or item.get('id')
                if isinstance(key, str) and key:
                    models[key] = [r['reasoningEffort'] for r in item.get('supportedReasoningEfforts', [])
                                   if isinstance(r, dict) and isinstance(r.get('reasoningEffort'), str)]
            cursor = page.get('nextCursor')
            if not cursor:
                if not models:
                    raise RuntimeError('当前 Codex 账号没有返回可选择的模型')
                return models
            if cursor in seen:
                raise RuntimeError('Codex 模型目录分页重复，未使用不完整列表')
            seen.add(cursor)
        raise RuntimeError('Codex 模型目录超过读取上限，未使用不完整列表')

    def _reader_loop(self):
        """Continuously read line-delimited JSON-RPC messages from process stdout."""
        stdout = self.process.stdout
        while self._running and stdout:
            line = stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.debug(f"Unparseable non-JSON stdout line from Codex: {line_str}")
                continue

            self._dispatch_message(msg)

        logger.info("Codex App Server reader loop finished.")
        with self._lock:
            self._running = False
            self._handshake_complete = False
            # Fail all pending futures
            for req_id, fut in list(self._pending_requests.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError("Codex app-server process terminated prematurely"))
            self._pending_requests.clear()

    def _dispatch_message(self, msg: dict[str, Any]):
        """Route message based on standard JSON-RPC 2.0 rules."""
        # Case 1: Response to a client request (contains id and result/error, no method)
        if "id" in msg and "method" not in msg:
            req_id = msg.get("id")
            with self._lock:
                fut = self._pending_requests.pop(req_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"Codex RPC error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))
            return

        # Case 2: Server-initiated Request to client (contains id AND method, e.g. approval request)
        if "id" in msg and "method" in msg:
            threading.Thread(
                target=self._handle_server_request,
                args=(msg,),
                daemon=True,
                name=f"CodexServerReq-{msg.get('id')}",
            ).start()
            return

        # Case 3: Server Notification to client (contains method, NO id)
        if "method" in msg and "id" not in msg:
            self._handle_server_notification(msg)
            return

    def _handle_server_request(self, msg: dict[str, Any]):
        """Handle server-side callbacks such as command/file change approval."""
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        logger.info(f"Received server-to-client request: method={method}, id={req_id}")

        # Command / file-change approvals answer with a decision: accept | acceptForSession | decline | cancel.
        # Other server requests (e.g. item/permissions/requestApproval, MCP elicitation) expect different
        # payloads, so they fall through to the unsupported-method error instead of a bogus decision.
        if method in APPROVAL_METHODS:
            decision = self._process_approval_request(method, params)
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"decision": decision},
            }
            self._write_raw(response)
            return

        # Default fallback: reject unknown requested capabilities
        logger.warning(f"Rejecting unhandled server request: {method}")
        response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method {method} not supported by client"},
        }
        self._write_raw(response)

    def _process_approval_request(self, method: str, params: dict[str, Any]) -> str:
        """Consult ApprovalManager for permission gating; returns 'accept' or 'decline'."""
        thread_id = params.get("threadId", "")
        turn_id = params.get("turnId", "")
        command = params.get("command") or params.get("cmd") or ""
        reason = params.get("reason", "Codex App Server requests permission for privileged execution")

        session = self._find_session_by_thread(thread_id)
        if not session or not session.owner_id or session.owner_id in ("unknown", "anonymous", ""):
            logger.warning(f"Declining approval request for unauthenticated session: thread_id={thread_id}")
            return "decline"
        owner_id = session.owner_id
        task_id = session.task_id or f"task_{thread_id[:8]}"

        self._emit_event(
            task_id,
            UnifiedTaskEvent.APPROVAL_REQUIRED,
            {
                "method": method,
                "command": command,
                "reason": reason,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "task_id": task_id,
                "owner_id": owner_id,
            },
        )

        if not self.approval_manager:
            try:
                from approval_manager import ApprovalManager
                self.approval_manager = ApprovalManager.get_instance()
            except Exception:
                pass

        if self.approval_manager:
            try:
                from approval_manager import ApprovalCategory
                cat = ApprovalCategory.HIGH_RISK_COMMAND if "command" in method.lower() else ApprovalCategory.SYSTEM_CONFIG
                approved, _ = self.approval_manager.check_and_request_approval(
                    task_id=task_id,
                    category=cat,
                    description=f"Codex App Server requesting execution: {command}",
                    details={"command": command, "reason": reason},
                    owner_id=owner_id,
                    sync_wait=True,
                    timeout_seconds=int(self.approval_timeout),
                )
                if approved:
                    return "accept"
            except Exception as ex:
                logger.error(f"Error checking approval in ApprovalManager: {ex}")

        # P0 Security Fix: Never default-accept simply because owner_id is admin/system or unknown
        return "decline"

    def _handle_server_notification(self, msg: dict[str, Any]):
        """Map streaming server notifications to UnifiedTaskEvents."""
        method = msg.get("method", "")
        params = msg.get("params") or {}
        thread_id = params.get("threadId", "")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id", "")

        session = self._find_session_by_thread(thread_id)
        task_id = session.task_id if session else ""

        # Official App Server token usage notifications carry an absolute
        # thread total. Account for the delta since this turn began, not for
        # the cumulative thread total across all previous turns.
        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage") or {}
            total = token_usage.get("total") or {}
            if total:
                try:
                    count = int(total.get("totalTokens") or total.get("total_tokens") or
                                (int(total.get("inputTokens", 0)) + int(total.get("outputTokens", 0))))
                    with self._lock:
                        self._thread_usage_totals[thread_id] = max(0, count)
                except (TypeError, ValueError):
                    pass
            return

        # 1. Turn Started
        if method == "turn/started":
            self._emit_event(
                task_id,
                UnifiedTaskEvent.STARTED,
                {"thread_id": thread_id, "turn_id": turn_id, "message": "Turn execution started"},
            )

        # 2. Tool / Item execution started
        elif method == "item/started":
            item = params.get("item") or {}
            itype = item.get("type", "unknown")
            self._emit_event(
                task_id,
                UnifiedTaskEvent.TOOL_RUNNING,
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item_id": item.get("id"),
                    "item_type": itype,
                    "item_data": item,
                },
            )

        # 3. Output stream delta (stdout / stderr)
        elif method in ("item/commandExecution/outputDelta", "item/agentMessage/delta"):
            delta = params.get("delta") or params.get("output", "")
            self._emit_event(
                task_id,
                UnifiedTaskEvent.PROGRESS,
                {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "delta": delta,
                    "message_delta": delta if method == "item/agentMessage/delta" else "",
                },
            )

        # 4. Aggregated unified diff of every file change in the turn
        elif method == "turn/diff/updated":
            diff = params.get("diff") or params.get("unifiedDiff") or ""
            if session:
                session.latest_diff = diff
                for f_path in _paths_in_unified_diff(diff):
                    if f_path not in session.modified_files:
                        session.modified_files.append(f_path)
            self._emit_event(
                task_id,
                UnifiedTaskEvent.PROGRESS,
                {"thread_id": thread_id, "turn_id": turn_id, "diff": diff},
            )

        # 5. Item completed (fileChange items carry changes[].path; older builds used a top-level path)
        elif method == "item/completed":
            item = params.get("item") or {}
            if session and item.get("type") == "fileChange":
                paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)]
                for f_path in paths + [item.get("path")]:
                    if f_path and f_path not in session.modified_files:
                        session.modified_files.append(f_path)

        # 6. Turn completed
        elif method == "turn/completed":
            turn_obj = params.get("turn") or {}
            t_status = turn_obj.get("status", "completed")
            usage = turn_obj.get("usage") or params.get("usage") or {}
            tokens = usage.get("total_tokens", 0)
            if not tokens:
                with self._lock:
                    total = self._thread_usage_totals.get(thread_id, 0)
                    baseline = self._turn_usage_baselines.get(thread_id, 0)
                tokens = max(0, total - baseline)
                if tokens:
                    usage = {"total_tokens": tokens, "source": "thread/tokenUsage/updated"}
            if session:
                session.total_tokens_used += tokens

            if t_status == "completed":
                event_type = UnifiedTaskEvent.COMPLETED
            elif t_status in ("interrupted", "cancelled"):
                event_type = UnifiedTaskEvent.CANCELLED
            else:
                event_type = UnifiedTaskEvent.FAILED

            payload = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": t_status,
                "error": turn_obj.get("error"),
                "usage": usage,
                "tokens_used": tokens,
                "modified_files": session.modified_files if session else [],
            }
            with self._lock:
                self._completed_turns[turn_id] = (event_type, payload)
            self._emit_event(
                task_id,
                event_type,
                payload,
            )

    def _write_raw(self, payload: dict[str, Any]):
        """Serialize and flush raw JSON to child stdin."""
        with self._lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError("Codex app-server process is not running")
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()

    def _send_request(self, method: str, params: dict[str, Any], timeout_sec: float = 30.0) -> Any:
        """Send JSON-RPC request and block until response received."""
        with self._lock:
            self._next_request_id += 1
            req_id = self._next_request_id
            fut = Future()
            self._pending_requests[req_id] = fut

        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        self._write_raw(msg)

        try:
            return fut.result(timeout=timeout_sec)
        except Exception:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            raise

    def _send_notification(self, method: str, params: dict[str, Any]):
        """Send one-way JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_raw(msg)

    def _find_session_by_thread(self, thread_id: str) -> CodexSessionInfo | None:
        with self._lock:
            return self._active_sessions.get(thread_id)

    def _emit_event(self, task_id: str, event: UnifiedTaskEvent, payload: dict[str, Any]):
        """Broadcast unified event to registered task callbacks."""
        listeners = []
        with self._lock:
            if task_id in self._active_listeners:
                listeners.extend(self._active_listeners[task_id])
            if "*" in self._active_listeners:
                listeners.extend(self._active_listeners["*"])

        for cb in listeners:
            try:
                cb(event, payload)
            except Exception as ex:
                logger.error(f"Error in event listener callback for task {task_id}: {ex}")

    # =========================================================================
    # PUBLIC API FOR AGENT CONTROL & TASK EXECUTION
    # =========================================================================

    def start_thread(
        self,
        cwd: Path,
        model: str | None = None,
        task_id: str = "",
        owner_id: str = "system",
        sandbox_policy: dict[str, Any] | None = None,
        approval_policy: str = "on-request",
        base_instructions: str = "",
    ) -> str:
        """Initialize or resume an isolated Codex thread session bound to task_id."""
        target_dir = Path(cwd).resolve()

        # P0 Security Fix: Never escalate to danger-full-access simply because sandbox_policy dict is present
        sandbox_mode = "workspace-write"
        if isinstance(sandbox_policy, dict):
            req_mode = str(sandbox_policy.get("mode") or "").strip().lower()
            if req_mode in ("danger-full-access", "full_computer_access", "full_disk"):
                sandbox_mode = "danger-full-access"
            elif req_mode in ("read-only", "readonly"):
                sandbox_mode = "read-only"
            elif req_mode in ("", "workspace-write"):
                sandbox_mode = "workspace-write"
            else:
                raise ValueError(f"Unknown Codex sandbox policy: {req_mode}")
        elif sandbox_policy is not None:
            raise TypeError("sandbox_policy must be a dict with mode")
        if sandbox_mode == "danger-full-access":
            # A caller-supplied JSON field is not an authorization grant.
            raise PermissionError("Codex unrestricted sandbox requires a separate trusted authorization path")
        if not str(owner_id or "").strip() or owner_id in ("unknown", "anonymous"):
            raise PermissionError("Codex thread requires a known task owner")
        if not task_id:
            raise ValueError("Codex thread must be bound to a task ID")

        self.start()
        target_dir.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = {
            "cwd": str(target_dir),
            "model": model or self.default_model,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox_mode,
        }
        if base_instructions:
            params["baseInstructions"] = base_instructions

        res = self._send_request("thread/start", params, timeout_sec=20.0)
        thread_id = res["thread"]["id"]

        session = CodexSessionInfo(
            thread_id=thread_id,
            task_id=task_id,
            owner_id=owner_id,
            cwd=str(target_dir),
            model=model or self.default_model,
        )
        with self._lock:
            self._active_sessions[thread_id] = session

        return thread_id

    def resume_thread(
        self,
        thread_id: str,
        cwd: Path,
        task_id: str = "",
        owner_id: str = "",
    ) -> str:
        """Resume a known thread without allowing ownership to change."""
        known = self._find_session_by_thread(thread_id)
        if not known or not owner_id or owner_id != known.owner_id or task_id != known.task_id:
            raise PermissionError("Cannot resume an unbound or foreign Codex thread")
        self.start()
        params = {
            "threadId": thread_id,
            "cwd": str(Path(cwd).resolve()),
            "excludeTurns": True,
        }
        res = self._send_request("thread/resume", params, timeout_sec=20.0)
        confirmed_id = res["thread"]["id"]
        if confirmed_id != thread_id:
            raise PermissionError('Resumed Codex thread differs from the authorized conversation; no turn submitted')

        with self._lock:
            if confirmed_id not in self._active_sessions:
                self._active_sessions[confirmed_id] = CodexSessionInfo(
                    thread_id=confirmed_id,
                    task_id=task_id,
                    owner_id=owner_id,
                    cwd=str(Path(cwd).resolve()),
                    model=self.default_model,
                )
        return confirmed_id

    def execute_turn(
        self,
        thread_id: str,
        prompt: str,
        task_id: str = "",
        owner_id: str = "",
        image_path: Path | None = None,
        on_event: Callable[[UnifiedTaskEvent, dict[str, Any]], None] | None = None,
        timeout_sec: float = 300.0,
    ) -> dict[str, Any]:
        """Execute a full turn, stream unified events, handle approvals, and return results."""
        session = self._find_session_by_thread(thread_id)
        if not session:
            raise PermissionError("Codex thread is not bound to an authenticated task")
        if task_id and task_id != session.task_id:
            raise PermissionError("Cannot transfer a Codex thread to another task")
        if not owner_id or owner_id != session.owner_id:
            raise PermissionError("Cannot transfer a Codex thread to another user")
        effective_task_id = session.task_id
        self.start()
        with self._lock:
            self._turn_usage_baselines[thread_id] = self._thread_usage_totals.get(thread_id, 0)

        output_chunks: list[str] = []
        def _capture(event: UnifiedTaskEvent, payload: dict[str, Any]):
            if event == UnifiedTaskEvent.PROGRESS and payload.get("message_delta"):
                output_chunks.append(str(payload["message_delta"]))
            if on_event:
                on_event(event, payload)

        with self._lock:
            self._active_listeners.setdefault(effective_task_id, []).append(_capture)

        inputs = [{"type": "text", "text": prompt}]
        if image_path and Path(image_path).is_file():
            inputs.append({"type": "localImage", "path": str(image_path)})

        turn_start_params = {
            "threadId": thread_id,
            "input": inputs,
        }
        turn_res = self._send_request("turn/start", turn_start_params, timeout_sec=30.0)
        turn_id = turn_res["turn"]["id"]

        if session:
            session.last_turn_id = turn_id
        if effective_task_id:
            with self._lock:
                self._active_turns[effective_task_id] = turn_id

        # Wait for turn completion event
        completion_fut = Future()

        def _completion_watcher(evt: UnifiedTaskEvent, payload: dict[str, Any]):
            if payload.get("turn_id") == turn_id and evt in (
                UnifiedTaskEvent.COMPLETED,
                UnifiedTaskEvent.CANCELLED,
                UnifiedTaskEvent.FAILED,
            ):
                if not completion_fut.done():
                    completion_fut.set_result((evt, payload))

        with self._lock:
            if turn_id in self._completed_turns:
                completion_fut.set_result(self._completed_turns.pop(turn_id))
            else:
                self._active_listeners.setdefault(effective_task_id, []).append(_completion_watcher)

        try:
            evt_type, payload = completion_fut.result(timeout=timeout_sec)
        except Exception as ex:
            # If timed out, interrupt the turn
            self.interrupt_turn(thread_id, turn_id)
            raise TimeoutError(f"Turn {turn_id} execution timed out after {timeout_sec}s: {ex}") from ex
        finally:
            with self._lock:
                if effective_task_id in self._active_turns:
                    self._active_turns.pop(effective_task_id, None)
                listeners = self._active_listeners.get(effective_task_id, [])
                for callback in (_capture, _completion_watcher):
                    if callback in listeners:
                        listeners.remove(callback)
                if not listeners:
                    self._active_listeners.pop(effective_task_id, None)
                self._completed_turns.pop(turn_id, None)
                self._turn_usage_baselines.pop(thread_id, None)

        return {
            "output": "".join(output_chunks),
            "event": evt_type.value,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": payload.get("status"),
            "tokens_used": payload.get("tokens_used", 0),
            "usage": payload.get("usage", {}),
            "modified_files": session.modified_files if session else [],
            "error": payload.get("error"),
        }

    def steer_turn(self, thread_id: str, turn_id: str, additional_instruction: str) -> bool:
        """Inject additional steering instructions into a running turn (turn/steer)."""
        logger.info(f"Steering turn {turn_id} in thread {thread_id}: {additional_instruction}")
        try:
            params = {
                "threadId": thread_id,
                "turnId": turn_id,
                "input": [{"type": "text", "text": additional_instruction}],
            }
            res = self._send_request("turn/steer", params, timeout_sec=10.0)
            return bool(res is not None)
        except Exception as ex:
            logger.error(f"Failed to steer turn {turn_id}: {ex}")
            return False

    def interrupt_turn(self, thread_id: str, turn_id: str) -> bool:
        """Interrupt and cancel a running turn immediately (turn/interrupt)."""
        logger.info(f"Interrupting turn {turn_id} in thread {thread_id}")
        try:
            params = {
                "threadId": thread_id,
                "turnId": turn_id,
            }
            res = self._send_request("turn/interrupt", params, timeout_sec=10.0)
            return bool(res is not None)
        except Exception as ex:
            logger.error(f"Failed to interrupt turn {turn_id}: {ex}")
            return False

    def steer_task(self, task_id: str, additional_instruction: str) -> bool:
        """High-level API: Steer a running task by task_id."""
        with self._lock:
            turn_id = self._active_turns.get(task_id)
            thread_id = None
            for s in self._active_sessions.values():
                if s.task_id == task_id:
                    thread_id = s.thread_id
                    break
        if thread_id and turn_id:
            return self.steer_turn(thread_id, turn_id, additional_instruction)
        return False

    def cancel_task(self, task_id: str) -> bool:
        """High-level API: Cancel a running task by task_id via turn/interrupt."""
        with self._lock:
            turn_id = self._active_turns.get(task_id)
            thread_id = None
            for s in self._active_sessions.values():
                if s.task_id == task_id:
                    thread_id = s.thread_id
                    break
        if thread_id and turn_id:
            return self.interrupt_turn(thread_id, turn_id)
        return False

    def close(self):
        """Terminate the server process and release resources cleanly."""
        with self._lock:
            self._running = False
            self._handshake_complete = False
            if self.process:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2.0)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                self.process = None


# Singleton instance
_CODEX_APP_SERVER_INSTANCE: CodexAppServerClient | None = None
_INSTANCE_LOCK = threading.Lock()


def get_codex_app_server_client(custom_path: str | None = None) -> CodexAppServerClient:
    global _CODEX_APP_SERVER_INSTANCE
    with _INSTANCE_LOCK:
        if _CODEX_APP_SERVER_INSTANCE is None:
            _CODEX_APP_SERVER_INSTANCE = CodexAppServerClient(custom_codex_path=custom_path)
        return _CODEX_APP_SERVER_INSTANCE


def set_codex_app_server_client(client: CodexAppServerClient | None):
    global _CODEX_APP_SERVER_INSTANCE
    with _INSTANCE_LOCK:
        _CODEX_APP_SERVER_INSTANCE = client
