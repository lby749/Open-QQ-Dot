"""Read-only legacy data and isolated, transport-free preview copies."""
from source_state import source_read, snapshot, require_snapshot
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import FriendScope, InvalidSource


TABLES = frozenset(('messages', 'metadata', 'memories', 'memory_entries',
                   'memory_versions', 'emotion_events', 'reply_attempts',
                   'topic_library', 'usage_calls'))


def _scope(scope):
    if not isinstance(scope, FriendScope):
        raise ScopeMismatch('a verified FriendScope is required')
    return FriendScope.load(scope.friend_id, project_root=scope.project_root,
                            friend_dir=scope.friend_dir,
                            allowed_pack_versions=scope.allowed_pack_versions)


def _limit(value):
    if type(value) is not int or not 1 <= value <= 500:
        raise InvalidSource('page limit must be 1..500')


def _history_path(scope):
    path = scope.friend_dir / 'history.sqlite3'
    if path.resolve() != path:
        raise ScopeMismatch('history file cannot redirect')
    if not path.is_file():
        raise Unavailable('legacy history is absent')
    if path.stat().st_nlink != 1:
        raise ScopeMismatch('history file cannot share a hardlink')
    with path.open('rb') as source:
        header = source.read(20)
    if not header.startswith(b'SQLite format 3\x00'):
        raise Unavailable('legacy history is not a SQLite database')
    # P1 rejects WAL before SQLite can create/update SHM. Never ignore a live WAL.
    if header[18:20] != b'\x01\x01' or any(
            Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise Unavailable('legacy history requires a stable DELETE journal for preview')
    return path


class ReadOnlyLegacyReader:
    def __init__(self, scope):
        self.scope = _scope(scope)
        self.path = _history_path(self.scope)
        self._db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                   timeout=2, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA query_only=ON')
            self._db.execute('PRAGMA trusted_schema=OFF')
            self._tables = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'metadata' not in self._tables:
                raise Unavailable('legacy identity metadata is absent')
            if self.get('qq_id') != self.scope.friend_id:
                raise ScopeMismatch('history identity differs from its friend')
        except Exception:
            self._db.close()
            raise

    def _table(self, table):
        if not isinstance(table, str) or table not in TABLES:
            raise InvalidSource('unsupported legacy table')
        if table not in self._tables:
            raise Unavailable('legacy table is absent: ' + table)

    @source_read
    def get(self, key, default=None):
        if not isinstance(key, str):
            raise InvalidSource('metadata key must be a string')
        self._table('metadata')
        row = self._db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    @source_read
    def page(self, table, *, after_rowid=0, limit=100):
        self._table(table)
        _limit(limit)
        if type(after_rowid) is not int or after_rowid < 0:
            raise InvalidSource('row cursor must be a nonnegative integer')
        return [dict(row) for row in self._db.execute(
            f'SELECT rowid AS snapshot_rowid,* FROM {table} '
            'WHERE rowid>? ORDER BY rowid LIMIT ?', (after_rowid, limit))]

    @source_read
    def recent_messages(self, limit=40):
        self._table('messages')
        _limit(limit)
        return [dict(row) for row in reversed(self._db.execute(
            'SELECT * FROM messages ORDER BY seq DESC LIMIT ?', (limit,)).fetchall())]

    @source_read
    def memory(self):
        self._table('memories')
        row = self._db.execute('SELECT body FROM memories ORDER BY seq DESC LIMIT 1').fetchone()
        return row[0] if row else ''

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _identity_bytes(scope):
    return {name: path.read_bytes() for name, path in (
        ('registry', scope.project_root / 'friends.json'),
        ('profile', scope.friend_dir / 'friend.json'))}


def _preview_profile(profile):
    value = json.loads(json.dumps(profile))
    for key in ('auto_reply', 'bridge_enabled',
                'daily_research_enabled', 'persona_enabled', 'hybrid_enabled',
                'sticker_metadata_enabled'):
        value[key] = False
    value['proactive'] = {**value.get('proactive', {}), 'enabled': False, 'time_aware': False}
    value['scheduled_care'] = {**value.get('scheduled_care', {}), 'enabled': False}
    value['feature_flags'] = {**value.get('feature_flags', {}), 'persona_enabled': False,
                              'hybrid_enabled': False, 'sticker_metadata_enabled': False}
    value['preview_only'] = True
    return value


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


@dataclass(frozen=True)
class PreviewSnapshot:
    scope: FriendScope
    source_friend_id: str
    source_profile_sha256: str

    @classmethod
    def create(cls, scope, *, timeout_seconds=10):
        scope = _scope(scope)
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
            raise InvalidSource('backup timeout must be greater than 0 and at most 60 seconds')
        before = _identity_bytes(scope)
        try:
            source_token = snapshot(scope)
        except sqlite3.OperationalError as exc:
            raise TimeoutError('legacy source snapshot is busy') from exc
        # Only a fresh directory under this project's build tree is accepted.
        base = scope.project_root / 'build' / 'persona-previews'
        if base.resolve() != base:
            raise ScopeMismatch('preview directory cannot redirect')
        root = base / uuid.uuid4().hex
        directory = root / 'friends' / scope.friend_id
        started = time.monotonic()

        def progress(status, remaining, total):
            if time.monotonic() - started >= timeout_seconds:
                raise TimeoutError('legacy backup exceeded its time budget')

        with ReadOnlyLegacyReader(scope) as source:
            directory.mkdir(parents=True, exist_ok=False)
            # No transport/model/GUI starts; only this destination is writable.
            source._db.execute('PRAGMA busy_timeout=0')
            with closing(sqlite3.connect(directory / 'history.sqlite3')) as destination:
                source._db.backup(destination, pages=128, progress=progress, sleep=0.05)
                identity = destination.execute('SELECT value FROM metadata WHERE key=?',
                                               ('qq_id',)).fetchone()
                if identity is None or identity[0] != scope.friend_id:
                    raise ScopeMismatch('backup identity differs from its friend')
                with destination:
                    destination.execute("DELETE FROM metadata WHERE key='thread_id'")
        if _identity_bytes(scope) != before:
            raise Conflict('friend profile or registry changed during preview backup')
        require_snapshot(scope, source_token)
        _scope(scope)
        profile = _preview_profile(json.loads(before['profile']))
        _json(directory / 'friend.json', profile)
        _json(root / 'friends.json', {'friends': {scope.friend_id: {
            'qq_id': scope.friend_id, 'directory': str(directory), 'self_id': '',
            'preview_only': True}}})
        isolated = FriendScope.load(scope.friend_id, project_root=root,
                                    allowed_pack_versions=scope.allowed_pack_versions)
        profile_hash = hashlib.sha256(before['profile']).hexdigest()
        _json(root / 'preview.json', {'state': 'COMPLETED', 'friend_id': scope.friend_id,
                                     'source_profile_sha256': profile_hash,
                                     'capabilities': ['legacy_read', 'isolated_write']})
        return cls(isolated, scope.friend_id, profile_hash)
