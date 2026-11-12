"""Bounded, evidence-linked scene observations; never publishes persona claims."""
from dataclasses import dataclass
from source_state import source_read
import re

from local_persona_analyzer import (LocalPersonaAnalyzer, PROMPT_VERSION, SCENES, model_name,
                                    provider_kind, valid_model_choice)
from persona_analysis_input import AnalysisInputBuilder, EvidenceSpan, _bounded_strings
from persona_store import Conflict, Unavailable
from persona_types import AnalysisCallPolicy, AnalysisPurpose, ProviderKind
from style_statistics import StyleStatistics


_IMAGE_TYPES = {'image', 'photo', 'picture'}
_STICKER_TYPES = {'sticker', 'face'}
_GREETING = re.compile(r'^(?:嗨|哈喽|你好|早上好|早安|晚上好|晚安)(?:[呀啊喔哦！!。,.，]*)$')
_COMMUNICATION_REQUEST = re.compile(
    r'(?:别|不要|不用|请别|请不要).{0,12}(?:每句|每句话|一大段|给建议|叫我)|'
    r'(?:先听我说说就好|文字说就可以|不用给建议)')
_LAUGHTER = re.compile(r'哈哈|呵呵|笑死|\[捂脸\]')
_TOPIC_MARKER = re.compile(r'^(?:对了|说起来|换个话题|另外)[，,！!。\s]*')
_DIMENSIONS = (
    'LanguageStyle', 'ReplyPattern', 'EmotionDynamic', 'RelationshipRole',
    'ContentThemes', 'Initiative', 'Humor', 'Support', 'Conflict', 'Silence',
    'TopicSwitch', 'ImageReaction', 'StickerBehavior',
)


@dataclass(frozen=True)
class BehaviorObservation:
    episode_id: str
    session_id: str
    pair_id: str | None
    relationship_event_id: str | None
    subject: str
    dimension: str
    scene: str
    trigger: str
    response_pattern: str
    evidence_spans: tuple[EvidenceSpan, ...]
    source_refs: tuple
    quality: str


@dataclass(frozen=True)
class SceneResult:
    episode_id: str
    session_id: str
    subject: str
    scene: str
    model_scene_proposal: str
    confidence: float
    quality: str
    unknown_reason: str


@dataclass(frozen=True)
class DimensionStatus:
    dimension: str
    opportunity_count: int
    observable_count: int
    unknown_count: int
    abstention_reasons: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class SceneAnalysis:
    friend_id: str
    source_revision: int
    analysis_input: object
    source_refs: tuple
    style_profile: dict
    scenes: tuple[SceneResult, ...]
    observations: tuple[BehaviorObservation, ...]
    model_proposals: tuple[BehaviorObservation, ...]
    dimension_status: tuple[DimensionStatus, ...]
    model_version: str
    prompt_version: str


