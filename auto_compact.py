"""Automatic context compaction at 90% / quota exhaustion.

The model backend performs the actual prompt truncation; this module makes sure
the durable parts are not lost: it refreshes the older-chat summary and asks the
memory layer to extract long-term facts.  At a hard quota stop it cannot call a
model, so it writes a bounded, deterministic checkpoint instead.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

COOLDOWN_SECONDS = 600

def _estimate_tokens(text: str) -> float:
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return cjk + max(0, len(text) - cjk) / 4
_active: set[str] = set()
_lock = threading.Lock()


def _recent_checkpoint(store, limit: int = 40, max_chars: int = 1800) -> str:
    rows = store.recent_messages(limit)
    lines = []
    for row in rows:
        who = "好友" if row.get("direction") == "friend" else "我"
        body = str(row.get("body") or "").strip().replace("\n", " ")
        if body:
            lines.append(f"{who}：{body[:160]}")
    text = "\n".join(lines)[-max_chars:]
    return f"[自动压缩 {datetime.now().astimezone().isoformat(timespec='minutes')}]\n{text}"


def _worker(directory: Path, profile: dict, reason: str) -> None:
    from store import FriendStore
    key = str(Path(directory).resolve())
    store = None
    try:
        store = FriendStore(directory)
        checkpoint = _recent_checkpoint(store)
        api = profile.get("model_api") or {}
        from provider_context import info
        ctx = info(api)
        summary = ""
        if reason != "quota":
            from main import codex_text
            try:
                from prompt_context import refresh_summary
                refresh_summary(store, profile, directory, codex_text)
                summary = store.get("context_summary", "") or ""
            except Exception:
                summary = ""
            try:
                from memory_management import refresh
                refresh(store, profile, directory, codex_text, force=True)
            except Exception:
                pass
        if not summary:
            summary = checkpoint
        # Save a bounded checkpoint as the earlier-chat summary, and one durable
        # memory entry so compaction is visible to future sessions.
        store.set("context_summary", summary[:1800])
        store.set("context_summary_seq", str(store.recent_messages(1)[0]["seq"]) if store.recent_messages(1) else store.get("context_summary_seq", "0"))
        try:
            from memory_management import insert, sync
            with store.db:
                insert(store, "自动整理（%s）：%s" % (reason, summary[:900]),
                       "fact", ["自动压缩", "上下文整理"], ["auto-compact"], False, category="other")
                sync(store)
        except Exception:
            pass
        store.set("auto_compact_at", datetime.now().astimezone().isoformat(timespec="seconds"))
        store.set("auto_compact_reason", reason)
        store.set("auto_compact_window", str(ctx.window))
        store.set("auto_compact_threshold", str(ctx.compact_at))
        store.set("auto_compact_status", "已整理摘要并写入长期记忆")
    except Exception as exc:
        if store is not None:
            try:
                store.set("auto_compact_status", f"自动整理失败：{type(exc).__name__}")
            except Exception:
                pass
    finally:
        if store is not None:
            store.close()
        with _lock:
            _active.discard(key)


def maybe_compact(directory: Path, profile: dict, prompt_text: str = '',
                  reason: str = "context", thread_id: str = "") -> bool:
    """Start one background compaction when the 90% point, a new compaction, or quota is reached."""
    from provider_context import info
    ctx = info(profile.get("model_api") or {})
    estimated = _estimate_tokens(str(prompt_text)) if prompt_text else 0
    should = reason == "quota" or estimated >= ctx.compact_at
    if not should and thread_id:
        try:
            from main import completed_compactions
            count = completed_compactions(thread_id)
            from store import FriendStore
            store = FriendStore(directory)
            try:
                last = int(store.get("auto_compact_compactions", "0") or 0)
                store.set("auto_compact_compactions", str(count))
                should = count > last
            finally:
                store.close()
        except Exception:
            pass
    if not should:
        return False
    key = str(Path(directory).resolve())
    with _lock:
        if key in _active:
            return False
        try:
            from store import FriendStore
            store = FriendStore(directory)
            try:
                if time.time() < float(store.get("auto_compact_retry_at", "0") or 0):
                    return False
                store.set("auto_compact_retry_at", str(time.time() + COOLDOWN_SECONDS))
            finally:
                store.close()
        except Exception:
            return False
        _active.add(key)
    threading.Thread(target=_worker, args=(directory, dict(profile), reason),
                     daemon=True, name="qq-auto-compact").start()
    return True
