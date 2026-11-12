"""Small, permission-checked adapters for public conversation records."""
import copy
import json
from dataclasses import dataclass

from dataset_importer import MAX_RECORD_BYTES, MAX_TEXT_CHARS
from dataset_registry import DatasetPermissions, DatasetRegistry
from dialogue_turns import MAX_SESSION_MESSAGES
from persona_store import ScopeMismatch, Unavailable
from persona_types import FriendScope, InvalidSource, PrivacyViolation


_JSONL_MAPPING = {
    'format': 'jsonl', 'record_id': 'record_id', 'message_seq': 'message_seq',
    'speaker_id': 'speaker_id', 'speaker_role': 'speaker_role', 'text': 'text',
    'event_time': 'event_time', 'source_origin': 'source_origin',
    'message_type': 'message_type', 'media_id': 'media_id',
}


@dataclass(frozen=True)
class AdaptedConversation:
    dataset_id: str
    version: str
    conversation_id: str
    rows: tuple[dict, ...]
    permissions: DatasetPermissions


class PublicDatasetAdapter:
    def __init__(self, scope, registry):
        if not isinstance(scope, FriendScope) or not isinstance(registry, DatasetRegistry):
            raise ScopeMismatch('PublicDatasetAdapter requires a verified scope and registry')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.registry = registry
        self._check_scope()

    @staticmethod
    def jsonl_mapping():
        return dict(_JSONL_MAPPING)

    def adapt_record(self, dataset_id, adapter_name, record, record_id):
        self._check_scope()
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise InvalidSource('dataset_id must be nonempty text')
        dataset = self.registry.get(dataset_id)
        if dataset is None or not dataset.active:
            raise ScopeMismatch('dataset is absent or inactive')
        if dataset.friend_id != self.scope.friend_id:
            raise ScopeMismatch('dataset belongs to another friend scope')
        if dataset.data_scope != 'PUBLIC_DATASET':
            raise ScopeMismatch('adapter accepts PUBLIC_DATASET records only')
        if not isinstance(dataset.version, str) or not dataset.version.strip():
            raise Unavailable('dataset version is not recorded')
        if dataset.local_use_allowed is None:
            raise Unavailable('dataset local-use permission is unknown')
        if dataset.local_use_allowed is False:
            raise PrivacyViolation('dataset local use is not permitted')
        if not isinstance(dataset.license_data, str) or not dataset.license_data.strip():
            raise Unavailable('dataset license evidence is not recorded')
        if not isinstance(record_id, (str, int)) or type(record_id) is bool or not str(record_id).strip() or len(str(record_id)) > 256:
            raise InvalidSource('record_id must be a nonempty string or integer')
        if type(record) is not dict:
            raise InvalidSource('adapter record must be a parsed object')
        try:
            record_size = len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        except (TypeError, ValueError):
            raise InvalidSource('adapter record must contain JSON values only') from None
        if record_size > MAX_RECORD_BYTES:
            raise InvalidSource('adapter record exceeds the importer record limit')
        if adapter_name not in ('personal_dialog', 'lccc_base'):
            raise Unavailable('adapter schema or data license is not verified')
        raw_record_id = str(record_id)
        conversation_id = _json_id((dataset.dataset_id, dataset.version, raw_record_id))
        if adapter_name == 'personal_dialog':
            texts, speaker_ids, roles, metadata = self._personal_dialog(
                record, dataset.dataset_id, dataset.version, raw_record_id)
        else:
            texts, speaker_ids, roles, metadata = self._lccc_base(record)
        rows = []
        for seq, (text, speaker_id, role) in enumerate(zip(texts, speaker_ids, roles)):
            row = {
                'record_id': conversation_id, 'source_record_id': raw_record_id,
                'source_version': dataset.version, 'message_seq': seq,
                'speaker_id': speaker_id, 'speaker_role': role, 'text': text,
                'event_time': None, 'source_origin': 'PUBLIC_DATASET',
                'message_type': 'text', 'media_id': None,
            }
            if seq == 0:
                row.update(copy.deepcopy(metadata))
            try:
                row_size = len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            except (TypeError, ValueError):
                raise InvalidSource('adapted row must contain JSON values only') from None
            if row_size > MAX_RECORD_BYTES:
                raise InvalidSource('adapted row exceeds the importer record limit')
            rows.append(row)
        permissions = self.registry.get_permissions(dataset_id)
        return AdaptedConversation(dataset_id=dataset.dataset_id, version=dataset.version,
            conversation_id=conversation_id, rows=tuple(rows), permissions=permissions)

    def _personal_dialog(self, record, dataset_id, version, record_id):
        dialog = record.get('dialog')
        if type(dialog) is not list or not 1 <= len(dialog) <= MAX_SESSION_MESSAGES:
            raise InvalidSource('PersonalDialog dialog must be a bounded nonempty list')
        if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS
               for text in dialog):
            raise InvalidSource('PersonalDialog utterance is invalid')
        uid = record.get('uid')
        if uid is not None and (type(uid) is not list or len(uid) != len(dialog)):
            raise InvalidSource('PersonalDialog uid length must match dialog')
        if type(record.get('profile')) not in (list, type(None)) or type(record.get('responder_profile')) not in (list, type(None)):
            raise InvalidSource('PersonalDialog profile fields must be lists or null')
        metadata = {key: copy.deepcopy(record[key]) for key in
                    ('profile', 'responder_profile', 'golden_response', 'is_biased', 'uid')
                    if key in record}
        speakers, roles = [], []
        for item in uid if uid is not None else [None] * len(dialog):
            if (item is None or isinstance(item, str) and (
                    not item.strip() or item.strip().lower() == 'unknown' or
                    item.strip().lower().startswith('unknown:'))):
                speakers.append('unknown')
                roles.append('unknown')
                continue
            if type(item) not in (str, int) or not str(item).strip() or len(str(item)) > 256:
                raise InvalidSource('PersonalDialog uid value is invalid')
            speakers.append(_json_id((dataset_id, version, record_id, item)))
            roles.append('human')
        return tuple(dialog), tuple(speakers), tuple(roles), metadata

    @staticmethod
    def _lccc_base(record):
        dialog = record.get('dialog')
        if type(dialog) is not list or not 1 <= len(dialog) <= MAX_SESSION_MESSAGES:
            raise InvalidSource('LCCC-base dialog must be a bounded nonempty list')
        if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS
               for text in dialog):
            raise InvalidSource('LCCC-base utterance is invalid')
        speakers = ('unknown',) * len(dialog)
        roles = ('unknown',) * len(dialog)
        return tuple(dialog), speakers, roles, {}

    def _check_scope(self):
        if (self.registry.friend_id != self.scope.friend_id or
                self.registry.scope.project_root != self.scope.project_root or
                self.registry.scope.friend_dir != self.scope.friend_dir):
            raise ScopeMismatch('registry and adapter scopes differ')
        refreshed = FriendScope.load(self.scope.friend_id,
            project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
            allowed_pack_versions=self.scope.allowed_pack_versions)
        if refreshed.friend_dir != self.scope.friend_dir:
            raise ScopeMismatch('friend binding changed during dataset adaptation')


def _json_id(parts):
    value = json.dumps(parts, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(value) > 256:
        raise InvalidSource('structured speaker or conversation id is too long')
    return value
