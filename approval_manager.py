"""Unified Event & Security Approval Center (ApprovalManager).

Protects sensitive and destructive actions across QQ, Web, and desktop:
- File deletion
- System configuration modification
- Package/software installation
- High-risk command execution
- External messages / email delivery
- Public publishing
- Paid / premium API invocation
- Multi-user shared personal subscription risk acknowledgement

P0-2 Security Invariants:
1. External approval entrypoints NEVER default to 'system' identity; omitting approver_id raises PermissionError.
2. Plain strings like 'system', 'admin', or 'system:admin' cannot escalate privileges for another user's request.
3. Internal system operations (such as timeout auto-expiration) must pass the unforgeable _INTERNAL_SYSTEM_TOKEN.
4. Full state machine (PENDING -> APPROVED / REJECTED / EXPIRED / CANCELLED, plus reopen) with persistent SQLite audit logs.
"""
from __future__ import annotations
from contextlib import contextmanager

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _InternalSystemTokenType:
    """Unforgeable module-private sentinel token for internal system transitions."""
    __slots__ = ()


_INTERNAL_SYSTEM_TOKEN = _InternalSystemTokenType()
_PROCESS_HMAC_KEY = os.urandom(32)


@dataclass(frozen=True)
class TrustedIdentityContext:
    """Server-verified identity context with cryptographic HMAC binding that cannot be forged by agents/clients."""
    user_id: str
    is_admin: bool = False
    issued_at: float = 0.0
    signature: str = ""

    def verify(self) -> bool:
        if not self.user_id or not self.signature:
            return False
        payload = f"{self.user_id}:{1 if self.is_admin else 0}:{self.issued_at:.6f}".encode("utf-8")
        expected = hmac.new(_PROCESS_HMAC_KEY, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)


