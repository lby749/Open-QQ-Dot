"""Friend-local, metadata-only retrieval of licensed sticker files."""
from source_state import source_read
from io import BytesIO
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from PIL import Image

from persona_store import PersonaStore, ScopeMismatch, Unavailable
from persona_types import (Candidate, DataScope, FriendScope, InvalidSource,
                           RetrievalBatch, SourceRef)
from sticker_library import original_path


_EXTENSIONS = {'.gif': 'GIF', '.png': 'PNG', '.jpg': 'JPEG', '.webp': 'WEBP'}
_ASSET_ID = re.compile(r'[0-9a-f]{64}\Z')
_MARKER = '[[LOCAL_STICKER:{}]]'
_MAX_BYTES = 32 * 1024 * 1024
_POOL_LIMIT = 256


class StickerRetriever:
    def __init__(self, scope: FriendScope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.store = PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                                  project_root=self.scope.project_root)

    def close(self):
        self.store.close()

    @source_read
    def retrieve(self, request, limit):
        started = time.perf_counter()
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or limit < 0:
            raise InvalidSource('sticker limit must be a nonnegative integer')
        if (limit == 0 or not request.filters.valid or
                request.filters.namespace and 'sticker' not in request.filters.namespace):
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000)
        if request.filters.privacy_scope != DataScope.PRIVATE_FRIEND:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000)
        unsupported = _unsupported_filters(request.filters)
        if unsupported:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000,
                                  degraded=True, errors=unsupported)

        terms = _query_terms(request)
        if not terms:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000)
        conditions = [
            'a.friend_id=?', 'a.privacy_scope=?', 'a.valid=1', 'a.declared_kind=?',
            'a.local_use_allowed=1', 'a.image_redistribution_allowed=1',
            'm.analysis_status=?', 'm.sticker_kind=?']
        values = [self.scope.friend_id, DataScope.PRIVATE_FRIEND.value, 'STICKER',
                  'COMPLETED', 'STICKER']
        if request.filters.emotion:
            conditions.append('m.emotion IN (' + ','.join('?' for _ in request.filters.emotion) + ')')
            values.extend(request.filters.emotion)
        rows = self.store.db.execute(
            'SELECT a.asset_id,a.original_relpath,a.source_origin,a.source,'
            'm.emotion,m.intent,m.visual_style,m.text_content,m.description,m.analyzed_at '
            'FROM sticker_assets a JOIN sticker_metadata m '
            'ON m.friend_id=a.friend_id AND m.asset_id=a.asset_id '
            'WHERE ' + ' AND '.join(conditions) +
            ' ORDER BY m.analyzed_at DESC,a.asset_id LIMIT ?',
            (*values, _POOL_LIMIT)).fetchall()

        ranked = []
        degraded = False
        errors = []
        for row in rows:
            source = _source_ref(row['source'], row['source_origin'], self.scope.friend_id)
            if source is None:
                degraded = True
                errors.append('source_unavailable')
                continue
            score, signals = _metadata_score(row, terms)
            if score < request.filters.confidence or score <= 0:
                continue
            if not _within_time_range(source, request.filters.time_range):
                continue
            path = _verified_original(self.scope, row['asset_id'], row['original_relpath'])
            ranked.append((score, row['asset_id'], row, source, signals, path))

        if not ranked:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000,
                                  degraded=degraded, errors=tuple(dict.fromkeys(errors)))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        try:
            with _readonly_history(self.scope) as history:
                candidates = []
                for score, asset_id, row, source, signals, _path in ranked:
                    marker = _MARKER.format(asset_id)
                    if history is not None and history.execute(
                            "SELECT 1 FROM messages WHERE direction='me' AND body=? LIMIT 1",
                            (marker,)).fetchone():
                        continue
                    candidates.append(_candidate(self.scope, asset_id, row, source, score, signals))
                    break
        except (OSError, sqlite3.Error, Unavailable) as exc:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter() - started) * 1000,
                                  degraded=True, errors=('success_history_unavailable',))
        return RetrievalBatch(candidates=tuple(candidates),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            degraded=degraded, errors=tuple(dict.fromkeys(errors)))


def _query_terms(request):
    values = [request.query, request.recent_context, request.scene]
    plan = request.plan
    if isinstance(plan, str):
        values.append(plan)
    elif plan is not None:
        values.extend((plan.intent, plan.tone, plan.core_meaning, plan.sticker_intent or ''))
    text = ' '.join(value for value in values if isinstance(value, str)).lower()
    terms = set(re.findall(r'[a-z0-9]+', text))
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def _unsupported_filters(filters):
    errors = []
    if filters.source_type:
        errors.append('unsupported_source_type_filter')
    if filters.topic is not None:
        errors.append('unsupported_topic_filter')
    if filters.relationship_tags:
        errors.append('unsupported_relationship_filter')
    return tuple(errors)


def _within_time_range(source, bounds):
    from datetime import datetime, timezone
    start, end = bounds
    if start is None and end is None:
        return True
    if source.event_time is None:
        return False
    try:
        event = _aware_time(source.event_time)
        for bound, lower in ((start, True), (end, False)):
            if bound is None:
                continue
            if len(bound) == 10:
                actual = event.date()
                edge = datetime.fromisoformat(bound).date()
            else:
                actual = event.astimezone(timezone.utc)
                edge = _aware_time(bound).astimezone(timezone.utc)
            if lower and actual < edge or not lower and actual > edge:
                return False
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _aware_time(value):
    from datetime import datetime
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timezone offset required')
    return parsed


