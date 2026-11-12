"""Revalidate DRAFT evidence against current source before claim validation."""
from source_state import snapshot, require_snapshot
from source_state import source_read
from dataclasses import asdict, dataclass

from persona_distiller import AnalysisResult, _drafts, _hash, _json, _metadata, _settings
from persona_scenes import SceneBehaviorAnalyzer
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import DataScope, InvalidSource, SourceOrigin


_TARGET_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL, SourceOrigin.PUBLIC_DATASET}


@dataclass(frozen=True)
class EvidenceLocator:
    claim_id: str
    evidence_role: str
    episode_id: str
    pair_id: str | None
    relationship_event_id: str | None
    subject: str
    dimension: str
    scene: str
    source_message_id: str
    span_start: int | None
    span_end: int | None
    span_text: str | None
    source_origin: str
    data_scope: str
    speaker_id: str
    quality: str


@dataclass(frozen=True)
class ValidatedAnalysis:
    result: AnalysisResult
    evidence: tuple[EvidenceLocator, ...]
    source_content_hash: str
    validated_source_refs: tuple
    validation_revision: int


class EvidenceValidator:
    def __init__(self, scope, pair_store):
        self.analyzer = SceneBehaviorAnalyzer(scope, pair_store)
        self.scope = self.analyzer.scope
        self.pair_store = pair_store

    @source_read
    def validate(self, result):
        if not isinstance(result, AnalysisResult) or result.friend_id != self.scope.friend_id:
            raise ScopeMismatch('analysis result belongs to another scope or type')
        if result.scene_analysis is None or type(result.source_revision) is not int or \
                result.source_revision < 0:
            raise InvalidSource('analysis result has no valid source snapshot')
        settings = result.settings
        try:
            expected_settings = _settings(tuple(settings['session_ids']),
                tuple(settings['target_speaker_ids']), tuple(settings['friend_speaker_ids']),
                settings['use_local_model'], settings.get('pair_ids'))
        except (KeyError, TypeError):
            raise InvalidSource('analysis settings are incomplete') from None
        if settings != expected_settings:
            raise InvalidSource('analysis settings differ from the frozen source selection')

        fresh = self.analyzer.analyze(settings['session_ids'], settings['target_speaker_ids'],
            settings['friend_speaker_ids'], use_local_model=False, pair_ids=settings.get('pair_ids'))
        source_hash = _hash(asdict(fresh.analysis_input))
        if (result.input_hash != source_hash or
                result.scene_analysis.analysis_input != fresh.analysis_input or
                result.scene_analysis.source_refs != fresh.source_refs or
                result.scene_analysis.observations != fresh.observations or
                _without_generated_at(result.style_profile) != _without_generated_at(fresh.style_profile)):
            raise InvalidSource('analysis no longer matches verified source content')
        if result.scene_analysis.dimension_status != fresh.dimension_status or \
                not _scene_previews_valid(result.scene_analysis, fresh):
            raise InvalidSource('scene labels or status differ from deterministic analysis')

        sessions = tuple(self.pair_store.dialogues.load_session(session_id)
                         for session_id in settings['session_ids'])
        drafts = _drafts(fresh, result.run_id, _metadata(fresh, sessions))
        role = tuple(draft for draft in drafts if draft.subject == 'BOT_PERSONA')
        relationship = tuple(draft for draft in drafts if draft.subject != 'BOT_PERSONA')
        if result.role_claim_drafts != role or result.relationship_claim_drafts != relationship:
            raise InvalidSource('claim drafts or support/counter locators were altered')
        rows = _evidence_rows(fresh, sessions, drafts)
        return ValidatedAnalysis(result, rows, source_hash, fresh.source_refs,
                                 self.pair_store.revision)

    def save(self, validated, expected_revision):
        source_token = snapshot(self.scope)
        if not isinstance(validated, ValidatedAnalysis) or \
                validated.result.friend_id != self.scope.friend_id:
            raise ScopeMismatch('validated analysis belongs to another scope or type')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be a nonnegative integer')
        current = self.validate(validated.result)
        if (current.source_content_hash != validated.source_content_hash or
                current.evidence != validated.evidence):
            raise InvalidSource('validated evidence differs from the current source')
        db = self.pair_store.db
        if self.pair_store.dialogues.store.readonly or db.execute('PRAGMA query_only').fetchone()[0]:
            raise Unavailable('readonly catalog cannot validate evidence')
        db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            revision = self.pair_store.revision
            run_id = validated.result.run_id
            run = db.execute('SELECT state,input_hash FROM persona_analysis_runs '
                'WHERE friend_id=? AND id=?', (self.scope.friend_id, run_id)).fetchone()
            if run is None or run['input_hash'] != current.source_content_hash:
                raise InvalidSource('saved run is absent or has a different source hash')
            persisted = tuple(db.execute('SELECT id,claim_key,subject,dimension,scene,state,claim_json '
                'FROM persona_claims WHERE friend_id=? AND run_id=? ORDER BY id',
                (self.scope.friend_id, run_id)).fetchall())
            expected_claims = tuple(sorted((*validated.result.role_claim_drafts,
                *validated.result.relationship_claim_drafts), key=lambda item: item.claim_id))
            if len(persisted) != len(expected_claims):
                raise InvalidSource('saved claims differ from validated drafts')
            for row, draft in zip(persisted, expected_claims):
                if (row['id'] != draft.claim_id or row['claim_key'] != draft.claim_key or
                        row['subject'] != draft.subject or row['dimension'] != draft.dimension or
                        row['scene'] != draft.scene or row['claim_json'] != _json(asdict(draft))):
                    raise InvalidSource('saved claim differs from validated draft')

            if run['state'] == 'COMPLETED':
                stored = tuple(tuple(row) for row in db.execute(
                    'SELECT claim_id,evidence_role,episode_id,source_message_id,span_start,span_end,'
                    'span_text,source_origin,data_scope,speaker_id,quality FROM persona_claim_evidence '
                    'WHERE friend_id=? AND run_id=? ORDER BY claim_id,evidence_role,episode_id,source_message_id',
                    (self.scope.friend_id, run_id)))
                wanted = _db_evidence(validated.evidence)
                if (stored == wanted and all(row['state'] == 'VALIDATED' for row in persisted)):
                    db.execute('ROLLBACK')
                    return revision
                raise Conflict('completed run already has different evidence')
            if run['state'] != 'DRAFT' or revision != expected_revision:
                raise Conflict('analysis catalog revision changed')

            # Source was rebuilt immediately before the transaction; the CAS now
            # prevents source/catalog writers from racing this state transition.
            db.executemany('INSERT INTO persona_claim_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(self.scope.friend_id, run_id, row.claim_id, row.evidence_role,
                  row.episode_id, row.source_message_id, row.span_start, row.span_end,
                  row.span_text, row.source_origin, row.data_scope, row.speaker_id, row.quality)
                 for row in validated.evidence])
            changed = db.execute("UPDATE persona_claims SET state='VALIDATED' "
                "WHERE friend_id=? AND run_id=? AND state='DRAFT'",
                (self.scope.friend_id, run_id)).rowcount
            if changed != len(persisted):
                raise Conflict('claim state changed during validation')
            if db.execute("UPDATE persona_analysis_runs SET state='COMPLETED' "
                "WHERE friend_id=? AND id=? AND state='DRAFT'", (self.scope.friend_id, run_id)).rowcount != 1:
                raise Conflict('run state changed during validation')
            db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                       (self.scope.friend_id,))
            db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise


