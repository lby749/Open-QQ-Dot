"""Bounded rank fusion, light contextual ordering and lexical MMR diversity."""
import math
import re
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from persona_types import DataScope, InvalidSource, RetrievalBatch, ScopeMismatch


def _key(candidate):
    source = candidate.source
    pack_id = source.pack_id if candidate.original_scope == DataScope.PUBLIC_PACK else None
    return (candidate.friend_id, candidate.namespace, candidate.id, candidate.pack_version, pack_id)


def _terms(text):
    terms = []
    for value in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text[:4000].casefold()):
        if value[0] >= '\u3400':
            terms.extend(value[i:i + 2] for i in range(len(value) - 1))
            terms.extend(value)
        else:
            terms.append(value)
    return Counter(terms)


def _similarity(a, b):
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[key] * b[key] for key in common)
    return dot / (math.sqrt(sum(v*v for v in a.values())) * math.sqrt(sum(v*v for v in b.values())))


def _scene_bias(request, candidate):
    scene = request.scene.casefold()
    route = candidate.retriever.casefold()
    preferred = ()
    if any(word in scene for word in ('knowledge', 'study', 'work')):
        preferred = ('topic', 'entity', 'knowledge')
    elif any(word in scene for word in ('memory', 'reminisc', 'recall')):
        preferred = ('memory', 'history', 'keyword', 'temporal')
    elif any(word in scene for word in ('active', 'proactive')):
        preferred = ('interest', 'temporal', 'topic')
    elif scene not in ('any', 'unknown'):
        preferred = ('persona', 'example', 'interest')
    return 0.002 if any(name in route or name in candidate.namespace for name in preferred) else 0.0


def _time_bias(candidate, now):
    if candidate.timestamp is None or len(candidate.timestamp) == 10:
        return 0.0
    try:
        stamp = datetime.fromisoformat(candidate.timestamp.replace('Z', '+00:00'))
        current = datetime.fromisoformat(now.replace('Z', '+00:00'))
        if stamp.tzinfo is None or current.tzinfo is None or stamp > current:
            return 0.0
        age_days = (current.astimezone(timezone.utc) -
                    stamp.astimezone(timezone.utc)).total_seconds() / 86400
        return 0.001 * max(0.0, 1.0 - age_days / 30.0)
    except ValueError:
        return 0.0


def _source_bias(candidate):
    return 0.001 if getattr(candidate.source.origin, 'value', None) == 'REAL_RELATIONSHIP' else 0.0


def rank_candidates(scope, request, batches, limit):
    """Fuse rank-only batches; output up to 20 source-validated Candidates."""
    started = time.perf_counter()
    if request.friend_id != scope.friend_id:
        raise ScopeMismatch('request belongs to another friend')
    if type(limit) is not int or not 1 <= limit <= 48:  # 48 = complex-scene example cap
        raise InvalidSource('ranking limit must be 1..48')
    if not isinstance(batches, (tuple, list)) or not 1 <= len(batches) <= 50:
        raise InvalidSource('1..50 bounded retrieval batches required')
    if any(not isinstance(batch, RetrievalBatch) for batch in batches):
        raise InvalidSource('rank fusion requires RetrievalBatch inputs')

    fused, errors = {}, []
    degraded = False
    total = 0
    for batch in batches:
        degraded = degraded or batch.degraded
        errors.extend(batch.errors)
        seen, position = set(), 0
        for candidate in batch.candidates:
            total += 1
            if total > 500:
                raise InvalidSource('candidate input exceeds 500')
            candidate.validate_scope(scope)
            if candidate.friend_id != request.friend_id:
                raise ScopeMismatch('candidate belongs to another friend')
            key = _key(candidate)
            if key in seen:
                continue
            seen.add(key)
            position += 1
            if candidate.original_scope == DataScope.PUBLIC_PACK:
                allowed = set(scope.allowed_pack_versions) & set(request.allowed_pack_versions)
                if not any(pack.pack_id == candidate.source.pack_id and
                           pack.version == candidate.pack_version for pack in allowed):
                    raise ScopeMismatch('candidate public pack is not allowed by this request')
            row = fused.setdefault(key, {'candidate': candidate, 'rrf': 0.0, 'paths': set()})
            if candidate.retriever in row['paths']:
                continue
            row['rrf'] += 1.0 / (60 + position)
            row['paths'].add(candidate.retriever)

    if not fused:
        return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter()-started)*1000,
                              degraded=degraded, errors=tuple(dict.fromkeys(errors)))
    maximum = max(row['rrf'] for row in fused.values())
    pool = []
    for row in fused.values():
        candidate = row['candidate']
        time_bias = _time_bias(candidate, request.now)
        scene_bias = _scene_bias(request, candidate)
        source_bias = _source_bias(candidate)
        score = row['rrf'] / maximum + scene_bias + source_bias + time_bias
        signals = tuple(dict.fromkeys(candidate.signals + ('RRF',) +
            tuple('path:' + item for item in sorted(row['paths'])) +
            (('scene:' + request.scene,) if scene_bias else ()) +
            (('source:real_chat',) if source_bias else ()) +
            (('time:recent',) if time_bias else ())))
        pool.append((candidate, score, row['rrf'], signals, _terms(candidate.content)))
    pool.sort(key=lambda item: (item[1], item[0].id), reverse=True)
    pool = pool[:50]

    selected, remaining = [], pool
    while remaining and len(selected) < limit:
        def mmr(item):
            relevance = item[1]
            redundancy = max((_similarity(item[4], chosen[4]) for chosen in selected), default=0.0)
            return 0.75 * relevance - 0.25 * redundancy
        chosen = max(remaining, key=lambda item: (mmr(item), item[1], item[0].id))
        selected.append(chosen)
        remaining.remove(chosen)

    output = []
    for rank, (candidate, score, raw, signals, _) in enumerate(selected, 1):
        reason = candidate.reason + f"; RRF={raw:.6f}; diversity-adjusted rank={rank}"
        output.append(replace(candidate, score=score, raw_score=raw, rank=rank,
                              signals=signals, reason=reason))
    return RetrievalBatch(candidates=tuple(output), elapsed_ms=(time.perf_counter()-started)*1000,
                          degraded=degraded, errors=tuple(dict.fromkeys(errors)))
