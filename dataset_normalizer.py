"""Local normalization and privacy views for verified imported messages."""
import json
import re
from source_state import source_read
from itertools import islice
from collections import deque
from dataclasses import dataclass, replace
from typing import Iterator
from urllib.parse import unquote

from dataset_registry import DatasetRegistry
from persona_store import ScopeMismatch
from persona_types import (DataScope, FriendScope, InvalidSource, NormalizedMessage,
                           PrivacyView, SourceOrigin, SourceRef)


_FILTERED_TYPES = {
    'system', 'revoke', 'recalled', 'withdraw', 'withdrawn', 'deleted', 'message_recalled',
    'red_packet', 'red_envelope', 'redpacket', 'transfer', 'advertisement', 'ad',
}
_BOT_ROLES = {'bot', 'assistant', 'ai', 'system'}
_SEEN_WINDOW = 10_000
_PLACEHOLDERS = {'[redacted]', '<redacted>', '***', '[已脱敏]'}
_URL_SECRETS = {'accesstoken', 'token', 'auth', 'authorization', 'password', 'passwd',
                'secret', 'apikey', 'key', 'code'}
_REDACTIONS = (
    ('url_secret', re.compile(r'([?&]([^=&#\s]+)=)([^&#\s]*)'), 'url'),
    ('address', re.compile(r'((?:收货地址|家庭住址|住址|地址)\s*[:：]\s*)([^,，;；\n。]+)', re.I), 'marked'),
    ('account', re.compile(r'((?:账号|帐号|用户名)\s*[:：]\s*)([^\s,，;；。]+)', re.I), 'marked'),
    ('id_number', re.compile(r'(?<!\d)(?:[1-9]\d{5}[- ]?(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[- ]?\d{3}[0-9Xx]|[1-9]\d{5}[- ]?\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[- ]?\d{3})(?!\d)'), 'whole'),
    ('phone', re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)'), 'whole'),
    ('email', re.compile(r'(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])', re.I), 'whole'),
)


@dataclass
class PrivacyResult:
    messages: Iterator[NormalizedMessage]
    redaction_counts: dict[str, int]


