"""Friend-bound Example retrieval using the source-verified index and rank fusion."""
from source_state import source_read
import json
import re
import time
from dataclasses import replace

from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import DataScope, FriendScope, InvalidSource, RetrievalBatch
from retrieval_ranking import rank_candidates
from semantic_index import SemanticIndex


_DENSE_MIN_SCORE = 0.2  # Conservative, uncalibrated admission heuristic.
_DENSE_STRONG_SCORE = 0.45  # Uncalibrated relevance threshold, used only before fusion.
_FTS_MIN_COVERAGE = 0.5  # Uncalibrated user/context query-term coverage threshold.


class ExampleRetriever:
    def __init__(self, scope, embedding_provider=None, *, source_refs=()):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.embedding_provider = embedding_provider
        self.source_refs = tuple(source_refs)
        self.index = SemanticIndex(self.scope, readonly=True, embedding_provider=embedding_provider)

    def close(self):
        self.index.close()

    def _head_identity(self):
        head = self.index.metadata()
        return None if head is None else (head['generation'], head['revision'])

    @source_read
    def retrieve(self, request, limit):
        started = time.perf_counter()
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or not 1 <= limit <= MAX_EXAMPLES:
            raise InvalidSource(f'example limit must be 1..{MAX_EXAMPLES}')
        if request.filters.privacy_scope not in (DataScope.PRIVATE_FRIEND, DataScope.PUBLIC_PACK):
            raise ScopeMismatch('unsupported Example privacy scope')
        if request.filters.namespace and 'example' not in request.filters.namespace:
            return RetrievalBatch(candidates=(), elapsed_ms=0.0)

        scene_limit = _scene_cap(request)

        # Retrieve a bounded pool, allowing fusion and MMR to choose below the cap.
        pool_limit = min(50, max(20, min(limit, scene_limit) * 3))
        filtered = replace(request, filters=replace(request.filters, namespace=('example',)))
        initial_head = self.index.metadata()
        initial_identity = _head_key(initial_head)
        keyword_batch = self.index.keyword(filtered, pool_limit)
        if self._head_identity() != initial_identity:
            return _empty_batch(started, list(keyword_batch.errors) +
                ['Conflict: semantic index generation changed'])
        semantic_batch = self.index.dense(filtered, pool_limit, source_refs=self.source_refs)
        errors = list(keyword_batch.errors + semantic_batch.errors)
        degraded = keyword_batch.degraded or semantic_batch.degraded
        if self._head_identity() != initial_identity:
            return _empty_batch(started, errors + ['Conflict: semantic index generation changed'])

        payloads = {}
        keyword_candidates, keyword_coverage = [], {}
        for item in keyword_batch.candidates:
            key = _candidate_key(item)
            raw, trigger_text = _trigger_payload(item)
            coverage = _query_coverage(request.query, trigger_text)
            if coverage >= _FTS_MIN_COVERAGE:
                payloads[key] = raw
                keyword_coverage[key] = coverage
                keyword_candidates.append(replace(item, content=trigger_text))

        dense_candidates, dense_scores = [], {}
        for item in semantic_batch.candidates:
            score = item.raw_score
            if score is None or score < _DENSE_MIN_SCORE:
                continue
            key = _candidate_key(item)
            raw, trigger_text = _trigger_payload(item)
            payloads[key] = raw
            dense_scores[key] = max(score, dense_scores.get(key, score))
            dense_candidates.append(replace(item, content=trigger_text))

        strong_keys = {key for key, score in dense_scores.items() if score >= _DENSE_STRONG_SCORE}
        weak_keys = ({key for key, score in dense_scores.items()
                      if _DENSE_MIN_SCORE <= score < _DENSE_STRONG_SCORE} |
                     set(keyword_coverage)) - strong_keys
        output_limit = min(limit, scene_limit, len(strong_keys) + min(len(weak_keys), 5))
        if output_limit <= 0:
            return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter()-started)*1000,
                degraded=degraded, errors=tuple(dict.fromkeys(errors)))

        ranking_batches = [replace(keyword_batch, candidates=tuple(keyword_candidates)),
                           replace(semantic_batch, candidates=tuple(dense_candidates))]
        result = rank_candidates(self.scope, filtered, ranking_batches, max(20, scene_limit))
        strong_ranked = [item for item in result.candidates if _candidate_key(item) in strong_keys]
        weak_ranked = [item for item in result.candidates if _candidate_key(item) in weak_keys]
        selected = (strong_ranked + weak_ranked[:5])[:output_limit]

        if self._head_identity() != initial_identity:
            return _empty_batch(started, errors + ['Conflict: semantic index generation changed'])

        # Re-read each selected indexed locator from the starting generation. Keep the
        # display payload captured during recall; this pass only validates its source.
        where, values = self.index._where(filtered, initial_head) if initial_head else ('0', [])
        candidates = []
        used_budget = 0
        for candidate in selected:
            candidate.validate_scope(self.scope)
            key = _candidate_key(candidate)
            raw = payloads[key]
            try:
                row = self._selected_row(candidate, initial_head, where, values)
                self.index._candidate(row, candidate.raw_score if candidate.raw_score is not None else 0,
                                      'example', filtered)
            except (InvalidSource, Conflict, Unavailable) as exc:
                errors.append(type(exc).__name__ + ': selected Example source invalid')
                degraded = True
                continue
            display = {
                'user': _display_bubbles(raw['user'], 'user'),
                'context': _display_context(raw['context']),
                'target_bubbles': _display_bubbles(raw['target_bubbles'], 'target_bubbles'),
                **{name: raw[name] for name in ('scene', 'emotion', 'topic', 'reaction_type', 'source_origin')},
            }
            display['relationship_tags'] = _json_value(raw['relationship_tags'], 'relationship_tags')
            content = json.dumps(display, ensure_ascii=False, separators=(',', ':'))
            # UTF-8 bytes conservatively proxy prompt tokens for multilingual content;
            # preserve the whole target payload and omit examples that do not fit.
            estimate = len(content.encode('utf-8')) + 24
            if used_budget + estimate > request.budgets.examples:
                continue
            used_budget += estimate
            candidates.append(replace(candidate, content=content))
        if self._head_identity() != initial_identity:
            return _empty_batch(started, errors + ['Conflict: semantic index generation changed'])
        return RetrievalBatch(candidates=tuple(candidates),
            elapsed_ms=(time.perf_counter()-started)*1000,
            degraded=degraded,
            errors=tuple(dict.fromkeys(errors)))

    def _selected_row(self, candidate, head, where, values):
        if head is None:
            raise InvalidSource('semantic index head is unavailable')
        pack_id = candidate.source.pack_id if candidate.original_scope == DataScope.PUBLIC_PACK else None
        rows = self.index.db.execute('SELECT d.* FROM semantic_documents d WHERE ' + where +
            ' AND d.namespace=? AND d.id=? AND d.original_scope=? AND d.pack_id IS ? AND d.pack_version IS ? LIMIT 2',
            list(values) + [candidate.namespace, candidate.id, candidate.original_scope.value,
                            pack_id, candidate.pack_version]).fetchmany(2)
        if len(rows) != 1:
            raise InvalidSource('selected Example locator is no longer uniquely available')
        return rows[0]