class SceneBehaviorAnalyzer:
    def __init__(self, scope, pair_store):
        self.builder = AnalysisInputBuilder(scope, pair_store)
        self.scope = self.builder.scope
        self.pair_store = pair_store

    @source_read
    def analyze(self, session_ids, target_speaker_ids=(), friend_speaker_ids=(),
                use_local_model=False, cancel=None, *, pair_ids=None):
        if isinstance(session_ids, (str, bytes)):
            raise ValueError('session_ids must be a sequence')
        if isinstance(target_speaker_ids, (str, bytes)) or isinstance(friend_speaker_ids, (str, bytes)):
            raise ValueError('speaker ids must be sequences')
        session_ids = _bounded_strings(session_ids, 'session_ids')
        target_speaker_ids = _bounded_strings(target_speaker_ids, 'target_speaker_ids')
        friend_speaker_ids = _bounded_strings(friend_speaker_ids, 'friend_speaker_ids')
        source_revision = self.pair_store.revision
        inputs = self.builder.build(session_ids, target_speaker_ids, friend_speaker_ids,
                                    pair_ids=pair_ids)
        episodes = (*inputs.role_pairs, *inputs.relationship_events)
        if len(episodes) > 64:
            raise Unavailable('scene analysis is limited to 64 episodes')
        if not episodes:
            raise Unavailable('scene analysis requires a verified Pair or relationship event')
        if not valid_model_choice(use_local_model):
            raise ValueError('use_local_model must be False, True (local 9B) or "cloud"')
        if use_local_model:
            inputs.validate_analysis_call(AnalysisPurpose.SCENE_ANALYSIS,
                                          ProviderKind(provider_kind(use_local_model)))
            local = LocalPersonaAnalyzer(self.scope, self.pair_store, use_local_model)
        else:
            local = None

        session_lookup = {session_id: self.pair_store.dialogues.load_session(session_id)
                          for session_id in dict.fromkeys(session_ids)}
        located = []
        for episode in episodes:
            is_role = hasattr(episode, 'target_bubbles')
            located.append((episode, is_role, episode.id if is_role else episode.event_id,
                            episode.session_id if is_role else _session_for_event(episode, session_lookup)))
        model_results = local.analyze_batch([(session_id, episode_id) for *_, episode_id, session_id in located],
            target_speaker_ids=target_speaker_ids, friend_speaker_ids=friend_speaker_ids,
            cancel=cancel) if use_local_model else {}
        scenes, observations, proposals = [], [], []
        for episode, is_role, episode_id, session_id in located:
            if cancel is not None and cancel.is_set():
                raise Unavailable('scene analysis cancelled')
            session = session_lookup[session_id]
            by_id = {message.id: message for message in session.messages}
            if is_role:
                incoming = episode.user_bubbles
                target = episode.target_bubbles
                context = tuple(message for turn in episode.context_turns for message in turn.bubbles)
                subject = 'BOT_PERSONA'
                refs = episode.source_refs
            else:
                incoming = tuple(by_id[item] for item in episode.inbound_message_ids)
                context = tuple(by_id[item] for item in episode.context_message_ids)
                target = ()
                subject = 'FRIEND'
                refs = episode.source_refs

            scene = _structural_scene(incoming)
            proposal = ''
            reason = '' if scene != 'unknown' else 'no_deterministic_scene_rule'
            quality = 'observed_structure' if scene != 'unknown' else 'unknown'
            if use_local_model:
                preview, result = model_results[episode_id]
                proposal = preview['scene']
                reason = reason if scene != 'unknown' else preview['unknown_reason']
                _append_model_proposals(result, episode_id, session_id, is_role, refs, proposals)
            scenes.append(SceneResult(episode_id, session_id, subject, scene, proposal,
                0.0, quality, reason))
            observations.extend(_observations(episode_id, session_id, is_role, incoming,
                target, context, episode, refs))

        if cancel is not None and cancel.is_set():
            raise Unavailable('scene analysis cancelled')
        if self.pair_store.revision != source_revision:
            raise Conflict('source revision changed during scene analysis')
        role_pairs = inputs.role_pairs
        style = StyleStatistics.calculate(role_pairs)
        status = _dimension_status(role_pairs, inputs.relationship_events, observations,
                                   proposals, len(episodes))
        return SceneAnalysis(self.scope.friend_id, source_revision, inputs,
            inputs.source_refs, style, tuple(scenes), tuple(observations),
            tuple(proposals), status,
            model_name(use_local_model, self.scope.friend_dir),
            PROMPT_VERSION if use_local_model else '')


def _session_for_event(event, sessions):
    ids = set(event.inbound_message_ids)
    for session_id, session in sessions.items():
        if ids and ids.issubset({message.id for message in session.messages}):
            return session_id
    raise Unavailable('relationship event is absent from its verified session')


def _structural_scene(incoming):
    types = {message.message_type.strip().lower() for message in incoming}
    if types & _STICKER_TYPES:
        return 'sticker_exchange'
    if types & _IMAGE_TYPES:
        return 'image_reaction'
    text = ''.join(message.text for message in incoming)
    if _GREETING.fullmatch(text.strip()):
        return 'greeting'
    if '?' in text or '？' in text:
        return 'concrete_question'
    return 'unknown'


def _span(message):
    return EvidenceSpan(message.id, 0, len(message.text), message.text) if message.text else None


def _observations(episode_id, session_id, is_role, incoming, target, context, episode, refs):
    out = []
    subject = 'BOT_PERSONA' if is_role else 'FRIEND'
    trigger_types = {message.message_type.strip().lower() for message in incoming}
    target_types = {message.message_type.strip().lower() for message in target}
    if is_role:
        spans = tuple(filter(None, map(_span, target)))
        text = ''.join(message.text for message in target)
        scene = _structural_scene(incoming)
        length = 'short_1_10' if 0 < len(text) <= 10 else 'longer_than_10' if text else 'no_text'
        for dimension, pattern in (
                ('LanguageStyle', length),
                ('ReplyPattern', 'bubbles=' + str(len(target)) + ';question=' + str('?' in text or '？' in text))):
            out.append(BehaviorObservation(episode_id, session_id, episode.id, None,
                'BOT_PERSONA', dimension, scene, 'verified input scene', pattern,
                spans, refs, 'observed_statistic'))
    if is_role and trigger_types & _IMAGE_TYPES:
        out.append(BehaviorObservation(episode_id, session_id, episode.id, None,
            'BOT_PERSONA', 'ImageReaction', 'image_reaction', 'input contains image type',
            ','.join(sorted(target_types or {'no_target_media'})),
            tuple(filter(None, map(_span, target))), refs, 'observed_structure'))
    if (_STICKER_TYPES & (trigger_types | target_types)):
        bubbles = incoming if not is_role else target
        out.append(BehaviorObservation(episode_id, session_id,
            episode.id if is_role else None, None if is_role else episode.event_id,
            subject, 'StickerBehavior', 'sticker_exchange', 'message_type is sticker/face',
            ','.join(sorted(message.message_type for message in bubbles)),
            tuple(filter(None, map(_span, bubbles))), refs, 'observed_structure'))
    for message in incoming if not is_role else ():
        if message.text and _COMMUNICATION_REQUEST.search(message.text):
            out.append(BehaviorObservation(episode_id, session_id,
                episode.id if is_role else None, None if is_role else episode.event_id,
                'DYAD', 'ReplyPattern', 'unknown', 'explicit communication request',
                message.text, (EvidenceSpan(message.id, 0, len(message.text), message.text),),
                refs, 'explicit_literal'))
    if is_role and episode.time_gap is not None:
        out.append(BehaviorObservation(episode_id, session_id, episode.id, None,
            'BOT_PERSONA', 'Silence', 'unknown', 'observed pair time gap',
            'time_gap_seconds=' + str(episode.time_gap), (), refs, 'observed_statistic'))
    response = target if is_role else incoming
    all_text = ''.join(message.text for message in response)
    if all_text and _TOPIC_MARKER.search(all_text):
        span = next((_span(m) for m in response if m.text and _TOPIC_MARKER.search(m.text)), None)
        out.append(BehaviorObservation(episode_id, session_id,
            episode.id if is_role else None, None if is_role else episode.event_id,
            subject, 'TopicSwitch', 'unknown', 'literal topic transition marker',
            'marker_present', (span,) if span else (), refs, 'rule_estimate'))
    for message in response:
        if message.text and _LAUGHTER.search(message.text):
            span = _span(message)
            out.append(BehaviorObservation(episode_id, session_id,
                episode.id if is_role else None, None if is_role else episode.event_id,
                subject, 'Humor', 'unknown', 'literal laughter marker', 'marker_present',
                (span,), refs, 'rule_estimate'))
            break
    return out


