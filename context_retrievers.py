"""Small metadata-first retrievers for friend entities, time, interests and topics."""
from source_state import source_read
import json
import time
from datetime import datetime, timezone

from interest_entries import InterestStore
from keyword_retriever import KeywordRetriever
from knowledge_entities import KnowledgeEntityStore, _decode_refs
from persona_store import Unavailable
from persona_types import Candidate, DataScope, FriendScope, InvalidSource, RetrievalBatch, ScopeMismatch
from topic_knowledge import TopicKnowledgeStore


def _scope(scope, request, limit):
    if not isinstance(scope, FriendScope):
        raise ScopeMismatch('a verified FriendScope is required')
    checked = FriendScope.load(scope.friend_id, project_root=scope.project_root,
        friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
    if request.friend_id != checked.friend_id:
        raise ScopeMismatch('request belongs to another friend')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise InvalidSource('retrieval limit must be 1..50')
    if request.filters.privacy_scope != DataScope.PRIVATE_FRIEND:
        raise ScopeMismatch('these retrievers are friend-private')
    return checked


def _allowed(request, namespace, source_type, confidence, timestamp, topic=None):
    filters = request.filters
    if not filters.valid or filters.namespace and namespace not in filters.namespace:
        return False
    if filters.source_type and source_type not in filters.source_type:
        return False
    if confidence < filters.confidence or filters.topic and topic != filters.topic:
        return False
    if filters.time_range != (None, None):
        if timestamp is None:
            return False
        actual = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        date_only = len(timestamp) == 10
        if not date_only and (actual.tzinfo is None or actual.utcoffset() is None):
            return False
        lower, upper = filters.time_range
        date_bounds = all(not value or len(value) == 10 for value in (lower, upper))
        if date_only and not date_bounds:
            return False
        if date_only and date_bounds:
            actual_date = actual.date()
            if lower and actual_date < datetime.fromisoformat(lower).date():
                return False
            if upper and actual_date > datetime.fromisoformat(upper).date():
                return False
            return True
        if lower:
            start = datetime.fromisoformat(lower.replace('Z', '+00:00'))
            if len(lower) == 10:
                if actual.date() < start.date():
                    return False
                start = None
            elif start.tzinfo is None:
                return False
            if start is None:
                pass
            elif actual.astimezone(timezone.utc) < start.astimezone(timezone.utc):
                return False
        if upper:
            end = datetime.fromisoformat(upper.replace('Z', '+00:00'))
            if len(upper) == 10:
                if actual.date() > end.date():
                    return False
                end = None
            elif end.tzinfo is None:
                return False
            if end is not None and actual.astimezone(timezone.utc) > end.astimezone(timezone.utc):
                return False
    return True


def _batch(scope, started, candidates, errors=()):
    return RetrievalBatch(candidates=tuple(candidates),
        elapsed_ms=(time.perf_counter()-started)*1000, degraded=bool(errors), errors=tuple(errors))


class EntityRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        scope = _scope(self.scope, request, limit)
        started, candidates = time.perf_counter(), []
        terms = {request.query.strip()} if request.query.strip() else set()
        terms.update(part.strip() for part in request.query.split() if part.strip())
        try:
            with KnowledgeEntityStore(scope, readonly=True) as store:
                rows = store.store.db.execute('SELECT * FROM knowledge_entities WHERE friend_id=? AND valid=1 '
                    'ORDER BY id LIMIT 500', (scope.friend_id,)).fetchall()
                for row in rows:
                    if not store._entity_current(row):
                        continue
                    aliases = json.loads(row['aliases_json'])
                    name = row['name']
                    if not any(term in name or name in term or any(
                            term in alias or alias in term for alias in aliases) for term in terms):
                        continue
                    refs = _decode_refs(row['source_refs_json'])
                    source_type = 'MANUAL' if refs[0].origin.value == 'MANUAL' else 'REAL_CHAT'
                    if not _allowed(request, 'entity', source_type, row['confidence'], None):
                        continue
                    candidates.append(Candidate(id=row['id'], namespace='entity', type=row['entity_type'],
                        content=json.dumps({'name': name, 'aliases': aliases}, ensure_ascii=False),
                        score=float(row['confidence']), source=refs[0], reason='verified entity name or alias',
                        retriever='entity', friend_id=scope.friend_id, timestamp=None,
                        confidence=float(row['confidence']), original_scope=DataScope.PRIVATE_FRIEND,
                        pack_version=None))
                    if len(candidates) >= limit:
                        break
                matched_ids = {item.id for item in candidates}
                for row in store.relations(request.now, 100):
                    if len(candidates) >= limit:
                        break
                    if not matched_ids.intersection((row['source_entity'], row['target_entity'])):
                        continue
                    refs = _decode_refs(row['source_refs_json'])
                    source_type = 'MANUAL' if refs[0].origin.value == 'MANUAL' else 'REAL_CHAT'
                    if not _allowed(request, 'entity', source_type, row['confidence'], row['last_seen']):
                        continue
                    names = {item['id']: item['name'] for item in store.store.db.execute(
                        'SELECT id,name FROM knowledge_entities WHERE friend_id=? AND id IN (?,?)',
                        (scope.friend_id, row['source_entity'], row['target_entity']))}
                    candidates.append(Candidate(id='relation:' + row['id'], namespace='entity', type='relation',
                        content=' '.join((names[row['source_entity']], row['relation'], names[row['target_entity']])),
                        score=float(row['strength']), source=refs[0], reason='verified relation between matched entities',
                        retriever='entity', friend_id=scope.friend_id, timestamp=row['last_seen'],
                        confidence=row['confidence'], original_scope=DataScope.PRIVATE_FRIEND,
                        pack_version=None, valid_time=(row['valid_from'], row['valid_to'])))
        except Unavailable as exc:
            return _batch(scope, started, (), (type(exc).__name__ + ': entity store unavailable',))
        return _batch(scope, started, candidates)


class InterestRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        scope = _scope(self.scope, request, limit)
        started, candidates = time.perf_counter(), []
        try:
            with InterestStore(scope, readonly=True) as store:
                for row in store.entries(request.now, 100):
                    if not _allowed(request, 'interest', row['source_type'], row['confidence'],
                                    row['last_seen'], row['topic']):
                        continue
                    searchable = ' '.join((row['topic'], row['content'], row['category']))
                    if request.query and not any(part.casefold() in searchable.casefold()
                            for part in (request.query, *request.query.split()) if part):
                        continue
                    refs = row['source_refs']
                    age = row['recency_days']
                    recency = 0.25 if age is None else 1.0 / (1.0 + age / 30.0)
                    candidates.append(Candidate(id=row['id'], namespace='interest', type=row['interest_type'],
                        content=json.dumps({key: row[key] for key in ('topic', 'content', 'category', 'strength')},
                                           ensure_ascii=False), score=float(row['strength'] * recency),
                        source=refs[0], reason='verified current-friend interest', retriever='interest',
                        friend_id=scope.friend_id, timestamp=row['last_seen'], confidence=row['confidence'],
                        original_scope=DataScope.PRIVATE_FRIEND, pack_version=None,
                        valid_time=(None, row['valid_to']),
                        signals=('time_unknown',) if age is None else ()))
                    if len(candidates) == limit:
                        break
        except Unavailable as exc:
            return _batch(scope, started, (), (type(exc).__name__ + ': interest store unavailable',))
        return _batch(scope, started, candidates)


class TopicKnowledgeRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        scope = _scope(self.scope, request, limit)
        started, candidates = time.perf_counter(), []
        try:
            with TopicKnowledgeStore(scope, readonly=True) as store:
                for row in store.entries(request.now, 100):
                    if not _allowed(request, 'knowledge', row['source_type'], row['confidence'],
                                    row['valid_from'], row['topic']):
                        continue
                    searchable = ' '.join((row['topic'], row['subtopic'], row['title'], row['content'],
                                           row['summary'], *row['keywords']))
                    if request.query and not any(part.casefold() in searchable.casefold()
                            for part in (request.query, *request.query.split()) if part):
                        continue
                    candidates.append(Candidate(id=row['id'], namespace='knowledge', type='topic',
                        content=' '.join((row['title'], row['summary'], row['content'])),
                        score=float(row['confidence'] * row['freshness_score']), source=row['source_refs'][0],
                        reason='verified topic knowledge with actual validity/freshness', retriever='topic',
                        friend_id=scope.friend_id, timestamp=row['valid_from'], confidence=row['confidence'],
                        original_scope=DataScope.PRIVATE_FRIEND, pack_version=None,
                        valid_time=(row['valid_from'], row['valid_to']),
                        signals=tuple(signal for signal, enabled in (
                            ('needs_verification', row['needs_verification']),
                            ('time_unknown', row['valid_from'] is None)) if enabled)))
                    if len(candidates) == limit:
                        break
        except Unavailable as exc:
            return _batch(scope, started, (), (type(exc).__name__ + ': topic store unavailable',))
        return _batch(scope, started, candidates)


class TemporalRetriever:
    """Combine verified dated chat, interest and topic evidence without inventing dates."""
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.keyword = KeywordRetriever(self.scope)
        self.interest = InterestRetriever(self.scope)
        self.topic = TopicKnowledgeRetriever(self.scope)

    @source_read
    def retrieve(self, request, limit):
        scope = _scope(self.scope, request, limit)
        started, results, errors = time.perf_counter(), [], []
        for retriever in (self.keyword, self.interest, self.topic):
            result = retriever.retrieve(request, limit)
            results.extend(result.candidates)
            errors.extend(result.errors)
        unique = { (item.namespace, item.id): item for item in results }
        candidates = list(unique.values())
        now = datetime.fromisoformat(request.now.replace('Z', '+00:00'))
        def order(item):
            if item.timestamp is None:
                return (1, 0.0, item.id)
            try:
                stamp = datetime.fromisoformat(item.timestamp.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    return (1, 0.0, item.id)
                age = max(0.0, (now.astimezone(timezone.utc)-stamp.astimezone(timezone.utc)).total_seconds())
                return (0, age, item.id)
            except ValueError:
                return (1, 0.0, item.id)
        candidates.sort(key=order)
        return _batch(scope, started, candidates[:limit], tuple(dict.fromkeys(errors)))
