"""Dense query-only retrieval over the approved semantic index."""
from source_state import source_read
from persona_store import Unavailable
from persona_types import FriendScope, InvalidSource, RetrievalBatch, ScopeMismatch
from semantic_index import SemanticIndex


class SemanticRetriever:
    def __init__(self, scope, embedding_provider=None, *, source_refs=()):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.embedding_provider = embedding_provider
        self.source_refs = tuple(source_refs)

    @source_read
    def retrieve(self, request, limit):
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise InvalidSource('retrieval limit must be 1..50')
        if not self.source_refs:
            return RetrievalBatch(candidates=(), elapsed_ms=0, degraded=True,
                                  errors=('Unavailable: query source provenance required',))
        try:
            index = SemanticIndex(self.scope, readonly=True,
                                  embedding_provider=self.embedding_provider)
            try:
                return index.dense(request, limit, source_refs=self.source_refs)
            finally:
                index.close()
        except Unavailable as exc:
            return RetrievalBatch(candidates=(), elapsed_ms=0, degraded=True,
                                  errors=(type(exc).__name__ + ': semantic index unavailable',))
