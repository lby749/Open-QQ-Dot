"""Per-friend SQLite history, cursor, and durable memory."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from source_state import SourceConnection, initialize_history, source_lock, source_read, source_recovery


class FriendStore:
    def __init__(self, friend_dir: Path):
        friend_dir.mkdir(parents=True, exist_ok=True)
        self.path = friend_dir / "history.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=10, factory=SourceConnection)
        self.db.bind_source(self.path)
        self.db.row_factory = sqlite3.Row
        with source_recovery(self.path.parent):
            self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                qq_id TEXT NOT NULL UNIQUE,
                direction TEXT NOT NULL CHECK(direction IN ('friend','me')),
                identity TEXT NOT NULL DEFAULT 'friend',
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memories (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reply_attempts (
                message_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                image_file TEXT,
                quote INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS emotion_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                user_label TEXT NOT NULL,
                self_label TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "handled" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN handled INTEGER NOT NULL DEFAULT 0")
        if "identity" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN identity TEXT NOT NULL DEFAULT 'friend'")
        emotion_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(emotion_events)')}
        for name, kind in [('self_intensity', 'INTEGER'), ('self_reason', 'TEXT')]:
            if name not in emotion_columns:
                self.db.execute(f'ALTER TABLE emotion_events ADD COLUMN {name} {kind}')
        self.db.commit()
        from memory_management import initialize
        with source_recovery(self.path.parent):
            initialize(self)
            initialize_history(self)
        from persona_incremental import reconcile
        reconcile(self)

    def close(self):
        try:
            from message_classification import flush_for_database
            flush_for_database(self.path, timeout=0.25)
        except Exception:
            pass
        self.db.close()

    @source_read
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str):
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    def add_message(self, qq_id: str, direction: str, body: str, created_at: str | None = None,
                    identity: str = "friend") -> bool:
        identity = "model" if identity == "model" else "friend"
        if direction == "me" and not str(qq_id).startswith("local:"):
            echo = self.db.execute(
                "SELECT seq FROM messages WHERE qq_id LIKE 'local:%' AND direction='me' AND body=? "
                "ORDER BY seq DESC LIMIT 1", (body,),
            ).fetchone()
            if echo:
                self.db.execute("UPDATE messages SET qq_id=?, identity=? WHERE seq=?",
                                (str(qq_id), identity, echo["seq"]))
                self.db.commit()
                return False
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages(qq_id,direction,body,created_at,handled,identity) VALUES(?,?,?,?,?,?)",
            (str(qq_id), direction, body, created_at or datetime.now().astimezone().isoformat(),
             int(direction == "me"), identity),
        )
        self.db.commit()
        inserted = cur.rowcount == 1
        if inserted:
            try:
                from message_classification import enqueue_for_database
                enqueue_for_database(self.path, cur.lastrowid)
                previous = self.db.execute(
                    "SELECT seq FROM messages WHERE seq<? ORDER BY seq DESC LIMIT 1",
                    (cur.lastrowid,),
                ).fetchone()
                if previous:
                    enqueue_for_database(self.path, previous["seq"], refresh=True)
            except Exception:
                pass
        return inserted

    def add_local_outbound(self, body: str):
        self.add_message(f"local:{uuid.uuid4().hex}", "me", body)

    def mark_handled(self, qq_id: str):
        self.db.execute("UPDATE messages SET handled=1 WHERE qq_id=?", (str(qq_id),))
        self.db.commit()

    @source_read
    def is_handled(self, qq_id: str) -> bool:
        row = self.db.execute("SELECT handled FROM messages WHERE qq_id=?", (str(qq_id),)).fetchone()
        return bool(row and row["handled"])

    @source_read
    def reply_attempt(self, message_id: str) -> dict | None:
        row = self.db.execute('SELECT * FROM reply_attempts WHERE message_id=?', (message_id,)).fetchone()
        return dict(row) if row else None

    def set_reply_attempt(self, message_id: str, state: str, error: str = '',
                          image_file: str | None = None, quote: bool = False):
        with self.db:
            self.db.execute(
                'INSERT INTO reply_attempts(message_id,state,image_file,quote,error,updated_at) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(message_id) DO UPDATE SET state=excluded.state,error=excluded.error,updated_at=excluded.updated_at',
                (message_id, state, image_file, int(quote), error[:200], datetime.now().astimezone().isoformat()))

    @source_read
    def reply_problems(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT r.*,m.created_at,m.body FROM reply_attempts r JOIN messages m ON m.qq_id=r.message_id "
            "WHERE r.state IN ('failed','uncertain','queued','processing','sending','waiting') ORDER BY m.seq DESC LIMIT 30")]

    def request_reply_retry(self, message_id: str):
        with self.db:
            result = self.db.execute(
                "UPDATE reply_attempts SET state='queued',error='',updated_at=? "
                "WHERE message_id=? AND state='failed' AND EXISTS "
                "(SELECT 1 FROM messages WHERE qq_id=? AND direction='friend' AND handled=0)",
                (datetime.now().astimezone().isoformat(), message_id, message_id))
            if result.rowcount != 1:
                raise ValueError('仅可重试明确未发送的失败消息；已发送或结果不确定的消息不能重发。')

    @source_read
    def recent_messages(self, limit: int = 40) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM messages ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @source_read
    def message_metadata(self, seq: int) -> dict:
        row = self.db.execute('SELECT value FROM metadata WHERE key=?',
                              (f'message_classification:{int(seq)}',)).fetchone()
        try:
            value = json.loads(row['value']) if row else None
        except (TypeError, ValueError):
            value = None
        valid = isinstance(value, dict) and set(value) == {'intent', 'emotion', 'tone', 'initiative'}
        return {'classification': value if valid else None}

    @source_read
    def has_outbound_body(self, body: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM messages WHERE direction='me' AND body=? LIMIT 1", (body,)
        ).fetchone() is not None

    @source_read
    def message_count(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM messages").fetchone()[0])

    def clear_messages(self) -> int:
        from persona_incremental import run_history_operation
        return run_history_operation(self, 'CLEAR_MESSAGES', self._clear_messages)

    def _clear_messages(self, operation_id=None) -> int:
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            count = self.message_count()
            self.db.execute("DELETE FROM messages")
            self.db.execute("DELETE FROM reply_attempts")
            from memory_management import meta
            meta(self, "memory_revision", self.memory_revision() + 1)
            self.db.execute("DELETE FROM metadata WHERE key IN ('context_summary','context_summary_seq','context_summary_status','context_summary_retry_at','last_generation_input','term_candidates','memory_refresh_count','memory_processed_seq','memory_retry_at','memory_batch_pending')")
            # 2026-10-13: a restart's QQ history sync must not bring the cleared messages back
            self.db.execute("INSERT INTO metadata(key,value) VALUES('history_sync_since',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            self.db.execute("DELETE FROM metadata WHERE substr(key,1,23)='message_classification:' "
                            "OR key='message_classification_backfill_seq'")
            cleared_reviews = self.db.execute(
                "DELETE FROM metadata WHERE substr(key,1,22)='classification_review:'").rowcount
            if operation_id is not None:
                self.db.execute('UPDATE source_state SET clear_epoch=clear_epoch+1,revision=revision+1 WHERE singleton=1')
                self.db.execute('UPDATE source_operations SET result=? WHERE id=?', (count, operation_id))
        if cleared_reviews or (self.path.parent / 'message_classification_review.md').is_file():
            from classification_review import _write_friend_doc
            _write_friend_doc(self.path)
        return count

    @source_read
    def inbound_count(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM messages WHERE direction='friend'").fetchone()[0])

    def set_memory(self, body: str, locked=False, expected_revision=None):
        from memory_management import replace
        replace(self, body, locked, expected_revision)

    def memory_revision(self):
        return int(self.get('memory_revision', '0'))

    def reset_memory(self):
        from memory_management import reset
        reset(self)

    def restore_memory(self):
        from memory_management import restore
        restore(self)

    @source_read
    def memory(self) -> str:
        """Active long-term memory entries, one per line. 2026-10-10: memory_entries is the only copy; the old
        memories table is only read once to import it (memory_management.initialize)."""
        return "\n".join(row["text"] for row in self.db.execute(
            "SELECT text FROM memory_entries WHERE active=1 ORDER BY rowid"))

    def record_emotions(self, user: str, own: str, source: str, intensity=None, reason=''):
        now = datetime.now().astimezone().isoformat()
        details = {'self_intensity': intensity, 'self_reason': reason} if intensity is not None or reason else {}
        state = json.dumps({"user": user, "self": own, "updated_at": time.time(), **details}, ensure_ascii=False)
        with self.db:
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('emotion_state',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (state,),
            )
            self.db.execute(
                "INSERT INTO emotion_events(user_label,self_label,source,created_at,self_intensity,self_reason) VALUES(?,?,?,?,?,?)",
                (user, own, source, now, intensity, reason),
            )

    def recent_emotions(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM emotion_events ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def initialize(self, qq_id: str, cursor: str | None, thread_id: str | None = None):
        self.set("qq_id", qq_id)
        self.set("baseline_set", "1" if cursor else "0")
        if cursor:
            self.set("last_seen_id", cursor)
        if thread_id:
            self.set("thread_id", thread_id)

    def record_inbound(self, qq_id: str, body: str, created_at: str | None = None) -> tuple[bool, int]:
        inserted = self.add_message(qq_id, "friend", body, created_at)
        generation = int(self.get("inbound_generation", "0") or 0)
        if not inserted:
            return False, generation
        generation = int(self.get("inbound_generation", "0") or 0) + 1
        self.set("inbound_generation", str(generation))
        self.set("unanswered_rounds", "0")
        self.set("proactive_paused", "0")
        self.set("reply_waiting", "0")
        self.set('reply_waiting_since', '')
        self.set('active_unanswered_rounds', '0')
        self.set('round_pause_since', '')
        # every follow-up limit starts over when the friend answers
        self.set('chat_continuation', '{}')
        return True, generation

    def complete_active_round(self, limit: int, now: datetime | None = None):
        now = now or datetime.now().astimezone()
        rounds = int(self.get('active_unanswered_rounds', '0')) + 1
        self.set('active_unanswered_rounds', str(rounds))
        if rounds >= limit and not self.get('round_pause_since'):
            self.set('round_pause_since', now.isoformat())
        return rounds

    def refresh_round_pause(self, limit: int = 2, minutes: int = 300, now: datetime | None = None):
        now = now or datetime.now().astimezone()
        if int(self.get('active_unanswered_rounds', '0')) < int(limit):
            if self.get('round_pause_since'):
                self.set('round_pause_since', '')
            return None
        since = self.get('round_pause_since')
        if not since:
            since = now.isoformat()
            self.set('round_pause_since', since)
        until = datetime.fromisoformat(since) + timedelta(minutes=int(minutes))
        if now < until:
            return until
        self.set('active_unanswered_rounds', '0')
        self.set('round_pause_since', '')
        # every follow-up limit starts over when the friend answers
        self.set('chat_continuation', '{}')
        return None

    def begin_reply_wait(self):
        self.set('reply_waiting', '1')
        self.set('reply_waiting_since', datetime.now().astimezone().isoformat())

    def refresh_reply_pause(self, minutes: int = 60, now: datetime | None = None):
        if self.get('reply_waiting', '0') != '1' and self.get('proactive_paused', '0') != '1':
            return None
        now = now or datetime.now().astimezone()
        since = self.get('reply_waiting_since')
        if not since:
            row = self.db.execute("SELECT created_at FROM messages WHERE direction='me' ORDER BY seq DESC LIMIT 1").fetchone()
            since = row['created_at'] if row else now.isoformat()
            self.set('reply_waiting_since', since)
        until = datetime.fromisoformat(since) + timedelta(minutes=int(minutes))
        if now < until:
            return until
        self.set('reply_waiting', '0')
        self.set('proactive_paused', '0')
        self.set('unanswered_rounds', '0')
        self.set('reply_waiting_since', '')
        return None

    def complete_round(self, slot_key: str) -> int:
        self.set(f"slot:{slot_key}", "sent")
        rounds = int(self.get("unanswered_rounds", "0") or 0) + 1
        self.set("unanswered_rounds", str(rounds))
        self.set("proactive_paused", "1" if rounds >= 2 else "0")
        self.begin_reply_wait()
        return rounds

    @source_read
    def state_summary(self) -> dict:
        return {
            "last_seen_id": self.get("last_seen_id"),
            "thread_id": self.get("thread_id"),
            "paused_reason": self.get("paused_reason"),
            "proactive_paused": self.get("proactive_paused", "0") == "1",
            "unanswered_rounds": int(self.get("unanswered_rounds", "0") or 0),
            "message_count": self.message_count(),
        }


def dump_json(path: Path, value):
    from source_state import profile_write
    path.parent.mkdir(parents=True, exist_ok=True)
    with profile_write(path):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

