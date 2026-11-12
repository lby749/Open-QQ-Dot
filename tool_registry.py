"""Unified Tool Registry, Untrusted Input Sanitizer & Safe Invocation Layer.

Provides unified tool registration, schema definition, permission checking,
timeout control, and defensive sanitization against prompt injection attacks
and escape sequences in third-party tool outputs.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from agent_errors import AgentError, AgentErrorType
from approval_manager import ApprovalCategory, ApprovalManager

logger = logging.getLogger(__name__)


class ToolPermissionLevel(str, Enum):
    SAFE_READ = "safe_read"          # Readonly without external side effects
    WORKSPACE_WRITE = "workspace_write" # Modifies local workspace files
    NETWORK_FETCH = "network_fetch"  # External HTTP requests
    SENSITIVE_EXEC = "sensitive_exec"# Shell/commands or system actions


def check_permission_allowed(
    permission: ToolPermissionLevel,
    computer_config: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Verify whether the requested tool permission is permitted under current settings."""
    config = computer_config or {}
    mode = config.get("mode", "workspace")
    is_read_only = (mode == "read_only")
    commands_enabled = config.get("enabled", True) is True and (config.get("commands") is True or config.get("commands_enabled") is True) and not is_read_only

    if permission == ToolPermissionLevel.SENSITIVE_EXEC and not commands_enabled:
        return False, "System configuration disabled command and Shell execution permissions (commands=False or read-only mode)"

    if permission == ToolPermissionLevel.WORKSPACE_WRITE and is_read_only:
        return False, "System configuration is read-only (mode=read_only); modifying or writing workspace files is prohibited"

    return True, ""


def validate_path_in_bounds(
    path: Path,
    workspace: Path,
    computer_config: dict[str, Any] | None = None,
) -> Path:
    """Normalize path, resolve symlinks, and ensure path does not escape authorized directory boundaries."""
    config = computer_config or {}
    resolved_path = path.resolve()
    mode = config.get("mode", "workspace")

    # Secrets stay out of reach even when full-disk access is granted. The host
    # itself needs these files; an agent/tool must never be able to read them.
    secret_names = {
        "house_web.local.json", "friends.json", "friend.json",
        "api-key.bin", "deepseek-key.bin", ".agent_mail_key_v1",
    }
    name = resolved_path.name.casefold()
    parts = {part.casefold() for part in resolved_path.parts}
    if (name in secret_names
            or (name.startswith("api-") and name.endswith("-key.bin"))
            or parts.intersection({".ssh", ".aws", ".gnupg"})
            or name in {".env", "credentials", "secrets"}):
        raise PermissionError("Refusing to expose host credentials or pairing secrets to an agent tool")

    # If full disk access is explicitly granted
    if mode in ("full_disk", "all", "unrestricted") or config.get("full_disk_access") is True or config.get("full_computer_access") is True:
        return resolved_path

    # Authorized root directories
    allowed_dirs = [workspace.resolve()]
    for custom_p in config.get("allowed_paths", []):
        try:
            allowed_dirs.append(Path(custom_p).resolve())
        except Exception:
            pass

    for allowed_root in allowed_dirs:
        try:
            resolved_path.relative_to(allowed_root)
            return resolved_path
        except ValueError:
            continue

    raise PermissionError(
        f"Path traversal access denied: target path [{resolved_path}] is not within allowed workspace or authorized directory"
    )


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    required_permission: ToolPermissionLevel
    timeout_sec: float = 30.0
    approval_category: ApprovalCategory | None = None
    handler: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "required_permission": self.required_permission.value,
            "timeout_sec": self.timeout_sec,
            "approval_category": self.approval_category.value if self.approval_category else None,
        }


