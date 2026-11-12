"""Exact, friend-scoped facts from the current profile and verified legacy memories."""
from source_state import source_read
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from persona_snapshot import ReadOnlyLegacyReader
from persona_store import ScopeMismatch, Unavailable
from persona_types import (Candidate, DataScope, FriendScope, InvalidSource, PrivacyView,
                           RetrievalBatch, RetrievalRequest, SourceOrigin, SourceRef)


_FIELDS = {
    'nickname': '称呼',
    'birthday': '生日',
    'school': '学校',
    'grade': '年级',
    'age': '年龄',
}
_LABELS = {label: key for key, label in _FIELDS.items()}
_LABELS.update({key: key for key in _FIELDS})
_LABELS.update({'years': 'grade', 'grade': 'grade', 'age': 'age'})
_FACT = re.compile(r'^\s*(?:\[(?:checked useful terms|已查实用语)\]\s*)?([^:：:]{1,20})\s*[:：:]\s*(\S.*?)\s*$')


def _manual_candidates(scope, basic_info, profile_hash, now):
    result = []
    for field, label in _FIELDS.items():
        value = basic_info.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        source = SourceRef(
            dataset_id='manual:friend_profile', import_id=profile_hash,
            record_id='basic_info.' + field, message_seq=None, speaker_id='operator',
            origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=scope.friend_id,
            event_time=None, recorded_at=now)
        result.append(Candidate(
            id='profile:' + field, namespace='structured', type='fact',
            content=f'{label}：{value}', score=1.0, source=source,
            reason='friend.json basic_info人工填写', retriever='structured',
            friend_id=scope.friend_id, timestamp=None, confidence=1.0,
            original_scope=DataScope.PRIVATE_FRIEND, pack_version=None))
    return result


def _legacy_candidates(scope, now):
    try:
        reader = ReadOnlyLegacyReader(scope)
    except Unavailable:
        return []
    with reader:
        try:
            rows = reader.page('memory_entries', limit=500)
        except Unavailable:
            return []
        try:
            reader._table('messages')
        except Unavailable:
            return []
        candidates = []
        for row in rows:
            text = row.get('text')
            confirmed = row.get('locked') == 1 or (
                isinstance(text, str) and text.startswith('[checked useful terms]'))
            if row.get('active') != 1 or row.get('kind') != 'fact' or not confirmed:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                source_ids = json.loads(row.get('sources', ''))
            except (TypeError, ValueError):
                continue
            if (not isinstance(source_ids, list) or not source_ids or len(source_ids) > 100 or
                    any(not isinstance(item, str) or not item or len(item) > 256
                        for item in source_ids)):
                continue
            marks = ','.join('?' for _ in source_ids)
            messages = reader._db.execute(
                f'SELECT seq,qq_id,direction,created_at FROM messages WHERE qq_id IN ({marks})',
                tuple(source_ids)).fetchall()
            by_id = {}
            counts = Counter()
            for item in messages:
                by_id[item['qq_id']] = item
                counts[item['qq_id']] += 1
            # Every citation must resolve to a real inbound message for this friend.
            if any(item not in by_id or counts[item] != 1 or
                   by_id[item]['direction'] != 'friend'
                   for item in source_ids):
                continue
            message = by_id[source_ids[0]]
            event_time = message['created_at']
            try:
                if event_time is not None:
                    datetime.fromisoformat(event_time.replace('Z', '+00:00'))
            except (AttributeError, ValueError):
                event_time = None
            recorded_at = row.get('updated_at')
            try:
                recorded = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
                if recorded.tzinfo is None or recorded.utcoffset() is None:
                    raise ValueError('naive timestamp')
            except (AttributeError, ValueError):
                recorded_at = now
            source = SourceRef(
                dataset_id='legacy:memory_entries', import_id=str(row['id']),
                record_id='memory_entries.' + str(row['id']),
                message_seq=message['seq'], speaker_id=scope.friend_id,
                origin=SourceOrigin.REAL_RELATIONSHIP,
                data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=scope.friend_id, event_time=event_time,
                recorded_at=recorded_at)
            result_text = text.strip()
            candidates.append(Candidate(
                id='memory:' + str(row['id']), namespace='structured', type='fact',
                content=result_text, score=0.9, source=source,
                reason='旧memory_entries锁定/查实事实；全部source均回查为好友来信',
                retriever='structured', friend_id=scope.friend_id,
                timestamp=event_time, confidence=1.0,
                original_scope=DataScope.PRIVATE_FRIEND, pack_version=None))
        return candidates


