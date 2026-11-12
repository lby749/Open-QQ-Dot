"""Bounded, in-memory evaluation cases with hidden targets."""
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import re

from dialogue_pairs import DialoguePair
from dialogue_turns import Turn
from persona_types import (DataScope, FriendScope, NormalizedMessage, SourceOrigin,
                           SourceRef)
from similarity_guard import SimilarityGuard


MAX_CASES = 64
MAX_BUBBLES = 64
MAX_CONTEXT_TURNS = 3
MAX_BUBBLE_CHARS = 4000
MAX_CASE_CHARS = 20000
MAX_TRAINING_SOURCE_REFS = 100_000
_PARTITIONS = {'test', 'eval', 'holdout'}
_REAL_HUMAN_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
                        SourceOrigin.REAL_PERSONA_POOL, SourceOrigin.PUBLIC_DATASET}
_HUMAN_ROLES = {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'}
_LOCAL_STICKER = re.compile(r'\[\[(?:本地表情|LOCAL_STICKER):([0-9a-f]{64})\]\]')
_QQ_STICKER_MARKER = re.compile(r'\[\[QQ表情:[^\]]+\]\]|\[QQ原生表情[：:][^\]]*\]')
_MEDIA_MARKER = re.compile(
    r'\[\[(?:本地表情|LOCAL_STICKER):[0-9a-f]{64}\]\]|\[\[QQ表情:[^\]]+\]\]|\[QQ原生表情[：:][^\]]*\]')


@dataclass(frozen=True)
class EvaluationBubble:
    text: str
    message_type: str = 'text'
    has_media: bool = False


@dataclass(frozen=True)
class InputTurn:
    speaker_id: str
    bubbles: tuple[EvaluationBubble, ...]


@dataclass(frozen=True)
class GenerationInput:
    context_turns: tuple[InputTurn, ...]
    user_bubbles: tuple[EvaluationBubble, ...]


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    friend_id: str
    split: str
    context_turns: tuple[InputTurn, ...]
    user_bubbles: tuple[EvaluationBubble, ...]
    _target_bubbles: tuple[EvaluationBubble, ...] = field(repr=False)
    _source_keys: frozenset[tuple] = field(repr=False)
    leakage_status: str


@dataclass(frozen=True)
class EstimatedSignal:
    status: str
    rule: str
    value: bool | None


@dataclass(frozen=True)
class EvaluationMetrics:
    target_chars: int
    generated_chars: int
    length_delta: int
    target_bubbles: int
    generated_bubbles: int
    question_bubbles: int
    question_marks: int
    question_rate: float | None
    source_copy_bubbles: int
    source_copy_rate: float | None
    target_stickers: int
    target_unknown_media: int
    generated_stickers: int
    generated_unknown_media: int
    sticker_delta: int | None
    over_explanation: EstimatedSignal


@dataclass(frozen=True)
class ModelEvaluationRecord:
    model: str | None
    prompt_version: str | None
    provider: str | None
    evaluated_at: str | None
    usage_status: str
    result: object


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    friend_id: str
    split: str
    metrics: EvaluationMetrics
    leakage_status: str
    human_rating: object
    model_evaluation: ModelEvaluationRecord


def source_locator(source: SourceRef) -> tuple:
    """Return the stable split/leakage key for a source reference."""
    if not isinstance(source, SourceRef):
        raise TypeError('source must be a SourceRef')
    locator_parts = (source.dataset_id, source.import_id, source.record_id)
    has_record_locator = all(locator_parts)
    if has_record_locator:
        if source.data_scope == DataScope.PRIVATE_FRIEND:
            return ('private-record', source.friend_id, source.dataset_id,
                    source.import_id, source.record_id)
        return ('public-record', source.dataset_id, source.import_id, source.record_id)
    if source.data_scope == DataScope.PRIVATE_FRIEND:
        if not any(locator_parts) and source.message_seq is not None:
            return ('history', source.friend_id, source.message_seq)
        if any(locator_parts):
            raise ValueError('source record locator must be complete')
        raise ValueError('source needs a complete record locator or private history sequence')
    raise ValueError('source needs a complete record locator')


def _partition(value):
    if isinstance(value, str):
        split, is_holdout = value, None
    elif isinstance(value, Mapping):
        split, is_holdout = value.get('split'), value.get('is_holdout')
        if is_holdout is not None and type(is_holdout) is not bool:
            raise ValueError('is_holdout must be an explicit bool')
    else:
        raise ValueError('partition must be a split string or metadata mapping')
    split = split.strip().casefold() if isinstance(split, str) else None
    if split is not None and split not in _PARTITIONS:
        raise ValueError('source split must be test, eval, or holdout')
    if is_holdout is True:
        return split or 'holdout'
    if is_holdout is False and split in _PARTITIONS:
        raise ValueError('split and is_holdout disagree')
    if split is None:
        raise ValueError('source needs an explicit held-out partition')
    return split


def _explicit_speaker_id(value):
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip().casefold()
    return normalized not in {'unknown', 'bot', 'assistant', 'system', 'ai'} and \
        not normalized.startswith('unknown:')


def _bubble(message):
    if not isinstance(message, NormalizedMessage):
        raise TypeError('pair bubbles must be NormalizedMessage values')
    text = getattr(message, 'text', None)
    message_type = getattr(message, 'message_type', None)
    has_media = getattr(message, 'media_id', None) is not None
    if not isinstance(text, str) or not isinstance(message_type, str):
        raise TypeError('pair bubbles must have text and message_type')
    if len(text) > MAX_BUBBLE_CHARS:
        raise ValueError('bubble exceeds character limit')
    return EvaluationBubble(text=text, message_type=message_type, has_media=has_media)


def _turn(turn):
    if not isinstance(turn, Turn):
        raise TypeError('context turns must be Dialogue Turn values')
    speaker_id = getattr(turn, 'speaker_id', None)
    bubbles = getattr(turn, 'bubbles', None)
    if not isinstance(speaker_id, str) or not isinstance(bubbles, Sequence):
        raise TypeError('context turns must have speaker_id and bubbles')
    return InputTurn(speaker_id=speaker_id, bubbles=tuple(_bubble(item) for item in bubbles))


def prepare_cases(scope, pairs, *, partition_by_source, training_source_refs, limit=64):
    """Prepare one homogeneous held-out batch from caller-verified pairs.

    partition_by_source maps source_locator(ref) to ``test``, ``eval``,
    ``holdout`` or metadata containing an explicit split/is_holdout value.
    Source existence, pair reconstruction, training-list completeness, and
    same-root pair provenance remain the caller's responsibility; DialoguePair
    carries no root identifier. Empty training refs mark leakage_status unverified.
    """
    if not isinstance(scope, FriendScope):
        raise TypeError('scope must be a verified FriendScope')
    scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
        friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CASES:
        raise ValueError(f'limit must be between 1 and {MAX_CASES}')
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)):
        raise TypeError('pairs must be a bounded sequence')
    if len(pairs) > limit:
        raise ValueError('evaluation batch exceeds limit')
    if not isinstance(partition_by_source, Mapping):
        raise TypeError('partition_by_source must be a mapping')
    if not isinstance(training_source_refs, Sequence) or isinstance(training_source_refs, (str, bytes)):
        raise TypeError('training_source_refs must be a sequence of SourceRef')
    if len(training_source_refs) > MAX_TRAINING_SOURCE_REFS:
        raise ValueError('training_source_refs exceeds limit')
    training_keys = {source_locator(ref) for ref in training_source_refs}
    leakage_status = 'locator_overlap_checked' if training_keys else 'unverified'

    cases = []
    seen_ids = set()
    batch_splits = set()
    for pair in pairs:
        if not isinstance(pair, DialoguePair):
            raise TypeError('pairs must be verified DialoguePair values')
        friend_id = pair.friend_id
        case_id = pair.id
        if friend_id != scope.friend_id:
            raise ValueError('pair belongs to another friend scope')
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise ValueError('pair ids must be nonempty and unique')
        seen_ids.add(case_id)
        refs = pair.source_refs
        if not isinstance(refs, Sequence) or not refs:
            raise ValueError('pair needs nonempty source refs')
        if len(refs) > MAX_BUBBLES:
            raise ValueError('pair source refs exceed case limit')
        keys = frozenset(source_locator(ref) for ref in refs)
        if keys & training_keys:
            raise ValueError('evaluation source overlaps training sources')
        if any(ref.data_scope == DataScope.PRIVATE_FRIEND and ref.friend_id != scope.friend_id
               for ref in refs):
            raise ValueError('private source belongs to another friend')
        try:
            splits = {_partition(partition_by_source[key]) for key in keys}
        except KeyError as exc:
            raise ValueError('every pair source needs an explicit held-out partition') from exc
        if len(splits) != 1:
            raise ValueError('pair sources have mixed held-out partitions')
        split = next(iter(splits))
        batch_splits.add(split)

        context_turns = pair.context_turns
        user_bubbles = pair.user_bubbles
        target_bubbles = pair.target_bubbles
        if not isinstance(context_turns, Sequence) or not isinstance(user_bubbles, Sequence) or \
                not isinstance(target_bubbles, Sequence) or not target_bubbles:
            raise TypeError('pair needs context, user and target sequences')
        if any(not isinstance(turn, Turn) for turn in context_turns):
            raise TypeError('context_turns must contain Dialogue Turn values')
        if len(context_turns) > MAX_CONTEXT_TURNS:
            raise ValueError('pair exceeds context turn limit')
        if any(not isinstance(turn.bubbles, Sequence) for turn in context_turns):
            raise TypeError('context turn bubbles must be a sequence')
        if len(user_bubbles) + len(target_bubbles) + sum(
                len(turn.bubbles) for turn in context_turns) > MAX_BUBBLES:
            raise ValueError('pair exceeds total bubble limit')
        if not user_bubbles:
            raise ValueError('pair needs current user bubbles')
        target_turn_id = pair.target_turn_id
        context_turn_ids = pair.context_turn_ids
        input_turn_ids = pair.input_turn_ids
        actual_context_ids = tuple(turn.id for turn in context_turns if isinstance(turn, Turn))
        ordinals = tuple(turn.ordinal for turn in context_turns)
        if target_turn_id and (target_turn_id in context_turn_ids or target_turn_id in input_turn_ids):
            raise ValueError('target turn appears in generation context')
        if (not input_turn_ids or actual_context_ids != tuple(context_turn_ids) or
                len(set(context_turn_ids)) != len(context_turn_ids) or
                any(turn.session_id != pair.session_id for turn in context_turns) or
                len(set(ordinals)) != len(ordinals) or ordinals != tuple(sorted(ordinals)) or
                set(context_turn_ids) & set(input_turn_ids)):
            raise ValueError('context turn ids, session and ordinal order do not match the pair')
        if not target_turn_id:
            raise ValueError('target turn appears in generation context')
        target_ids = {getattr(item, 'id', None) for item in target_bubbles}
        input_ids = ({getattr(item, 'id', None) for turn in context_turns for item in turn.bubbles} |
                     {getattr(item, 'id', None) for item in user_bubbles})
        if target_ids & input_ids:
            raise ValueError('target bubble appears in generation input')
        if any(getattr(item, 'target_selected', False) or getattr(item, 'target_role', None) == 'target'
               for item in user_bubbles):
            raise ValueError('target bubble appears in current user input')
        all_messages = [item for turn in context_turns for item in turn.bubbles]
        all_messages.extend(user_bubbles)
        all_messages.extend(target_bubbles)
        if any(not isinstance(item, NormalizedMessage) for item in all_messages):
            raise TypeError('context, user and target bubbles must be NormalizedMessage values')
        if any(item.source not in refs for item in all_messages):
            raise ValueError('every pair bubble source must appear in pair.source_refs')
        if {item.source for item in all_messages} != set(refs):
            raise ValueError('pair.source_refs must match all context, user and target bubble sources')
        if not _explicit_speaker_id(pair.target_speaker_id):
            raise ValueError('target_speaker_id must be explicit')
        if any(item.speaker_role.casefold() not in _HUMAN_ROLES or
               item.source.origin not in _REAL_HUMAN_ORIGINS or
               item.speaker_id != pair.target_speaker_id for item in target_bubbles):
            raise ValueError('target must be an explicit human with an allowed real source origin')
        if any(item.speaker_role.casefold() not in _HUMAN_ROLES or
               item.source.origin not in _REAL_HUMAN_ORIGINS for item in user_bubbles):
            raise ValueError('current user input must come from a human with an allowed real source origin')
        context = tuple(_turn(turn) for turn in context_turns)
        users = tuple(_bubble(item) for item in user_bubbles)
        targets = tuple(_bubble(item) for item in target_bubbles)
        case_chars = sum(len(bubble.text) for turn in context for bubble in turn.bubbles)
        case_chars += sum(len(bubble.text) for bubble in (*users, *targets))
        if case_chars > MAX_CASE_CHARS:
            raise ValueError('case exceeds character limit')
        cases.append(EvaluationCase(case_id, friend_id, split, context, users, targets, keys,
                                    leakage_status))
    if len(batch_splits) > 1:
        raise ValueError('evaluation batch mixes held-out partitions')
    return tuple(cases)


