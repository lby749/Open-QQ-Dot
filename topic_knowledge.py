"""Per-friend topic knowledge with a bounded, read-only legacy mirror."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timezone

from persona_snapshot import ReadOnlyLegacyReader
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, PrivacyView,
                           SourceOrigin, SourceRef)


_FIELDS = {'id', 'topic', 'subtopic', 'title', 'content', 'summary', 'keywords',
           'source_type', 'source_url', 'source_refs', 'valid_from', 'valid_to',
           'confidence', 'quality_score', 'time_sensitivity', 'last_verified_at',
           'embedding_model', 'embedding_version'}
_SOURCE_TYPES = {'REAL_CHAT', 'PUBLIC_RESEARCH', 'MANUAL', 'MODEL_SUMMARY',
                 'PUBLIC_DATASET', 'SYNTHETIC'}
_SENSITIVITY = {'STABLE', 'FAST_CHANGING', 'UNKNOWN'}
_LEGACY_DATASETS = {'legacy:topic_library', 'legacy:daily_research'}


def _time(value, name, aware=False):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidSource(name + ' must be ISO8601 or None')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InvalidSource(name + ' must be ISO8601') from exc
    if aware and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise InvalidSource(name + ' must include timezone')
    return parsed


def _source_refs(value):
    if isinstance(value, (str, bytes, dict)):
        raise InvalidSource('source_refs must be a bounded sequence')
    try:
        refs = tuple(value)
    except TypeError as exc:
        raise InvalidSource('source_refs must be a bounded sequence') from exc
    if not refs or len(refs) > 100 or any(not isinstance(ref, SourceRef) for ref in refs):
        raise InvalidSource('source_refs requires 1..100 SourceRef values')
    if len(set(refs)) != len(refs):
        raise InvalidSource('source_refs must be unique')
    return refs


def _ref_dict(ref):
    return {key: (value.value if hasattr(value, 'value') else value)
            for key in SourceRef.__dataclass_fields__
            for value in (getattr(ref, key),)}


def _decode_refs(value):
    try:
        raw = json.loads(value)
        return tuple(SourceRef(**ref) for ref in raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidSource('stored topic source locator is invalid') from exc


def _source_hash(entry, refs):
    value = {key: entry[key] for key in (
        'id', 'topic', 'subtopic', 'title', 'content', 'summary', 'keywords',
        'source_type', 'source_url', 'valid_from', 'valid_to', 'confidence',
        'quality_score', 'time_sensitivity', 'last_verified_at',
        'embedding_model', 'embedding_version')}
    value['source_refs'] = [{key: val for key, val in _ref_dict(ref).items()
                             if key != 'recorded_at'} for ref in refs]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _expired(value, now):
    parsed = _time(value, 'validity')
    if parsed is None:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.date() < now.date()
    return parsed.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def _future(value, now):
    parsed = _time(value, 'valid_from')
    if parsed is None:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.date() > now.date()
    return parsed.astimezone(timezone.utc) > now.astimezone(timezone.utc)


class TopicKnowledgeStore:
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
            raise Unavailable('topic store is closed')
        self.scope = FriendScope.load(
            self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        self._store._check(self._store.db)

    def _verify_sources(self, entry, refs, legacy_items=None):
        owner = self.scope.friend_id
        source_type = entry['source_type']
        if source_type == 'REAL_CHAT':
            from interest_entries import InterestStore
            verifier = InterestStore(self.scope, readonly=True)
            try:
                verifier._verify_sources(refs, source_type='REAL_CHAT', entry_id=entry['id'])
            finally:
                verifier.close()
            return
        for ref in refs:
            if source_type == 'PUBLIC_DATASET':
                if (ref.data_scope != DataScope.PUBLIC_DATASET or ref.friend_id is not None or
                        ref.origin != SourceOrigin.PUBLIC_DATASET):
                    raise ScopeMismatch('public dataset topic needs a public dataset source')
                continue
            if ref.data_scope != DataScope.PRIVATE_FRIEND or ref.friend_id != owner:
                raise ScopeMismatch('private topic source belongs to another friend/scope')
            if source_type == 'PUBLIC_RESEARCH':
                if (ref.origin != SourceOrigin.BOT_GENERATED or ref.dataset_id not in _LEGACY_DATASETS or
                        not all((ref.import_id, ref.record_id))):
                    raise ScopeMismatch('research mirror must retain its legacy locator and generated origin')
                actual = (legacy_items or {}).get(entry['id'])
                if (actual is None or actual['source_url'] != entry['source_url'] or
                        actual['source_hash'] != _source_hash(entry, refs)):
                    raise InvalidSource('research entry no longer matches its legacy source')
            elif source_type == 'MANUAL':
                if (ref.origin != SourceOrigin.MANUAL or ref.speaker_id != 'operator' or
                        ref.message_seq is not None or ref.dataset_id != 'manual:topic_knowledge' or
                        ref.import_id != entry['id'] or ref.record_id != entry['id']):
                    raise ScopeMismatch('manual topic requires an explicit operator locator')
            elif source_type == 'MODEL_SUMMARY':
                if ref.origin != SourceOrigin.BOT_GENERATED or not all(
                        (ref.dataset_id, ref.import_id, ref.record_id)):
                    raise ScopeMismatch('model summary must retain its generated source locator')
            elif source_type == 'SYNTHETIC':
                if ref.origin != SourceOrigin.SYNTHETIC or not all(
                        (ref.dataset_id, ref.import_id, ref.record_id)):
                    raise ScopeMismatch('synthetic topic must retain its synthetic source locator')

    def _mirror_rows(self):
        with ReadOnlyLegacyReader(self.scope) as reader:
            try:
                rows = reader.page('topic_library', limit=500)
            except Unavailable:
                try:
                    state = json.loads(reader.get('daily_research', '{}') or '{}')
                except (TypeError, ValueError) as exc:
                    raise InvalidSource('legacy daily_research metadata is invalid') from exc
                if not isinstance(state, dict) or not isinstance(state.get('items', []), list):
                    raise InvalidSource('legacy daily_research items are invalid')
                if len(state.get('items', [])) > 500:
                    raise InvalidSource('legacy mirror exceeds 500 items')
                rows = [{**item, 'day': item.get('day') or state.get('day')}
                        for item in state.get('items', []) if isinstance(item, dict)]
                dataset = 'legacy:daily_research'
            else:
                if rows and reader.page('topic_library', after_rowid=rows[-1]['snapshot_rowid'],
                                        limit=1):
                    raise InvalidSource('legacy topic mirror exceeds 500 rows')
                dataset = 'legacy:topic_library'
        normalized = []
        for row in rows:
            values = {key: row.get(key) for key in ('url', 'topic', 'summary', 'source_title')}
            if any(not isinstance(value, str) or not value.strip() for value in values.values()):
                raise InvalidSource('legacy topic row has missing required text/URL')
            day = row.get('day')
            if day is not None:
                if not isinstance(day, str):
                    raise InvalidSource('legacy topic day is invalid')
                try:
                    day = date.fromisoformat(day).isoformat()
                except ValueError:
                    day = None
            identifier = 'legacy-topic:' + hashlib.sha256(
                values['url'].encode('utf-8')).hexdigest()
            item = {'id': identifier, 'topic': values['topic'].strip(), 'subtopic': '',
                    'title': values['source_title'].strip(), 'content': values['summary'].strip(),
                    'summary': values['summary'].strip(), 'keywords': [],
                    'source_type': 'PUBLIC_RESEARCH', 'source_url': values['url'].strip(),
                    'valid_from': day, 'valid_to': None, 'confidence': 0.0,
                    'quality_score': 0.0, 'time_sensitivity': 'UNKNOWN',
                    'last_verified_at': None, 'embedding_model': None,
                    'embedding_version': None}
            ref = SourceRef(
                dataset_id=dataset, import_id='legacy-v1', record_id=identifier,
                message_seq=None, speaker_id='research_worker', origin=SourceOrigin.BOT_GENERATED,
                data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=self.scope.friend_id, event_time=None,
                recorded_at=datetime.now(timezone.utc).isoformat())
            item['source_refs'] = [ref]
            item['source_hash'] = _source_hash(item, (ref,))
            normalized.append(item)
        if len({item['id'] for item in normalized}) != len(normalized):
            raise InvalidSource('legacy topic URLs must be unique')
        return normalized

    def _write(self, entry, expected_revision, *, mirror=False, legacy_items=None):
        refs = _source_refs(entry['source_refs'])
        if entry['source_type'] == 'PUBLIC_RESEARCH' and legacy_items is None:
            legacy_items = {item['id']: item for item in self._mirror_rows()}
        self._verify_sources(entry, refs, legacy_items)
        source_hash = _source_hash(entry, refs)
        if entry.get('source_hash') and entry['source_hash'] != source_hash:
            raise InvalidSource('source_hash does not match entry content/source')
        now = datetime.now().astimezone().isoformat()
        db = self._store.db
        current = db.execute('SELECT * FROM topic_knowledge WHERE friend_id=? AND id=?',
                             (self.scope.friend_id, entry['id'])).fetchone()
        current_revision = current['revision'] if current else 0
        if current_revision != expected_revision:
            raise Conflict('topic row revision changed')
        if current and mirror:
            old_refs = _decode_refs(current['source_refs_json'])
            if (current['source_type'] != 'PUBLIC_RESEARCH' or
                    not any(ref.dataset_id in _LEGACY_DATASETS for ref in old_refs)):
                raise Conflict('legacy mirror cannot overwrite a non-mirror topic')
        if current and current['source_type'] == 'PUBLIC_RESEARCH' and not mirror:
            refs_old = _decode_refs(current['source_refs_json'])
            if any(ref.dataset_id in _LEGACY_DATASETS for ref in refs_old):
                raise Conflict('direct upsert cannot overwrite a legacy mirror')
        if current and current['source_hash'] == source_hash and current['valid']:
            return current_revision, False
        revision = current_revision + 1
        db.execute(
            'INSERT INTO topic_knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(friend_id,id) DO UPDATE SET topic=excluded.topic,subtopic=excluded.subtopic,'
            'title=excluded.title,content=excluded.content,summary=excluded.summary,'
            'keywords_json=excluded.keywords_json,source_type=excluded.source_type,'
            'source_url=excluded.source_url,source_refs_json=excluded.source_refs_json,'
            'source_hash=excluded.source_hash,updated_at=excluded.updated_at,valid_from=excluded.valid_from,'
            'valid_to=excluded.valid_to,confidence=excluded.confidence,quality_score=excluded.quality_score,'
            'time_sensitivity=excluded.time_sensitivity,last_verified_at=excluded.last_verified_at,'
            'embedding_model=excluded.embedding_model,embedding_version=excluded.embedding_version,'
            'valid=1,revision=excluded.revision',
            (self.scope.friend_id, entry['id'], entry['topic'], entry['subtopic'], entry['title'],
             entry['content'], entry['summary'], json.dumps(entry['keywords'], ensure_ascii=False),
             entry['source_type'], entry['source_url'],
             json.dumps([_ref_dict(ref) for ref in refs], ensure_ascii=False), source_hash,
             current['created_at'] if current else now, now, entry['valid_from'], entry['valid_to'],
             entry['confidence'], entry['quality_score'], entry['time_sensitivity'],
             entry['last_verified_at'], entry['embedding_model'], entry['embedding_version'], 1, revision))
        return revision, True

    def mirror_legacy(self):
        source_token = snapshot(self.scope)
        self._check_scope()
        if self.readonly:
            raise Unavailable('topic store is read-only')
        items = self._mirror_rows()
        db = self._store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._check_scope()
            before = self._store.revision
            current_ids = {item['id'] for item in items}
            changed = 0
            for item in items:
                row = db.execute('SELECT revision FROM topic_knowledge WHERE friend_id=? AND id=?',
                                 (self.scope.friend_id, item['id'])).fetchone()
                revision, wrote = self._write(item, row['revision'] if row else 0,
                                              mirror=True, legacy_items={x['id']: x for x in items})
                changed += int(wrote)
            mirrors = db.execute("SELECT id,revision,source_refs_json FROM topic_knowledge "
                                 "WHERE friend_id=? AND source_type='PUBLIC_RESEARCH' AND valid=1 LIMIT 501",
                                 (self.scope.friend_id,)).fetchall()
            if len(mirrors) > 500:
                raise InvalidSource('active topic mirror exceeds 500 entries')
            for row in mirrors:
                if row['id'] in current_ids:
                    continue
                refs = _decode_refs(row['source_refs_json'])
                if any(ref.dataset_id in _LEGACY_DATASETS for ref in refs):
                    db.execute('UPDATE topic_knowledge SET valid=0,revision=revision+1,updated_at=? '
                               'WHERE friend_id=? AND id=?',
                               (datetime.now().astimezone().isoformat(), self.scope.friend_id, row['id']))
                    changed += 1
            if changed:
                db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                           (self.scope.friend_id,))
            db.execute('COMMIT')
            return changed
        except Exception:
            db.execute('ROLLBACK')
            raise

    def upsert(self, entry, expected_revision=0):
        source_token = snapshot(self.scope)
        self._check_scope()
        if self.readonly:
            raise Unavailable('topic store is read-only')
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise InvalidSource('topic entry fields differ from the frozen contract')
        for key in ('id', 'topic', 'subtopic', 'title', 'content', 'summary'):
            if not isinstance(entry[key], str) or (key != 'subtopic' and not entry[key].strip()):
                raise InvalidSource(key + ' must be nonempty text')
        if not isinstance(entry['keywords'], (list, tuple)) or any(
                not isinstance(item, str) or not item.strip() for item in entry['keywords']):
            raise InvalidSource('keywords must be text values')
        if entry['source_type'] not in _SOURCE_TYPES or entry['time_sensitivity'] not in _SENSITIVITY:
            raise InvalidSource('invalid source_type or time_sensitivity')
        for key in ('confidence', 'quality_score'):
            value = entry[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise InvalidSource(key + ' must be finite in 0..1')
        for key in ('valid_from', 'valid_to', 'last_verified_at'):
            _time(entry[key], key)
        if entry['source_url'] is not None and not isinstance(entry['source_url'], str):
            raise InvalidSource('source_url must be text or None')
        if bool(entry['embedding_model']) != bool(entry['embedding_version']):
            raise InvalidSource('embedding model/version must both be set or None')
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (entry['embedding_model'], entry['embedding_version'])):
            raise InvalidSource('embedding metadata must be nonempty strings or None')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be >= 0')
        entry = dict(entry)
        entry['keywords'] = list(entry['keywords'])
        if entry['source_type'] == 'PUBLIC_RESEARCH' and entry['last_verified_at'] is not None:
            raise InvalidSource('research source cannot claim an unverified last_verified_at')
        db = self._store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._check_scope()
            revision, changed = self._write(entry, expected_revision)
            if changed:
                db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                           (self.scope.friend_id,))
            db.execute('COMMIT')
            return revision
        except Exception:
            db.execute('ROLLBACK')
            raise

    def tombstone(self, entry_id, expected_revision):
        self._check_scope()
        if self.readonly:
            raise Unavailable('topic store is read-only')
        if not isinstance(entry_id, str) or not entry_id.strip() or type(expected_revision) is not int:
            raise InvalidSource('invalid topic id/revision')
        db = self._store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT revision,valid FROM topic_knowledge WHERE friend_id=? AND id=?',
                             (self.scope.friend_id, entry_id)).fetchone()
            if row is None or row['revision'] != expected_revision:
                raise Conflict('topic row revision changed')
            if not row['valid']:
                db.execute('COMMIT')
                return expected_revision
            db.execute('UPDATE topic_knowledge SET valid=0,revision=revision+1,updated_at=? '
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
        rows = self._store.db.execute(
            'SELECT * FROM topic_knowledge WHERE friend_id=? AND valid=1 ORDER BY topic,id LIMIT 500',
            (self.scope.friend_id,)).fetchall()
        result = []
        legacy_items = None
        if any(row['source_type'] == 'PUBLIC_RESEARCH' for row in rows):
            try:
                legacy_items = {item['id']: item for item in self._mirror_rows()}
            except (Unavailable, InvalidSource):
                legacy_items = {}
        for row in rows:
            if _expired(row['valid_to'], parsed_now):
                continue
            if _future(row['valid_from'], parsed_now):
                continue
            refs = _decode_refs(row['source_refs_json'])
            entry = {key: row[key] for key in (
                'id', 'topic', 'subtopic', 'title', 'content', 'summary', 'source_type',
                'source_url', 'valid_from', 'valid_to', 'confidence', 'quality_score',
                'time_sensitivity', 'last_verified_at', 'embedding_model', 'embedding_version')}
            entry['keywords'] = json.loads(row['keywords_json'])
            try:
                self._verify_sources(entry, refs, legacy_items)
            except (ScopeMismatch, Unavailable, InvalidSource):
                continue
            if _source_hash(entry, refs) != row['source_hash']:
                continue
            entry['source_refs'] = refs
            entry['source_hash'] = row['source_hash']
            entry['revision'] = row['revision']
            entry['freshness_score'] = 1.0
            entry['needs_verification'] = False
            if row['time_sensitivity'] == 'UNKNOWN':
                entry['freshness_score'], entry['needs_verification'] = 0.25, True
            elif row['time_sensitivity'] == 'FAST_CHANGING':
                verified = _time(row['last_verified_at'], 'last_verified_at')
                if verified is None or verified.tzinfo is None or verified.utcoffset() is None:
                    entry['freshness_score'], entry['needs_verification'] = 0.25, True
                else:
                    age_days = max(0.0, (parsed_now.astimezone(timezone.utc) -
                                         verified.astimezone(timezone.utc)).total_seconds() / 86400)
                    entry['freshness_score'] = max(0.0, 1.0 - age_days / 7.0)
                    entry['needs_verification'] = age_days >= 7.0
            result.append(entry)
            if len(result) >= limit:
                break
        return result