def _bubble_texts(value):
    if not isinstance(value, list):
        raise InvalidSource('Example bubbles must be a list')
    result = []
    for bubble in value:
        if isinstance(bubble, dict):
            text = bubble.get('text')
            if isinstance(text, str):
                result.append(text)
    return result


def _head_key(head):
    return None if head is None else (head['generation'], head['revision'])


# User rule (2026-10-09): ordinary scenes take 1-8 examples, medium 8-24, complex 24-48.
# The lower bound is not padded: weakly related examples are still dropped.
MEDIUM_SCENES = frozenset({'concrete_question', 'knowledge_discussion', 'study_help',
                           'celebration', 'playful_tease', 'unfinished_topic'})
COMPLEX_SCENES = frozenset({'emotion_low', 'misunderstanding', 'conflict'})
MAX_EXAMPLES = 48


def scene_cap(scene):
    # 2026-10-09 user: caps shrunk from 8/24/48 to 3/6/12 to save tokens per reply
    scene = scene.strip().casefold()
    return 12 if scene in COMPLEX_SCENES else 6 if scene in MEDIUM_SCENES else 3


def scene_budget(scene):
    """Examples token budget (UTF-8 byte proxy, ~500 per example) matching the scene cap."""
    return scene_cap(scene) * 500


def _scene_cap(request):
    return scene_cap(request.scene)


