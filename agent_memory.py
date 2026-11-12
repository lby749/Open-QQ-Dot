"""Enhanced Long-Term Memory & Agent Experience Reuse Subsystem.

Provides scoped memory management:
- USER_PREFERENCE: Global user habits and directives.
- FRIEND_PRIVATE: Per-contact isolated private memory (strictly segregated).
- PROJECT_MEMORY: Codebase architecture and conventions.
- TASK_EXECUTION: History of durable tasks and outputs.
- TOOL_EXPERIENCE: Realized parameters and tool quirks.
- FAILURE_AND_FIX: Validated failure analysis and remediation playbook (experience reuse).

Includes memory lifecycle tracking: importance, source, confidence, conflict check, and expiration.
"""
from __future__ import annotations
from contextlib import contextmanager

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REMEDY_MIN_SCORE = 0.1  # 2026-10-26 benchmark: relevant fixes >= 0.79, unrelated requests <= 0.001


class MemoryScope(str, Enum):
    USER_PREFERENCE = "user_preference"
    FRIEND_PRIVATE = "friend_private"
    PROJECT_MEMORY = "project_memory"
    TASK_EXECUTION = "task_execution"
    TOOL_EXPERIENCE = "tool_experience"
    FAILURE_AND_FIX = "failure_and_fix"


class MemorySource(str, Enum):
    USER_EXPLICIT = "user_explicit"      # User explicitly specified
    TOOL_VERIFIED = "tool_verified"      # Verified by test/compiler/file check
    MODEL_INFERRED = "model_inferred"    # Model hypothesis/inference


@dataclass
class MemoryEntry:
    id: int | None
    scope: MemoryScope
    owner_id: str  # friend_id / project_id / 'system'
    key: str
    content: str
    importance: int = 5                  # 1 to 10
    source: MemorySource = MemorySource.MODEL_INFERRED
    confidence: float = 0.8              # 0.0 to 1.0 (prevents guessing as hard fact)
    project_id: str = ""
    goal_id: str = ""
    task_id: str = ""
    is_shared: bool = False
    created_at: str = ""
    updated_at: str = ""
    expires_at: str | None = None
    is_valid: bool = True
    metadata: dict[str, Any] = None

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['scope'] = self.scope.value if isinstance(self.scope, MemoryScope) else self.scope
        d['source'] = self.source.value if isinstance(self.source, MemorySource) else self.source
        return d


