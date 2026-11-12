"""Scene-bound persona claims and compact style from verified versions."""
from source_state import source_read
from contextlib import closing
from dataclasses import replace
import json
import re
import sqlite3
import time

from dataset_normalizer import DatasetNormalizer
from dataset_registry import DatasetRegistry
from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import DialogueStore
from persona_pack_bindings import PackBindingStore
from persona_store import PersonaStore, Unavailable
from persona_types import (Candidate, DataScope, FriendScope, InvalidSource, RetrievalBatch,
                           ScopeMismatch, SourceOrigin, SourceRef)
from persona_versions import PersonaVersionStore


_CJK = re.compile(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+')
_PUBLIC_ORIGINS = {SourceOrigin.PUBLIC_DATASET, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL}
_HUMAN_TARGET_ORIGINS = _PUBLIC_ORIGINS | {SourceOrigin.REAL_RELATIONSHIP}


class PersonaRetriever:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    @source_read
    def retrieve(self, request, limit):
        if request.friend_id != self.scope.friend_id:
            raise ScopeMismatch('request belongs to another friend')
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InvalidSource('persona limit must be 1..20')
        started = time.perf_counter()
        candidates, errors = [], []
        requested_namespaces = set(request.filters.namespace)
        if requested_namespaces and requested_namespaces.isdisjoint({'persona', 'style'}):
            pass
        elif request.filters.privacy_scope == DataScope.PRIVATE_FRIEND:
            try:
                candidates.extend(self._private(request, limit, errors))
            except Unavailable as exc:
                errors.append(type(exc).__name__ + ': private Persona version unavailable')
        elif request.filters.privacy_scope == DataScope.PUBLIC_PACK:
            try:
                candidates.extend(self._public(request, limit, errors))
            except Unavailable as exc:
                errors.append(type(exc).__name__ + ': bound public Persona version unavailable')
        else:
            raise ScopeMismatch('unsupported Persona privacy scope')
        if requested_namespaces:
            candidates = [item for item in candidates if item.namespace in requested_namespaces]
        candidates = _select_claims(candidates, request, limit)
        style = next((item for item in candidates if item.namespace == 'style'), None)
        claims = [item for item in candidates if item.namespace == 'persona']
        ordered = claims + ([style] if style is not None else [])
        budget = max(0, request.budgets.persona - request.budgets.examples)
        output = []
        used = 0
        for item in ordered:
            cost = len(item.content)
            if used + cost > budget:
                continue
            used += cost
            output.append(item)
        return RetrievalBatch(candidates=tuple(output), elapsed_ms=(
            time.perf_counter() - started) * 1000,
            degraded=bool(errors), errors=tuple(errors))

    def _private(self, request, limit, errors):
        directory = self.scope.friend_dir
        pointer = directory / 'persona_versions' / 'active.json'
        sidecar = directory / 'persona.sqlite3'
        if not pointer.is_file():
            return ()
        if not sidecar.is_file() or _schema_version(sidecar) < 14:
            errors.append('Unavailable: active Persona sidecar is absent or needs migration')
            return ()
        pairs = _readonly_pairs(self.scope)
        try:
            store = PersonaVersionStore(self.scope, pairs)
            active = store.read_active()
            if active is None:
                return ()
            manifest, profile, style = active['manifest'], active['profile'], active['style']
            store._recheck(manifest)
            settings = manifest.get('settings', {})
            session_ids = settings.get('session_ids')
            if not isinstance(session_ids, list) or not session_ids:
                raise InvalidSource('active Persona version lacks its source sessions')
            store._completed(manifest['run_id'], manifest['source_content_hash'])
            messages, loaded_messages = {}, []
            for session_id in session_ids:
                session = pairs.dialogues.load_session(session_id)
                loaded_messages.extend(session.messages)
                messages.update((message.id, message.source) for message in session.messages)
            target_ids = set(settings.get('target_speaker_ids', ()))
            style_source = _private_style_source(loaded_messages, target_ids)
            return _version_candidates(profile, style, manifest, messages, self.scope,
                request, limit, source_scope=DataScope.PRIVATE_FRIEND, style_source=style_source)
        finally:
            pairs.close()

    def _public(self, request, limit, errors):
        sidecar = self.scope.friend_dir / 'persona.sqlite3'
        if not sidecar.is_file():
            return ()
        bindings = PackBindingStore(self.scope, readonly=True)
        try:
            allowed_scope = bindings.bound_scope()
            allowed = allowed_scope.allowed_pack_versions & request.allowed_pack_versions
            output = []
            for pack_id, version in sorted((item.pack_id, item.version) for item in allowed):
                pack = bindings.read_pack(pack_id, version)
                sources = {}
                for example in pack['examples']:
                    for bubble in example['user'] + example['target_bubbles'] + [
                            bubble for turn in example['context'] for bubble in turn['bubbles']]:
                        sources[bubble['id']] = SourceRef(**bubble['source'])
                first_example = next(iter(pack['examples']), None)
                style_source = (SourceRef(**first_example['target_bubbles'][0]['source'])
                    if first_example and first_example['target_bubbles'] else None)
                public_manifest = dict(pack['manifest'])
                public_manifest['source_refs'] = pack['manifest']['source_refs']
                output.extend(_version_candidates(pack['persona_profile'], pack['style_profile'],
                    public_manifest, sources, self.scope, request, limit,
                    source_scope=DataScope.PUBLIC_PACK, pack_id=pack_id, version=version,
                    style_source=style_source))
            return output
        finally:
            bindings.close()


def _version_candidates(profile, style, manifest, sources, scope, request, limit, *,
                         source_scope, pack_id=None, version=None, style_source=None):
    expected_friend_id = None if source_scope == DataScope.PUBLIC_PACK else scope.friend_id
    if (profile.get('schema_version') != 'persona-profile-v1' or
            style.get('schema_version') != 'style-profile-v1'):
        raise InvalidSource('Persona or Style JSON schema differs')
    if profile.get('friend_id') != expected_friend_id or style.get('friend_id') != expected_friend_id:
        raise ScopeMismatch('Persona version belongs to another scope')
    expected_version = version or manifest.get('version')
    if (profile.get('version') != expected_version or
            style.get('version') != expected_version or manifest.get('version') != expected_version):
        raise InvalidSource('Persona JSON version differs from its manifest')
    refs = {}
    for claim in profile.get('claims', ()):
        base = profile.get('manual_claims', {}).get(claim.get('claim_key')) if (
            claim.get('quality') == 'MANUAL_LOCK') else None
        evidence = claim.get('evidence') or (base or {}).get('evidence') or ()
        source = _claim_source(claim, evidence, sources, scope, source_scope, pack_id, version)
        if source is not None:
            refs[claim.get('claim_key')] = source
    claims = []
    for claim in profile.get('claims', ()):
        key = claim.get('claim_key')
        source = refs.get(key)
        if source is None:
            continue
        content = {name: claim.get(name) for name in (
            'subject', 'dimension', 'scene', 'trigger', 'response_pattern', 'claim',
            'strength', 'confidence', 'quality', 'source_origin')}
        claims.append(Candidate(id=key, namespace='persona', type='persona_claim',
            content=json.dumps(content, ensure_ascii=False, separators=(',', ':')),
            score=0.0, source=source, reason='scene/evidence source verified from active version',
            retriever='persona', friend_id=scope.friend_id, timestamp=None,
            confidence=claim.get('confidence') or 0.0, original_scope=source.data_scope,
            pack_version=source.pack_version))
    if style_source is not None:
        if source_scope == DataScope.PRIVATE_FRIEND and (
                style_source.data_scope != DataScope.PRIVATE_FRIEND or
                style_source.friend_id != scope.friend_id):
            raise ScopeMismatch('private Style source is outside the current friend')
        if source_scope == DataScope.PUBLIC_PACK:
            if style_source.origin not in _PUBLIC_ORIGINS or style_source.friend_id is not None:
                raise ScopeMismatch('public Style source is private or nonhuman')
            style_source = replace(style_source, data_scope=DataScope.PUBLIC_PACK,
                                   pack_id=pack_id, pack_version=version)
        summary = _style_summary(style)
        if summary:
            claims.append(Candidate(id='style:' + str(profile['version']), namespace='style',
                type='style_summary', content=json.dumps(summary, ensure_ascii=False,
                    separators=(',', ':')), score=1.0, source=style_source,
                reason='compressed observed Style and manual overrides from active JSON',
                retriever='persona', friend_id=scope.friend_id, timestamp=None,
                confidence=0.0, original_scope=style_source.data_scope,
                pack_version=style_source.pack_version))
    return claims


def _claim_source(claim, evidence, sources, scope, source_scope, pack_id, version):
    if not isinstance(claim, dict) or not isinstance(evidence, (list, tuple)):
        return None
    if claim.get('quality') == 'MANUAL_LOCK' and not evidence:
        return None
    actual = []
    for item in evidence:
        if not isinstance(item, dict) or item.get('claim_id') != claim.get('claim_id'):
            raise InvalidSource('Persona evidence locator does not match its Claim')
        message_id = item.get('source_message_id')
        source = sources.get(message_id)
        if source is None:
            raise InvalidSource('Persona evidence source is no longer available')
        if (source.origin is None or source.speaker_id != item.get('speaker_id') or
                source.origin.value != item.get('source_origin') or
                source.data_scope.value != item.get('data_scope')):
            raise InvalidSource('Persona evidence provenance differs from the current source')
        if source_scope == DataScope.PRIVATE_FRIEND:
            if source.data_scope != DataScope.PRIVATE_FRIEND or source.friend_id != scope.friend_id:
                raise ScopeMismatch('private Persona evidence is outside the current friend')
        elif source.origin not in _PUBLIC_ORIGINS or source.data_scope not in (
                DataScope.PUBLIC_DATASET, DataScope.PUBLIC_PACK) or source.friend_id is not None:
            raise ScopeMismatch('public Persona evidence contains private or nonhuman provenance')
        actual.append(source)
    source = next((ref for ref, item in zip(actual, evidence)
                   if item.get('evidence_role') == 'support'), None)
    if source is None and actual:
        source = actual[0]
    if source is not None and source_scope == DataScope.PUBLIC_PACK:
        source = replace(source, data_scope=DataScope.PUBLIC_PACK, pack_id=pack_id,
                         pack_version=version)
    return source


def _style_summary(style):
    observed = style.get('observed', {})
    reply = observed.get('message_length', {}).get('reply', {})
    summary = {
        'sample_status': observed.get('sample_info', {}).get('sample_status'),
        'pair_count': observed.get('sample_info', {}).get('pair_count'),
        'reply_length': {key: reply.get(key) for key in ('median', 'p25', 'p75')},
        'bubble_rates': observed.get('bubble_behavior', {}).get('rates', {}),
        'question_rate': observed.get('question_behavior', {}).get('question_rate', {}).get('rate'),
        'period_rate': observed.get('punctuation', {}).get('period_rate', {}).get('rate'),
        'short_fragment_rate': observed.get('fragment_usage', {}).get(
            'short_no_terminator_rate', {}).get('rate'),
        'common_expressions': [item.get('text') for item in observed.get(
            'common_expressions', {}).get('items', ())[:4] if isinstance(item, dict)],
        'manual_overrides': style.get('manual_overrides', {}),
    }
    while summary['common_expressions'] and len(json.dumps(summary, ensure_ascii=False)) > 300:
        summary['common_expressions'].pop()
    if len(json.dumps(summary, ensure_ascii=False)) > 300:
        summary.pop('short_fragment_rate', None)
    if len(json.dumps(summary, ensure_ascii=False)) > 300:
        summary.pop('period_rate', None)
    return summary


def _select_claims(candidates, request, limit):
    query_terms = _terms(request.query + ' ' + request.recent_context)
    complex_scene = any(marker in request.scene.casefold() for marker in (
        'complex', 'high_dependency', 'deep_reasoning'))
    cap = min(limit, 12 if complex_scene else 6)
    ranked = []
    styles = []
    for candidate in candidates:
        if candidate.namespace == 'style':
            styles.append(candidate)
            continue
        claim = json.loads(candidate.content)
        scene_match = claim.get('scene') == request.scene
        text_terms = _terms(' '.join(str(claim.get(key) or '') for key in (
            'dimension', 'scene', 'trigger', 'response_pattern', 'claim')))
        overlap = len(query_terms & text_terms)
        if not scene_match and overlap == 0:
            continue
        score = (2 if scene_match else 0) + overlap
        locked = claim.get('quality') == 'MANUAL_LOCK'
        ranked.append((locked, score, candidate.confidence, candidate.id,
                       replace(candidate, score=float(score), reason=(
                           'manual lock; ' if locked else '') +
                           ('scene match' if scene_match else 'lexical evidence match'))))
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    return [item[-1] for item in ranked[:cap]] + styles[:1]


def _terms(text):
    values = []
    for token in _CJK.findall(text.casefold()):
        if token[0] >= '\u3400':
            values.extend(token[index:index + 2] for index in range(len(token) - 1))
            values.extend(token)
        else:
            values.append(token)
    return set(values)


def _schema_version(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            return db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def _private_style_source(messages, target_ids):
    return next((message.source for message in messages
        if message.target_selected and message.speaker_id in target_ids and
        message.source.origin in _HUMAN_TARGET_ORIGINS), None)


def _readonly_pairs(scope):
    """Adapt the existing pair reader to a read-only catalog connection."""
    catalog = PersonaStore(scope.friend_id, scope.friend_dir, readonly=True,
                           project_root=scope.project_root)
    registry = None
    try:
        dialogues = DialogueStore.__new__(DialogueStore)
        dialogues.scope = scope
        dialogues.store = catalog
        registry = DatasetRegistry(scope, readonly=True)
        dialogues.registry = registry
        dialogues.normalizer = DatasetNormalizer(scope, dialogues.registry)
        dialogues.db = catalog.db
        pairs = PairStore.__new__(PairStore)
        pairs.scope = scope
        pairs.dialogues = dialogues
        pairs.db = catalog.db
        pairs.builder = DialoguePairBuilder(scope)
        return pairs
    except Exception:
        try:
            if registry is not None:
                registry.close()
        finally:
            catalog.close()
        raise
