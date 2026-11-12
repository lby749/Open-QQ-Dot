"""Friend-scoped entities, explicit aliases, and source-backed relations."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import hashlib
import json
import math
from datetime import datetime, timezone

from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, PrivacyView,
                           SourceOrigin, SourceRef)


_TYPES = {'Person', 'Music', 'Game', 'Movie', 'Model', 'Product', 'Place', 'Topic', 'Event', 'Concept'}


def _refs(value):
    if isinstance(value, (str, bytes, dict)):
        raise InvalidSource('source_refs must be a bounded sequence')
    refs = tuple(value)
    if not refs or len(refs) > 100 or any(not isinstance(ref, SourceRef) for ref in refs):
        raise InvalidSource('source_refs requires 1..100 SourceRef values')
    return refs


def _ref_json(ref):
    return {key: (value.value if hasattr(value, 'value') else value)
            for key in SourceRef.__dataclass_fields__
            for value in (getattr(ref, key),)}


def _decode_refs(value):
    try:
        return tuple(SourceRef(**item) for item in json.loads(value))
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidSource('stored source locator is invalid') from exc


def _source_type(refs):
    origins = {ref.origin for ref in refs}
    if origins == {SourceOrigin.MANUAL}:
        return 'MANUAL'
    if origins == {SourceOrigin.REAL_RELATIONSHIP}:
        return 'REAL_CHAT'
    raise InvalidSource('entity evidence cannot mix or infer source types')


def _hash(value, refs):
    clean = [{k: v for k, v in _ref_json(ref).items() if k != 'recorded_at'} for ref in refs]
    raw = json.dumps([value, clean], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _time(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSource('time must be ISO8601 or None')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InvalidSource('time must be ISO8601') from exc


def _expired(value, now):
    parsed = _time(value)
    if parsed is None:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.date() < now.date()
    return parsed.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


class KnowledgeEntityStore:
    def __init__(self, scope, readonly=False):
        if not isinstance(scope, FriendScope) or type(readonly) is not bool:
            raise ScopeMismatch('verified FriendScope and readonly bool required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
                                      friend_dir=scope.friend_dir,
                                      allowed_pack_versions=scope.allowed_pack_versions)
        self.readonly = readonly
        self.store = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
                                  readonly=readonly, project_root=self.scope.project_root)
        self.closed = False

    def close(self):
        if not self.closed:
            self.store.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check(self):
        if self.closed:
            raise Unavailable('entity store is closed')
        self.scope = FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
                                      friend_dir=self.scope.friend_dir,
                                      allowed_pack_versions=self.scope.allowed_pack_versions)
        self.store._check(self.store.db)

    def _sources(self, refs, source_type, record_id):
        if source_type == 'REAL_CHAT':
            from interest_entries import InterestStore
            verifier = InterestStore(self.scope, readonly=True)
            try:
                return verifier._verify_sources(refs, source_type='REAL_CHAT', entry_id=record_id)
            finally:
                verifier.close()
        if source_type != 'MANUAL':
            raise InvalidSource('entity facts require REAL_CHAT or MANUAL evidence')
        for ref in refs:
            if (ref.data_scope != DataScope.PRIVATE_FRIEND or ref.friend_id != self.scope.friend_id or
                    ref.origin != SourceOrigin.MANUAL or ref.speaker_id != 'operator' or
                    ref.message_seq is not None or ref.event_time is not None or
                    ref.dataset_id != 'manual:knowledge_entities' or
                    ref.import_id != record_id or ref.record_id != record_id):
                raise ScopeMismatch('manual entity requires current-friend operator source')
        return None, None

    def _manual_relation_sources(self, refs, relation_id):
        for ref in refs:
            if (ref.data_scope != DataScope.PRIVATE_FRIEND or ref.friend_id != self.scope.friend_id or
                    ref.origin != SourceOrigin.MANUAL or ref.speaker_id != 'operator' or
                    ref.dataset_id != 'manual:knowledge_relations' or ref.import_id != relation_id or
                    ref.record_id != relation_id or ref.message_seq is not None):
                raise ScopeMismatch('manual relation requires operator confirmation locator')

    def _entity_current(self, row):
        refs = _decode_refs(row['source_refs_json'])
        kind = _source_type(refs)
        self._sources(refs, kind, row['id'])
        aliases = json.loads(row['aliases_json'])
        digest = _hash([row['id'], row['name'], row['entity_type'], aliases, kind,
                        row['confidence']], refs)
        return digest == row['source_hash']

    def upsert_entity(self, entry, expected_revision=0, *, manual=False):
        source_token = snapshot(self.scope)
        self._check()
        if self.readonly:
            raise Unavailable('entity store is read-only')
        fields = {'id', 'name', 'entity_type', 'aliases', 'source_type', 'source_refs', 'confidence'}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise InvalidSource('entity fields differ from contract')
        if any(not isinstance(entry[k], str) or not entry[k].strip() for k in ('id', 'name')):
            raise InvalidSource('entity id and name must be nonempty')
        if entry['entity_type'] not in _TYPES:
            raise InvalidSource('unknown entity_type')
        aliases = entry['aliases']
        if (not isinstance(aliases, (list, tuple)) or len(aliases) > 100 or
                any(not isinstance(a, str) or not a.strip() for a in aliases) or
                len(set(aliases)) != len(aliases)):
            raise InvalidSource('aliases must be unique explicit strings')
        if aliases and (not manual or entry['source_type'] != 'MANUAL'):
            raise InvalidSource('aliases require explicit manual confirmation')
        if type(entry['confidence']) not in (int, float) or not math.isfinite(entry['confidence']) or not 0 <= entry['confidence'] <= 1:
            raise InvalidSource('confidence must be finite in 0..1')
        if type(expected_revision) is not int or expected_revision < 0 or type(manual) is not bool:
            raise InvalidSource('invalid revision/manual flag')
        refs = _refs(entry['source_refs'])
        if manual != (entry['source_type'] == 'MANUAL'):
            raise InvalidSource('manual source requires manual=True')
        self._sources(refs, entry['source_type'], entry['id'])
        encoded = json.dumps(list(aliases), ensure_ascii=False)
        digest = _hash([entry['id'], entry['name'], entry['entity_type'], list(aliases),
                        entry['source_type'], entry['confidence']], refs)
        db = self.store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._check()
            old = db.execute('SELECT revision,source_hash FROM knowledge_entities WHERE friend_id=? AND id=?',
                             (self.scope.friend_id, entry['id'])).fetchone()
            revision = old['revision'] if old else 0
            if revision != expected_revision:
                raise Conflict('entity revision changed')
            if old and old['source_hash'] == digest:
                db.execute('COMMIT')
                return revision
            revision += 1
            now = datetime.now().astimezone().isoformat()
            db.execute('INSERT INTO knowledge_entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(friend_id,id) DO UPDATE SET name=excluded.name,entity_type=excluded.entity_type,'
                       'aliases_json=excluded.aliases_json,source_refs_json=excluded.source_refs_json,'
                       'source_hash=excluded.source_hash,confidence=excluded.confidence,valid=1,'
                       'revision=excluded.revision,updated_at=excluded.updated_at',
                       (self.scope.friend_id, entry['id'], entry['name'], entry['entity_type'], encoded,
                        json.dumps([_ref_json(r) for r in refs], ensure_ascii=False), digest,
                        entry['confidence'], 1, revision, now if not old else db.execute(
                            'SELECT created_at FROM knowledge_entities WHERE friend_id=? AND id=?',
                            (self.scope.friend_id, entry['id'])).fetchone()['created_at'], now))
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (self.scope.friend_id,))
            db.execute('COMMIT')
            return revision
        except Exception:
            db.execute('ROLLBACK')
            raise

    def upsert_relation(self, entry, expected_revision=0, *, manual=False):
        source_token = snapshot(self.scope)
        self._check()
        if self.readonly:
            raise Unavailable('entity store is read-only')
        fields = {'id', 'source_entity', 'relation', 'target_entity', 'confidence', 'strength',
                  'valid_from', 'valid_to', 'source_type', 'source_refs'}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise InvalidSource('relation fields differ from contract')
        for key in ('id', 'source_entity', 'relation', 'target_entity'):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise InvalidSource(key + ' must be nonempty')
        for key in ('confidence', 'strength'):
            if type(entry[key]) not in (int, float) or not math.isfinite(entry[key]) or not 0 <= entry[key] <= 1:
                raise InvalidSource(key + ' must be finite in 0..1')
        start, end = _time(entry['valid_from']), _time(entry['valid_to'])
        refs = _refs(entry['source_refs'])
        if manual != (entry['source_type'] == 'MANUAL'):
            raise InvalidSource('manual source requires manual=True')
        if entry['source_type'] == 'REAL_CHAT':
            first, last = self._sources(refs, 'REAL_CHAT', entry['id'])
        elif entry['source_type'] == 'MANUAL':
            self._manual_relation_sources(refs, entry['id'])
            first = last = None
        else:
            raise InvalidSource('relations require REAL_CHAT or MANUAL evidence')
        now = datetime.now().astimezone().isoformat()
        digest = _hash([entry[k] for k in sorted(fields - {'source_refs'})] + [first, last], refs)
        db = self.store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._check()
            members = db.execute('SELECT id,valid FROM knowledge_entities WHERE friend_id=? AND id IN (?,?)',
                                 (self.scope.friend_id, entry['source_entity'], entry['target_entity'])).fetchall()
            if len(members) != len({entry['source_entity'], entry['target_entity']}) or any(not r['valid'] for r in members):
                raise ScopeMismatch('relation endpoints must be valid entities in this friend scope')
            old = db.execute('SELECT revision,source_hash,created_at FROM knowledge_relations WHERE friend_id=? AND id=?',
                             (self.scope.friend_id, entry['id'])).fetchone()
            revision = old['revision'] if old else 0
            if revision != expected_revision:
                raise Conflict('relation revision changed')
            if old and old['source_hash'] == digest:
                db.execute('COMMIT')
                return revision
            revision += 1
            db.execute('INSERT INTO knowledge_relations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(friend_id,id) DO UPDATE SET source_entity=excluded.source_entity,'
                       'relation=excluded.relation,target_entity=excluded.target_entity,confidence=excluded.confidence,'
                       'strength=excluded.strength,first_seen=excluded.first_seen,last_seen=excluded.last_seen,'
                       'valid_from=excluded.valid_from,valid_to=excluded.valid_to,source_refs_json=excluded.source_refs_json,'
                       'source_hash=excluded.source_hash,valid=1,revision=excluded.revision,updated_at=excluded.updated_at',
                       (self.scope.friend_id, entry['id'], entry['source_entity'], entry['relation'],
                        entry['target_entity'], entry['confidence'], entry['strength'], first, last,
                        entry['valid_from'], entry['valid_to'],
                        json.dumps([_ref_json(r) for r in refs], ensure_ascii=False), digest, 1, revision,
                        old['created_at'] if old else now, now))
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (self.scope.friend_id,))
            db.execute('COMMIT')
            return revision
        except Exception:
            db.execute('ROLLBACK')
            raise

    @source_read
    def resolve(self, name):
        self._check()
        if not isinstance(name, str) or not name.strip():
            raise InvalidSource('name must be nonempty')
        rows = self.store.db.execute(
            'SELECT e.* FROM knowledge_entities e WHERE e.friend_id=? AND e.valid=1 AND '
            '(e.name=? OR EXISTS(SELECT 1 FROM json_each(e.aliases_json) a WHERE a.value=?)) '
            'ORDER BY e.id LIMIT 501', (self.scope.friend_id, name, name)).fetchall()
        if len(rows) > 500:
            return None
        found = []
        for row in rows:
            try:
                current = self._entity_current(row)
            except (InvalidSource, ScopeMismatch, Unavailable):
                current = False
            if current:
                found.append(dict(row))
                if len(found) > 1:
                    return None
        return found[0] if len(found) == 1 else None

    @source_read
    def relations(self, now, limit=100):
        self._check()
        parsed_now = _time(now)
        if parsed_now is None or parsed_now.tzinfo is None or type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidSource('aware now and limit 1..100 required')
        rows = self.store.db.execute('SELECT * FROM knowledge_relations WHERE friend_id=? AND valid=1 '
                                     'ORDER BY id LIMIT 500', (self.scope.friend_id,)).fetchall()
        result = []
        for row in rows:
            if _expired(row['valid_to'], parsed_now):
                continue
            start = _time(row['valid_from'])
            if start and ((start.tzinfo is not None and start.astimezone(timezone.utc) > parsed_now.astimezone(timezone.utc)) or
                          (start.tzinfo is None and start.date() > parsed_now.date())):
                continue
            refs = _decode_refs(row['source_refs_json'])
            try:
                kind = _source_type(refs)
                if kind == 'REAL_CHAT':
                    first, last = self._sources(refs, kind, row['id'])
                else:
                    self._manual_relation_sources(refs, row['id'])
                    first = last = None
                members = self.store.db.execute(
                    'SELECT * FROM knowledge_entities WHERE friend_id=? AND id IN (?,?)',
                    (self.scope.friend_id, row['source_entity'], row['target_entity'])).fetchall()
                if len(members) != len({row['source_entity'], row['target_entity']}) or any(
                        not member['valid'] or not self._entity_current(member) for member in members):
                    continue
                content = {key: row[key] for key in (
                    'id', 'source_entity', 'relation', 'target_entity', 'confidence', 'strength',
                    'valid_from', 'valid_to')}
                content['source_type'] = kind
                digest_value = [content[key] for key in sorted(
                    {'id', 'source_entity', 'relation', 'target_entity', 'confidence', 'strength',
                     'valid_from', 'valid_to', 'source_type'})] + [first, last]
                if _hash(digest_value, refs) != row['source_hash']:
                    continue
            except (InvalidSource, ScopeMismatch, Unavailable):
                continue
            result.append(dict(row))
            if len(result) == limit:
                break
        return result
