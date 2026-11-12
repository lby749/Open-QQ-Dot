"""Bounded session segmentation and bubble-preserving turn construction."""
from source_state import snapshot, require_snapshot
from source_state import source_read
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from collections.abc import Mapping

from dataset_normalizer import DatasetNormalizer, privacy
from dataset_registry import DatasetRegistry
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, NormalizedMessage,
                           PrivacyView, PrivacyViolation, SourceRef)


MAX_SESSION_MESSAGES = 10_000
_AWARE_ISO = re.compile(
    r'^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})$')


@dataclass(frozen=True)
class Session:
    id: str
    friend_id: str
    boundary_key: str
    speaker_set: tuple[str, ...]
    start_time: str | None
    end_time: str | None
    gap_seconds: float
    time_quality: str
    messages: tuple[NormalizedMessage, ...]
    conversation_id: str | None = None


@dataclass(frozen=True)
class Turn:
    id: str
    session_id: str
    ordinal: int
    speaker_id: str
    bubbles: tuple[NormalizedMessage, ...]
    previous_turn_id: str | None
    event_time: str | None
    time_quality: str


class SessionSegmenter:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('SessionSegmenter requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    def segment(self, messages, gap_seconds=1800, conversation_ids=None):
        gap_seconds = _gap_seconds(gap_seconds)
        if conversation_ids is not None and not isinstance(conversation_ids, Mapping):
            raise InvalidSource('conversation_ids must map message ids to explicit ids')
        current, current_boundary, current_ids = [], None, set()
        previous_time = None

        for message in messages:
            self._check_scope()
            _validate_message_scope(message, self.scope)
            if message.id in current_ids:
                raise InvalidSource('duplicate message id within a session')
            conversation_id = None
            if conversation_ids is not None and message.id in conversation_ids:
                conversation_id = conversation_ids[message.id]
                if not isinstance(conversation_id, str) or not conversation_id.strip():
                    raise InvalidSource('explicit conversation id must be nonempty text')
            descriptor = _boundary_descriptor(message, conversation_id)
            boundary = _canonical(descriptor)
            current_time = _aware_datetime(message.timestamp)
            split = bool(current) and boundary != current_boundary
            if current and not split and previous_time is not None and current_time is not None:
                delta = (current_time - previous_time).total_seconds()
                split = delta < 0 or delta > gap_seconds
            if split:
                self._check_scope()
                yield _make_session(self.scope.friend_id, current_boundary,
                                    current, gap_seconds, current_conversation_id)
                current, current_boundary = [], None
                current_ids = set()
                previous_time = None
            if len(current) >= MAX_SESSION_MESSAGES:
                raise Unavailable('session exceeds the 10000 message limit')
            if not current:
                current_boundary = boundary
                current_conversation_id = conversation_id
            current.append(message)
            current_ids.add(message.id)
            previous_time = current_time

        if current:
            self._check_scope()
            yield _make_session(self.scope.friend_id, current_boundary,
                                current, gap_seconds, current_conversation_id)

    def _check_scope(self):
        refreshed = FriendScope.load(self.scope.friend_id,
            project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        if refreshed.friend_dir != self.scope.friend_dir:
            raise ScopeMismatch('friend binding changed during segmentation')


def build_turns(session):
    if not isinstance(session, Session) or not session.messages:
        if isinstance(session, Session):
            return ()
        raise InvalidSource('build_turns requires a Session')
    turns, bubbles, speaker, seen = [], [], None, set()
    for message in session.messages:
        if message.id in seen:
            raise InvalidSource('duplicate message id within a session')
        seen.add(message.id)
        key = _turn_speaker(message)
        if bubbles and key != speaker:
            turns.append(_make_turn(session.id, len(turns), speaker, bubbles,
                                    turns[-1].id if turns else None))
            bubbles = []
        if not bubbles:
            speaker = key
        bubbles.append(message)
    if bubbles:
        turns.append(_make_turn(session.id, len(turns), speaker, bubbles,
                                turns[-1].id if turns else None))
    return tuple(turns)


def _make_turn(session_id, ordinal, speaker_id, bubbles, previous_turn_id):
    bubble_ids = [message.id for message in bubbles]
    turn_id = _digest({'session_id': session_id, 'ordinal': ordinal,
                       'speaker_id': speaker_id, 'bubble_ids': bubble_ids})
    quality = 'KNOWN' if all(_aware_datetime(item.timestamp) is not None for item in bubbles) else 'UNKNOWN'
    return Turn(id=turn_id, session_id=session_id, ordinal=ordinal,
                speaker_id=speaker_id, bubbles=tuple(bubbles),
                previous_turn_id=previous_turn_id,
                event_time=bubbles[0].timestamp, time_quality=quality)


def _make_session(friend_id, boundary_key, messages, gap_seconds, conversation_id=None):
    ordered_ids = [message.id for message in messages]
    session_id = _digest({'friend_id': friend_id, 'boundary_key': boundary_key,
                          'gap_seconds': gap_seconds, 'ordered_message_ids': ordered_ids})
    speakers = tuple(sorted({_turn_speaker(message) for message in messages}))
    quality = 'KNOWN' if all(_aware_datetime(item.timestamp) is not None for item in messages) else 'UNKNOWN'
    return Session(id=session_id, friend_id=friend_id, boundary_key=boundary_key,
                   speaker_set=speakers, start_time=messages[0].timestamp,
                   end_time=messages[-1].timestamp, gap_seconds=gap_seconds,
                   time_quality=quality, messages=tuple(messages),
                   conversation_id=conversation_id)


def _boundary_descriptor(message, conversation_id):
    source = message.source
    public_unassigned = (source.data_scope != DataScope.PRIVATE_FRIEND and
                         conversation_id is None)
    return {
        'data_scope': source.data_scope.value,
        'friend_id': source.friend_id,
        'privacy_view': source.privacy_view.value,
        'dataset_id': source.dataset_id,
        'import_id': source.import_id,
        'pack_id': source.pack_id,
        'pack_version': source.pack_version,
        'conversation_id': conversation_id,
        'unassigned_public_message': message.id if public_unassigned else None,
    }


def _turn_speaker(message):
    speaker = message.speaker_id
    if (message.speaker_role.strip().lower() == 'unknown' or
            speaker.strip().lower() == 'unknown' or speaker.startswith('unknown:')):
        return 'unknown:' + message.id
    return speaker


def _validate_message_scope(message, scope):
    if not isinstance(message, NormalizedMessage):
        raise InvalidSource('segment accepts only NormalizedMessage values')
    if message.source.data_scope == DataScope.PRIVATE_FRIEND:
        if message.source.friend_id != scope.friend_id:
            raise ScopeMismatch('message belongs to another friend')
    elif message.source.friend_id is not None:
        raise ScopeMismatch('public messages cannot claim a friend history identity')
    if message.source.data_scope == DataScope.PUBLIC_PACK:
        scope.require_pack(message.source.pack_id, message.source.pack_version)


def _gap_seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise InvalidSource('gap_seconds must be a finite nonnegative number')
    return float(value)


def _aware_datetime(value):
    if not isinstance(value, str) or not _AWARE_ISO.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00').replace('z', '+00:00'))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _digest(value):
    payload = _canonical(value).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


class DialogueStore:
    """Persist complete session and turn projections in the existing V4 sidecar."""

    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('DialogueStore requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.store = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
                                  project_root=self.scope.project_root)
        try:
            self.registry = DatasetRegistry(self.scope, readonly=True)
            self.normalizer = DatasetNormalizer(self.scope, self.registry)
        except Exception:
            if hasattr(self, 'registry'):
                self.registry.close()
            self.store.close()
            raise
        self.db = self.store.db

    @property
    def revision(self):
        return self.store.revision

    @source_read
    def list_sessions(self, limit=100):
        """Return bounded session metadata for local source pickers, without message text."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidSource('session list limit must be between 1 and 500')
        self._check_scope()
        rows = self.db.execute('SELECT id,speaker_set_json,start_time,end_time FROM dialogue_sessions '
            'WHERE friend_id=? ORDER BY COALESCE(end_time,start_time) DESC,id LIMIT ?',
            (self.scope.friend_id, limit)).fetchall()
        try:
            result = []
            for row in rows:
                speakers = json.loads(row['speaker_set_json'])
                if not isinstance(speakers, list) or any(not isinstance(item, str) for item in speakers):
                    raise ValueError('speaker list shape')
                result.append({'id': row['id'], 'speaker_ids': tuple(speakers),
                               'start_time': row['start_time'], 'end_time': row['end_time']})
            return tuple(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise InvalidSource('stored session metadata is invalid') from None

    def save_session(self, session, expected_revision):
        source_token = snapshot(self.scope)
        self._check_scope()
        if not isinstance(session, Session) or session.friend_id != self.scope.friend_id:
            raise ScopeMismatch('session belongs to another friend scope')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidSource('expected_revision must be a nonnegative integer')
        turns = build_turns(session)
        if len({item.id for item in session.messages}) != len(session.messages):
            raise InvalidSource('duplicate message id within session')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            require_snapshot(self.scope, source_token)
            self._check_scope()
            self._verify_messages(session.messages)
            self._verify_session_shape(session, check_id=False)
            existing = self.db.execute('SELECT 1 FROM dialogue_sessions WHERE friend_id=? AND id=?',
                                       (self.scope.friend_id, session.id)).fetchone()
            if existing:
                stored = self._load_session(session.id, verify=True)
                if stored != session:
                    raise Conflict('session id already stores different content')
                self.db.execute('ROLLBACK')
                return self.store.revision
            self._verify_session_shape(session)
            if self.store.revision != expected_revision:
                raise Conflict('catalog revision changed')
            self.db.execute('INSERT INTO dialogue_sessions VALUES(?,?,?,?,?,?,?,?,?)', (
                self.scope.friend_id, session.id, session.boundary_key,
                json.dumps(session.speaker_set, ensure_ascii=False, separators=(',', ':')),
                session.start_time, session.end_time, session.gap_seconds,
                session.time_quality,
                json.dumps([item.id for item in session.messages], ensure_ascii=False,
                           separators=(',', ':'))))
            for turn in turns:
                self.db.execute('INSERT INTO dialogue_turns VALUES(?,?,?,?,?,?,?,?,?)', (
                    self.scope.friend_id, turn.id, session.id, turn.ordinal,
                    turn.speaker_id, turn.event_time, turn.time_quality,
                    turn.previous_turn_id,
                    json.dumps([_message_payload(item) for item in turn.bubbles],
                               ensure_ascii=False, separators=(',', ':'), sort_keys=True)))
            self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                            (self.scope.friend_id,))
            self.db.execute('COMMIT')
            return expected_revision + 1
        except Exception:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    @source_read
    def load_session(self, session_id):
        self._check_scope()
        if not isinstance(session_id, str) or not session_id:
            raise InvalidSource('session id must be nonempty text')
        return self._load_session(session_id, verify=True)

    def _load_session(self, session_id, verify):
        row = self.db.execute('SELECT * FROM dialogue_sessions WHERE friend_id=? AND id=?',
                              (self.scope.friend_id, session_id)).fetchone()
        if row is None:
            raise InvalidSource('session does not exist in this friend scope')
        try:
            turns_rows = self.db.execute('SELECT * FROM dialogue_turns WHERE friend_id=? AND session_id=? '
                                         'ORDER BY ordinal',
                                         (self.scope.friend_id, session_id)).fetchall()
            turns, messages = [], []
            for item in turns_rows:
                bubble_values = json.loads(item['bubbles_json'])
                if not isinstance(bubble_values, list):
                    raise ValueError('bubble projection shape')
                bubbles = tuple(_message_from_payload(value) for value in bubble_values)
                messages.extend(bubbles)
                turns.append(Turn(item['id'], item['session_id'], item['ordinal'],
                                  item['speaker_id'], bubbles, item['previous_turn_id'],
                                  item['event_time'], item['time_quality']))
            ordered_ids = json.loads(row['ordered_message_ids_json'])
            speaker_set = json.loads(row['speaker_set_json'])
            if (not isinstance(ordered_ids, list) or not isinstance(speaker_set, list) or
                    [item.id for item in messages] != ordered_ids or not messages):
                raise ValueError('session projection shape')
            session = Session(row['id'], self.scope.friend_id, row['boundary_key'],
                              tuple(speaker_set), row['start_time'], row['end_time'],
                              row['gap_seconds'], row['time_quality'], tuple(messages),
                              _boundary_conversation(row['boundary_key']))
        except PrivacyViolation:
            raise
        except (InvalidSource, KeyError, TypeError, ValueError, IndexError):
            raise InvalidSource('stored dialogue projection is invalid') from None
        if verify:
            self._check_scope()
            self._verify_messages(session.messages)
            self._verify_session_shape(session)
            expected_turns = build_turns(session)
            if tuple(turns) != expected_turns:
                raise InvalidSource('stored turns differ from their ordered bubbles')
        return session

    def _verify_messages(self, messages):
        candidates = []
        locators = set()
        for message in messages:
            _validate_message_scope(message, self.scope)
            try:
                locator = json.loads(message.id.removeprefix('import:'))
            except (TypeError, ValueError) as exc:
                raise InvalidSource('message id has no persisted import locator') from exc
            source = message.source
            if (message.id[:7] != 'import:' or not isinstance(locator, list) or len(locator) != 4 or
                    locator[:3] != [source.dataset_id, source.import_id, source.record_id] or
                    type(locator[3]) is not int or locator[3] < 0):
                raise InvalidSource('message id and source locator disagree')
            key = tuple(locator)
            if key in locators:
                raise InvalidSource('duplicate persisted message locator')
            locators.add(key)
            candidates.append({'friend_id': self.scope.friend_id,
                'dataset_id': locator[0], 'import_id': locator[1],
                'record_id': locator[2], 'message_seq': locator[3]})
        verified = list(self.normalizer.normalize(candidates))
        if len(verified) != len(messages):
            raise InvalidSource('one or more session messages cannot be resolved')
        for supplied, original in zip(messages, verified):
            if supplied.source.data_scope == DataScope.PUBLIC_PACK:
                self.scope.require_pack(supplied.source.pack_id, supplied.source.pack_version)
                raise Unavailable('public pack source cannot be verified without its manifest')
            rebuilt = (list(privacy([original], PrivacyView.CLOUD_SAFE).messages)[0]
                       if supplied.source.privacy_view == PrivacyView.CLOUD_SAFE else original)
            if supplied != rebuilt:
                raise InvalidSource('session bubble differs from normalized source data')

    def _verify_session_shape(self, session, check_id=True):
        try:
            descriptor = json.loads(session.boundary_key)
        except (TypeError, ValueError) as exc:
            raise InvalidSource('session boundary descriptor is invalid') from exc
        if _canonical(descriptor) != session.boundary_key or not session.messages:
            raise InvalidSource('session boundary descriptor is not canonical')
        expected_descriptor = _boundary_descriptor(session.messages[0], session.conversation_id)
        if (descriptor != expected_descriptor or any(
                _boundary_descriptor(message, session.conversation_id) != descriptor
                for message in session.messages)):
            raise InvalidSource('session boundary differs from its message sources')
        conversation_ids = ({message.id: session.conversation_id for message in session.messages}
                            if session.conversation_id is not None else None)
        rebuilt = list(SessionSegmenter(self.scope).segment(
            session.messages, gap_seconds=session.gap_seconds,
            conversation_ids=conversation_ids))
        if len(rebuilt) != 1:
            raise InvalidSource('session messages require more than one session boundary')
        expected = rebuilt[0]
        if (expected != session and
                not (not check_id and expected.id != session.id and
                     replace(expected, id=session.id) == session)):
            raise InvalidSource('session projection differs from its ordered messages')

    def _check_scope(self):
        refreshed = FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir, allowed_pack_versions=self.scope.allowed_pack_versions)
        if (refreshed.friend_dir != self.scope.friend_dir or
                self.registry.scope.project_root != refreshed.project_root or
                self.registry.scope.friend_dir != refreshed.friend_dir):
            raise ScopeMismatch('friend binding changed or registry scope differs')

    def close(self):
        self.store.close()
        self.registry.close()


def _message_payload(message):
    return {'id': message.id, 'timestamp': message.timestamp,
            'speaker_id': message.speaker_id, 'speaker_role': message.speaker_role,
            'text': message.text, 'message_type': message.message_type,
            'media_id': message.media_id, 'source': asdict(message.source),
            'target_role': message.target_role, 'target_selected': message.target_selected}


def _message_from_payload(value):
    source = dict(value['source'])
    if source['origin'] is not None:
        from persona_types import SourceOrigin
        source['origin'] = SourceOrigin(source['origin'])
    source['data_scope'] = DataScope(source['data_scope'])
    source['privacy_view'] = PrivacyView(source['privacy_view'])
    return NormalizedMessage(id=value['id'], timestamp=value['timestamp'],
        speaker_id=value['speaker_id'], speaker_role=value['speaker_role'],
        text=value['text'], message_type=value['message_type'], media_id=value['media_id'],
        source=SourceRef(**source), target_role=value['target_role'],
        target_selected=value['target_selected'])


def _boundary_conversation(boundary_key):
    try:
        value = json.loads(boundary_key)
    except (TypeError, ValueError) as exc:
        raise InvalidSource('stored session boundary descriptor is invalid') from exc
    if not isinstance(value, dict):
        raise InvalidSource('stored session boundary descriptor is invalid')
    conversation_id = value.get('conversation_id')
    if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id.strip()):
        raise InvalidSource('stored conversation id is invalid')
    return conversation_id