def create_trusted_identity(
    user_id: str, is_admin: bool = False, *, session_token: str | None = None,
) -> TrustedIdentityContext:
    """Issue a request identity ONLY after validating an actual Web bearer token.

    A caller-chosen user_id (even with is_admin=True) is NEVER sufficient.
    The token remains server-side; do not pass it to untrusted Agent tools.
    """
    import house_web
    clean_uid = str(user_id or "").strip()
    authenticated_uid = house_web.friend_for_token(str(session_token or ""))
    if not authenticated_uid or authenticated_uid != clean_uid:
        raise PermissionError("Refusing to issue trusted identity: Missing valid session token matching user")
    authorized_admin = bool(house_web.is_admin_user(authenticated_uid))
    if is_admin and not authorized_admin:
        raise PermissionError("Current logged-in user does not have administrator privileges")
    now_ts = time.time()
    admin_bit = bool(is_admin and authorized_admin)
    payload = f"{clean_uid}:{1 if admin_bit else 0}:{now_ts:.6f}".encode("utf-8")
    sig = hmac.new(_PROCESS_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    return TrustedIdentityContext(user_id=clean_uid, is_admin=admin_bit, issued_at=now_ts, signature=sig)


_CHANNEL_ISSUERS: set[str] = set()
_CHANNEL_LOCK = threading.Lock()


class IdentityChannel:
    """Capability to mint identities for senders a transport has already authenticated.

    The QQ bridge receives messages over the loopback-validated QQ link socket, so the sender's QQ number
    is authenticated by the QQ login itself. ``open_identity_channel`` hands the bridge one capability per
    channel name per process; agent code never runs inside the bridge process (agents are subprocesses),
    so it cannot obtain one. Channel identities are never admin: cross-owner decisions still need a
    signed web identity."""
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def identity(self, user_id: str) -> "TrustedIdentityContext":
        clean_uid = str(user_id or "").strip()
        if not clean_uid:
            raise PermissionError("Channel identity requires the authenticated sender id")
        now_ts = time.time()
        payload = f"{clean_uid}:0:{now_ts:.6f}".encode("utf-8")
        sig = hmac.new(_PROCESS_HMAC_KEY, payload, hashlib.sha256).hexdigest()
        return TrustedIdentityContext(user_id=clean_uid, is_admin=False, issued_at=now_ts, signature=sig)


def open_identity_channel(name: str) -> IdentityChannel:
    """Issue the identity capability for an authenticated transport, once per name per process."""
    with _CHANNEL_LOCK:
        if name in _CHANNEL_ISSUERS:
            raise PermissionError(f"Identity channel '{name}' was already opened in this process")
        _CHANNEL_ISSUERS.add(name)
    return IdentityChannel(name)


class ApprovalPending(Exception):
    """Raised inside a task when an approval is still undecided after the short in-line wait.

    TaskManager parks the task in WAITING_APPROVAL (freeing the worker) and resumes it when the
    request is decided; the re-run consumes the approved request instead of asking again."""

    def __init__(self, req_id: str, description: str = ""):
        super().__init__(f"Waiting for approval {req_id}: {description}")
        self.req_id = req_id
        self.description = description


def action_fingerprint(category: "ApprovalCategory", description: str, details: dict[str, Any] | None) -> str:
    """Stable identity of one concrete action, so a re-run after approval matches the approved request."""
    raw = json.dumps([category.value, description, details or {}], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class ApprovalCategory(str, Enum):
    FILE_DELETE = "file_delete"
    SYSTEM_CONFIG = "system_config"
    PACKAGE_INSTALL = "package_install"
    HIGH_RISK_COMMAND = "high_risk_command"
    SEND_MESSAGE = "send_message"
    EXTERNAL_PUBLISH = "external_publish"
    PAID_API = "paid_api"
    SHARED_SUBSCRIPTION_RISK = "shared_subscription_risk"


class ApprovalPolicy(str, Enum):
    AUTO_ALLOW = "auto_allow"
    ASK_ALWAYS = "ask_always"
    TASK_ALLOW = "task_allow"
    SCOPE_ALLOW = "scope_allow"
    ALWAYS_DENY = "always_deny"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass
class ApprovalRequest:
    req_id: str
    task_id: str
    category: ApprovalCategory
    description: str
    details: dict[str, Any] = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_reason: str = ""
    allowed_scope: str = "once"  # once, task, always
    owner_id: str = "default"
    timeout_seconds: int = 300
    created_at: str = ""
    decided_at: str = ""
    decided_by: str = ""

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['category'] = self.category.value if isinstance(self.category, ApprovalCategory) else self.category
        d['status'] = self.status.value if isinstance(self.status, ApprovalStatus) else self.status
        return d


class ApprovalManager:
    """Manages risk approval requests, security policies, audit logs, and execution gating."""

    _instance: ApprovalManager | None = None
    _singleton_lock = threading.Lock()

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            data_dir = __import__("house_settings").data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = data_dir / "approvals.sqlite3"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._policies: dict[ApprovalCategory, ApprovalPolicy] = {
            ApprovalCategory.FILE_DELETE: ApprovalPolicy.ASK_ALWAYS,
            ApprovalCategory.SYSTEM_CONFIG: ApprovalPolicy.ASK_ALWAYS,
            ApprovalCategory.PACKAGE_INSTALL: ApprovalPolicy.ASK_ALWAYS,
            ApprovalCategory.HIGH_RISK_COMMAND: ApprovalPolicy.ASK_ALWAYS,
            ApprovalCategory.SEND_MESSAGE: ApprovalPolicy.TASK_ALLOW,
            ApprovalCategory.EXTERNAL_PUBLISH: ApprovalPolicy.ASK_ALWAYS,
            ApprovalCategory.PAID_API: ApprovalPolicy.TASK_ALLOW,
            ApprovalCategory.SHARED_SUBSCRIPTION_RISK: ApprovalPolicy.ASK_ALWAYS,
        }

        self._task_grants: set[tuple[str, str, str]] = set()
        self._always_grants: dict[str, set[str]] = {}
        self._trusted_admins: set[str] = set()
        self._events: dict[str, threading.Event] = {}
        self._db_lock = threading.RLock()
        self._init_db()

    @classmethod
    def get_instance(cls, db_path: Path | None = None) -> ApprovalManager:
        if cls._instance is None or (db_path is not None and cls._instance.db_path != Path(db_path)):
            with cls._singleton_lock:
                if cls._instance is None or (db_path is not None and cls._instance.db_path != Path(db_path)):
                    cls._instance = cls(db_path)
        return cls._instance

    def register_trusted_admin(self, admin_id: str, *, session_token: str | None = None) -> TrustedIdentityContext:
        """Authenticate the configured administrator; never allow registration by a plain ID."""
        identity = create_trusted_identity(admin_id, is_admin=True, session_token=session_token)
        # A trusted context does not grant arbitrary future plaintext strings administrator rights.
        return identity

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._db_lock, self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    req_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    decision_reason TEXT NOT NULL DEFAULT '',
                    allowed_scope TEXT NOT NULL DEFAULT 'once',
                    owner_id TEXT NOT NULL DEFAULT '',
                    timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    created_at TEXT NOT NULL,
                    decided_at TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_req_status ON approval_requests(status);
                CREATE INDEX IF NOT EXISTS idx_req_task ON approval_requests(task_id);
                """
            )
            for col_sql in (
                "ALTER TABLE approval_requests ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE approval_requests ADD COLUMN decided_by TEXT NOT NULL DEFAULT ''",
                # Matches a re-run's action to its approved request; consumed_at makes "once" single-use.
                "ALTER TABLE approval_requests ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE approval_requests ADD COLUMN consumed_at TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

            conn.execute("UPDATE approval_requests SET owner_id='system:legacy' WHERE owner_id IS NULL OR trim(owner_id)=''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_req_owner ON approval_requests(owner_id)")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_grants (
                    grant_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL,
                    allowed_scope TEXT NOT NULL,
                    granted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_grant_owner ON approval_grants(owner_id);

                CREATE TABLE IF NOT EXISTS approval_audit_logs (
                    audit_id TEXT PRIMARY KEY,
                    req_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_req ON approval_audit_logs(req_id);

                CREATE TABLE IF NOT EXISTS subscription_terms_acknowledgements (
                    ack_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL,
                    terms_version TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_terms_user ON subscription_terms_acknowledgements(user_id);
                """
            )
            cur = conn.execute("SELECT owner_id, task_id, category, allowed_scope FROM approval_grants")
            for r in cur.fetchall():
                o_id, t_id, cat, scope = r['owner_id'], r['task_id'], r['category'], r['allowed_scope']
                if scope == 'always':
                    if o_id not in self._always_grants:
                        self._always_grants[o_id] = set()
                    self._always_grants[o_id].add(cat)
                elif scope == 'task':
                    self._task_grants.add((o_id, t_id, cat))
            conn.commit()

    def _record_audit(
        self,
        conn: sqlite3.Connection,
        req_id: str,
        task_id: str,
        actor_id: str,
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
    ):
        aid = f"aud_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO approval_audit_logs (
                audit_id, req_id, task_id, actor_id, action, from_status, to_status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, req_id, task_id, actor_id, action, from_status, to_status, reason, datetime.now().isoformat()),
        )

    def get_audit_logs(self, req_id: str) -> list[dict[str, Any]]:
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM approval_audit_logs WHERE req_id = ? ORDER BY created_at ASC",
                (req_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _resolve_and_authorize_approver(
        self,
        conn: sqlite3.Connection,
        req_id: str,
        task_id: str,
        req_owner: str,
        category: str,
        approver_id: str | TrustedIdentityContext | None,
        _internal_token: object | None,
        action_label: str,
    ) -> str:
        """Strictly resolve and verify approver identity.

        P0-2 Core Rules:
        1. Internal system calls MUST present _internal_token is _INTERNAL_SYSTEM_TOKEN.
        2. External calls MUST NOT omit approver_id (omitting raises PermissionError; never defaults to 'system').
        3. Plain string 'system', 'admin', or 'system:admin' NEVER grants cross-tenant override over another user's request!
        4. Only the request owner (actor == req_owner) or a cryptographically/server-verified admin can approve/reject.
        """
        if _internal_token is _INTERNAL_SYSTEM_TOKEN:
            if action_label != "CANCEL":
                # A timeout/cancellation marker is NOT permission to approve an
                # external high-risk operation. No silent internal approvals.
                raise PermissionError("Internal state control token cannot be used to approve or reject user requests")
            return "internal:system_kernel"

        is_verified_admin = False
        actor_id = ""

        if isinstance(approver_id, TrustedIdentityContext):
            if not approver_id.verify() or time.time() - approver_id.issued_at > 3600 or approver_id.issued_at > time.time() + 60:
                self._record_audit(conn, req_id, task_id, "invalid_signature", f"{action_label}_DENIED", "PENDING", "PENDING", "Forged or invalid TrustedIdentityContext signature")
                conn.commit()
                raise PermissionError("Approval authentication failed: TrustedIdentityContext signature check failed")
            actor_id = approver_id.user_id
            is_verified_admin = approver_id.is_admin
        elif isinstance(approver_id, str) and approver_id.strip():
            # Text identifiers are labels, NOT proof of an authenticated request.
            # Never accept them even if they match request.owner_id.
            self._record_audit(conn, req_id, task_id, approver_id.strip(), f"{action_label}_DENIED", "PENDING", "PENDING", "Rejecting bare string identity; valid authenticated session context required")
            raise PermissionError("Approval requires TrustedIdentityContext bound to authenticated session; bare string identities not accepted")

        if not actor_id:
            self._record_audit(conn, req_id, task_id, "<omitted>", f"{action_label}_DENIED", "PENDING", "PENDING", "Missing approver identity (approver_id is empty)")
            conn.commit()
            raise PermissionError(
                f"Approval permission denied: Action [{action_label}] requires trusted approver identity (approver_id cannot be empty or default to system)"
            )

        # Check server-side verified admin registry / environment admin list (never trust bare 'system' or 'admin' string!)
        # A plaintext identity (including a configured admin QQ number) is not an
        # authenticated admin credential. Cross-owner approval requires signed Web identity.
        # TrustedIdentityContext can only be minted after validating a bearer token.

        # P0-2 Invariant: Plain strings 'system', 'admin', 'system:admin' cannot approve another user's request
        # unless that string is literally the req_owner or registered as a verified admin!
        if actor_id != req_owner and not is_verified_admin:
            self._record_audit(
                conn, req_id, task_id, actor_id, f"{action_label}_DENIED",
                "PENDING", "PENDING", f"Cross-user unauthorized access intercepted: Request belongs to [{req_owner}], caller [{actor_id}] unauthorized"
            )
            conn.commit()
            raise PermissionError(
                f"Unauthorized approval intercepted: Approval request [{req_id}] belongs to [{req_owner}], caller [{actor_id}] is unauthorized and cannot escalate privileges with fake system/admin strings"
            )

        return actor_id

    def set_category_policy(self, category: ApprovalCategory, policy: ApprovalPolicy):
        """Configure user preference for approval category."""
        with self._db_lock:
            self._policies[category] = policy

    def check_and_request_approval(
        self,
        task_id: str,
        category: ApprovalCategory,
        description: str,
        details: dict[str, Any] | None = None,
        timeout_seconds: int = 120,
        sync_wait: bool = True,
        owner_id: str = "default",
        *,
        should_stop: Any = None,
        defer_after: float | None = None,
        poll_interval: float = 0.5,
    ) -> tuple[bool, str]:
        """Check policy and request user approval if required.

        The decision may come from any process (QQ bridge, web panel, another worker), so waiting polls
        the shared database; the in-process event only shortens the wait. ``should_stop`` (e.g. a task's
        Execution.check) is called while waiting and may raise to abandon the wait (the request is then
        cancelled). With ``defer_after``, an undecided request raises ApprovalPending after that many
        seconds instead of blocking; a later identical call consumes the approved request."""
        policy = self._policies.get(category, ApprovalPolicy.ASK_ALWAYS)

        if policy == ApprovalPolicy.ALWAYS_DENY:
            return False, f"Operation globally denied by security policy ({category.value})"

        if policy == ApprovalPolicy.AUTO_ALLOW:
            return True, "Security policy allows automatic execution"

        eff_owner = str(owner_id or "default").strip() or "default"
        grant = self._find_grant(eff_owner, task_id, category.value)
        if grant == "always":
            return True, f"User [{eff_owner}] holds global permanent authorization for this operation"
        if grant == "task":
            return True, f"User [{eff_owner}] task [{task_id}] holds pre-authorization for this category"

        fingerprint = action_fingerprint(category, description, details)
        reused = self._consume_or_find(eff_owner, task_id, category.value, fingerprint)
        if reused and reused[1] == ApprovalStatus.APPROVED.value:
            return True, reused[2] or "User approved"
        if reused:
            req_id = reused[0]
        else:
            req_id = f"apr_{uuid.uuid4().hex[:8]}"
            req = ApprovalRequest(
                req_id=req_id,
                task_id=task_id,
                category=category,
                description=description,
                details=details or {},
                status=ApprovalStatus.PENDING,
                owner_id=eff_owner,
                timeout_seconds=timeout_seconds,
            )
            with self._db_lock, self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO approval_requests (
                        req_id, task_id, category, description, details, status,
                        allowed_scope, owner_id, timeout_seconds, created_at, fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        req.req_id, req.task_id, req.category.value, req.description,
                        json.dumps(req.details, ensure_ascii=False), req.status.value,
                        req.allowed_scope, req.owner_id, req.timeout_seconds, req.created_at, fingerprint
                    ),
                )
                self._record_audit(conn, req_id, task_id, eff_owner, "SUBMITTED", "", ApprovalStatus.PENDING.value, description)
                conn.commit()
            self._notify_request(req)

        if not sync_wait:
            return False, f"Pending user approval (Request ID: {req_id})"

        evt = threading.Event()
        with self._db_lock:
            self._events[req_id] = evt
        started = time.monotonic()
        try:
            while True:
                status = self._status_of(req_id)
                if status != ApprovalStatus.PENDING.value:
                    break
                waited = time.monotonic() - started
                if waited >= timeout_seconds:
                    self.expire_request(req_id, reason="Approval timed out with no response; expired and rejected by default", _internal_token=_INTERNAL_SYSTEM_TOKEN)
                    return False, "Approval timed out, automatically rejected"
                if defer_after is not None and waited >= defer_after:
                    raise ApprovalPending(req_id, description)
                if should_stop is not None:
                    try:
                        should_stop()
                    except BaseException:
                        self.cancel_request(req_id, reason="Requesting task stopped while waiting for approval",
                                            _internal_token=_INTERNAL_SYSTEM_TOKEN)
                        raise
                evt.wait(timeout=min(poll_interval, max(0.01, timeout_seconds - waited)))
                evt.clear()
        finally:
            with self._db_lock:
                self._events.pop(req_id, None)

        updated = self.get_request(req_id)
        if updated and updated.status == ApprovalStatus.APPROVED:
            self._mark_consumed(req_id)
            return True, updated.decision_reason or "User approved"
        return False, updated.decision_reason if updated else "User rejected operation"

    def _status_of(self, req_id: str) -> str:
        with self._db_lock, self._get_connection() as conn:
            row = conn.execute("SELECT status FROM approval_requests WHERE req_id = ?", (req_id,)).fetchone()
        return row["status"] if row else ApprovalStatus.CANCELLED.value

    def _find_grant(self, owner_id: str, task_id: str, category: str) -> str | None:
        """Standing grants from the shared database (a grant decided in another process counts here too)."""
        with self._db_lock:
            if owner_id in self._always_grants and category in self._always_grants[owner_id]:
                return "always"
            if (owner_id, task_id, category) in self._task_grants:
                return "task"
        with self._db_lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT allowed_scope FROM approval_grants WHERE owner_id = ? AND category = ? "
                "AND (allowed_scope = 'always' OR (allowed_scope = 'task' AND task_id = ?)) "
                "ORDER BY allowed_scope = 'always' DESC LIMIT 1",
                (owner_id, category, task_id),
            ).fetchone()
        return row["allowed_scope"] if row else None

    def _consume_or_find(self, owner_id: str, task_id: str, category: str, fingerprint: str):
        """An approved-but-unused or still-pending request for this exact action, as (req_id, status, reason).

        An approved one is consumed atomically, so a "once" approval authorises exactly one execution."""
        with self._db_lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT req_id, status, decision_reason FROM approval_requests WHERE owner_id = ? AND task_id = ? "
                "AND category = ? AND fingerprint = ? AND consumed_at = '' AND status IN ('APPROVED', 'PENDING') "
                "ORDER BY status = 'APPROVED' DESC, created_at DESC LIMIT 1",
                (owner_id, task_id, category, fingerprint),
            ).fetchone()
            if not row:
                return None
            if row["status"] == ApprovalStatus.APPROVED.value:
                cur = conn.execute("UPDATE approval_requests SET consumed_at = ? WHERE req_id = ? AND consumed_at = ''",
                                   (datetime.now().isoformat(), row["req_id"]))
                if cur.rowcount != 1:
                    return None
                self._record_audit(conn, row["req_id"], task_id, owner_id, "CONSUMED", "APPROVED", "APPROVED",
                                   "Approved action executed")
            return row["req_id"], row["status"], row["decision_reason"]

    def _mark_consumed(self, req_id: str):
        with self._db_lock, self._get_connection() as conn:
            conn.execute("UPDATE approval_requests SET consumed_at = ? WHERE req_id = ? AND consumed_at = ''",
                         (datetime.now().isoformat(), req_id))

    def _notify_request(self, req: ApprovalRequest):
        """Tell the owner a decision is needed and exactly how to answer (QQ or the panel)."""
        if req.owner_id.startswith("system") or req.owner_id in ("default", ""):
            return
        try:
            from notification_manager import NotificationManager, NotificationPriority
            from task_runtime import current_execution
            execution = current_execution.get()
            task = execution.manager.get_task(req.task_id) if execution and execution.task_id == req.task_id else None
            channel = 'local' if task and task.checkpoint.get('notify_qq') is False else 'all'
            from ui_locale import tr
            NotificationManager.get_instance().publish(
                title=f"需要操作确认（{req.req_id}）",
                content=(f"{tr(req.description)}\n"
                         f"回复「批准 {req.req_id}」允许操作，或「拒绝 {req.req_id}」拒绝操作。"
                         f"有效期为 {req.timeout_seconds // 60 or 1} 分钟。"),
                priority=NotificationPriority.HIGH, category="approval", recipient_id=req.owner_id,
                channel=channel,
                metadata={"dedup_key": f"{req.req_id}:requested", "req_id": req.req_id, "task_id": req.task_id},
            )
        except Exception as exc:
            logger.warning(f"Approval notification for {req.req_id} not published: {exc}")

    def _after_decision(self, req_id: str):
        """Wake in-process waiters and resume a task that was parked on this request (any process)."""
        evt = self._events.get(req_id)
        if evt:
            evt.set()
        try:
            req = self.get_request(req_id)
            if req and req.task_id:
                from task_manager import TaskManager
                TaskManager.get_instance().on_approval_decided(req.task_id, req_id,
                                                                req.status == ApprovalStatus.APPROVED)
        except Exception as exc:
            logger.debug(f"Task resume after approval {req_id} skipped: {exc}")

    def approve_request(
        self,
        req_id: str,
        allowed_scope: str = "once",
        reason: str = "",
        approver_id: str | TrustedIdentityContext | None = None,
        *,
        _internal_token: object | None = None,
    ) -> bool:
        """User confirms approval with strict non-default tenant ownership check."""
        now = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT task_id, category, status, owner_id FROM approval_requests WHERE req_id = ?",
                (req_id,),
            )
            row = cur.fetchone()
            if not row or row['status'] != ApprovalStatus.PENDING.value:
                return False

            task_id, cat, req_owner = row['task_id'], row['category'], row['owner_id']
            actor_id = self._resolve_and_authorize_approver(
                conn=conn,
                req_id=req_id,
                task_id=task_id,
                req_owner=req_owner,
                category=cat,
                approver_id=approver_id,
                _internal_token=_internal_token,
                action_label="APPROVE",
            )

            conn.execute(
                """
                UPDATE approval_requests SET
                    status = ?, allowed_scope = ?, decision_reason = ?, decided_at = ?, decided_by = ?
                WHERE req_id = ? AND status = ?
                """,
                (ApprovalStatus.APPROVED.value, allowed_scope, reason or "Authorized", now, actor_id, req_id, ApprovalStatus.PENDING.value),
            )
            self._record_audit(conn, req_id, task_id, actor_id, "APPROVED", ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value, reason or "Authorized")

            if allowed_scope in ("task", "always"):
                gid = f"gnt_{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO approval_grants (grant_id, owner_id, task_id, category, allowed_scope, granted_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (gid, req_owner, task_id if allowed_scope == 'task' else '', cat, allowed_scope, now),
                )
                if allowed_scope == "always":
                    if req_owner not in self._always_grants:
                        self._always_grants[req_owner] = set()
                    self._always_grants[req_owner].add(cat)
                elif allowed_scope == "task":
                    self._task_grants.add((req_owner, task_id, cat))

            conn.commit()

        self._after_decision(req_id)
        return True

    def reject_request(
        self,
        req_id: str,
        reason: str = "User rejected",
        approver_id: str | TrustedIdentityContext | None = None,
        *,
        _internal_token: object | None = None,
    ) -> bool:
        """User rejects approval with strict ownership verification."""
        now = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT task_id, category, status, owner_id FROM approval_requests WHERE req_id = ?", (req_id,))
            row = cur.fetchone()
            if not row or row['status'] != ApprovalStatus.PENDING.value:
                return False

            task_id, cat, req_owner = row['task_id'], row['category'], row['owner_id']
            actor_id = self._resolve_and_authorize_approver(
                conn=conn,
                req_id=req_id,
                task_id=task_id,
                req_owner=req_owner,
                category=cat,
                approver_id=approver_id,
                _internal_token=_internal_token,
                action_label="REJECT",
            )

            conn.execute(
                "UPDATE approval_requests SET status = ?, decision_reason = ?, decided_at = ?, decided_by = ? WHERE req_id = ? AND status = ?",
                (ApprovalStatus.REJECTED.value, reason, now, actor_id, req_id, ApprovalStatus.PENDING.value),
            )
            self._record_audit(conn, req_id, task_id, actor_id, "REJECTED", ApprovalStatus.PENDING.value, ApprovalStatus.REJECTED.value, reason)
            conn.commit()

        self._after_decision(req_id)
        return True

    def expire_request(
        self,
        req_id: str,
        reason: str = "Approval timed out, automatically expired",
        *,
        _internal_token: object | None = None,
    ) -> bool:
        """Internal timeout transition PENDING -> EXPIRED (requires _INTERNAL_SYSTEM_TOKEN)."""
        if _internal_token is not _INTERNAL_SYSTEM_TOKEN:
            raise PermissionError("expire_request is reserved for internal timeout mechanism and requires valid _INTERNAL_SYSTEM_TOKEN")
        now = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT task_id, status FROM approval_requests WHERE req_id = ?", (req_id,))
            row = cur.fetchone()
            if not row or row['status'] != ApprovalStatus.PENDING.value:
                return False
            conn.execute(
                "UPDATE approval_requests SET status = ?, decision_reason = ?, decided_at = ?, decided_by = ? WHERE req_id = ? AND status = ?",
                (ApprovalStatus.EXPIRED.value, reason, now, "internal:timeout", req_id, ApprovalStatus.PENDING.value),
            )
            self._record_audit(conn, req_id, row['task_id'], "internal:timeout", "EXPIRED", ApprovalStatus.PENDING.value, ApprovalStatus.EXPIRED.value, reason)
            conn.commit()
        self._after_decision(req_id)
        return True

    def cancel_request(
        self,
        req_id: str,
        approver_id: str | TrustedIdentityContext | None = None,
        reason: str = "Task or user cancelled approval request",
        *,
        _internal_token: object | None = None,
    ) -> bool:
        """Transition PENDING -> CANCELLED with ownership verification."""
        now = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT task_id, category, status, owner_id FROM approval_requests WHERE req_id = ?", (req_id,))
            row = cur.fetchone()
            if not row or row['status'] != ApprovalStatus.PENDING.value:
                return False
            actor_id = self._resolve_and_authorize_approver(
                conn, req_id, row['task_id'], row['owner_id'], row['category'], approver_id, _internal_token, "CANCEL"
            )
            conn.execute(
                "UPDATE approval_requests SET status = ?, decision_reason = ?, decided_at = ?, decided_by = ? WHERE req_id = ? AND status = ?",
                (ApprovalStatus.CANCELLED.value, reason, now, actor_id, req_id, ApprovalStatus.PENDING.value),
            )
            self._record_audit(conn, req_id, row['task_id'], actor_id, "CANCELLED", ApprovalStatus.PENDING.value, ApprovalStatus.CANCELLED.value, reason)
            conn.commit()
        self._after_decision(req_id)
        return True

    def reopen_request(
        self,
        req_id: str,
        approver_id: str | TrustedIdentityContext | None = None,
        reason: str = "User requested to reopen approval",
    ) -> bool:
        """Transition REJECTED/EXPIRED/CANCELLED -> PENDING for re-evaluation by the owner."""
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT task_id, category, status, owner_id FROM approval_requests WHERE req_id = ?", (req_id,))
            row = cur.fetchone()
            if not row or row['status'] not in (ApprovalStatus.REJECTED.value, ApprovalStatus.EXPIRED.value, ApprovalStatus.CANCELLED.value):
                return False
            old_status = row['status']
            actor_id = self._resolve_and_authorize_approver(
                conn, req_id, row['task_id'], row['owner_id'], row['category'], approver_id, None, "REOPEN"
            )
            conn.execute(
                "UPDATE approval_requests SET status = ?, decision_reason = ?, decided_at = '', decided_by = '' WHERE req_id = ?",
                (ApprovalStatus.PENDING.value, reason, req_id),
            )
            self._record_audit(conn, req_id, row['task_id'], actor_id, "REOPENED", old_status, ApprovalStatus.PENDING.value, reason)
            conn.commit()
        return True

    def get_request(self, req_id: str) -> ApprovalRequest | None:
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM approval_requests WHERE req_id = ?", (req_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_request(row)

    def list_pending_requests(self, owner_id: str | None = None) -> list[ApprovalRequest]:
        with self._db_lock, self._get_connection() as conn:
            if owner_id is not None:
                cur = conn.execute(
                    "SELECT * FROM approval_requests WHERE status = ? AND owner_id = ? ORDER BY created_at ASC",
                    (ApprovalStatus.PENDING.value, owner_id),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM approval_requests WHERE status = ? ORDER BY created_at ASC",
                    (ApprovalStatus.PENDING.value,),
                )
            return [self._row_to_request(r) for r in cur.fetchall()]

    def _row_to_request(self, row: sqlite3.Row) -> ApprovalRequest:
        keys = row.keys()
        return ApprovalRequest(
            req_id=row['req_id'],
            task_id=row['task_id'],
            category=ApprovalCategory(row['category']),
            description=row['description'],
            details=json.loads(row['details']),
            status=ApprovalStatus(row['status']),
            decision_reason=row['decision_reason'],
            allowed_scope=row['allowed_scope'],
            owner_id=row['owner_id'] if 'owner_id' in keys else '',
            timeout_seconds=row['timeout_seconds'],
            created_at=row['created_at'],
            decided_at=row['decided_at'],
            decided_by=row['decided_by'] if 'decided_by' in keys else '',
        )

    def revoke_grant(
        self,
        owner_id: str,
        category: ApprovalCategory | str | None = None,
        task_id: str | None = None,
    ) -> int:
        """Revoke user approval grants immediately."""
        eff_owner = str(owner_id).strip()
        cat_val = category.value if isinstance(category, ApprovalCategory) else (str(category) if category else None)
        with self._db_lock, self._get_connection() as conn:
            conditions = ["owner_id = ?"]
            params: list[Any] = [eff_owner]
            if cat_val is not None:
                conditions.append("category = ?")
                params.append(cat_val)
            if task_id is not None:
                conditions.append("task_id = ?")
                params.append(task_id)

            cur = conn.execute(f"DELETE FROM approval_grants WHERE {' AND '.join(conditions)}", params)
            deleted = cur.rowcount
            conn.commit()

            if eff_owner in self._always_grants:
                if cat_val is not None:
                    self._always_grants[eff_owner].discard(cat_val)
                else:
                    self._always_grants.pop(eff_owner, None)

            to_remove = set()
            for (o, t, c) in self._task_grants:
                if o == eff_owner:
                    if (cat_val is None or c == cat_val) and (task_id is None or t == task_id):
                        to_remove.add((o, t, c))
            self._task_grants.difference_update(to_remove)

            logger.info(f"Revoked {deleted} approval grants for user [{eff_owner}]")
            return deleted

    def submit_request(
        self,
        task_id: str,
        category: ApprovalCategory,
        description: str,
        details: dict[str, Any] | None = None,
        allowed_scope: str = "once",
        owner_id: str = "default",
        timeout_seconds: int = 300,
    ) -> str:
        """Create and persist an approval request, returning req_id."""
        req_id = f"appr_{uuid.uuid4().hex[:12]}"
        eff_owner = str(owner_id or "default").strip() or "default"
        req = ApprovalRequest(
            req_id=req_id,
            task_id=task_id,
            category=category,
            description=description,
            allowed_scope=allowed_scope,
            details=details or {},
            status=ApprovalStatus.PENDING,
            owner_id=eff_owner,
            timeout_seconds=timeout_seconds,
        )
        with self._db_lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO approval_requests (
                    req_id, task_id, category, description, details, status,
                    allowed_scope, owner_id, timeout_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.req_id, req.task_id, req.category.value, req.description,
                    json.dumps(req.details, ensure_ascii=False), req.status.value,
                    req.allowed_scope, req.owner_id, req.timeout_seconds, req.created_at
                ),
            )
            self._record_audit(conn, req_id, task_id, eff_owner, "SUBMITTED", "", ApprovalStatus.PENDING.value, description)
            conn.commit()
        return req_id

    def decide_request(
        self,
        req_id: str,
        approved: bool,
        decision_reason: str = "",
        allowed_scope: str = "once",
        approver_id: str | TrustedIdentityContext | None = None,
        *,
        _internal_token: object | None = None,
    ) -> bool:
        """Convenience method to approve or reject a request (requires explicit approver_id)."""
        if approved:
            return self.approve_request(
                req_id, allowed_scope=allowed_scope, reason=decision_reason, approver_id=approver_id, _internal_token=_internal_token
            )
        else:
            return self.reject_request(
                req_id, reason=decision_reason, approver_id=approver_id, _internal_token=_internal_token
            )

    # =========================================================================
    # MULTI-USER SHARED SUBSCRIPTION TERMS & COMPLIANCE GUARD
    # =========================================================================
    def check_admin_multitenant_subscription_risk(
        self,
        admin_id: str,
        new_user_id: str,
        provider: str = "codex",
    ) -> tuple[bool, str, str]:
        """Verify that admin has explicitly confirmed multi-user personal subscription sharing risk."""
        eff_admin = str(admin_id or "admin").strip()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM subscription_terms_acknowledgements WHERE user_id = ? AND role = 'admin' AND provider = ?",
                (eff_admin, provider),
            )
            row = cur.fetchone()
            if row:
                return False, "Admin has already confirmed personal subscription multi-user sharing terms and risks", ""

        desc = (
            f"[Multi-User Shared Subscription Risk Notice] System detected adding user [{new_user_id}] to shared subscription ({provider}). "
            "Per provider terms of service, sharing personal accounts across multiple users may carry rate limits or suspension risks. "
            "Please acknowledge and confirm operational risk."
        )
        req_id = self.submit_request(
            task_id=f"sub_risk_{int(time.time()*1000)%100000}",
            category=ApprovalCategory.SHARED_SUBSCRIPTION_RISK,
            description=desc,
            details={"admin_id": eff_admin, "new_user_id": new_user_id, "provider": provider},
            allowed_scope="always",
            owner_id=eff_admin,
            timeout_seconds=600,
        )
        return True, desc, req_id

    def check_friend_first_use_subscription_terms(
        self,
        friend_id: str,
        provider: str = "codex",
    ) -> tuple[bool, str, str]:
        """Verify that friend has explicitly acknowledged the shared personal subscription terms on first use."""
        eff_friend = str(friend_id or "").strip()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM subscription_terms_acknowledgements WHERE user_id = ? AND role = 'friend' AND provider = ?",
                (eff_friend, provider),
            )
            row = cur.fetchone()
            if row:
                return False, "Friend has completed first-time shared subscription acknowledgement", ""

        desc = (
            f"[Personal Shared Subscription Acknowledgement] You are initiating a task supported by admin personal subscription ({provider}). "
            "Please note this channel is personal sharing and subject to rate limits. Please acknowledge to proceed."
        )
        req_id = self.submit_request(
            task_id=f"friend_terms_{int(time.time()*1000)%100000}",
            category=ApprovalCategory.SHARED_SUBSCRIPTION_RISK,
            description=desc,
            details={"friend_id": eff_friend, "provider": provider},
            allowed_scope="always",
            owner_id=eff_friend,
            timeout_seconds=600,
        )
        return True, desc, req_id

    def record_terms_acknowledgement(
        self,
        user_id: str,
        role: str,
        provider: str = "codex",
        terms_version: str = "v1.0-personal-sharing",
        details: dict[str, Any] | None = None,
    ):
        """Persist explicit terms acknowledgement for audit archiving."""
        ack_id = f"ack_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO subscription_terms_acknowledgements (
                    ack_id, user_id, role, provider, acknowledged_at, terms_version, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ack_id, user_id, role, provider, now, terms_version, json.dumps(details or {}, ensure_ascii=False)),
            )
            conn.commit()
        logger.info(f"Recorded subscription terms acknowledgement for {role} [{user_id}] on {provider}")