@dataclass
class ToolExecutionResult:
    success: bool
    data: Any = None
    sanitized_text: str = ""
    error: str = ""
    duration_sec: float = 0.0
    thread_still_running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UntrustedInputSanitizer:
    """Defends against prompt injection and escape exploits in external tool responses."""

    # Patterns indicating malicious prompt injection or system instruction hijack
    _INJECTION_PATTERNS = [
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        re.compile(r"system\s*:\s*you\s+are\s+now", re.IGNORECASE),
        re.compile(r"<\s*script\s*>", re.IGNORECASE),
        re.compile(r"sudo\s+rm\s+-rf", re.IGNORECASE),
        re.compile(r"curl\s+.*\|\s*(ba)?sh", re.IGNORECASE),
        re.compile(r"powershell\s+-enc", re.IGNORECASE),
        re.compile(r"format\s+[c-z]:\s+/fs", re.IGNORECASE),
    ]

    @classmethod
    def sanitize(cls, raw_content: Any) -> str:
        """Sanitize raw third-party tool output into safe, neutralized text."""
        if raw_content is None:
            return ""

        text = json.dumps(raw_content, ensure_ascii=False) if isinstance(raw_content, (dict, list)) else str(raw_content)

        # 1. Strip raw ANSI terminal escape sequences
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        cleaned = ansi_escape.sub('', text)

        # 2. Defang detected dangerous shell injections
        for pattern in cls._INJECTION_PATTERNS:
            if pattern.search(cleaned):
                cleaned = pattern.sub(r'[WARNING: External prompt injection directive neutralized]', cleaned)

        # 3. Truncate extreme payloads to avoid context blowing
        if len(cleaned) > 20000:
            cleaned = cleaned[:20000] + "\n...[Data truncated due to length]"

        return cleaned


