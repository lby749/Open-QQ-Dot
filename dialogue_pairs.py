"""Build bounded, provenance-preserving human input/target pairs."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from dialogue_turns import MAX_SESSION_MESSAGES, Turn, _aware_datetime
from dialogue_turns import DialogueStore, build_turns
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, NormalizedMessage,
                           SourceOrigin, SourceRef)


_TARGET_ORIGINS = {SourceOrigin.REAL_RELATIONSHIP, SourceOrigin.REAL_SINGLE_SPEAKER,
                   SourceOrigin.REAL_PERSONA_POOL, SourceOrigin.PUBLIC_DATASET}
_HUMAN_ROLES = {'human', 'user', 'friend', 'contact', 'person', 'target', 'me'}
_BOT_ROLES = {'bot', 'assistant', 'ai', 'system'}
_TIME_QUALITY = {'KNOWN', 'UNKNOWN'}


@dataclass(frozen=True)
class DialoguePair:
    id: str
    friend_id: str
    session_id: str
    input_turn_ids: tuple[str, ...]
    context_turn_ids: tuple[str, ...]
    target_turn_id: str
    target_speaker_id: str
    user_bubbles: tuple[NormalizedMessage, ...]
    target_bubbles: tuple[NormalizedMessage, ...]
    context_turns: tuple[Turn, ...]
    source_refs: tuple[SourceRef, ...]
    time_gap: float | None
    time_quality: str


class DialoguePairBuilder:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('DialoguePairBuilder requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    def build_pairs(self, turns, target_speaker_ids):
        selected = _target_ids(target_speaker_ids)
        if not selected:
            return iter(())
        try:
            iter(turns)
        except TypeError as exc:
            raise InvalidSource('build_pairs requires a turn iterable') from exc
        return self._build(turns, selected)

    def _build(self, turns, selected):
        self._check_scope()
        values = []
        session_id = None
        message_count = 0
        for turn in turns:
            if not isinstance(turn, Turn):
                raise InvalidSource('build_pairs accepts Turn values only')
            if (not isinstance(turn.session_id, str) or not turn.session_id or
                    not isinstance(turn.bubbles, tuple) or not turn.bubbles):
                raise InvalidSource('turn session or bubbles are invalid')
            if session_id is None:
                session_id = turn.session_id
            if turn.session_id != session_id:
                raise InvalidSource('build_pairs accepts one complete session at a time')
            values.append(turn)
            if len(values) > MAX_SESSION_MESSAGES:
                raise Unavailable('session exceeds the 10000 turn limit')
            message_count += len(turn.bubbles)
            if message_count > MAX_SESSION_MESSAGES:
                raise Unavailable('session exceeds the 10000 message limit')
        if not values:
            return
        _validate_turns(values, self.scope)
        classifications = [_classify_turn(turn) for turn in values]
        if any(kind == 'unknown' for kind, _ in classifications):
            return
        speakers = {speaker for kind, speaker in classifications if kind == 'human'}
        if len(speakers) != 2:
            return
        for index, target in enumerate(values):
            kind, target_speaker = classifications[index]
            if kind != 'human' or target_speaker not in selected or index == 0:
                continue
            input_turn = values[index - 1]
            input_kind, input_speaker = classifications[index - 1]
            if input_kind != 'human' or input_speaker == target_speaker:
                continue
            context_start = max(0, index - 4)
            context = tuple(values[context_start:index - 1])
            time_gap, time_quality = _time_gap(input_turn, target)
            source_refs = _source_refs((*context, input_turn, target))
            context_ids = tuple(turn.id for turn in context)
            pair_id = _digest({'friend_id': self.scope.friend_id,
                'session_id': session_id, 'input_turn_id': input_turn.id,
                'context_turn_ids': context_ids, 'target_turn_id': target.id,
                'target_speaker_id': target_speaker})
            self._check_scope()
            yield DialoguePair(id=pair_id, friend_id=self.scope.friend_id,
                session_id=session_id, input_turn_ids=(input_turn.id,),
                context_turn_ids=context_ids, target_turn_id=target.id,
                target_speaker_id=target_speaker,
                user_bubbles=input_turn.bubbles, target_bubbles=target.bubbles,
                context_turns=context, source_refs=source_refs,
                time_gap=time_gap, time_quality=time_quality)

    def _check_scope(self):
        refreshed = FriendScope.load(self.scope.friend_id,
            project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        if refreshed.friend_dir != self.scope.friend_dir:
            raise ScopeMismatch('friend binding changed during pair building')


def _target_ids(values):
    if isinstance(values, (str, bytes, Mapping)) or values is None:
        raise InvalidSource('target_speaker_ids must be an explicit sequence or set')
    try:
        items = tuple(values)
    except TypeError as exc:
        raise InvalidSource('target_speaker_ids must be an explicit sequence or set') from exc
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise InvalidSource('target speaker ids must be nonempty strings')
    return frozenset(items)


def _validate_turns(turns, scope):
    previous_id = None
    source_boundary = None
    turn_ids = set()
    for ordinal, turn in enumerate(turns):
        if (not isinstance(turn.session_id, str) or not turn.session_id or
                type(turn.ordinal) is not int or turn.ordinal != ordinal or
                not isinstance(turn.id, str) or not turn.id or turn.id in turn_ids or
                not isinstance(turn.speaker_id, str) or not turn.speaker_id or
                not isinstance(turn.bubbles, tuple) or not turn.bubbles):
            raise InvalidSource('turn order or identity is invalid')
        if turn.previous_turn_id != previous_id:
            raise InvalidSource('turn chain differs from input order')
        if turn.time_quality not in _TIME_QUALITY:
            raise InvalidSource('turn time quality is invalid')
        for message in turn.bubbles:
            if not isinstance(message, NormalizedMessage):
                raise InvalidSource('turn bubble is not a NormalizedMessage')
            source = message.source
            if source.data_scope == DataScope.PRIVATE_FRIEND:
                if source.friend_id != scope.friend_id:
                    raise ScopeMismatch('turn belongs to another friend')
            elif source.friend_id is not None:
                raise ScopeMismatch('public source cannot claim a friend identity')
            if source.data_scope == DataScope.PUBLIC_PACK:
                scope.require_pack(source.pack_id, source.pack_version)
            boundary = (source.data_scope, source.privacy_view, source.friend_id,
                        source.dataset_id, source.import_id, source.pack_id, source.pack_version)
            if source_boundary is None:
                source_boundary = boundary
            elif boundary != source_boundary:
                raise InvalidSource('one session cannot mix source boundaries')
        turn_ids.add(turn.id)
        previous_id = turn.id


def _classify_turn(turn):
    actor = turn.speaker_id.strip().lower()
    if actor == 'unknown' or actor.startswith('unknown:'):
        return 'unknown', turn.speaker_id
    kinds = []
    for message in turn.bubbles:
        origin = message.source.origin
        role = message.speaker_role.strip().lower()
        if origin == SourceOrigin.BOT_GENERATED or role in _BOT_ROLES:
            kinds.append(('bot', turn.speaker_id))
        elif origin in _TARGET_ORIGINS and role in _HUMAN_ROLES:
            if message.speaker_id != turn.speaker_id:
                return 'unknown', turn.speaker_id
            kinds.append(('human', turn.speaker_id))
        else:
            return 'unknown', turn.speaker_id
    if not kinds or len({item[0] for item in kinds}) != 1:
        return 'unknown', turn.speaker_id
    return kinds[0]


def _source_refs(turns):
    refs, seen = [], set()
    for turn in turns:
        for bubble in turn.bubbles:
            source = bubble.source
            if source not in seen:
                seen.add(source)
                refs.append(source)
    return tuple(refs)


def _time_gap(input_turn, target_turn):
    start = _aware_time(input_turn.bubbles[-1].timestamp)
    end = _aware_time(target_turn.bubbles[0].timestamp)
    if start is None or end is None:
        return None, 'UNKNOWN'
    gap = (end - start).total_seconds()
    if not math.isfinite(gap) or gap < 0:
        return None, 'UNKNOWN'
    return gap, 'KNOWN'


def _aware_time(value):
    return _aware_datetime(value)


def _digest(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


class PairStore:
    """Persist rebuildable pair locators in the verified per-friend sidecar."""

    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('PairStore requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.dialogues = DialogueStore(self.scope)
        self.db = self.dialogues.db
        self.builder = DialoguePairBuilder(self.scope)

    @property
    def revision(self):
        return self.dialogues.revision

    def save_pairs(self, pairs, expected_revision):
        source_token = snapshot(self.scope)
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be a nonnegative integer')
        try:
            iterator = iter(pairs)
        except TypeError as exc:
            raise InvalidSource('save_pairs requires a pair iterable') from exc
        bounded = []
        for pair in iterator:
            if len(bounded) >= MAX_SESSION_MESSAGES:
                raise Unavailable('pair batch exceeds the 10000 session limit')
            if not isinstance(pair, DialoguePair):
                raise InvalidSource('save_pairs accepts DialoguePair values only')
            bounded.append(pair)
        pairs = tuple(bounded)
        if not pairs:
            raise InvalidSource('save_pairs requires a nonempty DialoguePair batch')
        session_ids = {pair.session_id for pair in pairs}
        if len(session_ids) != 1:
            raise InvalidSource('save_pairs accepts one complete session batch')
        session_id = next(iter(session_ids))
        if any(pair.friend_id != self.scope.friend_id for pair in pairs):
            raise ScopeMismatch('pair belongs to another friend scope')
        targets = {pair.target_speaker_id for pair in pairs}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            session = self.dialogues.load_session(session_id)
            rebuilt = tuple(self.builder.build_pairs(build_turns(session), targets))
            by_id = {pair.id: pair for pair in rebuilt}
            if (len(by_id) != len(rebuilt) or {pair.id for pair in pairs} != set(by_id) or
                    any(by_id.get(pair.id) != pair for pair in pairs)):
                raise InvalidSource('pair differs from its verified source session')
            if len({pair.id for pair in pairs}) != len(pairs):
                raise InvalidSource('duplicate pair id in batch')
            new_pairs = []
            for pair in pairs:
                row = self.db.execute('SELECT * FROM dialogue_pairs WHERE friend_id=? AND id=?',
                    (self.scope.friend_id, pair.id)).fetchone()
                expected = (pair.session_id, pair.input_turn_ids[0], pair.target_turn_id,
                    _canonical_json(pair.context_turn_ids), pair.target_speaker_id,
                    pair.time_gap, pair.time_quality)
                if row is not None:
                    actual = tuple(row[key] for key in ('session_id', 'input_turn_id',
                        'target_turn_id', 'context_turn_ids_json', 'target_speaker_id',
                        'time_gap', 'time_quality'))
                    if actual != expected:
                        raise Conflict('pair id already stores different content')
                else:
                    new_pairs.append((self.scope.friend_id, pair.id, *expected))
            if new_pairs:
                if self.revision != expected_revision:
                    raise Conflict('catalog revision changed')
                self.db.executemany('INSERT INTO dialogue_pairs VALUES(?,?,?,?,?,?,?,?,?)', new_pairs)
                self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                                (self.scope.friend_id,))
                self.db.execute('COMMIT')
                return expected_revision + 1
            self.db.execute('ROLLBACK')
            return self.revision
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    @source_read
    def load_pairs(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise InvalidSource('session id must be nonempty text')
        self.builder._check_scope()
        self.db.execute('BEGIN')
        try:
            rows = self.db.execute('SELECT * FROM dialogue_pairs WHERE friend_id=? AND session_id=? '
                'ORDER BY target_turn_id', (self.scope.friend_id, session_id)).fetchall()
            if not rows:
                self.db.execute('ROLLBACK')
                return ()
            session = self.dialogues.load_session(session_id)
            target_ids = {row['target_speaker_id'] for row in rows}
            rebuilt = tuple(self.builder.build_pairs(build_turns(session), target_ids))
            stored = {row['id']: row for row in rows}
            if len(stored) != len(rows) or set(stored) != {pair.id for pair in rebuilt}:
                raise InvalidSource('stored pair projection differs from source session')
            for pair in rebuilt:
                row = stored[pair.id]
                actual = (row['session_id'], row['input_turn_id'], row['target_turn_id'],
                    row['context_turn_ids_json'], row['target_speaker_id'], row['time_gap'],
                    row['time_quality'])
                expected = (pair.session_id, pair.input_turn_ids[0], pair.target_turn_id,
                    _canonical_json(pair.context_turn_ids), pair.target_speaker_id,
                    pair.time_gap, pair.time_quality)
                if actual != expected:
                    raise InvalidSource('stored pair locator differs from rebuilt pair')
            self.db.execute('COMMIT')
            return rebuilt
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.dialogues.close()


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