class AgentMemoryStore:
    """Persistent scoped memory engine with failure experience reuse and strict owner isolation."""

    _instance: AgentMemoryStore | None = None
    _singleton_lock = threading.Lock()

    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            data_dir = __import__("house_settings").data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = data_dir / "agent_memory.sqlite3"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db_lock = threading.RLock()
        self._init_db()

    @classmethod
    def get_instance(cls, db_path: Path | None = None) -> AgentMemoryStore:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance

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
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 5,
                    source TEXT NOT NULL DEFAULT 'model_inferred',
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_mem_scope_owner ON memories(scope, owner_id);
                CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(key);
                """
            )
            # Schema migrations for backward compatibility
            for col, col_def in [
                ("project_id", "TEXT NOT NULL DEFAULT ''"),
                ("goal_id", "TEXT NOT NULL DEFAULT ''"),
                ("task_id", "TEXT NOT NULL DEFAULT ''"),
                ("is_shared", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    pass

            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_owner_project ON memories(owner_id, project_id)")
            # 2026-10-26: every overwritten version is kept, so a changed fact stays traceable to what it replaced
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL, owner_id TEXT NOT NULL,
                    content TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL, metadata TEXT NOT NULL,
                    valid_from TEXT NOT NULL, replaced_at TEXT NOT NULL, replaced_by TEXT NOT NULL, reason TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_history ON memory_history(memory_id)")
            conn.commit()

    @staticmethod
    def _archive(conn, memory_id: int, by: str, reason: str, new_content: str | None = None):
        """Keep the version about to be overwritten (nothing when the content does not change)."""
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None or (new_content is not None and row["content"] == new_content):
            return
        conn.execute(
            "INSERT INTO memory_history (memory_id, owner_id, content, source, confidence, metadata, valid_from,"
            " replaced_at, replaced_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (memory_id, row["owner_id"], row["content"], row["source"], row["confidence"], row["metadata"] or "{}",
             row["updated_at"], datetime.now().isoformat(), by, reason))

    def history(self, memory_id: int, owner_id: str, is_admin: bool = False) -> list[dict[str, Any]]:
        """Earlier versions of a memory, oldest first; the same owner rule as editing it."""
        clean_owner = str(owner_id).strip()
        with self._db_lock, self._get_connection() as conn:
            row = conn.execute("SELECT owner_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return []
            if row["owner_id"] != clean_owner and not (clean_owner in ("system", "admin") or is_admin):
                raise PermissionError("无权查看其他用户记忆的历史版本")
            return [dict(r) for r in conn.execute(
                "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY history_id", (memory_id,))]

    def store_memory(
        self,
        scope: MemoryScope,
        owner_id: str,
        key: str,
        content: str,
        importance: int = 5,
        source: MemorySource = MemorySource.MODEL_INFERRED,
        confidence: float = 0.8,
        ttl_days: int | None = None,
        project_id: str = "",
        goal_id: str = "",
        task_id: str = "",
        is_shared: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Store or update a scoped memory item with explicit ownership and project/goal context."""
        now = datetime.now()
        now_str = now.isoformat()
        expires_str = (now + timedelta(days=ttl_days)).isoformat() if ttl_days else None
        owner = str(owner_id).strip() or "system"

        with self._db_lock, self._get_connection() as conn:
            # Check for existing key in the same scope, owner, and project
            cur = conn.execute(
                "SELECT id, confidence FROM memories WHERE scope = ? AND owner_id = ? AND key = ? AND project_id = ?",
                (scope.value, owner, key, project_id),
            )
            existing = cur.fetchone()
            if existing:
                row_id = existing['id']
                self._archive(conn, row_id, owner, "updated with the same key", content)
                conn.execute(
                    """
                    UPDATE memories SET
                        content = ?, importance = ?, source = ?, confidence = ?,
                        updated_at = ?, expires_at = ?, is_valid = 1,
                        project_id = ?, goal_id = ?, task_id = ?, is_shared = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (content, importance, source.value, confidence, now_str, expires_str,
                     project_id, goal_id, task_id, 1 if is_shared else 0,
                     json.dumps(metadata or {}, ensure_ascii=False), row_id),
                )
                conn.commit()
                return row_id
            else:
                cur = conn.execute(
                    """
                    INSERT INTO memories (
                        scope, owner_id, key, content, importance, source,
                        confidence, created_at, updated_at, expires_at, is_valid,
                        project_id, goal_id, task_id, is_shared, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (scope.value, owner, key, content, importance, source.value,
                     confidence, now_str, now_str, expires_str,
                     project_id, goal_id, task_id, 1 if is_shared else 0,
                     json.dumps(metadata or {}, ensure_ascii=False)),
                )
                conn.commit()
                return cur.lastrowid

    def record_failure_experience(
        self,
        task_category: str,
        failed_action: str,
        root_cause: str,
        solution: str,
        owner_id: str = "system",
        project_id: str = "",
        goal_id: str = "",
        task_id: str = "",
        is_shared: bool = False,
        verified: bool = False,
    ) -> int:
        """Save a failure and proposed remedy; only a caller with real validation may mark it verified."""
        owner = str(owner_id).strip() or "system"
        key = f"fix:{task_category}:{failed_action[:30]}"
        content = (
            f"【失败案例与排错经验】\n"
            f"- 任务/动作: {failed_action}\n"
            f"- 原因/条件: {root_cause}\n"
            f"- {'验证解决方案' if verified else '建议改法（待验证）'}: {solution}"
        )
        return self.store_memory(
            scope=MemoryScope.FAILURE_AND_FIX,
            owner_id=owner,
            key=key,
            content=content,
            importance=9,
            source=MemorySource.TOOL_VERIFIED if verified else MemorySource.MODEL_INFERRED,
            confidence=1.0 if verified else 0.5,
            project_id=project_id,
            goal_id=goal_id,
            task_id=task_id,
            is_shared=is_shared,
            metadata={
                "category": task_category,
                "failed_action": failed_action,
                "root_cause": root_cause,
                "solution": solution,
                "verified": bool(verified),
                "result_status": "resolved" if verified else "failed",
                "recorded_at": datetime.now().isoformat(),
            },
        )

    def retrieve_experience_remedies(
        self,
        query: str,
        owner_id: str = "system",
        project_id: str | None = None,
        limit: int = 3,
    ) -> list[str]:
        """Retrieve applicable failure & fix patterns strictly scoped to owner_id or verified shared system knowledge."""
        now_str = datetime.now().isoformat()
        owner = str(owner_id).strip() or "system"

        query_sql = """
            SELECT content, metadata FROM memories
            WHERE scope = ?
              AND (owner_id = ? OR (owner_id = 'system' AND is_shared = 1 AND source IN ('tool_verified','user_explicit')))
              AND is_valid = 1
              AND (expires_at IS NULL OR expires_at > ?)
        """
        params = [MemoryScope.FAILURE_AND_FIX.value, owner, now_str]

        if project_id is not None:
            query_sql += " AND (project_id = ? OR project_id = '')"
            params.append(project_id)

        query_sql += " ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT 200"

        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(query_sql, params)
            rows = cur.fetchall()
        return [rows[i]['content'] for i, _ in self._rank(query, rows, limit)]

    def _rank(self, query: str, rows, limit: int):
        """[(row index, score)] relevant to query (retrieval_core.rank_texts). 2026-10-26: the query was split on
        spaces, so a Chinese request was one token that never matched while English 'to' matched everything; the
        order was importance, not relevance."""
        import retrieval_core
        texts = []
        for r in rows:
            try:
                meta = json.loads(r['metadata'] or '{}')
            except (TypeError, ValueError):
                meta = {}
            texts.append(' '.join([r['content']] + [str(meta.get(key, '')) for key in
                                                    ('category', 'failed_action', 'root_cause', 'solution')]))
        return retrieval_core.rank_texts(query, texts, limit=limit, task='remedies', floor=REMEDY_MIN_SCORE,
                                         cache_path=self.db_path.parent / 'retrieval_cache.sqlite3')

    def search_experience(
        self,
        query: str,
        owner_id: str = "system",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Convenience query for failure & fix memories, returning structured dictionaries."""
        mems = self.list_memories(owner_id=owner_id, scope=MemoryScope.FAILURE_AND_FIX, limit=200)
        if not query.strip():
            ranked = [(i, 0.0) for i in range(min(limit, len(mems)))]
        else:
            ranked = self._rank(query, [{'content': str(m.get('content', '')), 'metadata': m.get('metadata') if isinstance(
                m.get('metadata'), str) else json.dumps(m.get('metadata') or {}, ensure_ascii=False)} for m in mems], limit)
        results = []
        for i, _ in ranked:
            m = mems[i]
            raw_meta = m.get("metadata")
            meta = {}
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            results.append({
                "id": m.get("id"),
                "content": str(m.get("content", "")),
                "root_cause": meta.get("root_cause", ""),
                "solution": meta.get("solution", ""),
                "failed_action": meta.get("failed_action", ""),
                "metadata": meta,
            })
        return results

    # ------------------ Memory Lifecycle Management APIs ------------------

    def assess_importance(self, memory_id: int, importance: int) -> bool:
        """Update memory importance score (1 to 10)."""
        score = max(1, min(10, int(importance)))
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?", (score, datetime.now().isoformat(), memory_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_memory(self, memory_id: int, owner_id: str, is_admin: bool = False) -> bool:
        """User explicitly deletes a memory item with strict owner authorization."""
        clean_owner = str(owner_id).strip()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT owner_id, scope FROM memories WHERE id = ?", (memory_id,))
            row = cur.fetchone()
            if not row:
                return False
            mem_owner = row["owner_id"]
            if mem_owner == "system":
                if not (clean_owner in ("system", "admin") or is_admin):
                    raise PermissionError("普通用户无权删除系统级公共记忆")
            else:
                if mem_owner != clean_owner and not (clean_owner in ("system", "admin") or is_admin):
                    raise PermissionError("无权删除其他用户的私有记忆")

            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    def update_memory_content(self, memory_id: int, owner_id: str, new_content: str, is_admin: bool = False) -> bool:
        """User explicitly modifies a memory item with strict owner authorization."""
        clean_owner = str(owner_id).strip()
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT owner_id, scope, content, metadata FROM memories WHERE id = ?", (memory_id,))
            row = cur.fetchone()
            if not row:
                return False
            mem_owner = row["owner_id"]
            if mem_owner == "system":
                if not (clean_owner in ("system", "admin") or is_admin):
                    raise PermissionError("普通用户无权修改系统级公共记忆")
            else:
                if mem_owner != clean_owner and not (clean_owner in ("system", "admin") or is_admin):
                    raise PermissionError("无权修改其他用户的私有记忆")

            self._archive(conn, memory_id, clean_owner, "edited", new_content)
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            meta.setdefault("edit_history", []).append({
                "edited_at": now_str,
                "edited_by": clean_owner,
                "prev_content_snippet": row["content"][:100],
            })
            cur = conn.execute(
                "UPDATE memories SET content = ?, updated_at = ?, metadata = ? WHERE id = ?",
                (new_content, now_str, json.dumps(meta, ensure_ascii=False), memory_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def correct_memory(self, memory_id: int, owner_id: str, corrected_content: str, reason: str = "", is_admin: bool = False) -> bool:
        """User actively corrects a hallucinated or erroneous memory with auditing."""
        clean_owner = str(owner_id).strip()
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("SELECT owner_id, metadata FROM memories WHERE id = ?", (memory_id,))
            row = cur.fetchone()
            if not row:
                return False
            mem_owner = row["owner_id"]
            if mem_owner != clean_owner and not (clean_owner in ("system", "admin") or is_admin):
                raise PermissionError("无权纠错其他用户的私有记忆")

            self._archive(conn, memory_id, clean_owner, "corrected: " + (reason or "")[:200], corrected_content)
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            meta["user_corrected"] = True
            meta["correction_reason"] = reason
            meta["corrected_at"] = now_str
            cur = conn.execute(
                "UPDATE memories SET content = ?, updated_at = ?, confidence = 1.0, metadata = ? WHERE id = ?",
                (corrected_content, now_str, json.dumps(meta, ensure_ascii=False), memory_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_memories(
        self,
        owner_id: str,
        scope: MemoryScope | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List active memories belonging to a specific owner_id."""
        with self._db_lock, self._get_connection() as conn:
            conditions = ["(owner_id = ? OR (owner_id = 'system' AND is_shared = 1 AND source IN ('tool_verified','user_explicit')))", "is_valid = 1"]
            params: list[Any] = [owner_id]
            if scope:
                conditions.append("scope = ?")
                params.append(scope.value)

            sql = f"SELECT * FROM memories WHERE {' AND '.join(conditions)} ORDER BY importance DESC, updated_at DESC LIMIT ?"
            params.append(limit)
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def detect_and_clean_expired(self) -> int:
        """Detect and invalidate expired memory records."""
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("UPDATE memories SET is_valid = 0 WHERE expires_at IS NOT NULL AND expires_at <= ? AND is_valid = 1", (now_str,))
            conn.commit()
            return cur.rowcount

    def get_scoped_memories(
        self,
        scope: MemoryScope,
        owner_id: str,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        """Retrieve active memories strictly within the specified scope and owner."""
        now_str = datetime.now().isoformat()
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT * FROM memories
                WHERE scope = ? AND (owner_id = ? OR (owner_id = 'system' AND is_shared = 1 AND source IN ('tool_verified','user_explicit'))) AND is_valid = 1
                AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY importance DESC, updated_at DESC LIMIT ?
                """,
                (scope.value, owner_id, now_str, limit),
            )
            entries = []
            for r in cur.fetchall():
                entries.append(MemoryEntry(
                    id=r['id'],
                    scope=MemoryScope(r['scope']),
                    owner_id=r['owner_id'],
                    key=r['key'],
                    content=r['content'],
                    importance=r['importance'],
                    source=MemorySource(r['source']),
                    confidence=r['confidence'],
                    project_id=r['project_id'] if 'project_id' in r.keys() else "",
                    goal_id=r['goal_id'] if 'goal_id' in r.keys() else "",
                    task_id=r['task_id'] if 'task_id' in r.keys() else "",
                    is_shared=bool(r['is_shared']) if 'is_shared' in r.keys() else False,
                    created_at=r['created_at'],
                    updated_at=r['updated_at'],
                    expires_at=r['expires_at'],
                    is_valid=bool(r['is_valid']),
                    metadata=json.loads(r['metadata']),
                ))
            return entries

    def invalidate_memory(self, memory_id: int, owner_id: str | None = None, is_admin: bool = False) -> bool:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise PermissionError("Invalidating memory requires trusted current user ID; omitting identity is prohibited")
        if is_admin:
            raise PermissionError("Public memory interface does not accept boolean is_admin privilege escalation; admin operations require authenticated server entrypoint")
        with self._db_lock, self._get_connection() as conn:
            row = conn.execute("SELECT owner_id FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return False
            eff_owner = owner_id.strip()
            if row["owner_id"] != eff_owner:
                raise PermissionError(
                    f"Cross-tenant memory access unauthorized: User [{eff_owner}] is not authorized to invalidate memory (id={memory_id}) belonging to [{row['owner_id']}]"
                )
            conn.execute("UPDATE memories SET is_valid = 0 WHERE id = ?", (memory_id,))
            conn.commit()
            return True


# Alias for backward compatibility
AgentMemory = AgentMemoryStore
