"""Scoped statistical DRAFTs; source chat and authority JSON remain separate."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from source_state import source_read, snapshot, require_snapshot
import hashlib
import json
import math
import uuid

from persona_analysis_input import _bounded_strings
from persona_scenes import SceneBehaviorAnalyzer
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import DataScope, InvalidSource


_RELIABILITY = {'REAL_RELATIONSHIP': .9, 'REAL_SINGLE_SPEAKER': .85,
                'REAL_PERSONA_POOL': .75, 'PUBLIC_DATASET': .65}


@dataclass(frozen=True)
class ClaimDraft:
    claim_id: str
    claim_key: str
    subject: str
    dimension: str
    scene: str
    trigger: str
    reaction: str
    claim: str
    strength: float | None
    confidence: float
    support_count: int
    opportunity_count: int
    session_count: int
    speaker_count: int
    support_observations: tuple[str, ...]
    counter_observations: tuple[str, ...]
    source_origins: tuple[str, ...]
    quality: str
    sample_status: str
    effective_support_count: float
    effective_opportunity_count: float
    coverage: float


@dataclass(frozen=True)
class AnalysisResult:
    run_id: str
    friend_id: str
    created_at: str
    source_revision: int
    input_hash: str
    settings: dict
    role_claim_drafts: tuple[ClaimDraft, ...]
    relationship_claim_drafts: tuple[ClaimDraft, ...]
    style_profile: dict
    scene_analysis: object
    sample_status: dict
    model: str
    prompt_version: str


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _tier(count):
    return 'insufficient' if count < 50 else 'low' if count < 200 else 'medium' if count <= 1000 else 'high'


def _settings(session_ids, targets, friends, use_model, pair_ids=None):
    settings = {'session_ids': list(session_ids), 'target_speaker_ids': list(targets),
            'friend_speaker_ids': list(friends), 'use_local_model': use_model,
            'formula_version': 'conditional-statistics-v1', 'wilson_z': 1.96,
            'minimum_support_episodes': 5, 'minimum_effective_support': 5,
            'minimum_sessions': 2, 'session_speaker_cap': 3,
            'public_actor_cap': 3, 'public_session_cap': 3,
            'source_reliability': dict(_RELIABILITY),
            'confidence_formula': 'coverage*reliability*diversity*(1-Wilson_width)',
            'diversity_formula': 'min(sessions/2,1); public additionally min(speakers/2,1)',
            'literal_exception': 'PRIVATE_DYAD_exact_quote_only'}
    if pair_ids is not None:
        selection = _bounded_strings(pair_ids, 'pair_ids')
        if not selection:
            raise InvalidSource('explicit pair selection must not be empty')
        settings['pair_ids'] = list(selection)
    return settings


def _metadata(scene, sessions):
    messages = {m.id: m for session in sessions for m in session.messages}
    selected = scene.analysis_input.sample_info.selected_target_speaker_ids
    rows = {}
    for episode in (*scene.analysis_input.role_pairs, *scene.analysis_input.relationship_events):
        role = hasattr(episode, 'target_bubbles')
        bubbles = episode.target_bubbles if role else tuple(messages[i] for i in episode.inbound_message_ids)
        origins = {m.source.origin.value for m in bubbles}
        actors = {m.speaker_id for m in bubbles}
        scopes = {m.source.data_scope for m in bubbles}
        # An ambiguous/mixed response stays an observation, never a combined source statistic.
        if len(origins) != 1 or len(actors) != 1 or len(scopes) != 1:
            continue
        origin, actor, data_scope = next(iter(origins)), next(iter(actors)), next(iter(scopes))
        if origin not in _RELIABILITY:
            continue
        episode_id = episode.id if role else episode.event_id
        session_id = episode.session_id if role else next(s.id for s in sessions
            if episode.inbound_message_ids[0] in {m.id for m in s.messages})
        cohort = 'explicit_target_pool:' + _json(sorted(selected)) if role else 'inbound_actor:' + actor
        rows[episode_id] = {'origin': origin, 'speaker': actor, 'scope': data_scope,
                            'session': session_id, 'cohort': cohort, 'role': role}
    return rows


def _drafts(scene, run_id, metadata):
    groups, literals, seen = defaultdict(list), [], set()
    for observation in scene.observations:
        meta = metadata.get(observation.episode_id)
        if meta is None or observation.session_id != meta['session']:
            continue
        identity = (observation.episode_id, observation.subject, observation.dimension)
        if identity in seen:
            continue
        seen.add(identity)
        if observation.quality == 'explicit_literal':
            if (not meta['role'] and meta['scope'] == DataScope.PRIVATE_FRIEND and
                    meta['origin'] == 'REAL_RELATIONSHIP' and observation.subject == 'DYAD' and
                    observation.evidence_spans and len(observation.evidence_spans) == 1 and
                    observation.response_pattern == observation.evidence_spans[0].text):
                literals.append(observation)
        elif (observation.quality in {'observed_statistic', 'observed_structure'} and
              observation.scene != 'unknown' and
              observation.subject == ('BOT_PERSONA' if meta['role'] else 'FRIEND')):
            key = (observation.subject, observation.dimension, observation.scene,
                   meta['origin'], meta['cohort'])
            groups[key].append(observation)

    output = []
    def make(key, supporting, opportunities, effective_support, effective_total, coverage, literal=False):
        subject, dimension, scene_name, origin, cohort = key
        reaction = supporting[0].response_pattern
        sessions = {m.session_id for m in supporting}
        speakers = {metadata[m.episode_id]['speaker'] for m in supporting}
        strength = effective_support / effective_total
        z, n = 1.96, effective_total
        width = 2 * z * math.sqrt(strength * (1 - strength) / n + z*z/(4*n*n)) / (1 + z*z/n)
        diversity = min(len(sessions) / 2, 1)
        if metadata[supporting[0].episode_id]['scope'] == DataScope.PUBLIC_DATASET:
            diversity *= min(len(speakers) / 2, 1)
        confidence = coverage * _RELIABILITY[origin] * diversity * (1 - width)
        claim_key = _hash([scene.friend_id, subject, dimension, scene_name, origin, cohort, reaction])
        support_ids = tuple(sorted(m.episode_id for m in supporting))
        counters = tuple(sorted(m.episode_id for m in opportunities if m.episode_id not in support_ids))
        output.append(ClaimDraft(_hash([run_id, claim_key]), claim_key, subject, dimension,
            scene_name, supporting[0].trigger, reaction,
            reaction if literal else '在' + scene_name + '条件下，观测反应：' + reaction,
            None if literal else strength, confidence, len(supporting), len(opportunities),
            len(sessions), len(speakers), support_ids, counters, (origin,),
            'explicit_literal' if literal else 'observed_statistic', _tier(len(opportunities)),
            effective_support, effective_total, coverage))

    scene_by_id = {row.episode_id: row.scene for row in scene.scenes}
    for key, opportunities in sorted(groups.items()):
        subject, dimension, scene_name, origin, cohort = key
        cells = Counter((m.session_id, metadata[m.episode_id]['speaker']) for m in opportunities)
        actor_counts = Counter(metadata[m.episode_id]['speaker'] for m in opportunities)
        session_counts = Counter(m.session_id for m in opportunities)
        weights = {m.episode_id: min(1, 3 / cells[(m.session_id, metadata[m.episode_id]['speaker'])])
                   for m in opportunities}
        if metadata[opportunities[0].episode_id]['scope'] == DataScope.PUBLIC_DATASET:
            weights = {m.episode_id: min(weights[m.episode_id],
                3 / actor_counts[metadata[m.episode_id]['speaker']], 3 / session_counts[m.session_id])
                for m in opportunities}
        universe = {m.episode_id for m in opportunities} | {episode_id for episode_id, meta in metadata.items()
            if meta['origin'] == origin and meta['cohort'] == cohort and
            meta['role'] == (subject == 'BOT_PERSONA') and scene_by_id.get(episode_id) == scene_name}
        coverage = len(opportunities) / len(universe)
        effective_total = sum(weights.values())
        reactions = defaultdict(list)
        for observation in opportunities:
            reactions[observation.response_pattern].append(observation)
        for reaction, supporting in sorted(reactions.items()):
            effective_support = sum(weights[m.episode_id] for m in supporting)
            if len(supporting) >= 5 and effective_support >= 5 and len({m.session_id for m in supporting}) >= 2:
                make(key, supporting, opportunities, effective_support, effective_total, coverage)
    for observation in literals:
        meta = metadata[observation.episode_id]
        make(('DYAD', observation.dimension, observation.scene, meta['origin'], meta['cohort']),
             [observation], [observation], 1., 1., 1., literal=True)
    # Repeated literal wording shares the stable key; merge into a single literal DRAFT.
    return tuple({draft.claim_key: draft for draft in output}.values())


class PersonaDistiller:
    def __init__(self, scope, pair_store):
        self.analyzer = SceneBehaviorAnalyzer(scope, pair_store)
        self.scope = self.analyzer.scope
        self.pair_store = pair_store

    @source_read
    def analyze(self, session_ids, target_speaker_ids=(), friend_speaker_ids=(), use_local_model=False, cancel=None,
                *, pair_ids=None):
        sessions = _bounded_strings(session_ids, 'session_ids')
        targets = _bounded_strings(target_speaker_ids, 'target_speaker_ids')
        friends = _bounded_strings(friend_speaker_ids, 'friend_speaker_ids')
        settings = _settings(sessions, targets, friends, use_local_model, pair_ids)
        scene = self.analyzer.analyze(sessions, targets, friends, use_local_model, cancel,
                                      pair_ids=settings.get('pair_ids'))
        loaded = tuple(self.pair_store.dialogues.load_session(i) for i in sessions)
        if self.pair_store.revision != scene.source_revision:
            raise Conflict('source revision changed during distillation')
        run_id = uuid.uuid4().hex
        drafts = _drafts(scene, run_id, _metadata(scene, loaded))
        return AnalysisResult(run_id, self.scope.friend_id, datetime.now(timezone.utc).isoformat(),
            scene.source_revision, _hash(asdict(scene.analysis_input)), settings,
            tuple(d for d in drafts if d.subject == 'BOT_PERSONA'),
            tuple(d for d in drafts if d.subject != 'BOT_PERSONA'), scene.style_profile, scene,
            {'sample_tier': _tier(len(scene.scenes)), 'dimensions': [asdict(r) for r in scene.dimension_status],
             'rule_estimates_excluded': sum(o.quality == 'rule_estimate' for o in scene.observations),
             'model_proposals_excluded': len(scene.model_proposals)}, scene.model_version, scene.prompt_version)

    def save_result(self, result, expected_revision):
        source_token = snapshot(self.scope)
        if not isinstance(result, AnalysisResult) or result.friend_id != self.scope.friend_id:
            raise ScopeMismatch('analysis result belongs to another scope or type')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be a nonnegative integer')
        db = self.pair_store.db
        if self.pair_store.dialogues.store.readonly or db.execute('PRAGMA query_only').fetchone()[0]:
            raise Unavailable('readonly catalog cannot save analysis results')
        settings = result.settings
        if settings != _settings(tuple(settings['session_ids']), tuple(settings['target_speaker_ids']),
                                  tuple(settings['friend_speaker_ids']), settings['use_local_model'],
                                  settings.get('pair_ids')):
            raise InvalidSource('analysis settings differ from the frozen formula')
        fresh = self.analyzer.analyze(settings['session_ids'], settings['target_speaker_ids'],
                                      settings['friend_speaker_ids'], use_local_model=False,
                                      pair_ids=settings.get('pair_ids'))
        loaded = tuple(self.pair_store.dialogues.load_session(i) for i in settings['session_ids'])
        drafts = _drafts(fresh, result.run_id, _metadata(fresh, loaded))
        if (result.scene_analysis.friend_id != self.scope.friend_id or
                result.scene_analysis.analysis_input != fresh.analysis_input or
                result.scene_analysis.observations != fresh.observations or
                result.scene_analysis.source_refs != fresh.source_refs or
                result.input_hash != _hash(asdict(fresh.analysis_input)) or
                {k: v for k, v in result.style_profile.items() if k != 'generated_at'} !=
                {k: v for k, v in fresh.style_profile.items() if k != 'generated_at'} or
                result.role_claim_drafts != tuple(d for d in drafts if d.subject == 'BOT_PERSONA') or
                result.relationship_claim_drafts != tuple(d for d in drafts if d.subject != 'BOT_PERSONA')):
            raise InvalidSource('analysis draft differs from verified source observations')
        all_drafts = (*result.role_claim_drafts, *result.relationship_claim_drafts)
        scopes = {ref.data_scope for ref in fresh.source_refs}
        effective_scope = (DataScope.PRIVATE_FRIEND if DataScope.PRIVATE_FRIEND in scopes else
                           DataScope.PUBLIC_PACK if DataScope.PUBLIC_PACK in scopes else DataScope.PUBLIC_DATASET)
        summary = {'run_id': result.run_id, 'friend_id': result.friend_id, 'created_at': result.created_at,
            'source_revision': result.source_revision, 'input_hash': result.input_hash,
            'settings': settings, 'claims': [asdict(d) for d in all_drafts],
            'style_profile': result.style_profile, 'sample_status': result.sample_status,
            'source_refs': [asdict(ref) for ref in fresh.source_refs], 'data_scope': effective_scope.value,
            'observations': [{'episode_id': o.episode_id, 'session_id': o.session_id,
                'subject': o.subject, 'dimension': o.dimension, 'scene': o.scene, 'quality': o.quality}
                for o in fresh.observations], 'model': result.model, 'prompt_version': result.prompt_version}
        payload, settings_json = _json(summary), _json(settings)
        claim_jsons = tuple(_json(asdict(d)) for d in all_drafts)
        if sum(len(s.encode('utf-8')) for s in (payload, settings_json, *claim_jsons)) > 2 * 1024**2:
            raise Unavailable('analysis persistence exceeds the 2MiB summary budget')
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            # Source readback is repeated under the write lock, without invoking a model.
            if tuple(self.pair_store.dialogues.load_session(i) for i in settings['session_ids']) != loaded:
                raise InvalidSource('source changed before analysis save')
            existing = db.execute('SELECT result_json FROM persona_analysis_runs WHERE friend_id=? AND id=?',
                                  (self.scope.friend_id, result.run_id)).fetchone()
            if existing is not None:
                if existing['result_json'] != payload:
                    raise Conflict('run id already stores different content')
                db.execute('ROLLBACK')
                return self.pair_store.revision
            if self.pair_store.revision != expected_revision or result.source_revision != expected_revision:
                raise Conflict('analysis catalog revision changed')
            db.execute('INSERT INTO persona_analysis_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                (result.friend_id, result.run_id, 'DRAFT', result.created_at, result.source_revision,
                 result.input_hash, settings_json, payload, result.model, result.prompt_version))
            db.executemany('INSERT INTO persona_claims VALUES(?,?,?,?,?,?,?,?,?)', [
                (result.friend_id, d.claim_id, result.run_id, d.claim_key, d.subject, d.dimension,
                 d.scene, 'DRAFT', body) for d, body in zip(all_drafts, claim_jsons)])
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (result.friend_id,))
            db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
