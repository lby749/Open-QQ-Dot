"""Validated data contracts; no model calls, database writes or sending."""
import json
import math
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from persona_store import ScopeMismatch, Unavailable, validate_friend_directory


class InvalidSource(ValueError):
    pass


class PrivacyViolation(ValueError):
    pass


class DimensionMismatch(Unavailable):
    pass


class ModelVersionMismatch(Unavailable):
    pass


class DataScope(str, Enum):
    PRIVATE_FRIEND = 'PRIVATE_FRIEND'
    PUBLIC_DATASET = 'PUBLIC_DATASET'
    PUBLIC_PACK = 'PUBLIC_PACK'


class PrivacyView(str, Enum):
    LOCAL_FULL = 'LOCAL_FULL'
    CLOUD_SAFE = 'CLOUD_SAFE'


class SourceOrigin(str, Enum):
    REAL_RELATIONSHIP = 'REAL_RELATIONSHIP'
    REAL_SINGLE_SPEAKER = 'REAL_SINGLE_SPEAKER'
    REAL_PERSONA_POOL = 'REAL_PERSONA_POOL'
    PUBLIC_DATASET = 'PUBLIC_DATASET'
    MANUAL = 'MANUAL'
    SYNTHETIC = 'SYNTHETIC'
    BOT_GENERATED = 'BOT_GENERATED'


class AnalysisPurpose(str, Enum):
    DISTILLATION = 'distillation'
    JUDGE = 'judge'
    SCENE_ANALYSIS = 'scene_analysis'
    EMBEDDING = 'embedding'
    STICKER_ANALYSIS = 'sticker_analysis'


class ProviderKind(str, Enum):
    LOCAL = 'LOCAL'
    CLOUD = 'CLOUD'


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise InvalidSource(name + ' must be a nonempty string')