def _without_generated_at(style):
    return {key: value for key, value in style.items() if key != 'generated_at'}


def _scene_previews_valid(scene, fresh):
    expected = {row.episode_id: row for row in fresh.scenes}
    actual = {row.episode_id: row for row in scene.scenes}
    if set(actual) != set(expected):
        return False
    from local_persona_analyzer import SCENES
    for episode_id, row in actual.items():
        base = expected[episode_id]
        if (row.session_id, row.subject, row.scene, row.confidence, row.quality) != (
                base.session_id, base.subject, base.scene, base.confidence, base.quality):
            return False
        if row.model_scene_proposal not in (*SCENES, '') or row.confidence != 0.0:
            return False
    return _model_proposals_valid(scene, fresh)


def _model_proposals_valid(scene, fresh):
    pairs = {pair.id: pair for pair in fresh.analysis_input.role_pairs}
    events = {event.event_id: event for event in fresh.analysis_input.relationship_events}
    for proposal in scene.model_proposals:
        if proposal.quality != 'model_estimate_uncalibrated_preview_only' or \
                proposal.scene != 'unknown' or proposal.dimension not in {
                    'ContentThemes', 'ReplyPattern', 'Support'} or not proposal.response_pattern:
            return False
        pair = pairs.get(proposal.episode_id)
        event = events.get(proposal.episode_id)
        if proposal.subject == 'BOT_PERSONA' and pair is not None:
            allowed = {message.id: message for message in pair.target_bubbles}
            refs = pair.source_refs
        elif proposal.subject in {'FRIEND', 'DYAD'} and event is not None:
            if event.friend_id != scene.friend_id or event.source_refs != proposal.source_refs:
                return False
            session_messages = {message_id for message_id in event.inbound_message_ids}
            allowed = {span.message_id: span.text for span in event.explicit_spans
                       if span.message_id in session_messages}
            refs = event.source_refs
        else:
            return False
        if proposal.subject == 'DYAD' and proposal.dimension != 'ReplyPattern':
            return False
        if proposal.source_refs != refs or len(proposal.evidence_spans) != 1:
            return False
        span = proposal.evidence_spans[0]
        if proposal.response_pattern != span.text or span.message_id not in allowed:
            return False
        if isinstance(allowed[span.message_id], str):
            if allowed[span.message_id] != span.text or span.start != 0 or \
                    span.end != len(span.text):
                return False
        else:
            message = allowed[span.message_id]
            if span.start != 0 or span.end != len(message.text) or span.text != message.text:
                return False
    return True