class ToolRegistry:
    """Unified tool registry with permission verification and sandbox execution."""

    _instance: ToolRegistry | None = None
    _singleton_lock = threading.Lock()

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._registry_lock = threading.RLock()
        self._register_default_tools()

    @classmethod
    def get_instance(cls) -> ToolRegistry:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register_tool(self, tool: ToolDefinition):
        with self._registry_lock:
            self._tools[tool.name] = tool
            logger.info(f"Registered tool: {tool.name} (permission: {tool.required_permission.value})")

    def get_tool(self, name: str) -> ToolDefinition | None:
        with self._registry_lock:
            return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        with self._registry_lock:
            return [t.to_dict() for t in self._tools.values()]

    def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        task_id: str = "interactive",
        workspace: Path | None = None,
        computer_config: dict[str, Any] | None = None,
        definition: ToolDefinition | None = None,
    ) -> ToolExecutionResult:
        """Execute a tool with permission gate, timeout control, and result sanitization."""
        tool = definition or self.get_tool(name)
        if not tool:
            return ToolExecutionResult(
                success=False,
                error=f"Registered tool not found: {name}",
            )

        start_time = time.time()
        from jsonschema import Draft202012Validator
        try:
            Draft202012Validator(tool.parameters).validate(arguments)
        except Exception as exc:
            return ToolExecutionResult(success=False, error=f'Invalid tool parameters: {exc}')
        # 1. Enforce actual permission gate
        allowed, perm_reason = check_permission_allowed(tool.required_permission, computer_config)
        if not allowed:
            return ToolExecutionResult(
                success=False,
                error=f"Permission denied: {perm_reason}",
                duration_sec=time.time() - start_time,
            )

        from task_runtime import current_execution
        exec_ctx = current_execution.get()
        effective_owner = "system:legacy"
        cancel_evt = threading.Event()
        if exec_ctx:
            effective_owner = exec_ctx.owner_id or effective_owner
            cancel_evt = exec_ctx.cancel_event
        elif computer_config and "_owner_id" in computer_config:
            effective_owner = str(computer_config["_owner_id"]).strip()

        # 2. Approval Check for Sensitive Actions (Decoupled from base permission, user-isolated)
        if tool.approval_category is not None:
            approval_mgr = ApprovalManager.get_instance()
            # Inside a task: wait briefly in line, then park the task (ApprovalPending propagates to
            # TaskManager, which resumes it once the owner decides) instead of holding a worker.
            approved, reason = approval_mgr.check_and_request_approval(
                task_id=task_id,
                category=tool.approval_category,
                description=f"Tool [{name}] requesting sensitive operation execution",
                details={"arguments": arguments},
                sync_wait=True,
                owner_id=effective_owner,
                should_stop=exec_ctx.check if exec_ctx else None,
                defer_after=15.0 if exec_ctx else None,
            )
            if not approved:
                return ToolExecutionResult(
                    success=False,
                    error=f"Operation not approved: {reason}",
                    duration_sec=time.time() - start_time,
                )

        # 3. Execution with Timeout and Context
        context = {
            "workspace": workspace or (exec_ctx.workspace() if exec_ctx else Path.cwd()),
            "task_id": task_id,
            "computer_config": computer_config or {},
            "owner_id": effective_owner,
            "cancel_event": cancel_evt,
            "execution": exec_ctx,
        }
        result_holder = {}
        error_holder = {}

        def _worker():
            try:
                res = tool.handler(arguments, context)
                result_holder["data"] = res
            except Exception as exc:
                error_holder["error"] = str(exc)

        from contextvars import copy_context
        th = threading.Thread(target=copy_context().run, args=(_worker,), daemon=True)
        th.start()
        th.join(timeout=tool.timeout_sec)

        duration = time.time() - start_time
        if th.is_alive():
            cancel_evt.set()
            # Precisely clean up child processes owned by this task without touching the host process!
            if exec_ctx and hasattr(exec_ctx, "manager") and hasattr(exec_ctx.manager, "_terminate_owned_processes"):
                try:
                    exec_ctx.manager._terminate_owned_processes(task_id)
                except Exception as ex:
                    logger.debug(f"Process cleanup notice: {ex}")
            return ToolExecutionResult(
                success=False,
                error=f"Tool [{name}] execution timed out ({tool.timeout_sec}s); interrupt signal sent and child processes cleaned up",
                duration_sec=duration,
                thread_still_running=True,
            )

        if cancel_evt.is_set():
            return ToolExecutionResult(
                success=False,
                error=f"Task was cancelled or interrupted by user; late artifact from tool [{name}] safely discarded",
                duration_sec=duration,
            )

        if "error" in error_holder:
            return ToolExecutionResult(
                success=False,
                error=error_holder["error"],
                duration_sec=duration,
            )

        raw_data = result_holder.get("data")
        sanitized = UntrustedInputSanitizer.sanitize(raw_data)

        return ToolExecutionResult(
            success=True,
            data=raw_data,
            sanitized_text=sanitized,
            duration_sec=duration,
        )

    def _register_default_tools(self):
        """Register built-in system, filesystem, shell, and network tools."""
        # 1. System state reader
        def _get_system_info(args, ctx):
            import os
            import platform
            return {
                "system": platform.system(),
                "release": platform.release(),
                "architecture": platform.machine(),
                "cpu_count": os.cpu_count(),
                "cwd": str(ctx.get("workspace")),
            }

        self.register_tool(ToolDefinition(
            name="system_info",
            description="Get basic system environment, CPU, and platform architecture information",
            parameters={"type": "object", "properties": {}},
            required_permission=ToolPermissionLevel.SAFE_READ,
            handler=_get_system_info,
        ))

        # 2. Safe file reader with path boundary checks
        def _read_file(args, ctx):
            file_path = Path(args["path"])
            ws = ctx.get("workspace") or Path.cwd()
            target = file_path if file_path.is_absolute() else (ws / file_path)
            full_path = validate_path_in_bounds(target, ws, ctx.get("computer_config"))
            if not full_path.exists():
                raise FileNotFoundError(f"File does not exist: {file_path}")
            return full_path.read_text(encoding="utf-8", errors="ignore")[:20000]

        self.register_tool(ToolDefinition(
            name="read_file",
            description="Safely read specified text file in workspace (max 20,000 chars)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative or absolute path"}},
                "required": ["path"]
            },
            required_permission=ToolPermissionLevel.SAFE_READ,
            handler=_read_file,
        ))

        # 3. Safe file writer with path boundary checks
        def _write_file(args, ctx):
            file_path = Path(args["path"])
            content = args.get("content", "")
            ws = ctx.get("workspace") or Path.cwd()
            target = file_path if file_path.is_absolute() else (ws / file_path)
            full_path = validate_path_in_bounds(target, ws, ctx.get("computer_config"))
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            return f"File written successfully: {file_path} ({len(content)} characters)"

        self.register_tool(ToolDefinition(
            name="write_file",
            description="Safely write or update text file content in workspace",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative or absolute path"},
                    "content": {"type": "string", "description": "File text content"},
                },
                "required": ["path", "content"]
            },
            required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
            handler=_write_file,
        ))

        # 4. Command execution tool guarded by SENSITIVE_EXEC and approval
        def _run_command(args, ctx):
            command = args["command"]
            import secret_guard
            if secret_guard.command_touches_secret(command):
                raise PermissionError('Command references host credentials or pairing secrets')
            ws = ctx.get("workspace") or Path.cwd()
            timeout = min(int(args.get("timeout", 30)), 300)
            from task_runtime import run_process
            res = run_process(
                command,
                cwd=str(ws),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
            }

        self.register_tool(ToolDefinition(
            name="run_command",
            description="Execute controlled shell command (protected by commands switch and high-risk approval gating)",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command-line instruction to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"]
            },
            required_permission=ToolPermissionLevel.SENSITIVE_EXEC,
            approval_category=ApprovalCategory.HIGH_RISK_COMMAND,
            timeout_sec=60.0,
            handler=_run_command,
        ))

        # 5. Webhook HTTP request
        def _http_webhook(args, ctx):
            import urllib.request
            url = args["url"]
            method = args.get("method", "GET").upper()
            data = args.get("body")
            headers = args.get("headers", {})

            req_data = json.dumps(data).encode("utf-8") if data else None
            req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="ignore")[:15000]

        self.register_tool(ToolDefinition(
            name="http_webhook",
            description="Send HTTP request to communicate with external API / Webhook",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target URL of the request"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                    "body": {"type": "object", "description": "Request body JSON"},
                },
                "required": ["url"]
            },
            required_permission=ToolPermissionLevel.NETWORK_FETCH,
            timeout_sec=20.0,
            approval_category=ApprovalCategory.EXTERNAL_PUBLISH,
            handler=_http_webhook,
        ))

        # 6. Multi-tenant Isolated Browser Navigation & Extraction
        def _browser_navigate(args, ctx):
            from browser_automation import get_browser_session, BrowserInterventionRequired
            owner_id = str(ctx.get("owner_id") or "").strip()
            if not owner_id:
                raise PermissionError("Browser operation missing authenticated user identity")
            ws = ctx.get("workspace") or Path.cwd()
            session = get_browser_session(owner_id, base_dir=ws / "data" / "browser_sessions", use_headless_chromium=True)
            try:
                return session.navigate(args["url"], timeout_sec=float(args.get("timeout", 15.0)))
            except BrowserInterventionRequired as exc:
                session.request_human_takeover(str(exc), task_id=str(ctx.get("task_id") or ""))
                raise

        self.register_tool(ToolDefinition(
            name="browser_navigate",
            description="Open webpage, extract content, and detect CAPTCHA barriers in user-isolated browser session",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target webpage URL to visit"},
                    "timeout": {"type": "number", "description": "Timeout in seconds (default 15)"},
                },
                "required": ["url"],
            },
            required_permission=ToolPermissionLevel.NETWORK_FETCH,
            timeout_sec=25.0,
            handler=_browser_navigate,
        ))

        # 7. Browser DOM Interaction & Screenshot
        def _browser_interact(args, ctx):
            from browser_automation import get_browser_session
            owner_id = str(ctx.get("owner_id") or "").strip()
            if not owner_id:
                raise PermissionError("Browser operation missing authenticated user identity")
            ws = ctx.get("workspace") or Path.cwd()
            session = get_browser_session(owner_id, base_dir=ws / "data" / "browser_sessions", use_headless_chromium=True)
            action = str(args.get("action") or "read").lower()
            if action == "click":
                return session.click(args.get("selector", ""))
            if action == "input":
                return session.input_text(args.get("selector", ""), args.get("text", ""))
            if action == "scroll":
                return session.scroll(args.get("direction", "down"), int(args.get("pixels", 600)))
            if action == "screenshot":
                out_file = ws / (args.get("output_path") or f"snapshot_{int(time.time())}.png")
                full_out = validate_path_in_bounds(out_file, ws, ctx.get("computer_config"))
                saved = session.take_screenshot(full_out)
                return {"status": "screenshot_saved", "path": str(saved)}
            return session.read_content(max_chars=int(args.get("max_chars", 4000)), selector=args.get("selector"))

        self.register_tool(ToolDefinition(
            name="browser_interact",
            description="Execute text read, click, input, scroll, or screenshot in isolated browser session",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "click", "input", "scroll", "screenshot"]},
                    "selector": {"type": "string", "description": "Target element selector or text"},
                    "text": {"type": "string", "description": "Input text content"},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "pixels": {"type": "integer"},
                    "output_path": {"type": "string"},
                },
                "required": ["action"],
            },
            required_permission=ToolPermissionLevel.NETWORK_FETCH,
            timeout_sec=20.0,
            handler=_browser_interact,
        ))

        # 8. End-to-End Video Production Pipeline
        def _video_produce(args, ctx):
            from video_ecommerce_pipeline import VideoAutomationPipeline
            ws = ctx.get("workspace") or Path.cwd()
            pipe = VideoAutomationPipeline(workspace_dir=ws / "video_render")
            return pipe.run_full_pipeline(
                topic=args["topic"],
                target_duration_sec=int(args.get("duration_sec", 8)),
                output_filename=args.get("output_filename", "output_video.mp4"),
            )

        self.register_tool(ToolDefinition(
            name="video_produce",
            description="Execute automated video workflow: storyboard generation, voiceover/SRT synthesis, FFmpeg rendering and QA",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Video topic or title description"},
                    "duration_sec": {"type": "integer", "description": "Target video duration in seconds"},
                    "output_filename": {"type": "string", "description": "Output MP4 filename"},
                },
                "required": ["topic"],
            },
            required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
            timeout_sec=45.0,
            handler=_video_produce,
        ))

        # 9. Read-Only E-Commerce Query & CS Draft Service (Strictly SAFE_READ, zero side effects)
        def _ecommerce_query(args, ctx):
            from video_ecommerce_pipeline import EcommerceAutomationPipeline
            owner_id = str(ctx.get("owner_id") or "default")
            ws = ctx.get("workspace") or Path.cwd()
            pipe = EcommerceAutomationPipeline(db_path=ws / "data" / "ecommerce_state.sqlite3")
            op = str(args.get("operation") or "search").lower()
            if op == "search":
                return pipe.search_and_compare_products(
                    query=args.get("query", ""),
                    platforms=args.get("platforms"),
                    owner_id=owner_id,
                )
            if op == "track_order":
                return pipe.track_order_status(order_id=args.get("order_id", ""), owner_id=owner_id)
            if op == "cs_draft":
                order_info = pipe.track_order_status(order_id=args.get("order_id", ""), owner_id=owner_id)
                draft = pipe.generate_customer_service_draft(args.get("customer_query", ""), order_info)
                return {"order_info": order_info, "cs_draft": draft}
            raise ValueError(f"Read-only tool ecommerce_query does not support write operation '{op}'; for dispatch use approval-gated ecommerce_dispatch tool")

        self.register_tool(ToolDefinition(
            name="ecommerce_query",
            description="Read-only e-commerce query: multi-platform product search, order tracking, and customer service draft (side-effect free)",
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["search", "track_order", "cs_draft"]},
                    "query": {"type": "string"},
                    "order_id": {"type": "string"},
                    "customer_query": {"type": "string"},
                },
                "required": ["operation"],
            },
            required_permission=ToolPermissionLevel.SAFE_READ,
            timeout_sec=20.0,
            handler=_ecommerce_query,
        ))

        # 9b. Gated E-Commerce Order Dispatch (SENSITIVE_EXEC + Approval Gate)
        def _ecommerce_dispatch(args, ctx):
            from video_ecommerce_pipeline import EcommerceAutomationPipeline
            owner_id = str(ctx.get("owner_id") or "default")
            ws = ctx.get("workspace") or Path.cwd()
            pipe = EcommerceAutomationPipeline(db_path=ws / "data" / "ecommerce_state.sqlite3")
            ok, msg, data = pipe.execute_order_dispatch(
                order_id=args["order_id"],
                carrier=args.get("carrier", "FedEx Express"),
                owner_id=owner_id,
                idempotency_key=args.get("idempotency_key", ""),
                task_id=str(ctx.get("task_id") or ""),
            )
            return {"success": ok, "message": msg, "data": data}

        self.register_tool(ToolDefinition(
            name="ecommerce_dispatch",
            description="Execute e-commerce order dispatch protected by idempotency key and human approval gating (write operation with side-effects)",
            parameters={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "carrier": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["order_id", "idempotency_key"],
            },
            required_permission=ToolPermissionLevel.SENSITIVE_EXEC,
            approval_category=ApprovalCategory.EXTERNAL_PUBLISH,
            timeout_sec=25.0,
            handler=_ecommerce_dispatch,
        ))

        # 10. High-Risk Gated E-Commerce Payment
        def _ecommerce_payment(args, ctx):
            from video_ecommerce_pipeline import EcommerceAutomationPipeline
            owner_id = str(ctx.get("owner_id") or "default")
            ws = ctx.get("workspace") or Path.cwd()
            pipe = EcommerceAutomationPipeline(db_path=ws / "data" / "ecommerce_state.sqlite3")
            ok, msg, receipt = pipe.execute_payment(
                order_id=args["order_id"],
                amount=float(args["amount"]),
                owner_id=owner_id,
                idempotency_key=args.get("idempotency_key", ""),
                task_id=str(ctx.get("task_id") or ""),
            )
            return {"success": ok, "message": msg, "receipt": receipt}

        self.register_tool(ToolDefinition(
            name="ecommerce_payment",
            description="Execute e-commerce payment deduction protected by idempotency key and human approval gating",
            parameters={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "amount": {"type": "number"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["order_id", "amount", "idempotency_key"],
            },
            required_permission=ToolPermissionLevel.SENSITIVE_EXEC,
            approval_category=ApprovalCategory.HIGH_RISK_COMMAND,
            timeout_sec=30.0,
            handler=_ecommerce_payment,
        ))

        # 11. Academic Research Literature Digest Pipeline
        def _academic_research_digest(args, ctx):
            from local_retrieval_engine import get_local_retrieval_engine, AcademicResearchPipeline
            ws = ctx.get("workspace") or Path.cwd()
            owner_id = str(ctx.get("owner_id") or "system")
            engine = get_local_retrieval_engine(ws / "data" / "retrieval_engine.sqlite3")
            pipe = AcademicResearchPipeline(engine)
            return pipe.generate_research_daily_digest(
                topic=args["topic"],
                owner_id=owner_id,
                user_research_focus=args.get("user_research_focus", ""),
            )

        self.register_tool(ToolDefinition(
            name="academic_research_digest",
            description="Retrieve arXiv papers and generate structured research digest with innovation candidates and verifiable hypotheses",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Research topic or query keyword"},
                    "user_research_focus": {"type": "string", "description": "User specific research focus"},
                },
                "required": ["topic"],
            },
            required_permission=ToolPermissionLevel.NETWORK_FETCH,
            timeout_sec=25.0,
            handler=_academic_research_digest,
        ))