def _matches_filters(candidate, filters):
    if not filters.valid or candidate.confidence < filters.confidence:
        return False
    if filters.topic or filters.emotion or filters.relationship_tags:
        return False
    if filters.source_type:
        source = candidate.source
        source_type = None
        if source.origin == SourceOrigin.MANUAL and source.dataset_id == 'manual:friend_profile':
            source_type = 'MANUAL'
        elif (source.origin == SourceOrigin.REAL_RELATIONSHIP and
              source.dataset_id == 'legacy:memory_entries'):
            source_type = 'REAL_CHAT'
        if source_type not in filters.source_type:
            return False
    lower, upper = filters.time_range
    if not lower and not upper:
        return True
    stamp = candidate.timestamp
    if stamp is None:
        return False
    try:
        actual = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        date_only = len(stamp) == 10
        if date_only:
            if any(bound and len(bound) != 10 for bound in (lower, upper)):
                return False
        elif actual.tzinfo is None or actual.utcoffset() is None:
            return False
        for bound, is_lower in ((lower, True), (upper, False)):
            if not bound:
                continue
            endpoint = datetime.fromisoformat(bound.replace('Z', '+00:00'))
            if len(bound) == 10:
                value, endpoint = actual.date(), endpoint.date()
            else:
                if endpoint.tzinfo is None or endpoint.utcoffset() is None:
                    return False
                value = actual
            if value < endpoint if is_lower else value > endpoint:
                return False
    except ValueError:
        return False
    return True


class StructuredFactRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(
            scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir,
            allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        started = time.perf_counter()
        if not isinstance(request, RetrievalRequest):
            raise TypeError('a validated RetrievalRequest is required')
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidSource('limit must be 1..500')
        if request.filters.privacy_scope != DataScope.PRIVATE_FRIEND:
            raise ScopeMismatch('structured friend facts are private')
        if request.filters.namespace and 'structured' not in request.filters.namespace:
            return RetrievalBatch(candidates=(), elapsed_ms=0.0)

        profile_path = self.scope.friend_dir / 'friend.json'
        raw = profile_path.read_bytes()
        profile = json.loads(raw.decode('utf-8'))
        basic_info = profile.get('basic_info', {})
        if not isinstance(basic_info, dict):
            basic_info = {}
        now = datetime.now(timezone.utc).isoformat()
        candidates = _manual_candidates(
            self.scope, basic_info, hashlib.sha256(raw).hexdigest(), now)
        candidates.extend(_legacy_candidates(self.scope, now))

        requested_fields = {field for label, field in _LABELS.items()
                            if label.lower() in request.query.lower()}
        if requested_fields:
            candidates = [item for item in candidates if (match := _FACT.match(item.content))
                          and _LABELS.get(match.group(1).strip()) in requested_fields]
        candidates = [item for item in candidates if _matches_filters(item, request.filters)]

        # Explicit manual fields outrank locked legacy facts. Retain both on conflict.
        by_field = {}
        for item in candidates:
            match = _FACT.match(item.content)
            key = _LABELS.get(match.group(1).strip()) if match else None
            if key:
                by_field.setdefault(key, []).append((item, match.group(2).strip()))
        conflicts = {key for key, values in by_field.items()
                     if len({value for _, value in values}) > 1}
        if conflicts:
            adjusted = []
            for item in candidates:
                match = _FACT.match(item.content)
                key = _LABELS.get(match.group(1).strip()) if match else None
                if key in conflicts:
                    reason = item.reason + '；同字段值冲突，人工profile优先，冲突值均保留'
                    item = replace(item, reason=reason,
                                   score=1.0 if item.source.origin == SourceOrigin.MANUAL else 0.9)
                adjusted.append(item)
            candidates = adjusted
        candidates.sort(key=lambda item: (item.source.origin != SourceOrigin.MANUAL,
                                          item.id))
        elapsed = (time.perf_counter() - started) * 1000
        return RetrievalBatch(candidates=tuple(candidates[:limit]), elapsed_ms=elapsed)