def _evidence_rows(scene, sessions, drafts):
    observations = {(item.episode_id, item.subject, item.dimension, item.scene): item
                    for item in scene.observations}
    pairs = {pair.id: pair for pair in scene.analysis_input.role_pairs}
    events = {event.event_id: event for event in scene.analysis_input.relationship_events}
    messages = {message.id: message for session in sessions for message in session.messages}
    rows = []
    for claim in drafts:
        for evidence_role, episode_ids in (('support', claim.support_observations),
                                           ('counter', claim.counter_observations)):
            if len(episode_ids) != len(set(episode_ids)):
                raise InvalidSource('duplicate evidence episode locator')
            for episode_id in episode_ids:
                observation = observations.get((episode_id, claim.subject,
                                                claim.dimension, claim.scene))
                if observation is None:
                    raise InvalidSource('evidence locator does not match an observation')
                if evidence_role == 'support' and observation.response_pattern != claim.reaction:
                    raise InvalidSource('support locator has a different response pattern')
                if evidence_role == 'counter' and observation.response_pattern == claim.reaction:
                    raise InvalidSource('counter locator repeats the supported response')
                if claim.subject == 'BOT_PERSONA':
                    pair = pairs.get(episode_id)
                    if pair is None or pair.target_speaker_id not in {
                            mapping.speaker_id for mapping in scene.analysis_input.target_mapping}:
                        raise InvalidSource('BOT evidence is not a mapped Target Pair')
                    if any(message.source.origin not in _TARGET_ORIGINS or
                           message.source.data_scope == DataScope.PRIVATE_FRIEND and
                           message.source.friend_id != scene.friend_id for message in pair.target_bubbles):
                        raise InvalidSource('BOT evidence Target source is invalid')
                    if claim.dimension == 'ImageReaction' and not any(
                            message.message_type.strip().lower() in {'image', 'photo', 'picture'}
                            for message in pair.user_bubbles):
                        raise InvalidSource('image reaction trigger is absent from the Pair input')
                    if claim.dimension == 'Silence' and pair.time_gap is None:
                        raise InvalidSource('silence observation has no measured Pair gap')
                    selected = list(pair.target_bubbles)
                    trigger_ids = set()
                    if claim.dimension == 'ImageReaction':
                        trigger_ids = {message.id for message in pair.user_bubbles
                            if message.message_type.strip().lower() in {'image', 'photo', 'picture'}}
                    elif claim.dimension == 'Silence':
                        trigger_ids = {message.id for message in pair.user_bubbles}
                else:
                    event = events.get(episode_id)
                    if event is None or event.friend_id != scene.friend_id:
                        raise InvalidSource('relationship evidence is not a verified inbound event')
                    selected = [messages[message_id] for message_id in event.inbound_message_ids]
                    if any(message.source.origin != SourceOrigin.REAL_RELATIONSHIP or
                           message.source.data_scope != DataScope.PRIVATE_FRIEND or
                           message.source.friend_id != scene.friend_id for message in selected):
                        raise InvalidSource('relationship evidence is not same-friend private input')
                    if claim.subject == 'DYAD' and observation.quality != 'explicit_literal':
                        raise InvalidSource('DYAD claim is not an explicit communication request')
                selected = list({message.id: message for message in selected}.values())
                allowed_ids = {message.id for message in selected}
                for span in observation.evidence_spans:
                    message = messages.get(span.message_id)
                    is_target = span.message_id in allowed_ids
                    is_trigger = claim.subject == 'BOT_PERSONA' and span.message_id in trigger_ids
                    if (message is None or not (is_target or is_trigger) or
                            span.start != 0 or span.end != len(message.text) or
                            span.text != message.text):
                        raise InvalidSource('evidence span differs from the source message')
                spans = {span.message_id: span for span in observation.evidence_spans}
                for message in selected:
                    span = spans.get(message.id)
                    # When an observation has no textual span (for example a media-only
                    # event), preserve the real message locator with NULL text offsets.
                    start, end, text = ((span.start, span.end, span.text) if span else
                                        (0, len(message.text), message.text) if message.text else
                                        (None, None, None))
                    rows.append(EvidenceLocator(claim.claim_id, evidence_role, episode_id,
                        episode_id if claim.subject == 'BOT_PERSONA' else None,
                        episode_id if claim.subject != 'BOT_PERSONA' else None,
                        claim.subject, claim.dimension, claim.scene, message.id, start, end, text,
                        message.source.origin.value, message.source.data_scope.value,
                        message.speaker_id, observation.quality))
    unique = {}
    for row in rows:
        key = (row.claim_id, row.evidence_role, row.episode_id, row.source_message_id)
        if key in unique and unique[key] != row:
            raise InvalidSource('duplicate evidence source has conflicting metadata')
        unique[key] = row
    return tuple(unique[key] for key in sorted(unique))


def _db_evidence(rows):
    return tuple(sorted((row.claim_id, row.evidence_role, row.episode_id, row.source_message_id,
        row.span_start, row.span_end, row.span_text, row.source_origin, row.data_scope,
        row.speaker_id, row.quality) for row in rows))