def generation_input(case):
    """Expose only earlier context and the current USER bubbles."""
    if not isinstance(case, EvaluationCase):
        raise TypeError('case must be an EvaluationCase')
    return GenerationInput(case.context_turns, case.user_bubbles)


def _output_bubbles(values):
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError('generated_bubbles must be a sequence')
    if len(values) > MAX_BUBBLES:
        raise ValueError('generated output exceeds bubble limit')
    output = []
    for value in values:
        if isinstance(value, str):
            bubble = EvaluationBubble(value)
        elif isinstance(value, EvaluationBubble):
            bubble = value
        else:
            raise TypeError('generated bubbles must be strings or EvaluationBubble values')
        if (not isinstance(bubble.text, str) or not isinstance(bubble.message_type, str) or
                not bubble.message_type or type(bubble.has_media) is not bool):
            raise TypeError('generated bubble text and message_type must be text')
        if len(bubble.text) > MAX_BUBBLE_CHARS:
            raise ValueError('generated bubble exceeds character limit')
        output.append(bubble)
    if sum(len(item.text) for item in output) > MAX_CASE_CHARS:
        raise ValueError('generated output exceeds character limit')
    return tuple(output)


def _sticker_counts(bubbles):
    stickers = unknown_media = 0
    for bubble in bubbles:
        kind = bubble.message_type.strip().casefold()
        if (kind in {'sticker', 'face'} or _LOCAL_STICKER.fullmatch(bubble.text) or
                _QQ_STICKER_MARKER.search(bubble.text)):
            stickers += 1
        elif bubble.has_media or kind != 'text':
            unknown_media += 1
    return stickers, unknown_media


