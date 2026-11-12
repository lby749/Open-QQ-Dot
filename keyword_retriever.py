"""Friend-scoped keyword retrieval over the approved semantic index."""
from source_state import source_read
from persona_store import Unavailable
from persona_types import FriendScope, InvalidSource, RetrievalBatch, ScopeMismatch
from semantic_index import SemanticIndex


class KeywordRetriever:
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
        try:
            index = SemanticIndex(self.scope, readonly=True)
            try:
                return index.keyword(request, limit)
            finally:
                index.close()
        except Unavailable as exc:
            return RetrievalBatch(candidates=(), elapsed_ms=0, degraded=True,
                                  errors=(type(exc).__name__ + ': keyword index unavailable',))
