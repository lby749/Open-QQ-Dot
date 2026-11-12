"""Bounded, friend-scoped retrieval orchestration with privacy-safe trace."""
from source_state import snapshot as source_snapshot
from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from context_retrievers import EntityRetriever, InterestRetriever, TemporalRetriever, TopicKnowledgeRetriever
from example_retriever import MAX_EXAMPLES, ExampleRetriever, scene_cap
from keyword_retriever import KeywordRetriever
from memory_retriever import MemoryRetriever
from persona_retriever import PersonaRetriever
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import (Candidate, DataScope, FriendScope, InvalidSource, PrivacyViolation,
    RetrievalBatch, RetrievalRequest)
from retrieval_ranking import rank_candidates
from retrieval_router import RetrievalQueryRouter, RouterDecision
from semantic_retriever import SemanticRetriever
from structured_profile import StructuredFactRetriever


@dataclass(frozen=True)
class RuntimeRelevantContext:
    structured: tuple[Candidate, ...]
    memory: tuple[Candidate, ...]
    persona: tuple[Candidate, ...]
    interests: tuple[Candidate, ...]
    topics: tuple[Candidate, ...]
    examples: tuple[Candidate, ...]
    style: tuple[Candidate, ...]
    stickers: tuple[Candidate, ...]
    router: RouterDecision
    trace: dict
    degraded: bool


_ROUTE_NAMESPACES = {
    'structured': frozenset({'structured'}),
    'memory': frozenset({'memory'}),
    'keyword': frozenset({'structured', 'memory', 'knowledge', 'interest', 'entity'}),
    'semantic': frozenset({'structured', 'memory', 'knowledge', 'interest', 'entity'}),
    'entity': frozenset({'entity'}),
    'temporal': frozenset({'memory', 'interest', 'knowledge'}),
    'interest': frozenset({'interest'}),
    'topic': frozenset({'knowledge'}),
    'persona': frozenset({'persona', 'style'}),
    'example': frozenset({'example'}),
    'sticker': frozenset({'sticker'}),
}
_PUBLIC_NAMESPACES = frozenset({'persona', 'style', 'example', 'sticker'})
_OUTPUT_FIELDS = {
    'structured': 'structured', 'entity': 'structured', 'memory': 'memory',
    'persona': 'persona', 'interest': 'interests', 'knowledge': 'topics',
    'example': 'examples', 'style': 'style', 'sticker': 'stickers',
}
_FALLBACKS = {'semantic': ('keyword',), 'persona': ('memory',), 'example': ('persona',)}
_UNIMPLEMENTED_ROUTES = frozenset({'unfinished_topic'})
_SAFE_ERRORS = {'Unavailable', 'InvalidSource', 'Conflict', 'OSError', 'DatabaseError',
                'TimeoutError', 'retriever_degraded'}
_REASONS = {'active_topic', 'similarity', 'history_recall', 'third_party_topic',
            'friend_profile', 'knowledge_query', 'emotion', 'daily_scene', 'default',
            'planner_skip'}