class DatasetNormalizer:
    def __init__(self, scope, registry):
        if not isinstance(scope, FriendScope) or not isinstance(registry, DatasetRegistry):
            raise ScopeMismatch('DatasetNormalizer requires a verified scope and registry')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        if registry.friend_id != self.scope.friend_id:
            raise ScopeMismatch('registry owner differs from verified scope')
        if (registry.scope.project_root != self.scope.project_root or
                registry.scope.friend_dir != self.scope.friend_dir):
            raise ScopeMismatch('registry directory differs from verified scope')
        self.registry = registry
        self.db = registry.db

    def normalize(self, messages):
        messages = iter(messages)
        seen, recent = set(), deque()
        while True:
            batch = tuple(islice(messages, 256))
            if not batch:
                return
            yield from self._normalize_batch(batch, seen, recent)

    @source_read
    def _normalize_batch(self, messages, seen, recent):
        for candidate in messages:
            self._check_scope()
            row = self._persisted_row(candidate)
            self._validate_message_row(row)
            dataset_id, import_id = row['dataset_id'], row['import_id']
            dataset = self.registry.get(dataset_id)
            if (dataset is None or not dataset.active or
                    dataset.friend_id not in (None, self.scope.friend_id) or
                    dataset.local_use_allowed is False):
                raise ScopeMismatch('dataset is absent, inactive or not locally usable')
            imported = self.db.execute('SELECT data_scope FROM dataset_imports '
                'WHERE friend_id=? AND import_id=? AND dataset_id=?',
                (self.scope.friend_id, import_id, dataset_id)).fetchone()
            if imported is None or imported['data_scope'] != dataset.data_scope:
                raise ScopeMismatch('import locator or registered scope is invalid')
            locator = (dataset_id, import_id, row['record_id'], row['message_seq'])
            if locator in seen:
                continue
            if len(recent) == _SEEN_WINDOW:
                seen.discard(recent.popleft())
            seen.add(locator)
            recent.append(locator)
            if self._filtered(row):
                continue
            origin = self._origin(row['source_origin'], dataset.data_scope)
            speaker = row['speaker_id']
            role = row['speaker_role'] or 'unknown'
            if speaker is None or not str(speaker).strip():
                speaker = 'unknown:' + json.dumps(locator, ensure_ascii=False, separators=(',', ':'))
                role = 'unknown'
            else:
                speaker = str(speaker)
            bot = role.strip().lower() in _BOT_ROLES or origin == SourceOrigin.BOT_GENERATED
            target_role = 'context' if bot else 'unmapped'
            data_scope = DataScope(dataset.data_scope)
            is_public = data_scope == DataScope.PUBLIC_DATASET
            source = SourceRef(
                dataset_id=dataset_id, import_id=import_id, record_id=row['record_id'],
                message_seq=None, speaker_id=speaker, origin=origin,
                data_scope=data_scope, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=None if is_public else self.scope.friend_id,
                event_time=row['event_time'], recorded_at=row['recorded_at'],
                license_id=dataset.license_data if is_public else None,
                model_analysis_allowed=(is_public and dataset.model_analysis_allowed is True
                                        and bool(dataset.license_data)))
            ordinal = row['message_seq']
            self._check_scope()
            yield NormalizedMessage(
                id='import:' + json.dumps(locator, ensure_ascii=False, separators=(',', ':')),
                timestamp=row['event_time'], speaker_id=speaker,
                speaker_role=role, text=row['text'],
                message_type=row['message_type'] or 'unknown',
                media_id=row['media_id'] or None, source=source,
                target_role=target_role, target_selected=False)

    def _check_scope(self):
        refreshed = FriendScope.load(self.scope.friend_id,
            project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        if refreshed.friend_dir != self.scope.friend_dir:
            raise ScopeMismatch('friend binding changed during normalization')

    def _persisted_row(self, candidate):
        def value(key):
            try:
                return candidate[key]
            except (KeyError, IndexError, TypeError):
                raise InvalidSource('import row is missing its source locator') from None

        friend_id, dataset_id, import_id = value('friend_id'), value('dataset_id'), value('import_id')
        record_id, seq = value('record_id'), value('message_seq')
        if friend_id != self.scope.friend_id:
            raise ScopeMismatch('import row friend differs from verified scope')
        if (not all(isinstance(item, str) and item.strip()
                    for item in (dataset_id, import_id, record_id)) or type(seq) is not int or seq < 0):
            raise InvalidSource('import row locator is invalid')
        rows = self.db.execute('SELECT * FROM imported_messages WHERE friend_id=? AND dataset_id=? '
            'AND import_id=? AND record_id=? AND message_seq=? LIMIT 2',
            (self.scope.friend_id, dataset_id, import_id, record_id, seq)).fetchall()
        if len(rows) != 1:
            raise InvalidSource('import row does not identify one persisted message')
        return rows[0]

    @staticmethod
    def _origin(value, data_scope):
        if value is None:
            return None
        try:
            origin = SourceOrigin(value)
        except (TypeError, ValueError):
            return None
        if data_scope == 'PUBLIC_DATASET' and origin == SourceOrigin.REAL_RELATIONSHIP:
            raise ScopeMismatch('public dataset cannot claim private relationship origin')
        return origin

    @staticmethod
    def _validate_message_row(row):
        if not isinstance(row['text'], str) or not isinstance(row['speaker_role'], str) or not isinstance(row['message_type'], str):
            raise InvalidSource('persisted message text/role/type is invalid')
        if row['speaker_id'] is not None and not isinstance(row['speaker_id'], str):
            raise InvalidSource('persisted speaker id is invalid')
        if row['media_id'] is not None and not isinstance(row['media_id'], str):
            raise InvalidSource('persisted media id is invalid')
        if row['event_time'] is not None and not isinstance(row['event_time'], str):
            raise InvalidSource('persisted event time is invalid')
        if not isinstance(row['recorded_at'], str):
            raise InvalidSource('persisted recorded time is invalid')

    @staticmethod
    def _filtered(row):
        role = (row['speaker_role'] or '').strip().lower()
        kind = (row['message_type'] or '').strip().lower().replace('-', '_').replace(' ', '_')
        text = row['text'] or ''
        return (role == 'system' or kind in _FILTERED_TYPES or
                not text.strip() and not row['media_id'] or
                '\ufffd' in text and not row['media_id'])


def privacy(messages, mode):
    """Return a lazy copied view and counts accumulated as its messages are consumed."""
    try:
        view = PrivacyView(mode)
    except (TypeError, ValueError) as exc:
        raise InvalidSource('unknown privacy view') from exc
    counts = {name: 0 for name, _, _ in _REDACTIONS}

    def converted():
        for message in messages:
            if not isinstance(message, NormalizedMessage):
                raise InvalidSource('privacy conversion requires normalized messages')
            if (view == PrivacyView.LOCAL_FULL and
                    message.source.privacy_view == PrivacyView.CLOUD_SAFE):
                raise InvalidSource('CloudSafe text cannot be restored to LocalFull')
            text = message.text
            if view == PrivacyView.CLOUD_SAFE:
                for name, pattern, kind in _REDACTIONS:
                    text, count = _redact(text, pattern, kind)
                    counts[name] += count
            source = replace(message.source, privacy_view=view)
            yield replace(message, text=text, source=source)

    return PrivacyResult(converted(), counts)


def _redact(text, pattern, kind):
    count = 0

    def replacement(match):
        nonlocal count
        if kind == 'url':
            key = re.sub(r'[_-]', '', unquote(match.group(2)).lower())
            value = match.group(3)
            if key not in _URL_SECRETS or unquote(value).lower() in _PLACEHOLDERS:
                return match.group(0)
            count += 1
            return match.group(1) + '[REDACTED]'
        if kind == 'marked':
            value = match.group(2)
            if value.lower() in _PLACEHOLDERS:
                return match.group(0)
            count += 1
            return match.group(1) + '[REDACTED]'
        count += 1
        return '[REDACTED]'

    return pattern.sub(replacement, text), count