def _append_model_proposals(result, episode_id, session_id, is_role, refs, output):
    if result.get('confidence') != 0.0:
        return
    for candidate in result.get('candidates', ()):
        kind = candidate.get('kind')
        dimension = 'ContentThemes' if kind == 'explicit_preference' else (
            'ReplyPattern')
        evidence = candidate['evidence']
        output.append(BehaviorObservation(episode_id, session_id,
            episode_id if is_role else None, None if is_role else episode_id,
            candidate['subject'], dimension, 'unknown', 'uncalibrated local model candidate',
            candidate['text'], (EvidenceSpan(**evidence),), refs,
            'model_estimate_uncalibrated_preview_only'))


def _dimension_status(role_pairs, relationship_events, observations, proposals, count):
    observed = {item.dimension for item in observations
                if item.quality in {'observed_structure', 'observed_statistic', 'explicit_literal', 'rule_estimate'}}
    previews = {item.dimension for item in proposals}
    opportunity = {
        'LanguageStyle': len(role_pairs), 'ReplyPattern': len(role_pairs) + len(relationship_events),
        'EmotionDynamic': count, 'RelationshipRole': len(relationship_events),
        'ContentThemes': len(relationship_events), 'Initiative': count, 'Humor': count,
        'Support': count, 'Conflict': count, 'Silence': len(role_pairs),
        'TopicSwitch': count, 'ImageReaction': len(role_pairs),
        'StickerBehavior': len(role_pairs) + len(relationship_events),
    }
    reasons = {
        'EmotionDynamic': 'no calibrated emotion classifier',
        'RelationshipRole': 'no relationship inference from isolated wording',
        'ContentThemes': 'semantic preferences require calibrated classification',
        'Initiative': 'complete initiation/time evidence not established',
        'Humor': 'literal laughter marker is only a rule estimate',
        'Support': 'support intent is not deterministically observable',
        'Conflict': 'no calibrated conflict semantics',
        'Silence': 'only timestamped Pair gaps are observable',
        'TopicSwitch': 'only explicit transition markers are estimated',
        'ImageReaction': 'requires a verified image input and role Target',
        'StickerBehavior': 'requires explicit sticker/face message type',
    }
    rows = []
    for dimension in _DIMENSIONS:
        if dimension == 'LanguageStyle':
            status = 'observed' if role_pairs else 'not_analyzed'
            reason = () if role_pairs else ('no verified role Target Pairs',)
            observable_count = len(role_pairs)
        elif dimension in previews and dimension not in observed:
            status = 'model_preview_only'
            reason = ('uncalibrated model proposal excluded from evidence counts',)
            observable_count = 0
        elif dimension in observed:
            status = 'observed_or_rule_estimate'
            reason = ()
            observable_count = len({item.episode_id for item in observations if item.dimension == dimension and item.quality in {
                'observed_structure', 'observed_statistic', 'explicit_literal', 'rule_estimate'}
                })
        else:
            status = 'not_analyzed'
            reason = (reasons.get(dimension, 'no verified observable evidence'),)
            observable_count = 0
        rows.append(DimensionStatus(dimension, opportunity[dimension], observable_count,
            max(0, opportunity[dimension] - observable_count), reason, status))
    return tuple(rows)