def _qq(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{5,12}', value):
        raise ScopeMismatch('friend/account ID must be 5–12 ASCII digits')


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise InvalidSource(name + ' must be an integer >= ' + str(minimum))


def _number(value, name, minimum=None, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or (
            minimum is not None and value < minimum or maximum is not None and value > maximum):
        raise InvalidSource(name + ' is outside its finite range')


def _time(value, name, optional=False, aware=False):
    if value is None and optional:
        return
    _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise InvalidSource(name + ' must be ISO8601') from exc
    if aware and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise InvalidSource(name + ' requires an explicit timezone offset')


def _sequence(values, name):
    if isinstance(values, (str, bytes, dict)):
        raise InvalidSource(name + ' must be a sequence of records/values')
    try:
        return tuple(values)
    except TypeError as exc:
        raise InvalidSource(name + ' must be a sequence of records/values') from exc


def _enum(obj, name, enum_type):
    try:
        object.__setattr__(obj, name, enum_type(getattr(obj, name)))
    except (TypeError, ValueError) as exc:
        raise InvalidSource('invalid ' + name) from exc


@dataclass(frozen=True, kw_only=True)
class PackVersion:
    pack_id: str
    version: str

    def __post_init__(self):
        for name in ('pack_id', 'version'):
            value = getattr(self, name)
            _text(value, name)
            if value in ('.', '..') or any(c in value for c in '/\\:'):
                raise InvalidSource('pack identity cannot contain path components')


@dataclass(frozen=True, kw_only=True)
class FriendScope:
    friend_id: str
    friend_dir: Path
    allowed_pack_versions: frozenset[PackVersion]
    privacy_scope: DataScope
    project_root: Path | None = field(default=None, repr=False)

    @classmethod
    def load(cls, friend_id, *, project_root=None, friend_dir=None, allowed_pack_versions=()):
        """Read identity only; never initialize a profile, directory or database."""
        _qq(friend_id)
        root = Path(project_root if project_root is not None else os.environ.get(
            'QQ_BRIDGE_HOME', Path(__file__).resolve().parent)).resolve()
        directory = friend_dir if friend_dir is not None else root / 'friends' / friend_id
        return cls(friend_id=friend_id, friend_dir=directory, project_root=root,
                   allowed_pack_versions=frozenset(allowed_pack_versions),
                   privacy_scope=DataScope.PRIVATE_FRIEND)

    def __post_init__(self):
        root = Path(self.project_root if self.project_root is not None else os.environ.get(
            'QQ_BRIDGE_HOME', Path(__file__).resolve().parent)).resolve()
        directory = validate_friend_directory(self.friend_id, self.friend_dir, root)
        _enum(self, 'privacy_scope', DataScope)
        if self.privacy_scope != DataScope.PRIVATE_FRIEND:
            raise ScopeMismatch('FriendScope is a private friend, not a public pack')
        packs = frozenset(self.allowed_pack_versions)
        if any(not isinstance(pack, PackVersion) for pack in packs):
            raise InvalidSource('allowlist requires explicit pack_id and version')
        try:
            paths = (root / 'friends.json', directory / 'friend.json')
            if any(path.resolve() != path for path in paths):
                raise ScopeMismatch('identity files cannot redirect')
            registry, profile = (json.loads(path.read_text(encoding='utf-8')) for path in paths)
            record = registry['friends'][self.friend_id]
            if (record['qq_id'] != self.friend_id or profile['qq_id'] != self.friend_id or
                    Path(record['directory']).resolve() != directory):
                raise ScopeMismatch('registry, directory and profile identities differ')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ScopeMismatch('friend identity cannot be verified') from exc
        object.__setattr__(self, 'friend_dir', directory)
        object.__setattr__(self, 'project_root', root)
        object.__setattr__(self, 'allowed_pack_versions', packs)

    def require_pack(self, pack_id, version):
        if PackVersion(pack_id=pack_id, version=version) not in self.allowed_pack_versions:
            raise ScopeMismatch('public pack version is not bound to this friend')


@dataclass(frozen=True, kw_only=True)
class SourceRef:
    dataset_id: str | None
    import_id: str | None
    record_id: str | None
    message_seq: int | None
    speaker_id: str
    origin: SourceOrigin | None
    data_scope: DataScope
    privacy_view: PrivacyView
    friend_id: str | None
    event_time: str | None
    recorded_at: str
    pack_id: str | None = None
    pack_version: str | None = None
    license_id: str | None = None
    model_analysis_allowed: bool = False

    def __post_init__(self):
        _enum(self, 'data_scope', DataScope)
        _enum(self, 'privacy_view', PrivacyView)
        if self.origin is not None:
            _enum(self, 'origin', SourceOrigin)
        _text(self.speaker_id, 'speaker_id')
        _time(self.event_time, 'event_time', optional=True)
        _time(self.recorded_at, 'recorded_at', aware=True)
        for name in ('dataset_id', 'import_id', 'record_id', 'license_id'):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if type(self.model_analysis_allowed) is not bool:
            raise InvalidSource('model analysis permission must be explicit bool')
        if self.message_seq is not None:
            _integer(self.message_seq, 'message_seq', 1)
        if self.data_scope == DataScope.PRIVATE_FRIEND:
            _qq(self.friend_id)
            if self.message_seq is None and not all((self.dataset_id, self.import_id, self.record_id)):
                raise InvalidSource('private source needs a message seq or complete import locator')
            if self.pack_id is not None or self.pack_version is not None:
                raise ScopeMismatch('private source cannot be a public pack')
        else:
            if self.friend_id is not None or self.message_seq is not None:
                raise ScopeMismatch('public sources cannot claim a friend history identity')
            if not all((self.dataset_id, self.import_id, self.record_id)):
                raise InvalidSource('public source needs a complete dataset/import/record locator')
            if self.origin == SourceOrigin.REAL_RELATIONSHIP:
                raise PrivacyViolation('real friend relationship evidence remains private')
            if self.data_scope == DataScope.PUBLIC_PACK:
                PackVersion(pack_id=self.pack_id, version=self.pack_version)
            elif self.pack_id is not None or self.pack_version is not None:
                raise InvalidSource('dataset source cannot masquerade as a public pack')

    @property
    def privacy_scope(self):
        return self.data_scope


def _sources(values):
    refs = _sequence(values, 'source refs')
    if not refs or any(not isinstance(ref, SourceRef) for ref in refs):
        raise InvalidSource('nonempty validated source refs required')
    friends = {ref.friend_id for ref in refs if ref.data_scope == DataScope.PRIVATE_FRIEND}
    if len(friends) > 1:
        raise ScopeMismatch('sources from different friends cannot be combined')
    return refs


class AnalysisCallPolicy:
    @staticmethod
    def validate(purpose, provider_kind, source_refs):
        # Legacy calls keep their existing entry points; workers have no legacy exemption.
        try:
            AnalysisPurpose(purpose)
            provider = ProviderKind(provider_kind)
        except (TypeError, ValueError) as exc:
            raise PrivacyViolation('unknown/legacy analysis purpose or provider rejected') from exc
        refs = _sources(source_refs)
        if any(ref.origin is None for ref in refs):
            raise InvalidSource('unmapped origins cannot enter analysis')
        taint = DataScope.PRIVATE_FRIEND if any(
            ref.data_scope == DataScope.PRIVATE_FRIEND for ref in refs) else DataScope.PUBLIC_DATASET
        # 2026-10-09 user decision: private friend data may go to the friend's configured cloud model.
        if provider == ProviderKind.CLOUD and any(
                ref.data_scope == DataScope.PUBLIC_DATASET and
                (ref.model_analysis_allowed is not True or not ref.license_id) for ref in refs):
            raise PrivacyViolation('public cloud analysis needs recorded model-use permission')
        return taint


@dataclass(frozen=True, kw_only=True)
class NormalizedMessage:
    id: str
    timestamp: str | None
    speaker_id: str
    speaker_role: str
    text: str
    message_type: str
    media_id: str | None
    source: SourceRef
    target_role: str
    target_selected: bool

    def __post_init__(self):
        for name in ('id', 'speaker_id', 'speaker_role', 'message_type'):
            _text(getattr(self, name), name)
        if not isinstance(self.source, SourceRef) or self.speaker_id != self.source.speaker_id:
            raise InvalidSource('message speaker must match its source')
        _time(self.timestamp, 'timestamp', optional=True)
        if self.timestamp != self.source.event_time:
            raise InvalidSource('normalized event time cannot invent a source timestamp')
        if not isinstance(self.text, str) or not self.text and self.media_id is None:
            raise InvalidSource('message needs text or a media reference')
        if self.media_id is not None:
            _text(self.media_id, 'media_id')
        if self.target_role not in ('input', 'target', 'context', 'unmapped') or (
                type(self.target_selected) is not bool or
                self.target_selected != (self.target_role == 'target')):
            raise InvalidSource('target mapping must be explicit and consistent')
        if self.target_selected and (self.source.origin in (None, SourceOrigin.BOT_GENERATED) or
                                     self.speaker_role in ('bot', 'assistant', 'system', 'unknown')):
            raise InvalidSource('unmapped/bot history cannot be a real target')

    @property
    def friend_id(self):
        return self.source.friend_id

    @property
    def source_dataset(self):
        return self.source.dataset_id

    @property
    def source_record_id(self):
        return self.source.record_id

    @property
    def source_origin(self):
        return self.source.origin

    @property
    def data_scope(self):
        return self.source.data_scope

    @property
    def privacy_view(self):
        return self.source.privacy_view

    @property
    def privacy_scope(self):
        return self.source.data_scope


@dataclass(frozen=True, kw_only=True)
class RetrievalFilters:
    namespace: tuple[str, ...] = ()
    source_type: tuple[str, ...] = ()
    valid: bool = True
    time_range: tuple[str | None, str | None] = (None, None)
    topic: str | None = None
    confidence: float = 0.0
    privacy_scope: DataScope = DataScope.PRIVATE_FRIEND
    emotion: tuple[str, ...] = ()
    relationship_tags: tuple[str, ...] = ()

    def __post_init__(self):
        _enum(self, 'privacy_scope', DataScope)
        for name in ('namespace', 'source_type', 'emotion', 'relationship_tags'):
            values = _sequence(getattr(self, name), name)
            for value in values:
                _text(value, name)
            object.__setattr__(self, name, tuple(values))
        time_range = _sequence(self.time_range, 'time_range')
        if type(self.valid) is not bool or len(time_range) != 2:
            raise InvalidSource('invalid retrieval filter')
        for value in time_range:
            _time(value, 'time_range', optional=True)
        object.__setattr__(self, 'time_range', time_range)
        _number(self.confidence, 'confidence', 0, 1)


@dataclass(frozen=True, kw_only=True)
class ContextBudgets:
    persona: int = 5000
    examples: int = 4000
    knowledge: int = 2000

    def __post_init__(self):
        for name in ('persona', 'examples', 'knowledge'):
            _integer(getattr(self, name), name)
        if self.examples > self.persona:
            raise InvalidSource('example sub-budget cannot exceed the persona total')


@dataclass(frozen=True, kw_only=True)
class RetrievalRequest:
    friend_id: str
    query: str
    recent_context: str
    scene: str
    plan: 'ConversationPlan | str | None'
    filters: RetrievalFilters
    budgets: ContextBudgets
    allowed_pack_versions: frozenset[PackVersion]
    now: str

    def __post_init__(self):
        _qq(self.friend_id)
        if not isinstance(self.query, str) or not isinstance(self.recent_context, str):
            raise InvalidSource('query/context must be strings')
        _text(self.scene, 'scene')
        if self.plan is not None and not isinstance(self.plan, (ConversationPlan, str)):
            raise InvalidSource('plan must be None, existing task intent or ConversationPlan')
        if isinstance(self.plan, str):
            _text(self.plan, 'task intent')
        if not isinstance(self.filters, RetrievalFilters) or not isinstance(self.budgets, ContextBudgets):
            raise InvalidSource('validated filters and budgets required')
        packs = frozenset(self.allowed_pack_versions)
        if any(not isinstance(pack, PackVersion) for pack in packs):
            raise InvalidSource('pack allowlist requires explicit versions')
        object.__setattr__(self, 'allowed_pack_versions', packs)
        _time(self.now, 'now', aware=True)


@dataclass(frozen=True, kw_only=True)
class Candidate:
    id: str
    namespace: str
    type: str
    content: str
    score: float
    source: SourceRef
    reason: str
    retriever: str
    friend_id: str
    timestamp: str | None
    confidence: float
    original_scope: DataScope
    pack_version: str | None
    rank: int | None = None
    raw_score: float | None = None
    signals: tuple[str, ...] = ()
    valid_time: tuple[str | None, str | None] = (None, None)

    def __post_init__(self):
        _qq(self.friend_id)
        for name in ('id', 'namespace', 'type', 'content', 'reason', 'retriever'):
            _text(getattr(self, name), name)
        _number(self.score, 'score')
        _number(self.confidence, 'confidence', 0, 1)
        _time(self.timestamp, 'timestamp', optional=True)
        _enum(self, 'original_scope', DataScope)
        if not isinstance(self.source, SourceRef) or self.original_scope != self.source.data_scope:
            raise InvalidSource('candidate provenance cannot change the source scope')
        if self.source.friend_id is not None and self.source.friend_id != self.friend_id:
            raise ScopeMismatch('candidate belongs to another friend')
        if self.pack_version != self.source.pack_version:
            raise ScopeMismatch('candidate public version differs from its source')
        if self.rank is not None:
            _integer(self.rank, 'rank', 1)
        if self.raw_score is not None:
            _number(self.raw_score, 'raw_score')
        valid_time = _sequence(self.valid_time, 'valid_time')
        if len(valid_time) != 2:
            raise InvalidSource('valid_time needs two endpoints')
        for value in valid_time:
            _time(value, 'valid_time', optional=True)
        signals = _sequence(self.signals, 'signals')
        for value in signals:
            _text(value, 'signal')
        object.__setattr__(self, 'signals', signals)
        object.__setattr__(self, 'valid_time', valid_time)

    def validate_scope(self, scope):
        if not isinstance(scope, FriendScope) or self.friend_id != scope.friend_id:
            raise ScopeMismatch('candidate/request owner differs')
        if self.original_scope != DataScope.PRIVATE_FRIEND:
            if self.original_scope != DataScope.PUBLIC_PACK or self.namespace not in (
                    'persona', 'example', 'style', 'sticker'):
                raise ScopeMismatch('public data is not friend memory/facts')
            scope.require_pack(self.source.pack_id, self.pack_version)


@dataclass(frozen=True, kw_only=True)
class RetrievalBatch:
    candidates: tuple[Candidate, ...]
    elapsed_ms: float
    degraded: bool = False
    errors: tuple[str, ...] = ()

    def __post_init__(self):
        candidates = _sequence(self.candidates, 'candidates')
        if any(not isinstance(item, Candidate) for item in candidates):
            raise InvalidSource('retrieval batch requires validated Candidates')
        if len({item.friend_id for item in candidates}) > 1:
            raise ScopeMismatch('retrieval batch mixes friend owners')
        _number(self.elapsed_ms, 'retrieval elapsed_ms', 0)
        if type(self.degraded) is not bool:
            raise InvalidSource('retrieval degraded must be bool')
        errors = _sequence(self.errors, 'errors')
        for error in errors:
            _text(error, 'retrieval error')
        object.__setattr__(self, 'candidates', candidates)
        object.__setattr__(self, 'errors', errors)


@dataclass(frozen=True, kw_only=True)
class EmbeddingMetadata:
    model: str
    dimension: int
    version: str

    def __post_init__(self):
        _text(self.model, 'model')
        _text(self.version, 'version')
        _integer(self.dimension, 'dimension', 1)

    def validate_compatible(self, other):
        if not isinstance(other, EmbeddingMetadata):
            raise InvalidSource('embedding metadata required')
        if self.dimension != other.dimension:
            raise DimensionMismatch('embedding dimensions differ')
        if (self.model, self.version) != (other.model, other.version):
            raise ModelVersionMismatch('embedding model/version differs')


@dataclass(frozen=True, kw_only=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimension: int
    version: str

    def __post_init__(self):
        EmbeddingMetadata(model=self.model, dimension=self.dimension, version=self.version)
        rows = []
        for vector in _sequence(self.vectors, 'vectors'):
            vector = _sequence(vector, 'vector')
            if len(vector) != self.dimension:
                raise DimensionMismatch('vector length differs from declared dimension')
            for value in vector:
                _number(value, 'embedding component')
            rows.append(tuple(vector))
        object.__setattr__(self, 'vectors', tuple(rows))

    @property
    def metadata(self):
        return EmbeddingMetadata(model=self.model, dimension=self.dimension, version=self.version)


@dataclass(frozen=True, kw_only=True)
class ConversationPlan:
    action: str
    intent: str
    tone: str
    core_meaning: str
    ask_question: bool
    continue_topic: bool
    introduce_new_topic: bool
    desired_bubbles: int
    sticker_intent: str | None

    def __post_init__(self):
        if self.action not in ('reply', 'wait', 'ignore'):
            raise InvalidSource('invalid conversation action')
        for name in ('intent', 'tone', 'core_meaning'):
            if not isinstance(getattr(self, name), str):
                raise InvalidSource(name + ' must be a string')
        for name in ('ask_question', 'continue_topic', 'introduce_new_topic'):
            if type(getattr(self, name)) is not bool:
                raise InvalidSource(name + ' must be a bool')
        _integer(self.desired_bubbles, 'desired_bubbles')
        if self.sticker_intent is not None:
            _text(self.sticker_intent, 'sticker_intent')


@dataclass(frozen=True, kw_only=True)
class RuntimeSnapshot:
    scope: FriendScope
    bound_self_id: str
    inbound_generation: int
    source_message_id: str | None
    attempt_id: str
    mode: str
    created_at: str
    profile_revision: int

    def __post_init__(self):
        if not isinstance(self.scope, FriendScope):
            raise ScopeMismatch('runtime identity requires a verified FriendScope')
        _qq(self.bound_self_id)
        _integer(self.inbound_generation, 'inbound_generation')
        _integer(self.profile_revision, 'profile_revision')
        _text(self.attempt_id, 'attempt_id')
        if self.source_message_id is not None:
            _text(self.source_message_id, 'source_message_id')
        if self.mode not in ('reply', 'continuation', 'proactive', 'care'):
            raise InvalidSource('invalid runtime mode')
        _time(self.created_at, 'created_at', aware=True)

    @property
    def friend_id(self):
        return self.scope.friend_id

    @property
    def friend_dir(self):
        return self.scope.friend_dir


@dataclass(frozen=True, kw_only=True)
class SendStateSnapshot:
    friend_id: str
    verified_self_id: str
    identity_checked_at: str
    current_generation: int
    current_profile: dict
    attempt_state: str
    successful_bubble_ids: tuple[str, ...]
    now: str
    search_remaining: int | None
    contact_attempts_remaining: int | None
    window_open: bool | None
    cooldown_until: str | None
    reply_pause_until: str | None
    unanswered_pause_until: str | None
    care_slot_state: str | None

    def __post_init__(self):
        _qq(self.friend_id)
        _qq(self.verified_self_id)
        _integer(self.current_generation, 'current_generation')
        if not isinstance(self.current_profile, dict) or self.current_profile.get('qq_id') != self.friend_id:
            raise ScopeMismatch('sending profile belongs to another friend')
        object.__setattr__(self, 'current_profile', MappingProxyType(deepcopy(self.current_profile)))
        _text(self.attempt_state, 'attempt_state')
        for name in ('search_remaining', 'contact_attempts_remaining'):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        for name in ('window_open',):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise InvalidSource(name + ' must be bool or explicit unavailable None')
        for name in ('identity_checked_at', 'now'):
            _time(getattr(self, name), name, aware=True)
        for name in ('cooldown_until', 'reply_pause_until', 'unanswered_pause_until'):
            _time(getattr(self, name), name, optional=True, aware=True)
        if self.care_slot_state is not None:
            _text(self.care_slot_state, 'care_slot_state')
        receipts = _sequence(self.successful_bubble_ids, 'bubble receipts')
        for receipt in receipts:
            _text(receipt, 'bubble receipt')
        object.__setattr__(self, 'successful_bubble_ids', receipts)


@dataclass(frozen=True, kw_only=True)
class SendPlan:
    runtime_snapshot: RuntimeSnapshot
    bubbles: tuple[str, ...]
    sticker_intent: str | None
    delay_hint: float | None
    confidence: float

    def __post_init__(self):
        if not isinstance(self.runtime_snapshot, RuntimeSnapshot):
            raise ScopeMismatch('send plan requires a trusted runtime snapshot')
        bubbles = _sequence(self.bubbles, 'bubbles')
        for bubble in bubbles:
            _text(bubble, 'bubble')
        object.__setattr__(self, 'bubbles', bubbles)
        _number(self.confidence, 'confidence', 0, 1)
        if self.delay_hint is not None:
            _number(self.delay_hint, 'delay_hint', 0)
        if self.sticker_intent is not None:
            _text(self.sticker_intent, 'sticker_intent')

    @property
    def friend_id(self):
        return self.runtime_snapshot.friend_id

    @property
    def generation(self):
        return self.runtime_snapshot.inbound_generation

    @property
    def mode(self):
        return self.runtime_snapshot.mode

    @property
    def source_message_id(self):
        return self.runtime_snapshot.source_message_id

    @property
    def attempt_id(self):
        return self.runtime_snapshot.attempt_id


PROFILE_VERSION_FILES = ('persona_profile.json', 'style_profile.json', 'persona_skill.md',
                         'persona_report.md', 'examples.db')


@dataclass(frozen=True, kw_only=True)
class ProfileVersion:
    friend_id: str | None
    data_scope: DataScope
    version: str
    artifact_hashes: dict[str, str]
    pack_id: str | None = None

    def __post_init__(self):
        _enum(self, 'data_scope', DataScope)
        _text(self.version, 'version')
        if self.data_scope == DataScope.PRIVATE_FRIEND:
            _qq(self.friend_id)
            if self.pack_id is not None:
                raise ScopeMismatch('private profile cannot publish as a public pack')
        elif self.data_scope == DataScope.PUBLIC_PACK and self.friend_id is None:
            PackVersion(pack_id=self.pack_id, version=self.version)
        else:
            raise ScopeMismatch('profile version needs private friend or explicit public pack scope')
        if not isinstance(self.artifact_hashes, dict) or set(self.artifact_hashes) != set(PROFILE_VERSION_FILES):
            raise InvalidSource('a profile version requires all five authoritative/derived artifacts')
        if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
               for value in self.artifact_hashes.values()):
            raise InvalidSource('artifact hashes must be SHA256')
        object.__setattr__(self, 'artifact_hashes', MappingProxyType(dict(self.artifact_hashes)))
