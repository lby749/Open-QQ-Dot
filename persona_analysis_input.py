"""Build bounded, provenance-preserving inputs for later persona analysis."""
from source_state import source_read
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import MAX_SESSION_MESSAGES, build_turns
from persona_store import ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, DataScope, FriendScope, InvalidSource,
                           SourceOrigin)


_HUMAN_ROLES = {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'}


@dataclass(frozen=True)
class EvidenceSpan:
    message_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RelationshipEvent:
    event_id: str
    friend_id: str
    inbound_message_ids: tuple[str, ...]
    context_message_ids: tuple[str, ...]
    source_refs: tuple
    explicit_spans: tuple[EvidenceSpan, ...]
    time_quality: str


@dataclass(frozen=True)
class TargetMapping:
    speaker_id: str
    subject: str = 'BOT_PERSONA'


@dataclass(frozen=True)
class SampleInfo:
    session_count: int
    message_count: int
    turn_count: int
    role_pair_count: int
    relationship_event_count: int
    source_origins: tuple[str, ...]
    data_scopes: tuple[str, ...]
    time_qualities: tuple[str, ...]
    selected_target_speaker_ids: tuple[str, ...]
    selected_friend_speaker_ids: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisInput:
    role_pairs: tuple
    relationship_events: tuple[RelationshipEvent, ...]
    source_refs: tuple
    target_mapping: tuple[TargetMapping, ...]
    sample_info: SampleInfo

    def validate_analysis_call(self, purpose, provider_kind):
        if not self.role_pairs and not self.relationship_events:
            raise Unavailable('empty analysis input cannot start model analysis')
        return AnalysisCallPolicy.validate(purpose, provider_kind, self.source_refs)


class AnalysisInputBuilder:
    def __init__(self, scope, pair_store):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('AnalysisInputBuilder requires a verified FriendScope')
        if not isinstance(pair_store, PairStore):
            raise ScopeMismatch('AnalysisInputBuilder requires an existing PairStore')
        if pair_store.scope != scope:
            raise ScopeMismatch('PairStore must use the same FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.pair_store = pair_store

    @source_read
    def build(self, session_ids, target_speaker_ids=(), friend_speaker_ids=(), *, pair_ids=None):
        sessions_requested = _bounded_strings(session_ids, 'session_ids')
        targets = _bounded_strings(target_speaker_ids, 'target_speaker_ids')
        friend_selection = _bounded_strings(friend_speaker_ids, 'friend_speaker_ids')
        selected_pairs = None if pair_ids is None else _bounded_strings(pair_ids, 'pair_ids')
        if selected_pairs == ():
            raise InvalidSource('explicit pair selection must not be empty')
        friend_ids = friend_selection or (self.scope.friend_id,)
        self._check_scope()
        sessions, turns_by_session, all_sources = [], {}, []
        source_seen = set()
        message_count = turn_count = 0
        for session_id in dict.fromkeys(sessions_requested):
            session = self.pair_store.dialogues.load_session(session_id)
            if session.friend_id != self.scope.friend_id:
                raise ScopeMismatch('session belongs to another friend scope')
            message_count += len(session.messages)
            if message_count > MAX_SESSION_MESSAGES:
                raise Unavailable('analysis input exceeds the 10000 message limit')
            turns = build_turns(session)
            turn_count += len(turns)
            sessions.append(session)
            turns_by_session[session_id] = turns
            for message in session.messages:
                if message.source not in source_seen:
                    source_seen.add(message.source)
                    all_sources.append(message.source)

        role_pairs = []
        if targets:
            pair_builder = DialoguePairBuilder(self.scope)
            for session in sessions:
                turns = turns_by_session[session.id]
                candidates = tuple(pair_builder.build_pairs(turns, targets))
                stored = self.pair_store.load_pairs(session.id)
                stored_selected = tuple(pair for pair in stored
                                        if pair.target_speaker_id in targets)
                by_id = {pair.id: pair for pair in stored_selected}
                if len(by_id) != len(stored_selected):
                    raise InvalidSource('duplicate stored selected pair')
                expected_ids = {pair.id for pair in candidates}
                if expected_ids - set(by_id):
                    raise Unavailable('a selected pairable target has no persisted Pair')
                if set(by_id) - expected_ids or any(by_id[p.id] != p for p in candidates):
                    raise InvalidSource('stored selected pairs differ from rebuilt source')
                role_pairs.extend(by_id[p.id] for p in candidates)

        if selected_pairs is not None:
            selected_set = set(selected_pairs)
            if selected_set - {pair.id for pair in role_pairs}:
                raise InvalidSource('selected pair is absent from verified sessions and Targets')
            role_pairs = [pair for pair in role_pairs if pair.id in selected_set]

        events = []
        for session in sessions:
            turns = turns_by_session[session.id]
            for index, turn in enumerate(turns):
                if turn.speaker_id not in friend_ids or not _relationship_turn(turn, self.scope):
                    continue
                context = turns[max(0, index - 3):index]
                inbound = tuple(message.id for message in turn.bubbles)
                context_ids = tuple(message.id for prior in context for message in prior.bubbles)
                messages = (*tuple(message for prior in context for message in prior.bubbles),
                            *turn.bubbles)
                refs = _unique_sources(message.source for message in messages)
                spans = tuple(EvidenceSpan(message.id, 0, len(message.text), message.text)
                              for message in turn.bubbles if message.text)
                identity = {'friend_id': self.scope.friend_id, 'session_id': session.id,
                            'inbound': inbound, 'context': context_ids}
                event_id = hashlib.sha256(json.dumps(identity, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
                events.append(RelationshipEvent(event_id, self.scope.friend_id, inbound,
                    context_ids, refs, spans, turn.time_quality))
                for source in refs:
                    if source not in source_seen:
                        source_seen.add(source)
                        all_sources.append(source)

        self._check_scope()
        return AnalysisInput(tuple(role_pairs), tuple(events), tuple(all_sources),
            tuple(TargetMapping(speaker) for speaker in targets),
            SampleInfo(len(sessions), message_count, turn_count, len(role_pairs), len(events),
                tuple(sorted({ref.origin.value if ref.origin else 'UNKNOWN' for ref in all_sources})),
                tuple(sorted({ref.data_scope.value for ref in all_sources})),
                tuple(sorted({turn.time_quality for turns in turns_by_session.values()
                              for turn in turns})),
                targets, tuple(friend_ids)))

    def _check_scope(self):
        refreshed = FriendScope.load(self.scope.friend_id,
            project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        if refreshed != self.scope or self.pair_store.scope != refreshed:
            raise ScopeMismatch('friend binding changed or PairStore scope differs')


def _bounded_strings(values, name):
    if isinstance(values, (str, bytes, Mapping)) or values is None:
        raise InvalidSource(name + ' must be a bounded iterable of nonempty strings')
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise InvalidSource(name + ' must be iterable') from exc
    items = []
    for value in iterator:
        if len(items) >= MAX_SESSION_MESSAGES:
            raise Unavailable(name + ' exceeds the 10000 item limit')
        if not isinstance(value, str) or not value.strip():
            raise InvalidSource(name + ' contains an invalid value')
        items.append(value)
    return tuple(dict.fromkeys(items))


def _relationship_turn(turn, scope):
    if not turn.bubbles or turn.speaker_id.strip().lower() == 'unknown' or \
            turn.speaker_id.strip().lower().startswith('unknown:'):
        return False
    for message in turn.bubbles:
        source = message.source
        if (message.speaker_id != turn.speaker_id or
                message.speaker_role.strip().lower() not in _HUMAN_ROLES or
                source.origin != SourceOrigin.REAL_RELATIONSHIP or
                source.data_scope != DataScope.PRIVATE_FRIEND or
                source.friend_id != scope.friend_id):
            return False
    return True


def _unique_sources(values):
    found, seen = [], set()
    for source in values:
        if source not in seen:
            seen.add(source)
            found.append(source)
    return tuple(found)