def _empty_fields():
    return {name: [] for name in ('structured', 'memory', 'persona', 'interests',
        'topics', 'examples', 'style', 'stickers')}


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _file_signature(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return 'missing'


def _snapshot_signature(scope, request):
    """Read only the publication pointers needed to reject mixed retrieval generations."""
    friend_dir = scope.friend_dir
    sidecar = friend_dir / 'persona.sqlite3'
    state = {'source_token': ('PUBLIC_PACK' if request.filters.privacy_scope == DataScope.PUBLIC_PACK else source_snapshot(scope)),
        'friend_id': scope.friend_id,
        'structured_pointer': _file_signature(friend_dir / 'friend.json'),
        'persona_pointer': _file_signature(friend_dir / 'persona_versions' / 'active.json'),
        'allowed_packs': sorted((item.pack_id, item.version) for item in request.allowed_pack_versions),
        'sidecar': 'missing'}
    if sidecar.is_file():
        uri = sidecar.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'scope_state' in tables:
                row = db.execute('SELECT revision FROM scope_state WHERE friend_id=?',
                                 (scope.friend_id,)).fetchone()
                revision = row[0] if row else 'missing'
            else:
                revision = 'missing'
            if 'embedding_index_metadata' in tables:
                row = db.execute('SELECT generation,revision,model,dimension,version FROM '
                    "embedding_index_metadata WHERE friend_id=? AND state='ACTIVE'",
                    (scope.friend_id,)).fetchone()
                index = tuple(row) if row else 'missing'
            else:
                index = 'missing'
            if 'pack_bindings' in tables:
                rows = db.execute('SELECT pack_id,pack_version,enabled,revision FROM pack_bindings '
                    'WHERE friend_id=? ORDER BY pack_id', (scope.friend_id,)).fetchall()
                bindings = tuple(tuple(row) for row in rows)
            else:
                bindings = 'missing'
        state['sidecar'] = (revision, index, bindings)
    return _digest(state)


class RetrievalOrchestrator:
    """Calls existing friend-scoped retrievers serially and fuses bounded results."""

    def __init__(self, scope, embedding_provider=None, *, source_refs=()):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.embedding_provider = embedding_provider
        self.source_refs = tuple(source_refs)
        self.router = RetrievalQueryRouter()

    def _retriever(self, route):
        if route == 'structured':
            return StructuredFactRetriever(self.scope)
        if route == 'memory':
            return MemoryRetriever(self.scope)
        if route == 'keyword':
            return KeywordRetriever(self.scope)
        if route == 'semantic':
            return SemanticRetriever(self.scope, self.embedding_provider, source_refs=self.source_refs)
        if route == 'entity':
            return EntityRetriever(self.scope)
        if route == 'temporal':
            return TemporalRetriever(self.scope)
        if route == 'interest':
            return InterestRetriever(self.scope)
        if route == 'topic':
            return TopicKnowledgeRetriever(self.scope)
        if route == 'persona':
            return PersonaRetriever(self.scope)
        if route == 'example':
            return ExampleRetriever(self.scope, self.embedding_provider, source_refs=self.source_refs)
        return None

    @staticmethod
    def _error_code(error):
        if isinstance(error, Unavailable):
            return 'Unavailable'
        if isinstance(error, InvalidSource):
            return 'InvalidSource'
        if isinstance(error, Conflict):
            return 'Conflict'
        if isinstance(error, OSError):
            return 'OSError'
        if isinstance(error, sqlite3.Error):
            return 'DatabaseError'
        if isinstance(error, TimeoutError):
            return 'TimeoutError'
        return 'retriever_degraded'

    @staticmethod
    def _batch_error_codes(batch):
        result = set()
        for error in batch.errors:
            name = error.split(':', 1)[0]
            if name in ('ScopeMismatch', 'PrivacyViolation'):
                raise ScopeMismatch('retriever reported an isolation failure')
            result.add(name if name in _SAFE_ERRORS else 'retriever_degraded')
        return tuple(sorted(result))

    def _request_for_route(self, request, route):
        allowed = _ROUTE_NAMESPACES[route]
        if request.filters.privacy_scope == DataScope.PUBLIC_PACK:
            allowed &= _PUBLIC_NAMESPACES
        elif request.filters.privacy_scope != DataScope.PRIVATE_FRIEND:
            return None, 0
        known = set().union(*_ROUTE_NAMESPACES.values())
        unknown_count = sum(name not in known for name in request.filters.namespace)
        if request.filters.namespace:
            allowed &= set(request.filters.namespace)
        if not allowed:
            return None, unknown_count
        filters = replace(request.filters, namespace=tuple(sorted(allowed)))
        return replace(request, filters=filters), unknown_count

    def _empty_result(self, decision, trace, degraded=True):
        return RuntimeRelevantContext(**{key: tuple(value) for key, value in _empty_fields().items()},
            router=decision, trace=trace, degraded=degraded)

    def retrieve(self, request):
        if not isinstance(request, RetrievalRequest):
            raise TypeError('request must be RetrievalRequest')
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        decision = self.router.route(request)
        trace = {'snapshot_version': None, 'router': {
            'mode': decision.mode, 'reason_code': decision.reason if decision.reason in _REASONS else 'unknown',
            'route_count': len(decision.retrievers)}, 'routes': [], 'candidates': [],
            'unknown_namespace_count': 0, 'total_received': 0, 'generic_selected': 0,
            'snapshot_changed': False}
        if decision.mode == 'skip':
            trace['snapshot_version'] = 'skipped'
            return self._empty_result(decision, trace, degraded=False)

        retrieval_request = request
        if decision.mode == 'supplemental':
            core = request.plan.core_meaning.strip()
            if core:
                retrieval_request = replace(request, query=core)
        try:
            snapshot = _snapshot_signature(self.scope, retrieval_request)
        except (OSError, sqlite3.Error, Conflict):
            trace['snapshot_error'] = 'snapshot_unavailable'
            return self._empty_result(decision, trace)
        trace['snapshot_version'] = snapshot

        fields = _empty_fields()
        route_batches, general_candidates = [], []
        errors_seen, degraded = False, False
        route_queue = list(decision.retrievers)
        invoked = set()
        remaining = 50
        index = 0
        while index < len(route_queue):
            route = route_queue[index]
            index += 1
            if route in _UNIMPLEMENTED_ROUTES:
                trace['routes'].append({'route': route, 'limit': 0, 'received': 0,
                    'elapsed_ms': 0, 'degraded': True, 'error_codes': ['not_implemented']})
                degraded = True
                try:
                    current = _snapshot_signature(self.scope, retrieval_request)
                except (OSError, sqlite3.Error, Conflict):
                    current = None
                if current != snapshot:
                    trace['snapshot_changed'] = True
                    trace['snapshot_error'] = 'snapshot_changed'
                    return self._empty_result(decision, trace)
                continue
            if route not in _ROUTE_NAMESPACES:
                trace['routes'].append({'route': 'unknown', 'limit': 0, 'received': 0,
                    'elapsed_ms': 0, 'degraded': True, 'error_codes': ['unknown_route']})
                degraded = True
                continue
            if route in invoked:
                continue
            invoked.add(route)
            route_request, unknown_count = self._request_for_route(retrieval_request, route)
            trace['unknown_namespace_count'] += unknown_count
            if route_request is None:
                trace['routes'].append({'route': route, 'limit': 0, 'received': 0,
                    'elapsed_ms': 0, 'degraded': False, 'error_codes': ['namespace_excluded']})
                continue
            if remaining <= 0:
                trace['routes'].append({'route': route, 'limit': 0, 'received': 0,
                    'elapsed_ms': 0, 'degraded': True, 'error_codes': ['candidate_budget_exhausted']})
                degraded = True
                continue
            routes_left = max(1, len(route_queue) - index + 1)
            # Examples take their own scene cap (8/24/48) instead of an even share of the pool.
            limit = (min(remaining, scene_cap(retrieval_request.scene)) if route == 'example' else
                     min(remaining, math.ceil(remaining / routes_left), 20 if route == 'persona' else 50))
            started = time.perf_counter()
            lane_codes, lane_failed = [], False
            candidates = []
            retriever = None
            try:
                retriever = self._retriever(route)
                if retriever is None:
                    lane_codes.append('not_implemented')
                    lane_failed = True
                else:
                    batch = retriever.retrieve(route_request, limit)
                    if not isinstance(batch, RetrievalBatch) or len(batch.candidates) > limit:
                        raise InvalidSource('retriever batch exceeds its bounded contract')
                    if len(batch.candidates) > remaining:
                        raise InvalidSource('retriever exceeded the total candidate budget')
                    remaining -= len(batch.candidates)
                    trace['total_received'] += len(batch.candidates)
                    lane_codes.extend(self._batch_error_codes(batch))
                    lane_failed = batch.degraded or bool(batch.errors)
                    for input_rank, item in enumerate(batch.candidates, 1):
                        item.validate_scope(self.scope)
                        if item.original_scope != retrieval_request.filters.privacy_scope:
                            raise ScopeMismatch('candidate privacy scope differs from the request')
                        if item.original_scope == DataScope.PUBLIC_PACK and not any(
                                pack.pack_id == item.source.pack_id and pack.version == item.pack_version
                                for pack in self.scope.allowed_pack_versions & retrieval_request.allowed_pack_versions):
                            raise ScopeMismatch('candidate public pack is not allowed for this request')
                        if item.namespace not in _ROUTE_NAMESPACES[route]:
                            if item.original_scope == DataScope.PUBLIC_PACK:
                                raise ScopeMismatch('public candidate entered a disallowed route namespace')
                            all_namespaces = set().union(*_ROUTE_NAMESPACES.values())
                            if item.namespace not in all_namespaces:
                                trace['unknown_namespace_count'] += 1
                            lane_failed = True
                            lane_codes.append('namespace_excluded')
                            continue
                        if item.namespace not in route_request.filters.namespace:
                            continue
                        candidates.append((input_rank, item))
            except (ScopeMismatch, PrivacyViolation):
                raise
            except (Unavailable, InvalidSource, Conflict, OSError, sqlite3.Error, TimeoutError) as exc:
                lane_codes.append(self._error_code(exc))
                lane_failed = True
                candidates = []
            finally:
                if retriever is not None and hasattr(retriever, 'close'):
                    retriever.close()

            elapsed = (time.perf_counter() - started) * 1000
            trace['routes'].append({'route': route, 'limit': limit,
                'received': len(candidates), 'elapsed_ms': round(elapsed, 3),
                'degraded': lane_failed, 'error_codes': sorted(set(lane_codes))})
            if lane_failed:
                degraded = True
                errors_seen = True
                route_queue.extend(name for name in _FALLBACKS.get(route, ())
                    if name not in invoked and name not in route_queue[index:])
            for input_rank, item in candidates:
                field = _OUTPUT_FIELDS.get(item.namespace)
                if field is None:
                    trace['unknown_namespace_count'] += 1
                    degraded = True
                elif item.namespace == 'example':
                    fields['examples'].append(item)
                elif item.namespace == 'style':
                    fields['style'].append(item)
                else:
                    general_candidates.append((route, input_rank, item))

            try:
                current = _snapshot_signature(self.scope, retrieval_request)
            except (OSError, sqlite3.Error, Conflict):
                current = None
            if current != snapshot:
                trace['snapshot_changed'] = True
                trace['snapshot_error'] = 'snapshot_changed'
                return self._empty_result(decision, trace)

            if route == 'example' and len(fields['examples']) > MAX_EXAMPLES:
                fields['examples'] = fields['examples'][:MAX_EXAMPLES]

        if general_candidates:
            grouped = {}
            for route, input_rank, candidate in general_candidates:
                grouped.setdefault(route, []).append(candidate)
            for route, candidates in grouped.items():
                route_batches.append(RetrievalBatch(candidates=tuple(candidates), elapsed_ms=0,
                    degraded=False, errors=()))
            ranked = rank_candidates(self.scope, retrieval_request, route_batches, 20)
            trace['generic_selected'] = len(ranked.candidates)
            trace['candidates'] = self._candidate_trace(general_candidates, ranked.candidates)
            for item in ranked.candidates:
                field = _OUTPUT_FIELDS.get(item.namespace)
                if field:
                    fields[field].append(item)
            degraded = degraded or ranked.degraded

        trace['candidates'].extend(self._direct_candidate_trace('example', fields['examples']))
        trace['candidates'].extend(self._direct_candidate_trace('persona', fields['style']))
        self._apply_budgets(fields, retrieval_request.budgets, trace)

        if len(fields['examples']) > MAX_EXAMPLES:
            fields['examples'] = fields['examples'][:MAX_EXAMPLES]
        try:
            current = _snapshot_signature(self.scope, retrieval_request)
        except (OSError, sqlite3.Error, Conflict):
            current = None
        if current != snapshot:
            trace['snapshot_changed'] = True
            trace['snapshot_error'] = 'snapshot_changed'
            return self._empty_result(decision, trace)
        trace['candidate_count'] = sum(len(items) for items in fields.values())
        if trace['unknown_namespace_count']:
            trace['excluded_unknown_namespace'] = trace['unknown_namespace_count']
        return RuntimeRelevantContext(**{key: tuple(value) for key, value in fields.items()},
            router=decision, trace=trace, degraded=degraded or errors_seen)

    @staticmethod
    def _identity(candidate):
        return (candidate.friend_id, candidate.namespace, candidate.id, candidate.pack_version,
            candidate.source.pack_id if candidate.original_scope == DataScope.PUBLIC_PACK else None)

    @classmethod
    def _candidate_trace(cls, inputs, selected):
        by_key, rrf_scores, paths = {}, {}, {}
        seen_by_route, position_by_route = {}, {}
        for route, input_rank, candidate in inputs:
            key = cls._identity(candidate)
            seen = seen_by_route.setdefault(route, set())
            if key not in seen:
                seen.add(key)
                position_by_route[route] = position_by_route.get(route, 0) + 1
                path_set = paths.setdefault(key, set())
                if candidate.retriever not in path_set:
                    rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (
                        60 + position_by_route[route])
                    path_set.add(candidate.retriever)
            row = by_key.setdefault(key, {'key': _digest(key), 'namespace': candidate.namespace,
                'route': route, 'inputs': [], 'raw_score': None,
                'final_score': None, 'input_rank': input_rank, 'output_rank': None,
                'signals': [], 'selected': False, 'diversity': 'not_selected',
                'excluded_by': 'mmr_limit'})
            row['inputs'].append({'route': route, 'rank': input_rank,
                'raw_score': candidate.raw_score if candidate.raw_score is not None else candidate.score})
            if row['raw_score'] is None:
                row['raw_score'] = candidate.raw_score if candidate.raw_score is not None else candidate.score
        for key, row in by_key.items():
            row['rrf'] = rrf_scores.get(key, 0.0)
        for item in selected:
            row = by_key[cls._identity(item)]
            row.update(rrf=item.raw_score, final_score=item.score,
                output_rank=item.rank, selected=True, diversity='mmr_selected', excluded_by=None)
            row['signals'] = [value for value in item.signals
                if value in ('RRF', 'time:recent', 'source:real_chat') or
                value.startswith('path:') and value[5:] in _ROUTE_NAMESPACES]
        return list(by_key.values())

    @classmethod
    def _direct_candidate_trace(cls, route, candidates):
        result = []
        for rank, item in enumerate(candidates, 1):
            result.append({'key': _digest(cls._identity(item)), 'namespace': item.namespace,
                'route': route, 'inputs': [{'route': route, 'rank': item.rank or rank,
                    'raw_score': item.raw_score if item.raw_score is not None else item.score}],
                'raw_score': item.raw_score if item.raw_score is not None else item.score,
                'rrf': item.raw_score if route == 'example' else None,
                'final_score': item.score, 'input_rank': item.rank or rank,
                'output_rank': rank, 'signals': [], 'selected': True,
                'diversity': 'example_specialized' if route == 'example' else 'persona_selected',
                'excluded_by': None})
        return result

    @staticmethod
    def _estimate(candidate):
        return len(candidate.content.encode('utf-8')) + 24

    @classmethod
    def _apply_budgets(cls, fields, budgets, trace):
        excluded = []
        records = {row['key']: row for row in trace['candidates']}

        def discard(bucket, candidate, code):
            estimate = cls._estimate(candidate)
            key = _digest(cls._identity(candidate))
            record = records.get(key)
            if record is not None:
                record.update(selected=False, excluded_by=code)
            excluded.append({'key': key, 'bucket': bucket, 'excluded_by': code,
                'estimated_bytes': estimate})

        kept_examples, examples_used = [], 0
        for item in fields['examples']:
            estimate = cls._estimate(item)
            if examples_used + estimate <= budgets.examples:
                kept_examples.append(item)
                examples_used += estimate
            else:
                discard('examples', item, 'examples_budget')
        fields['examples'] = kept_examples

        knowledge = fields['topics'] + [item for item in fields['structured']
                                        if item.namespace == 'entity']
        knowledge_order = sorted(knowledge, key=lambda item: (item.rank is None,
            item.rank or 0, -item.score, item.id))
        kept_knowledge, knowledge_used = set(), 0
        for item in knowledge_order:
            estimate = cls._estimate(item)
            if knowledge_used + estimate <= budgets.knowledge:
                kept_knowledge.add(cls._identity(item))
                knowledge_used += estimate
            else:
                discard('knowledge', item, 'knowledge_budget')
        fields['topics'] = [item for item in fields['topics']
                            if cls._identity(item) in kept_knowledge]
        fields['structured'] = [item for item in fields['structured']
            if item.namespace != 'entity' or cls._identity(item) in kept_knowledge]

        def persona_used():
            return sum(cls._estimate(item) for bucket in ('examples', 'persona', 'style')
                       for item in fields[bucket])

        for bucket in ('examples', 'persona', 'style'):
            while persona_used() > budgets.persona and fields[bucket]:
                discard(bucket, fields[bucket].pop(), 'persona_budget')
        trace['budget'] = {'estimate': 'utf8_bytes_plus_24',
            'knowledge_limit': budgets.knowledge, 'knowledge_used': knowledge_used,
            'examples_limit': budgets.examples,
            'examples_used': sum(cls._estimate(item) for item in fields['examples']),
            'persona_limit': budgets.persona,
            'persona_used': persona_used(), 'excluded': excluded}