def _copy_source_texts(case):
    bubbles = tuple(bubble for turn in case.context_turns for bubble in turn.bubbles) + case.user_bubbles
    return tuple(text for bubble in bubbles if (text := _plain_text(bubble)))


def _plain_text(bubble):
    if bubble.message_type != 'text' or bubble.has_media:
        return ''
    return _MEDIA_MARKER.sub('', bubble.text)


def _is_source_copy(generated, source_texts):
    if not generated.strip() or not source_texts:
        return False
    guard = SimilarityGuard()
    return any(guard.check([generated], [], source_texts[start:start + 50]).flagged
               for start in range(0, len(source_texts), 50))


def _model_record(value):
    if value is None:
        return ModelEvaluationRecord(None, None, None, None, 'unknown', None)
    if not isinstance(value, Mapping):
        raise TypeError('model_evaluation must be a mapping or None')
    names = ('model', 'prompt_version', 'provider', 'evaluated_at', 'usage_status')
    if any(not isinstance(value.get(name), str) or not value[name].strip() for name in names):
        raise ValueError('model_evaluation requires model, prompt_version, provider, evaluated_at and usage_status')
    status = value['usage_status'].strip().casefold()
    if status not in ('known', 'unknown'):
        raise ValueError('usage_status must be known or unknown')
    try:
        evaluated_at = datetime.fromisoformat(value['evaluated_at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('evaluated_at must be an ISO timestamp') from exc
    if evaluated_at.tzinfo is None:
        raise ValueError('evaluated_at must include a timezone')
    return ModelEvaluationRecord(value['model'], value['prompt_version'], value['provider'],
        value['evaluated_at'], status, deepcopy(value.get('result')))


def evaluate(case, generated_bubbles, *, human_rating=None, model_evaluation=None):
    """Compute deterministic text/bubble metrics after generation.

    source_copy_* are SimilarityGuard's thresholded near-copy signals against
    USER/context text; they are neither exact-copy counts nor Target matches.
    """
    if not isinstance(case, EvaluationCase):
        raise TypeError('case must be an EvaluationCase')
    generated = _output_bubbles(generated_bubbles)
    target = case._target_bubbles
    source_texts = _copy_source_texts(case)
    plain_target = tuple(text for item in target if (text := _plain_text(item)))
    plain_generated = tuple(text for item in generated if (text := _plain_text(item)))
    target_chars = sum(len(text) for text in plain_target)
    generated_chars = sum(len(text) for text in plain_generated)
    question_bubbles = sum(any(mark in text for mark in ('?', '？')) for text in plain_generated)
    question_marks = sum(text.count('?') + text.count('? ') for text in plain_generated)
    source_copies = sum(bool(text) and _is_source_copy(text, source_texts) for text in plain_generated)
    over_explanation = EstimatedSignal('estimated', 'generated_text_chars > 2 * target_text_chars',
        None if target_chars == 0 else generated_chars > 2 * target_chars)
    target_stickers, target_unknown_media = _sticker_counts(target)
    generated_stickers, generated_unknown_media = _sticker_counts(generated)
    metrics = EvaluationMetrics(
        target_chars=target_chars,
        generated_chars=generated_chars,
        length_delta=generated_chars - target_chars,
        target_bubbles=len(target),
        generated_bubbles=len(generated),
        question_bubbles=question_bubbles,
        question_marks=question_marks,
        question_rate=question_bubbles / len(plain_generated) if plain_generated else None,
        source_copy_bubbles=source_copies,
        source_copy_rate=source_copies / len(plain_generated) if plain_generated else None,
        target_stickers=target_stickers,
        target_unknown_media=target_unknown_media,
        generated_stickers=generated_stickers,
        generated_unknown_media=generated_unknown_media,
        sticker_delta=(generated_stickers - target_stickers
            if not target_unknown_media and not generated_unknown_media else None),
        over_explanation=over_explanation,
    )
    return EvaluationResult(case.case_id, case.friend_id, case.split, metrics, case.leakage_status,
                            deepcopy(human_rating), _model_record(model_evaluation))