def _metadata_score(row, query_terms):
    values = [row[name] for name in ('intent', 'emotion', 'visual_style', 'text_content', 'description')]
    metadata_terms = set()
    for value in values:
        if isinstance(value, str):
            metadata_terms.update(re.findall(r'[a-z0-9]+', value.lower()))
            for run in re.findall(r'[\u4e00-\u9fff]+', value.lower()):
                metadata_terms.update(run[index:index + 2] for index in range(len(run) - 1))
    matches = query_terms & metadata_terms
    if not matches:
        return 0.0, ()
    score = min(1.0, len(matches) / max(1, len(metadata_terms)))
    return score, tuple(sorted(matches))


def _source_ref(source_json, source_origin, friend_id):
    try:
        document = json.loads(source_json)
        refs = document['source_refs']
        for record in refs:
            if not isinstance(record, dict):
                continue
            source = SourceRef(
                dataset_id=record['dataset_id'], import_id=record['import_id'],
                record_id=record['record_id'], message_seq=record['message_seq'],
                speaker_id=record['speaker_id'], origin=record['origin'],
                data_scope=record['data_scope'], privacy_view=record['privacy_view'],
                friend_id=record['friend_id'], event_time=record['event_time'],
                recorded_at=record['recorded_at'], license_id=record['license_id'],
                model_analysis_allowed=record['model_analysis_allowed'])
            if (source.data_scope == DataScope.PRIVATE_FRIEND and
                    source.friend_id == friend_id and source.origin is not None and
                    source.origin.value == source_origin):
                return source
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _verified_original(scope, asset_id, original_relpath):
    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise ScopeMismatch('sticker asset id is invalid')
    if not isinstance(original_relpath, str):
        raise ScopeMismatch('sticker path is invalid')
    relative = Path(original_relpath)
    suffix = relative.suffix.lower()
    if (relative.is_absolute() or relative.name != original_relpath or
            relative.name != asset_id + suffix or suffix not in _EXTENSIONS):
        raise ScopeMismatch('sticker filename does not match its content hash')

    friend_root = scope.friend_dir.resolve()
    media_entry = friend_root / 'media'
    if media_entry.is_symlink():
        raise ScopeMismatch('friend media directory is a symbolic link')
    try:
        media_root = media_entry.resolve(strict=True)
    except OSError as exc:
        raise ScopeMismatch('friend media directory is unavailable') from exc
    if media_root == friend_root or not media_root.is_relative_to(friend_root):
        raise ScopeMismatch('friend media directory redirects outside friend scope')
    lexical = friend_root / 'media' / relative
    if lexical.is_symlink():
        raise ScopeMismatch('sticker original path is redirected')
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ScopeMismatch('sticker original is unavailable') from exc
    if resolved.parent != media_root or not resolved.is_relative_to(media_root):
        raise ScopeMismatch('sticker original path leaves friend media')
    try:
        legacy_path = original_path(scope.friend_dir, asset_id)
    except (RuntimeError, OSError, ValueError) as exc:
        raise ScopeMismatch('existing sticker library has no original') from exc
    try:
        legacy_resolved = legacy_path.resolve(strict=True)
    except OSError as exc:
        raise ScopeMismatch('existing sticker library original is unavailable') from exc
    if legacy_resolved != resolved:
        raise ScopeMismatch('sticker library path differs from asset metadata')
    if not resolved.is_file() or resolved.stat().st_size > _MAX_BYTES:
        raise ScopeMismatch('sticker original is missing or exceeds 32 MiB')
    with resolved.open('rb') as source_file:
        data = source_file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ScopeMismatch('sticker original exceeds 32 MiB')
    if hashlib.sha256(data).hexdigest() != asset_id:
        raise ScopeMismatch('sticker original content hash differs from asset id')
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            if image.format != _EXTENSIONS[suffix]:
                raise ScopeMismatch('sticker image format differs from filename')
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ScopeMismatch('sticker original is not a supported image') from exc
    return resolved


class _readonly_history:
    def __init__(self, scope):
        self.scope = scope
        self.connection = None

    def __enter__(self):
        path = self.scope.friend_dir / 'history.sqlite3'
        if not path.exists():
            raise Unavailable('friend success history is unavailable')
        friend_root = self.scope.friend_dir.resolve()
        if path.is_symlink() or path.resolve(strict=True).parent != friend_root:
            raise ScopeMismatch('friend history path redirects')
        self.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                          timeout=5)
        self.connection.row_factory = sqlite3.Row
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            self.connection.close()
            self.connection = None
            raise Unavailable('friend success history is unavailable')
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        if self.connection is not None:
            self.connection.close()


def _candidate(scope, asset_id, row, source, score, signals):
    content = json.dumps({
        'marker': _MARKER.format(asset_id),
        'emotion': row['emotion'], 'intent': row['intent'],
        'visual_style': row['visual_style'], 'text_content': row['text_content'],
        'description': row['description'],
    }, ensure_ascii=False, separators=(',', ':'))
    return Candidate(id=asset_id, namespace='sticker', type='image_sticker',
        content=content, score=score, source=source, reason='metadata_match',
        retriever='sticker', friend_id=scope.friend_id, timestamp=source.event_time,
        confidence=score, original_scope=DataScope.PRIVATE_FRIEND, pack_version=None,
        rank=1, signals=signals)
