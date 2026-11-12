"""Current-friend memory retrieval with the verified legacy fact fallback."""
from source_state import source_read
import time
from dataclasses import replace
from datetime import datetime, timezone

from persona_store import Unavailable
from persona_types import DataScope, FriendScope, InvalidSource, RetrievalBatch, ScopeMismatch
from semantic_index import SemanticIndex
from structured_profile import _legacy_candidates


class MemoryRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidSource('retrieval limit must be 1..50')
        if request.filters.namespace and 'memory' not in request.filters.namespace:
            return RetrievalBatch(candidates=(), elapsed_ms=0)
        if request.filters.privacy_scope not in (None, DataScope.PRIVATE_FRIEND):
            raise ScopeMismatch('friend memory is private')
        memory_request = replace(request, filters=replace(request.filters,
            namespace=('memory',), privacy_scope=DataScope.PRIVATE_FRIEND))
        started = time.perf_counter()
        errors, indexed = (), ()
        try:
            index = SemanticIndex(self.scope, readonly=True)
            try:
                result = index.keyword(memory_request, limit)
                indexed = result.candidates
                errors = result.errors
            finally:
                index.close()
        except Unavailable as exc:
            errors = (type(exc).__name__ + ': memory index unavailable',)
        if indexed:
            return RetrievalBatch(candidates=indexed,
                elapsed_ms=(time.perf_counter()-started)*1000, errors=errors,
                degraded=bool(errors))

        # Reuse the legacy verifier, retaining only its source-checked memory rows.
        now = datetime.now(timezone.utc).isoformat()
        query = request.query.casefold().strip()
        candidates = []
        for item in _legacy_candidates(self.scope, now):
            if not request.filters.valid:
                continue
            if query and query not in item.content.casefold():
                continue
            if request.filters.source_type and 'REAL_CHAT' not in request.filters.source_type:
                continue
            if request.filters.confidence > item.confidence or request.filters.topic or (
                    request.filters.emotion or request.filters.relationship_tags):
                continue
            timestamp = item.timestamp
            start, end = request.filters.time_range
            if start or end:
                try:
                    actual = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    lower = datetime.fromisoformat(start.replace('Z', '+00:00')) if start else None
                    upper = datetime.fromisoformat(end.replace('Z', '+00:00')) if end else None
                    if actual.tzinfo is None:
                        continue
                    if lower and (actual.date() < lower.date() if len(start) == 10 else
                            lower.tzinfo is None or actual < lower):
                        continue
                    if upper and (actual.date() > upper.date() if len(end) == 10 else
                            upper.tzinfo is None or actual > upper):
                        continue
                except (AttributeError, ValueError):
                    continue
            candidates.append(replace(item, namespace='memory', retriever='memory'))
            if len(candidates) >= limit:
                break
        return RetrievalBatch(candidates=tuple(candidates),
            elapsed_ms=(time.perf_counter()-started)*1000, degraded=True,
            errors=tuple(dict.fromkeys(errors + ('legacy verified-memory fallback',))))
