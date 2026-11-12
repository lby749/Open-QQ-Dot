"""Message Queue Governor & Zero-Backlog Dispatcher for Open QQ Dot.

Eliminates message stagnation, queue backlogs, and multi-message cascading delays:
1. Non-blocking asynchronous inbound dispatch (group queries never block private chats).
2. Per-friend message consolidation: collapses multiple pending user messages into a single
   coherent context turn instead of processing N sequential blocking turns.
3. Fast-path command routing: task approvals, cancels, and status checks bypass LLM latency.
4. Auto-drainage of orphaned and stale states on startup, reconnection, and on-demand flush.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("queue_governor")


class QueueGovernor:
    _instance: Optional[QueueGovernor] = None
    _lock = threading.Lock()

    def __init__(self, max_group_workers: int = 4, max_friend_workers: int = 4):
        self._group_pool = ThreadPoolExecutor(max_workers=max_group_workers, thread_name_prefix="group-worker")
        self._friend_pool = ThreadPoolExecutor(max_workers=max_friend_workers, thread_name_prefix="friend-worker")
        self._friend_locks: Dict[str, threading.Lock] = {}
        self._group_queues: Dict[str, Any] = {}
        self._active_groups: set[str] = set()
        self._locks_guard = threading.Lock()
        self._stats = {
            "inbound_total": 0,
            "consolidated_turns": 0,
            "fast_path_hits": 0,
            "drained_orphans": 0,
            "last_flush_at": "",
        }

    @classmethod
    def get_instance(cls) -> QueueGovernor:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _get_friend_lock(self, qq_id: str) -> threading.Lock:
        with self._locks_guard:
            if qq_id not in self._friend_locks:
                self._friend_locks[qq_id] = threading.Lock()
            return self._friend_locks[qq_id]

    # -------------------------------------------------------------------------
    # Fast Path Command Checker
    # -------------------------------------------------------------------------
    @staticmethod
    def is_fast_command(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        fast_keywords = (
            "批准", "拒绝", "待审批", "审批", "approve", "reject", "pending",
            "/status", "状态", "/help", "帮助", "/stop", "取消", "cancel",
            "清空队列", "疏通", "flush"
        )
        return any(t.startswith(kw) or t == kw for kw in fast_keywords)

    # -------------------------------------------------------------------------
    # Telemetry & Health Inspection
    # -------------------------------------------------------------------------
    def inspect_queue_health(self, friends_registry: Dict[str, Any]) -> Dict[str, Any]:
        """Scans all registered friends and calculates queue depth, waiting items, and stagnation."""
        from store import FriendStore

        total_unhandled = 0
        total_waiting = 0
        total_stuck = 0
        friend_queues = []
        now = datetime.now().astimezone()

        for qq_id, record in friends_registry.items():
            dir_path = Path(record.get("directory", ""))
            if not dir_path.is_dir():
                continue
            store = FriendStore(dir_path)
            try:
                # Count unhandled incoming friend messages
                unhandled_rows = store.db.execute(
                    "SELECT qq_id, body, created_at, seq FROM messages WHERE direction='friend' AND handled=0 ORDER BY seq ASC"
                ).fetchall()
                unhandled_count = len(unhandled_rows)
                total_unhandled += unhandled_count

                # Count waiting / stuck reply attempts
                attempt_rows = store.db.execute(
                    "SELECT message_id, state, error, updated_at FROM reply_attempts WHERE state IN ('waiting', 'processing', 'queued', 'sending')"
                ).fetchall()
                waiting_count = sum(1 for r in attempt_rows if r["state"] == "waiting")
                stuck_count = sum(1 for r in attempt_rows if r["state"] in ("processing", "sending", "queued"))
                total_waiting += waiting_count
                total_stuck += stuck_count

                reply_waiting = store.get("reply_waiting", "0") == "1"
                paused = bool(store.get("round_pause_since"))

                friend_queues.append({
                    "qq_id": str(qq_id),
                    "name": record.get("display_name") or str(qq_id),
                    "unhandled_count": unhandled_count,
                    "waiting_count": waiting_count,
                    "stuck_count": stuck_count,
                    "reply_waiting": reply_waiting,
                    "paused": paused,
                    "latest_message": unhandled_rows[-1]["body"] if unhandled_rows else "",
                    "oldest_timestamp": unhandled_rows[0]["created_at"] if unhandled_rows else "",
                })
            except Exception as exc:
                logger.error("Failed inspecting friend %s: %s", qq_id, exc)
            finally:
                store.close()

        status_label = "HEALTHY"
        if total_stuck > 0 or total_unhandled > 5:
            status_label = "CONGESTED" if total_unhandled > 10 else "ATTENTION"

        return {
            "status": status_label,
            "total_unhandled": total_unhandled,
            "total_waiting": total_waiting,
            "total_stuck": total_stuck,
            "friend_count": len(friend_queues),
            "friends": sorted(friend_queues, key=lambda x: (x["unhandled_count"] + x["stuck_count"]), reverse=True),
            "stats": dict(self._stats),
            "timestamp": now.isoformat(),
        }

    # -------------------------------------------------------------------------
    # Backlog Consolidation & Auto Drainage
    # -------------------------------------------------------------------------
    def consolidate_friend_inbound(self, store: Any) -> Optional[Tuple[List[str], str, Optional[str]]]:
        """Finds all unhandled waiting messages for a friend and merges them into a single coherent turn.
        
        Returns (list_of_message_ids, consolidated_body, latest_image_file) or None if no waiting messages.
        """
        rows = store.db.execute(
            "SELECT m.qq_id, m.body, m.created_at, r.image_file, r.quote "
            "FROM messages m LEFT JOIN reply_attempts r ON m.qq_id = r.message_id "
            "WHERE m.direction='friend' AND m.handled=0 "
            "AND (r.state IS NULL OR r.state NOT IN ('uncertain','sending','sent','cancelled')) "
            "ORDER BY m.seq ASC LIMIT 30"
        ).fetchall()

        if not rows:
            return None

        message_ids = [str(r["qq_id"]) for r in rows]
        latest_image = next((r["image_file"] for r in reversed(rows) if r["image_file"]), None)

        if len(rows) == 1:
            return message_ids, rows[0]["body"], latest_image

        # Multiple messages arrived before or during reply generation: consolidate!
        with self._lock:
            self._stats["consolidated_turns"] += 1

        bodies = [r["body"].strip() for r in rows if r["body"].strip()]
        consolidated = "\n".join(f"{i+1}. {b}" for i, b in enumerate(bodies))
        merged_body = f"[用户连续发送了 {len(bodies)} 条消息]:\n{consolidated}"

        return message_ids, merged_body, latest_image

    def drain_friend_backlog(self, store: Any, max_age_minutes: int = 120) -> int:
        """Drains stuck attempts and marks ancient unhandled messages handled to prevent endless stalls."""
        drained = 0
        cutoff = (datetime.now().astimezone() - timedelta(minutes=max_age_minutes)).isoformat()

        # 1. Clear stuck reply attempts
        stuck_rows = store.db.execute(
            "SELECT message_id FROM reply_attempts WHERE state IN ('processing', 'queued', 'sending') AND updated_at < ?",
            (cutoff,)
        ).fetchall()
        for r in stuck_rows:
            state = store.reply_attempt(r['message_id'])['state']
            store.set_reply_attempt(r['message_id'], 'uncertain' if state == 'sending' else 'failed',
                                    'Interrupted send: delivery unconfirmed' if state == 'sending'
                                    else 'Stale execution recovered; safe to retry')
            drained += 1

        # 2. Reset reply_waiting lock if older than 10 minutes
        waiting_since = store.get("reply_waiting_since")
        if waiting_since and waiting_since < (datetime.now().astimezone() - timedelta(minutes=10)).isoformat():
            store.set("reply_waiting", "0")
            store.set("reply_waiting_since", "")

        return drained

    def flush_all(self, friends_registry: Dict[str, Any], mark_ancient_handled: bool = False) -> Dict[str, Any]:
        """Recover stuck attempts safely; never acknowledge unread user messages."""
        if mark_ancient_handled:
            raise ValueError('Bulk marking unread messages handled is disabled; manual review is required')
        from store import FriendStore

        total_drained = 0
        now_str = datetime.now().astimezone().isoformat()

        for qq_id, record in friends_registry.items():
            dir_path = Path(record.get("directory", ""))
            if not dir_path.is_dir():
                continue
            store = FriendStore(dir_path)
            try:
                # Reset locks
                store.set("reply_waiting", "0")
                store.set("reply_waiting_since", "")
                store.set("bridge_error", "")

                # Sending has an unknown external outcome: never auto-replay it.
                # Every other attempt can be rescheduled without marking the source handled.
                rows = store.db.execute(
                    "SELECT message_id, state FROM reply_attempts WHERE state IN ('processing', 'queued', 'sending', 'waiting')"
                ).fetchall()
                for r in rows:
                    next_state = 'uncertain' if r['state'] == 'sending' else 'failed'
                    store.set_reply_attempt(r['message_id'], next_state,
                                            'Queue recovery: check delivery before retrying' if next_state == 'uncertain'
                                            else 'Queue recovery: ready to be processed again')
                    total_drained += 1
            except Exception as exc:
                logger.error("Error flushing friend %s: %s", qq_id, exc)
            finally:
                store.close()

        with self._lock:
            self._stats["drained_orphans"] += total_drained
            self._stats["last_flush_at"] = now_str

        return {
            "success": True,
            "drained_count": total_drained,
            "flushed_at": now_str,
            "message": f"成功疏通队列，清理并重置了 {total_drained} 项滞留状态！",
        }

    # -------------------------------------------------------------------------
    # Asynchronous Execution Helpers
    # -------------------------------------------------------------------------
    def dispatch_group_event(self, fn: Callable[[], Any], group_id: str = ''):
        """FIFO dispatch per group, with independent groups sharing a bounded worker pool."""
        if not group_id:
            return self._group_pool.submit(fn)
        group_id = str(group_id)
        result = Future()
        with self._locks_guard:
            pending = self._group_queues.setdefault(group_id, deque())
            if len(pending) >= 200:
                result.set_exception(RuntimeError('Group queue saturated; backpressure applied'))
                return result
            pending.append((fn, result))
            if group_id not in self._active_groups:
                self._active_groups.add(group_id)
                self._group_pool.submit(self._drain_group, group_id)
        return result

    def _drain_group(self, group_id: str):
        while True:
            with self._locks_guard:
                pending = self._group_queues[group_id]
                if not pending:
                    self._active_groups.discard(group_id)
                    self._group_queues.pop(group_id, None)
                    return
                fn, future = pending.popleft()
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)
                logger.exception('Group %s handler failed', group_id)

    def dispatch_friend_event(self, qq_id: str, fn: Callable[[], Any]):
        """Executes friend message processing under per-friend serial locking without blocking other friends."""
        def _task():
            lock = self._get_friend_lock(qq_id)
            with lock:
                return fn()
        return self._friend_pool.submit(_task)
