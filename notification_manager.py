"""Unified Cross-Device Reliable Notification Subsystem.

Coordinates notifications and approval prompts across QQ, Web Console, and local daemon.
Provides SQLite persistence, delivery state machine (PENDING/SENDING/SENT/FAILED),
idempotency deduplication, crash recovery, and bounded retries.
"""
from __future__ import annotations
from contextlib import contextmanager

import collections
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class NotificationDeferred(RuntimeError):
    """Preparation is still running; preserve the delivery retry budget."""


class NotificationPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


@dataclass
class NotificationMessage:
    id: str
    title: str
    content: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    category: str = "general"  # task, approval, proactive, system, goal
    recipient_id: str = ""  # target qq or empty for system-wide
    status: NotificationStatus = NotificationStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    error_reason: str = ""
    channel: str = "all"
    metadata: dict[str, Any] = None
    created_at: str = ""
    sent_at: str = ""
    read: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['priority'] = self.priority.value if isinstance(self.priority, NotificationPriority) else self.priority
        d['status'] = self.status.value if isinstance(self.status, NotificationStatus) else self.status
        return d


class NotificationManager:
    """Dispatches durable notifications across connected channels with crash recovery."""

    _instance: NotificationManager | None = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path | None = None, max_history: int = 100):
        import tempfile
        self._max_history = max_history
        self._db_lock = threading.RLock()
        if db_path is None:
            import tempfile
            tf = tempfile.NamedTemporaryFile(suffix="_notifs.sqlite3", delete=False)
            tf.close()
            self.db_path = Path(tf.name)
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._channel_sinks: dict[str, Callable[[NotificationMessage], bool | None]] = {}
        self._queue: collections.deque[NotificationMessage] = collections.deque(maxlen=max_history)
        self._init_db()
        self._recover_pending_notifications()

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
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    category TEXT NOT NULL DEFAULT 'general',
                    recipient_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    dedup_key TEXT NOT NULL DEFAULT '',
                    error_reason TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT 'all',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    read INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
                CREATE INDEX IF NOT EXISTS idx_notif_recipient ON notifications(recipient_id);
                CREATE INDEX IF NOT EXISTS idx_notif_dedup ON notifications(recipient_id, dedup_key);
                """
            )
            conn.commit()

    def _recover_pending_notifications(self):
        """Recover un-dispatched or failed notifications from previous restarts."""
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM notifications WHERE status IN ('PENDING', 'SENDING') AND retry_count < max_retries ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
            for r in rows:
                msg = self._row_to_msg(r)
                self._queue.append(msg)

    def _row_to_msg(self, r: sqlite3.Row) -> NotificationMessage:
        return NotificationMessage(
            id=r['id'],
            title=r['title'],
            content=r['content'],
            priority=NotificationPriority(r['priority']),
            category=r['category'],
            recipient_id=r['recipient_id'],
            status=NotificationStatus(r['status']),
            retry_count=r['retry_count'],
            max_retries=r['max_retries'],
            error_reason=r['error_reason'],
            channel=r['channel'],
            metadata=json.loads(r['metadata']) if r['metadata'] else {},
            created_at=r['created_at'],
            sent_at=r['sent_at'],
            read=bool(r['read']),
        )

    @classmethod
    def get_instance(cls, db_path: Path | None = None) -> NotificationManager:
        with cls._lock:
            if cls._instance is None:
                if db_path is not None:
                    actual_path = Path(db_path)
                else:
                    import os
                    root_dir = Path(os.environ.get("QQ_BRIDGE_HOME", Path(__file__).resolve().parent))
                    data_dir = root_dir / "data"
                    data_dir.mkdir(parents=True, exist_ok=True)
                    actual_path = data_dir / "notifications.sqlite3"
                cls._instance = cls(actual_path)
            return cls._instance

    def register_channel_sink(
        self,
        sink_fn: Callable[[NotificationMessage], bool | None],
        name: str | None = None,
    ):
        """Register a named delivery sink (replaces old sink with same name to prevent duplicates on reconnect)."""
        sink_name = name or getattr(sink_fn, "__name__", None) or f"sink_{id(sink_fn)}"
        with self._lock:
            self._channel_sinks[sink_name] = sink_fn
        try:
            self.retry_pending_notifications()
        except Exception as e:
            logger.debug(f"Immediate flush upon sink registration notice: {e}")

    def unregister_channel_sink(self, name: str) -> bool:
        """Unregister a delivery sink by name."""
        with self._lock:
            if name in self._channel_sinks:
                del self._channel_sinks[name]
                return True
        return False

    def publish(
        self,
        title: str,
        content: str,
        priority: NotificationPriority = NotificationPriority.NORMAL,
        category: str = "general",
        recipient_id: str = "",
        channel: str = "all",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Publish a reliable notification with persistence, deduplication, and retry."""
        now_str = datetime.now().isoformat()
        dedup_key = (metadata or {}).get('dedup_key', '')

        # 1. Deduplication check
        if dedup_key:
            with self._db_lock, self._get_connection() as conn:
                cur = conn.execute(
                    "SELECT id, status FROM notifications WHERE recipient_id = ? AND dedup_key = ? ORDER BY created_at DESC LIMIT 1",
                    (recipient_id, dedup_key),
                )
                existing = cur.fetchone()
                if existing:
                    logger.debug(f"Notification with dedup_key {dedup_key} already recorded ({existing['status']}).")
                    return existing['id']

        msg_id = f"notif_{uuid.uuid4().hex[:10]}"
        msg = NotificationMessage(
            id=msg_id,
            title=title,
            content=content,
            priority=priority,
            category=category,
            recipient_id=recipient_id,
            status=NotificationStatus.PENDING,
            channel=channel,
            metadata=metadata or {},
            created_at=now_str,
        )

        # 2. Persist to SQLite
        with self._db_lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO notifications (
                    id, title, content, priority, category, recipient_id,
                    status, retry_count, max_retries, dedup_key, error_reason,
                    channel, metadata, created_at, updated_at, sent_at, read
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, ?, '', ?, ?, ?, ?, '', 0)
                """,
                (
                    msg.id, msg.title, msg.content,
                    msg.priority.value if isinstance(msg.priority, NotificationPriority) else msg.priority,
                    msg.category, msg.recipient_id, NotificationStatus.PENDING.value,
                    dedup_key, msg.channel, json.dumps(msg.metadata, ensure_ascii=False),
                    now_str, now_str,
                ),
            )
            conn.commit()

        # Update in-memory fast ring
        with self._lock:
            self._queue.append(msg)
            sinks = list(self._channel_sinks.values())

        # 3. Attempt immediate dispatch to sinks
        self._dispatch_message(msg, sinks)
        return msg_id

    def _dispatch_message(self, msg: NotificationMessage, sinks: list[Callable]):
        """Deliver to registered sinks with failure tracking."""
        success_count = 0
        last_error = ""
        deferred = False
        if msg.channel == 'local':
            sinks = []

        # Update status to SENDING
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conn.execute("UPDATE notifications SET status = 'SENDING', updated_at = ? WHERE id = ?", (now_str, msg.id))
            conn.commit()

        for sink in sinks:
            try:
                res = sink(msg)
                if res is not False:
                    success_count += 1
            except NotificationDeferred as exc:
                deferred = True
                last_error = str(exc)
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"Notification sink failed for {msg.id}: {exc}")

        # Truly delivered if and only if at least one registered sink confirmed delivery
        delivered = msg.channel == 'local' or ((len(sinks) > 0) and (success_count > 0))
        with self._db_lock, self._get_connection() as conn:
            if delivered:
                conn.execute(
                    "UPDATE notifications SET status = 'SENT', sent_at = ?, error_reason = '', updated_at = ? WHERE id = ?",
                    (now_str, now_str, msg.id),
                )
                msg.status = NotificationStatus.SENT
                msg.sent_at = now_str
                msg.error_reason = ""
            elif len(sinks) == 0 or deferred:
                # No sinks registered yet: remain PENDING without draining retries so future sinks can deliver it
                reason = last_error if deferred else "暂无可用通知渠道 Sink (未注册通知发送端)"
                conn.execute(
                    "UPDATE notifications SET status = 'PENDING', error_reason = ?, updated_at = ? WHERE id = ?",
                    (reason, now_str, msg.id),
                )
                msg.status = NotificationStatus.PENDING
                msg.error_reason = reason
            else:
                # Sinks were called but all failed
                msg.retry_count += 1
                new_status = NotificationStatus.FAILED.value if msg.retry_count >= msg.max_retries else NotificationStatus.PENDING.value
                err_text = last_error or "所有已注册通知渠道均返回投递失败"
                conn.execute(
                    "UPDATE notifications SET status = ?, retry_count = ?, error_reason = ?, updated_at = ? WHERE id = ?",
                    (new_status, msg.retry_count, err_text, now_str, msg.id),
                )
                msg.status = NotificationStatus(new_status)
                msg.error_reason = err_text
            conn.commit()

    def update_metadata(self, message: NotificationMessage, updates: dict):
        """Persist channel preparation/receipt state without losing another channel's metadata."""
        with self._db_lock, self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT metadata FROM notifications WHERE id=?', (message.id,)).fetchone()
            metadata = json.loads(row['metadata']) if row else dict(message.metadata or {})
            metadata.update(updates)
            conn.execute('UPDATE notifications SET metadata=? WHERE id=?',
                         (json.dumps(metadata, ensure_ascii=False), message.id))
        message.metadata = metadata

    def retry_pending_notifications(self) -> int:
        """Retry sending pending or failed notifications."""
        with self._lock:
            sinks = list(self._channel_sinks.values())

        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM notifications WHERE status = 'PENDING' AND retry_count < max_retries ORDER BY created_at ASC"
            )
            rows = cur.fetchall()

        retried = 0
        for r in rows:
            msg = self._row_to_msg(r)
            self._dispatch_message(msg, sinks)
            retried += 1
        return retried

    def list_recent(
        self,
        limit: int = 20,
        unread_only: bool = False,
        recipient_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent notifications from SQLite (fallback to memory queue if needed)."""
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            conditions = []
            params: list[Any] = []

            if recipient_id is not None:
                conditions.append("recipient_id = ?")
                params.append(recipient_id)
            if unread_only:
                conditions.append("read = 0")

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            sql = f"SELECT * FROM notifications {where_clause} ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            cur = conn.execute(sql, params)
            return [self._row_to_msg(r).to_dict() for r in cur.fetchall()]

    def mark_as_read(self, msg_id: str, recipient_id: str | None = None, is_admin: bool = False) -> bool:
        """Mark a notification as read in both memory and database, enforcing recipient_id ownership unless is_admin=True."""
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            if recipient_id is not None and not is_admin:
                cur = conn.execute(
                    "UPDATE notifications SET read = 1, updated_at = ? WHERE id = ? AND recipient_id = ?",
                    (now_str, msg_id, str(recipient_id)),
                )
            else:
                cur = conn.execute("UPDATE notifications SET read = 1, updated_at = ? WHERE id = ?", (now_str, msg_id))
            conn.commit()
            updated = cur.rowcount > 0

        if updated:
            with self._lock:
                for m in self._queue:
                    if m.id == msg_id:
                        if is_admin or recipient_id is None or m.recipient_id == str(recipient_id):
                            m.read = True
                        break
        return updated

    def mark_all_as_read(self, recipient_id: str) -> int:
        """Mark all unread notifications belonging to recipient_id as read."""
        if not recipient_id:
            return 0
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE notifications SET read = 1, updated_at = ? WHERE recipient_id = ? AND read = 0",
                (now_str, str(recipient_id)),
            )
            conn.commit()
            count = cur.rowcount

        with self._lock:
            for m in self._queue:
                if m.recipient_id == str(recipient_id):
                    m.read = True
        return count

    def delete_notification(self, msg_id: str, recipient_id: str | None = None, is_admin: bool = False) -> bool:
        """Safely delete a notification, enforcing recipient_id ownership unless is_admin=True."""
        with self._db_lock, self._get_connection() as conn:
            if recipient_id is not None and not is_admin:
                cur = conn.execute("DELETE FROM notifications WHERE id = ? AND recipient_id = ?", (msg_id, str(recipient_id)))
            else:
                cur = conn.execute("DELETE FROM notifications WHERE id = ?", (msg_id,))
            conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            with self._lock:
                import collections
                self._queue = collections.deque([m for m in self._queue if m.id != msg_id], maxlen=self._queue.maxlen)
        return deleted

    def get_recipient_notifications(self, recipient_id: str, limit: int = 50) -> list[NotificationMessage]:
        """Convenience alias for list_recent filtered by recipient_id."""
        return self.list_recent(recipient_id=recipient_id, limit=limit)
