"""Immutable, friend-scoped human feedback; no chat text or training writes."""
from collections.abc import Mapping
from datetime import datetime, timezone

from persona_store import (Conflict, FEEDBACK_STATEMENTS, PersonaStore,
                           SchemaMismatch, ScopeMismatch, Unavailable)
from persona_types import FriendScope
from source_state import source_lock


_FIELDS = ('friend_id', 'comparison_id', 'a_version', 'b_version',
           'comparison_kind', 'choice')
_KINDS = {'context_only', 'offline_reply'}
_CHOICES = {'A', 'B', 'similar', 'neither'}


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.strip() or '\0' in value:
        raise ValueError('identifiers must be nonempty text of at most 128 characters')
    return value


class PersonaFeedbackStore:
    def __init__(self, scope, *, readonly=True):
        if not isinstance(scope, FriendScope) or type(readonly) is not bool:
            raise ScopeMismatch('a verified FriendScope and explicit readonly bool are required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.readonly = readonly
        self._closed = False
        self._store = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
            readonly=readonly, project_root=self.scope.project_root)
        try:
            self._check_schema()
        except Exception:
            self._store.close()
            raise

    def _check_schema(self):
        for name, sql in zip(('persona_feedback', 'persona_feedback_recent'), FEEDBACK_STATEMENTS):
            row = self._store.db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            if row is None or row[0] != sql:
                raise SchemaMismatch('feedback table or index differs from the V18 migration')

    def _check_scope(self):
        if self._closed:
            raise Unavailable('feedback store is closed')
        FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir, allowed_pack_versions=self.scope.allowed_pack_versions)
        self._store._check(self._store.db)
        self._check_schema()

    def _get(self, comparison_id):
        row = self._store.db.execute(
            'SELECT * FROM persona_feedback WHERE friend_id=? AND comparison_id=?',
            (self.scope.friend_id, comparison_id)).fetchone()
        return dict(row) if row is not None else None

    def save(self, entry):
        if self.readonly:
            raise Unavailable('feedback store is read-only')
        if not isinstance(entry, Mapping) or set(entry) != set(_FIELDS):
            raise ValueError('feedback accepts only the six comparison metadata fields')
        entry = dict(entry)
        if entry['friend_id'] != self.scope.friend_id:
            raise ScopeMismatch('feedback belongs to another friend')
        for name in ('comparison_id', 'a_version', 'b_version'):
            _identifier(entry[name])
        if (not isinstance(entry['comparison_kind'], str) or entry['comparison_kind'] not in _KINDS or
                not isinstance(entry['choice'], str) or entry['choice'] not in _CHOICES):
            raise ValueError('unknown comparison kind or feedback choice')
        with source_lock(self.scope.friend_dir):
            self._check_scope()
            current = self._get(entry['comparison_id'])
            if current is not None:
                if any(current[name] != entry[name] for name in _FIELDS):
                    raise Conflict('comparison already has different human feedback')
                return current
            db = self._store.db
            db.execute('BEGIN IMMEDIATE')
            try:
                created_at = datetime.now(timezone.utc).isoformat()
                db.execute('INSERT INTO persona_feedback VALUES(?,?,?,?,?,?,?,?)',
                    tuple(entry[name] for name in _FIELDS) + ('HUMAN_SUBJECTIVE', created_at))
                saved = self._get(entry['comparison_id'])
                db.execute('COMMIT')
                return saved
            except Exception:
                db.execute('ROLLBACK')
                raise

    def get(self, comparison_id):
        self._check_scope()
        return self._get(_identifier(comparison_id))

    def list_recent(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('feedback limit must be between 1 and 500')
        self._check_scope()
        return tuple(dict(row) for row in self._store.db.execute(
            'SELECT * FROM persona_feedback WHERE friend_id=? '
            'ORDER BY created_at DESC,comparison_id LIMIT ?', (self.scope.friend_id, limit)))

    def close(self):
        if not self._closed:
            self._store.close()
            self._closed = True