def _empty_batch(started, errors):
    return RetrievalBatch(candidates=(), elapsed_ms=(time.perf_counter()-started)*1000,
        degraded=True, errors=tuple(dict.fromkeys(errors)))


def _candidate_key(item):
    pack_id = item.source.pack_id if item.original_scope == DataScope.PUBLIC_PACK else None
    return (item.friend_id, item.namespace, item.id, item.pack_version, pack_id)


def _trigger_payload(item):
    try:
        raw = json.loads(item.content)
        user = _json_value(raw['user'], 'user')
        context = _json_value(raw['context'], 'context')
        trigger_text = ' '.join(_bubble_texts(user) + _context_texts(context))
        raw.update(user=user, context=context,
                   target_bubbles=_json_value(raw['target_bubbles'], 'target_bubbles'))
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidSource('indexed Example trigger payload is invalid') from exc
    return raw, trigger_text


def _query_coverage(query, trigger_text):
    query_terms = _lexical_terms(query, query=True)
    if not query_terms:
        return 0.0
    trigger_terms = _lexical_terms(trigger_text, query=False)
    return len(query_terms & trigger_terms) / len(query_terms)


def _lexical_terms(text, *, query):
    terms = set()
    for word in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text.casefold()):
        if '\u3400' <= word[0] <= '\u9fff':
            if query:
                terms.update(word if len(word) == 1 else
                             (word[index:index + 2] for index in range(len(word) - 1)))
            else:
                terms.update(word)
                terms.update(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.add(word)
    return terms


def _context_texts(value):
    if not isinstance(value, list):
        raise InvalidSource('Example context must be a list')
    result = []
    for turn in value:
        if not isinstance(turn, dict) or not isinstance(turn.get('bubbles'), list):
            continue
        result.extend(_bubble_texts(turn['bubbles']))
    return result


def _json_value(value, field):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise InvalidSource('Example ' + field + ' JSON is invalid') from exc
    if not isinstance(value, list):
        raise InvalidSource('Example ' + field + ' must be a JSON list')
    return value


def _display_bubbles(value, field):
    bubbles = _json_value(value, field)
    result = []
    for bubble in bubbles:
        if not isinstance(bubble, dict):
            raise InvalidSource('Example ' + field + ' bubble must be an object')
        display = {name: bubble[name] for name in ('text', 'message_type') if name in bubble}
        if bubble.get('media_id') is not None:
            display['has_media'] = True
        result.append(display)
    return result


def _display_context(value):
    turns = _json_value(value, 'context')
    result = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get('bubbles'), list):
            raise InvalidSource('Example context turn must contain bubbles')
        bubbles = turn['bubbles']
        roles = [bubble.get('target_role') for bubble in bubbles if isinstance(bubble, dict)]
        display_turn = {'bubbles': _display_bubbles(bubbles, 'context')}
        allowed_roles = {'input', 'target', 'context', 'unmapped'}
        if (roles and len(roles) == len(bubbles) and all(role in allowed_roles for role in roles)
                and len(set(roles)) == 1):
            display_turn['role'] = roles[0]
        result.append(display_turn)
    return result
