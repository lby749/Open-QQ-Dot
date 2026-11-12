"""Per-friend, source-checked interest records in the persona sidecar."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import json
import math
from datetime import datetime, timezone

from persona_snapshot import ReadOnlyLegacyReader
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, PrivacyView,
                           SourceOrigin, SourceRef)


_FIELDS = {'id', 'topic', 'content', 'category', 'strength', 'confidence',
           'interest_type', 'source_type', 'source_refs', 'first_seen',
           'last_seen', 'locked', 'valid_to'}
_TYPES = {'stable', 'recent', 'temporary', 'one_off', 'manual'}
_SOURCE_TYPES = {'REAL_CHAT', 'MANUAL'}


def _time(value, name, *, aware=False):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidSource(name + ' must be an ISO8601 string or None')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InvalidSource(name + ' must be ISO8601') from exc
    if aware and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise InvalidSource(name + ' needs a timezone offset')
    return parsed


def _source_dict(source):
    return {name: (getattr(source, name).value if hasattr(getattr(source, name), 'value')
                   else getattr(source, name)) for name in SourceRef.__dataclass_fields__}


def _source_refs(value):
    if isinstance(value, (str, bytes, dict)):
        raise InvalidSource('source_refs must be a nonempty list of SourceRef values')
    try:
        refs = tuple(value)
    except TypeError as exc:
        raise InvalidSource('source_refs must be a nonempty list of SourceRef values') from exc
    if not refs or any(not isinstance(ref, SourceRef) for ref in refs):
        raise InvalidSource('source_refs must be a nonempty list of SourceRef values')
    if len(refs) > 100 or len(set(refs)) != len(refs):
        raise InvalidSource('source_refs must be unique and bounded to 100')
    return refs


def _expired(valid_to, now):
    if valid_to is None:
        return False
    expiry = _time(valid_to, 'valid_to')
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return expiry.date() < now.date()
    return expiry.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


class InterestStore:
    def __init__(self, scope, readonly=False):
        if not isinstance(scope, FriendScope) or type(readonly) is not bool:
            raise ScopeMismatch('a verified FriendScope and explicit readonly bool are required')
        self.scope = FriendScope.load(
            scope.friend_id, project_root=scope.project_root, friend_dir=scope.friend_dir,
            allowed_pack_versions=scope.allowed_pack_versions)
        self.readonly = readonly
        self._store = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
                                   readonly=readonly, project_root=self.scope.project_root)
        self._closed = False

    def close(self):
        if not self._closed:
            self._store.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_scope(self):
        if self._closed:
            raise Unavailable('interest store is closed')
        self.scope = FriendScope.load(
            self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        self._store._check(self._store.db)

    def _verify_sources(self, source_refs, *, source_type, entry_id):
        times = []
        if source_type == 'MANUAL':
            for ref in source_refs:
                if (ref.data_scope != DataScope.PRIVATE_FRIEND or
                        ref.friend_id != self.scope.friend_id or ref.origin != SourceOrigin.MANUAL or
                        ref.message_seq is not None or ref.speaker_id != 'operator' or
                        ref.dataset_id != 'manual:interest_entries' or
                        ref.import_id != entry_id or ref.record_id != entry_id or
                        ref.event_time is not None):
                    raise ScopeMismatch('manual interest needs its explicit current-friend operator locator')
            return None, None

        if source_type != 'REAL_CHAT':
            raise InvalidSource('unsupported interest source_type')
        history = None
        try:
            for ref in source_refs:
                if (ref.data_scope != DataScope.PRIVATE_FRIEND or
                        ref.friend_id != self.scope.friend_id or
                        ref.origin != SourceOrigin.REAL_RELATIONSHIP or
                        ref.speaker_id != self.scope.friend_id):
                    raise ScopeMismatch('real-chat interest source must belong to this friend')
                imported = all((ref.dataset_id, ref.import_id, ref.record_id))
                if imported:
                    row = self._store.db.execute(
                        'SELECT m.direction,m.speaker_id,m.speaker_role,m.source_origin,m.event_time,'
                        'i.data_scope AS import_scope,i.status,r.data_scope AS registry_scope,r.active '
                        'FROM imported_messages m JOIN dataset_imports i ON '
                        'i.friend_id=m.friend_id AND i.import_id=m.import_id AND i.dataset_id=m.dataset_id '
                        'JOIN dataset_registry r ON r.friend_id=m.friend_id AND r.dataset_id=m.dataset_id '
                        'WHERE m.friend_id=? AND m.dataset_id=? AND m.import_id=? '
                        'AND m.record_id=? AND m.message_seq=?',
                        (self.scope.friend_id, ref.dataset_id, ref.import_id,
                         ref.record_id, ref.message_seq)).fetchone()
                    if (row is None or row['import_scope'] != DataScope.PRIVATE_FRIEND.value or
                            row['registry_scope'] != DataScope.PRIVATE_FRIEND.value or
                            row['active'] != 1 or row['status'] != 'COMPLETED' or
                            row['direction'] != 'friend' or
                            row['source_origin'] != SourceOrigin.REAL_RELATIONSHIP.value or
                            row['speaker_role'] not in ('friend', 'human', 'target') or
                            row['speaker_id'] not in (None, self.scope.friend_id)):
                        raise ScopeMismatch('imported source is not a completed friend-human inbound record')
                    event_time = row['event_time']
                else:
                    if ref.message_seq is None:
                        raise InvalidSource('real-chat source requires a verifiable message locator')
                    if history is None:
                        history = ReadOnlyLegacyReader(self.scope)
                    row = history._db.execute(
                        'SELECT direction,created_at FROM messages WHERE seq=?',
                        (ref.message_seq,)).fetchone()
                    if row is None or row['direction'] != 'friend':
                        raise ScopeMismatch('history source is not a friend inbound message')
                    event_time = row['created_at']
                if ref.event_time is not None and ref.event_time != event_time:
                    raise ScopeMismatch('source timestamp differs from the current record')
                if event_time is not None:
                    _time(event_time, 'source event_time')
                    times.append(event_time)
        finally:
            if history is not None:
                history.close()
        parsed = [_time(value, 'source event_time') for value in times]
        if not parsed:
            return None, None
        aware = [item.tzinfo is not None and item.utcoffset() is not None for item in parsed]
        if any(aware) and not all(aware):
            return None, None
        if all(aware):
            parsed = [item.astimezone(timezone.utc) for item in parsed]
        return min(parsed).isoformat(), max(parsed).isoformat()

    def upsert(self, entry, expected_revision=0, manual=False):
        source_token = snapshot(self.scope)
        self._check_scope()
        if self.readonly:
            raise Unavailable('interest store is read-only')
        if type(manual) is not bool or type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('manual and expected_revision are invalid')
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise InvalidSource('interest entry fields differ from the frozen contract')
        for key in ('id', 'topic', 'content', 'category'):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise InvalidSource(key + ' must be a nonempty string')
        for key in ('strength', 'confidence'):
            value = entry[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise InvalidSource(key + ' must be finite in 0..1')
        if (not isinstance(entry['interest_type'], str) or
                entry['interest_type'] not in _TYPES or
                not isinstance(entry['source_type'], str) or
                entry['source_type'] not in _SOURCE_TYPES):
            raise InvalidSource('invalid interest type or source_type')
        if type(entry['locked']) is not bool:
            raise InvalidSource('locked must be bool')
        if manual:
            if (entry['source_type'] != 'MANUAL' or entry['interest_type'] != 'manual' or
                    not entry['locked']):
                raise InvalidSource('manual writes must be MANUAL, manual type and locked')
        elif (entry['source_type'] != 'REAL_CHAT' or entry['interest_type'] == 'manual' or
              entry['locked']):
            raise InvalidSource('automatic writes must be unlocked REAL_CHAT interests')
        _time(entry['first_seen'], 'first_seen')
        _time(entry['last_seen'], 'last_seen')
        _time(entry['valid_to'], 'valid_to')
        refs = _source_refs(entry['source_refs'])
        first_seen, last_seen = self._verify_sources(
            refs, source_type=entry['source_type'], entry_id=entry['id'])
        if manual and (entry['first_seen'] is not None or entry['last_seen'] is not None):
            raise InvalidSource('manual interest has no real-chat first_seen/last_seen')
        if not manual and (entry['first_seen'] != first_seen or entry['last_seen'] != last_seen):
            raise InvalidSource('first_seen/last_seen must match verified source event times')
        now = datetime.now().astimezone().isoformat()
        db = self._store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            current = db.execute(
                'SELECT revision,locked,created_at FROM interest_entries WHERE friend_id=? AND id=?',
                (self.scope.friend_id, entry['id'])).fetchone()
            current_revision = current['revision'] if current else 0
            if current_revision != expected_revision:
                raise Conflict('interest row revision changed')
            if current and current['locked'] and not manual:
                raise Conflict('locked interest requires explicit manual override')
            revision = current_revision + 1
            db.execute(
                'INSERT INTO interest_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(friend_id,id) DO UPDATE SET topic=excluded.topic,content=excluded.content,'
                'category=excluded.category,strength=excluded.strength,confidence=excluded.confidence,'
                'interest_type=excluded.interest_type,source_type=excluded.source_type,'
                'source_refs_json=excluded.source_refs_json,first_seen=excluded.first_seen,'
                'last_seen=excluded.last_seen,valid_to=excluded.valid_to,locked=excluded.locked,'
                'valid=1,revision=excluded.revision,updated_at=excluded.updated_at',
                (self.scope.friend_id, entry['id'], entry['topic'].strip(), entry['content'].strip(),
                 entry['category'].strip(), entry['strength'], entry['confidence'],
                 entry['interest_type'], entry['source_type'],
                 json.dumps([_source_dict(ref) for ref in refs], ensure_ascii=False),
                 first_seen, last_seen, entry['valid_to'], int(entry['locked']), 1, revision,
                 current['created_at'] if current else now, now))
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                       (self.scope.friend_id,))
            db.execute('COMMIT')
            return revision
        except Exception:
            db.execute('ROLLBACK')
            raise

    def tombstone(self, entry_id, expected_revision, manual=False):
        self._check_scope()
        if self.readonly:
            raise Unavailable('interest store is read-only')
        if (not isinstance(entry_id, str) or not entry_id.strip() or
                type(expected_revision) is not int or expected_revision < 1 or
                type(manual) is not bool):
            raise InvalidSource('invalid tombstone identity/revision/manual')
        db = self._store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT revision,locked FROM interest_entries '
                             'WHERE friend_id=? AND id=?',
                             (self.scope.friend_id, entry_id)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise Conflict('interest row revision changed')
            if row['locked'] and not manual:
                raise Conflict('locked interest requires explicit manual override')
            db.execute('UPDATE interest_entries SET valid=0,revision=revision+1,updated_at=? '
                       'WHERE friend_id=? AND id=?',
                       (datetime.now().astimezone().isoformat(), self.scope.friend_id, entry_id))
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                       (self.scope.friend_id,))
            db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            db.execute('ROLLBACK')
            raise

    @source_read
    def entries(self, now, limit=100):
        self._check_scope()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidSource('limit must be 1..100')
        parsed_now = _time(now, 'now', aware=True)
        result = []
        rows = self._store.db.execute(
            'SELECT * FROM interest_entries WHERE friend_id=? AND valid=1 '
            'ORDER BY CASE WHEN last_seen IS NULL THEN 1 ELSE 0 END,last_seen DESC,id LIMIT 500',
            (self.scope.friend_id,)).fetchall()
        for row in rows:
            if _expired(row['valid_to'], parsed_now):
                continue
            try:
                raw_refs = json.loads(row['source_refs_json'])
                refs = tuple(SourceRef(**ref) for ref in raw_refs)
                first_seen, last_seen = self._verify_sources(
                    refs, source_type=row['source_type'], entry_id=row['id'])
            except (TypeError, ValueError, KeyError, ScopeMismatch, Unavailable):
                continue
            if (first_seen, last_seen) != (row['first_seen'], row['last_seen']):
                continue
            item = dict(row)
            item.pop('friend_id', None)
            item.pop('source_refs_json', None)
            item['source_refs'] = refs
            item['scope_revision'] = self._store.revision
            item['recency_days'] = None
            if row['last_seen'] is not None:
                stamp = _time(row['last_seen'], 'last_seen')
                if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                    item['recency_days'] = max(
                        0.0, (parsed_now.astimezone(timezone.utc) -
                              stamp.astimezone(timezone.utc)).total_seconds() / 86400)
            result.append(item)
            if len(result) >= limit:
                break
        return result
