"""Bounded, local-only dataset import into the per-friend persona sidecar."""
import codecs
import csv
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from dataset_registry import DatasetRegistry
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import FriendScope, SourceOrigin


BATCH_ROWS = 100
BATCH_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_TEXT_CHARS = 100_000
MAX_REJECTION_INDICES = 100
_FORMATS = {'json', 'jsonl', 'csv', 'txt'}
_FIELD_KEYS = {'record_id', 'message_seq', 'speaker_id', 'speaker_role',
               'direction', 'event_time', 'text', 'source_origin', 'message_type',
               'media_id'}
_MAPPING_KEYS = _FIELD_KEYS | {'records_path', 'encoding', 'format'}
_ORIGINS = {item.value for item in SourceOrigin}


class InvalidImport(ValueError):
    pass


@dataclass(frozen=True)
class ImportPreview:
    count_or_unknown: int | None
    detected_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    source_hash: str


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    accepted: int
    rejected: int
    duplicates: int
    checkpoint: dict


def _now():
    return datetime.now().astimezone().isoformat()


def _canonical_hash(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(mapping, format):
    if not isinstance(mapping, dict) or set(mapping) - _MAPPING_KEYS:
        raise InvalidImport('mapping has unsupported fields')
    normalized = dict(mapping)
    if 'format' in normalized and normalized['format'] != format:
        raise InvalidImport('mapping format differs from import format')
    normalized['format'] = format
    if not isinstance(format, str):
        raise InvalidImport('format must be a supported name')
    for key in _FIELD_KEYS:
        if key in normalized and (not isinstance(normalized[key], str) or not normalized[key]):
            raise InvalidImport('mapping field names must be nonempty strings')
    encoding = normalized.setdefault('encoding', 'utf-8-sig')
    if not isinstance(encoding, str) or not encoding:
        raise InvalidImport('encoding must be explicit text')
    if format == 'json' and encoding.lower().replace('_', '-') not in ('utf-8', 'utf-8-sig'):
        raise InvalidImport('streaming JSON supports UTF-8 only')
    if format == 'csv':
        try:
            csv_encoding = codecs.lookup(encoding).name
        except LookupError:
            raise InvalidImport('unsupported CSV encoding') from None
        if csv_encoding not in ('utf-8', 'utf-8-sig', 'ascii', 'gbk', 'gb18030', 'gb2312',
                                'iso8859-1', 'cp1252'):
            raise InvalidImport('CSV encoding is not supported by the bounded reader')
    if format in ('json', 'jsonl'):
        records_path = normalized.setdefault('records_path', 'item')
        if not isinstance(records_path, str) or not records_path:
            raise InvalidImport('records_path must identify a JSON array')
    if format == 'txt' and normalized.get('text') != '__line__':
        raise InvalidImport("TXT mapping requires text='__line__'")
    if format != 'txt' and not isinstance(normalized.get('text'), str):
        raise InvalidImport('text field mapping is required')
    if normalized.get('source_origin') in _ORIGINS:
        pass  # A declared constant is explicit provenance.
    elif 'source_origin' in normalized and not isinstance(normalized['source_origin'], str):
        raise InvalidImport('source_origin must be a declared value or field name')
    return normalized


def _jsonl_rows(path, encoding):
    with Path(path).open('rb') as source:
        for index, line, oversized in _bounded_lines(source):
            if oversized:
                yield index, None, MAX_RECORD_BYTES + 1, 'oversized'
                continue
            try:
                row = json.loads(line.decode(encoding))
                if not isinstance(row, dict):
                    raise ValueError
                yield index, row, len(line), None
            except (UnicodeError, ValueError, json.JSONDecodeError):
                yield index, None, len(line), 'malformed'


def _rows(path, format, mapping):
    encoding = mapping['encoding']
    if format == 'json':
        try:
            import ijson
        except ImportError as exc:
            raise Unavailable('ijson is required for streaming JSON imports') from exc
        with Path(path).open('rb') as source:
            if mapping['encoding'].lower().replace('_', '-') == 'utf-8-sig':
                header = source.read(3)
                if header != b'\xef\xbb\xbf':
                    source.seek(0)
            try:
                for index, row in enumerate(ijson.items(source, mapping['records_path']), 1):
                    if not isinstance(row, dict):
                        yield index, None, 0, 'malformed'
                        continue
                    # ijson bounds whole-file memory, but a single scalar is materialized by its backend.
                    size = len(json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                                          default=str).encode('utf-8'))
                    yield index, row, size, 'oversized' if size > MAX_RECORD_BYTES else None
            except InvalidImport:
                raise
            except Exception:
                raise InvalidImport('malformed JSON input') from None
        return
    if format == 'jsonl':
        yield from _jsonl_rows(path, encoding)
        return
    if format == 'csv':
        with Path(path).open('rb') as source:
            if codecs.lookup(encoding).name == 'utf-8-sig':
                if source.read(3) != b'\xef\xbb\xbf':
                    source.seek(0)
            records = _csv_records(source)
            first = next(records, None)
            if first is None:
                return
            header_bytes, oversized = first
            if oversized:
                raise InvalidImport('CSV header exceeds byte limit') from None
            try:
                header = next(csv.reader([header_bytes.decode(encoding)], strict=True))
            except Exception:
                raise InvalidImport('malformed CSV header') from None
            index = 0
            for record_bytes, oversized in records:
                index += 1
                size = len(record_bytes) if not oversized else MAX_RECORD_BYTES + 1
                if oversized:
                    yield index, None, size, 'oversized'
                    continue
                try:
                    cells = next(csv.reader([record_bytes.decode(encoding)], strict=True))
                except Exception:
                    yield index, None, size, 'malformed'
                    continue
                if len(cells) > len(header):
                    yield index, None, size, 'malformed'
                    continue
                row = dict(zip(header, cells + [''] * (len(header) - len(cells))))
                yield index, row, size, None
        return
    with Path(path).open('rb') as source:
        for index, line, oversized in _bounded_lines(source):
            if oversized:
                yield index, None, MAX_RECORD_BYTES + 1, 'oversized'
                continue
            try:
                text = line.decode(encoding).rstrip('\r\n')
                yield index, {'__line__': text}, len(line), None
            except UnicodeError:
                yield index, None, len(line), 'malformed'


def _value(row, mapping, key, default=None):
    if key not in mapping:
        return default
    field = mapping[key]
    if field in _ORIGINS and key == 'source_origin':
        return field
    if field == '__line__':
        return row.get('__line__')
    return row.get(field)


def _normalize(row, index, mapping, source_hash, data_scope):
    text = _value(row, mapping, 'text')
    if text is None:
        text = ''
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise InvalidImport('invalid text')
    media_id = _value(row, mapping, 'media_id')
    if media_id is not None and (not isinstance(media_id, (str, int)) or len(str(media_id)) > 512):
        raise InvalidImport('invalid media reference')
    if not text.strip() and media_id is None:
        raise InvalidImport('empty text without media')
    message_type = _value(row, mapping, 'message_type', 'unknown')
    if message_type is None:
        message_type = 'unknown'
    if not isinstance(message_type, (str, int, Decimal)) or not str(message_type).strip() or len(str(message_type)) > 64:
        raise InvalidImport('invalid message type')
    record_id = _value(row, mapping, 'record_id')
    if record_id is None or record_id == '':
        record_id = source_hash + ':' + str(index)
    if isinstance(record_id, Decimal) and record_id == record_id.to_integral_value():
        record_id = int(record_id)
    if type(record_id) not in (str, int) or len(str(record_id)) > 256:
        raise InvalidImport('invalid record id')
    seq = _value(row, mapping, 'message_seq', 0)
    if seq in (None, ''):
        seq = 0
    if isinstance(seq, Decimal) and seq == seq.to_integral_value():
        seq = int(seq)
    elif isinstance(seq, str) and re.fullmatch(r'[0-9]+', seq):
        seq = int(seq)
    elif type(seq) is not int:
        raise InvalidImport('invalid message sequence')
    if seq < 0:
        raise InvalidImport('invalid message sequence')
    speaker = _value(row, mapping, 'speaker_id')
    role = _value(row, mapping, 'speaker_role', 'unknown')
    direction = _value(row, mapping, 'direction')
    event_time = _value(row, mapping, 'event_time')
    origin = _value(row, mapping, 'source_origin')
    if origin is not None and (not isinstance(origin, str) or origin not in _ORIGINS):
        raise InvalidImport('invalid source origin')
    if data_scope == 'PUBLIC_DATASET' and origin == SourceOrigin.REAL_RELATIONSHIP.value:
        raise ScopeMismatch('private relationship origin cannot be imported as public data')
    if isinstance(speaker, Decimal) and speaker == speaker.to_integral_value():
        speaker = int(speaker)
    if speaker is not None and (type(speaker) not in (str, int) or len(str(speaker)) > 256):
        raise InvalidImport('invalid speaker id')
    if role is None:
        role = 'unknown'
    if type(role) not in (str, int) or not str(role).strip() or len(str(role)) > 64:
        raise InvalidImport('invalid speaker role')
    if direction is not None and (type(direction) not in (str, int) or len(str(direction)) > 64):
        raise InvalidImport('invalid direction')
    if event_time is not None:
        if not isinstance(event_time, str):
            event_time = str(event_time)
        try:
            datetime.fromisoformat(event_time.replace('Z', '+00:00'))
        except ValueError:
            event_time = None
    raw = json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str)
    if len(raw.encode('utf-8')) > MAX_RECORD_BYTES:
        raise InvalidImport('oversized record')
    speaker = None if speaker is None else str(speaker)
    body = {'record_id': str(record_id), 'message_seq': seq,
            'speaker_id': speaker, 'event_time': event_time, 'text': text,
            'message_type': str(message_type),
            'media_id': None if media_id is None else str(media_id)}
    return (str(record_id), seq, speaker, str(role),
            None if direction is None else str(direction), event_time, text,
            str(message_type), None if media_id is None else str(media_id),
            _canonical_hash(body), origin, raw)


class DatasetImporter:
    def __init__(self, registry):
        if not isinstance(registry, DatasetRegistry):
            raise ScopeMismatch('DatasetImporter requires a verified DatasetRegistry')
        self.registry = registry
        self.scope = FriendScope.load(registry.friend_id,
            project_root=registry.scope.project_root, friend_dir=registry.scope.friend_dir,
            allowed_pack_versions=registry.scope.allowed_pack_versions)
        if registry.friend_id != self.scope.friend_id:
            raise ScopeMismatch('registry owner differs from verified scope')
        self.db = registry.db
        self.store = registry.store

    def inspect(self, path, format, mapping):
        normalized = self._settings(path, format, mapping)
        digest = _hash_file(path)
        count, fields, warnings = 0, set(), set()
        try:
            for index, row, size, error in _rows(path, format, normalized):
                count += 1
                if error:
                    warnings.add(error)
                elif isinstance(row, dict) and len(fields) < 128:
                    for key in row:
                        if len(fields) >= 128:
                            break
                        fields.add(str(key))
        except Exception:
            return ImportPreview(None, tuple(sorted(fields)), ('parse_error',), digest)
        return ImportPreview(count, tuple(sorted(fields)), tuple(sorted(warnings)), digest)

    def import_records(self, scope, path, mapping, source, checkpoint=None, cancel=None):
        if not isinstance(scope, FriendScope) or scope.friend_id != self.scope.friend_id:
            raise ScopeMismatch('import scope differs from verified registry owner')
        verified = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        if verified.friend_dir != self.scope.friend_dir:
            raise ScopeMismatch('import directory differs from registry scope')
        if not isinstance(source, str) or not source:
            raise InvalidImport('source must be the registered dataset_id')
        format = _format_for(path, mapping)
        normalized = self._settings(path, format, mapping)
        dataset = self._active_dataset(source)
        source_hash = _hash_file(path)
        mapping_hash = _canonical_hash(normalized)
        settings_hash = _canonical_hash({'format': format,
            'encoding': normalized['encoding'], 'batch_rows': BATCH_ROWS,
            'batch_bytes': BATCH_BYTES, 'max_record_bytes': MAX_RECORD_BYTES,
            'max_text_chars': MAX_TEXT_CHARS})
        supplied = dict(checkpoint or {})
        if supplied:
            expected = {'source_hash': source_hash, 'mapping_hash': mapping_hash,
                        'settings_hash': settings_hash, 'dataset_id': source,
                        'friend_id': self.scope.friend_id}
            if any(supplied.get(key) != value for key, value in expected.items()):
                raise InvalidImport('checkpoint inputs changed')
            import_id = supplied.get('import_id')
            row = self.db.execute('SELECT * FROM dataset_imports WHERE friend_id=? AND import_id=?',
                                   (self.scope.friend_id, import_id)).fetchone()
            if row is None:
                raise InvalidImport('checkpoint import does not exist')
            if (row['source_hash'] != source_hash or row['mapping_hash'] != mapping_hash or
                    row['settings_hash'] != settings_hash or row['dataset_id'] != source):
                raise InvalidImport('checkpoint inputs changed')
            current = json.loads(row['checkpoint_json'] or '{}')
            if current.get('next_index') != supplied.get('next_index'):
                raise Conflict('checkpoint is stale')
            if row['status'] == 'COMPLETED':
                return ImportResult(import_id, current['accepted'], current['rejected'],
                                    current['duplicates'], current)
            state = current
            expected_revision = self.store.revision
        else:
            import_id = uuid.uuid4().hex
            expected_revision = self.store.revision
            state = {'friend_id': self.scope.friend_id, 'dataset_id': source,
                'import_id': import_id, 'source_hash': source_hash,
                'mapping_hash': mapping_hash, 'settings_hash': settings_hash,
                'next_index': 0, 'accepted': 0, 'rejected': 0, 'duplicates': 0,
                'rejection_categories': {}, 'rejected_rows': []}
            self._begin(expected_revision, source, dataset.data_scope)
            try:
                now = _now()
                self.db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,'
                    'source_origin,data_scope,source_hash,mapping_hash,settings_hash,checkpoint_json,'
                    'status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.scope.friend_id, import_id, source,
                     normalized.get('source_origin') if normalized.get('source_origin') in _ORIGINS else None,
                     dataset.data_scope, source_hash, mapping_hash, settings_hash,
                     json.dumps(state, separators=(',', ':')), 'CREATED', now, now))
                expected_revision += 1
                self.db.execute('UPDATE scope_state SET revision=? WHERE friend_id=?',
                                 (expected_revision, self.scope.friend_id))
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
        accepted, rejected, duplicates = (state['accepted'], state['rejected'], state['duplicates'])
        batch, batch_bytes, processed_in_batch, index = [], 0, 0, -1
        committed_state = deepcopy(state)
        try:
            for index, raw, size, error in _rows(path, format, normalized):
                if index <= state['next_index']:
                    continue
                if _cancelled(cancel):
                    break
                processed_in_batch += 1
                batch_bytes += size
                if error:
                    rejected += 1
                    _category(state, error, index)
                else:
                    try:
                        values = _normalize(raw, index, normalized, source_hash, dataset.data_scope)
                        (record_id, seq, speaker, role, direction, event_time, text,
                         message_type, media_id, fingerprint, origin, raw_json) = values
                        batch.append((self.scope.friend_id, import_id, source, record_id,
                            seq, speaker, role, direction, event_time, _now(), text,
                            message_type, media_id, fingerprint, origin, raw_json))
                    except InvalidImport as exc:
                        rejected += 1
                        _category(state, str(exc), index)
                if processed_in_batch >= BATCH_ROWS or batch_bytes >= BATCH_BYTES:
                    state.update(rejected=rejected)
                    inserted, dupes = self._commit_batch(batch, source, dataset.data_scope,
                        expected_revision, state, index, import_id)
                    accepted += inserted
                    duplicates += dupes
                    state.update(accepted=accepted, duplicates=duplicates,
                                 rejected=rejected, next_index=index)
                    committed_state = deepcopy(state)
                    expected_revision += 1
                    batch, batch_bytes, processed_in_batch = [], 0, 0
                    if _cancelled(cancel):
                        break
            cancelled = _cancelled(cancel)
            if not cancelled and _hash_file(path) != source_hash:
                raise InvalidImport('source changed during import')
            if processed_in_batch and not cancelled:
                state.update(rejected=rejected)
                inserted, dupes = self._commit_batch(batch, source, dataset.data_scope,
                    expected_revision, state, index, import_id)
                accepted += inserted
                duplicates += dupes
                state.update(accepted=accepted, duplicates=duplicates,
                             rejected=rejected, next_index=index)
                committed_state = deepcopy(state)
                expected_revision += 1
            if cancelled:
                state = committed_state
                accepted, rejected, duplicates = (state['accepted'], state['rejected'],
                                                   state['duplicates'])
                self._set_status(import_id, source, dataset.data_scope, expected_revision,
                                 state, 'CANCELLED')
            else:
                state.update(accepted=accepted, duplicates=duplicates,
                             rejected=rejected, next_index=max(index, state['next_index']))
                self._set_status(import_id, source, dataset.data_scope, expected_revision,
                                 state, 'COMPLETED')
            return ImportResult(import_id, accepted, rejected, duplicates, dict(state))
        except Conflict:
            raise
        except Exception:
            # Preserve earlier committed batches and mark this import as failed.
            try:
                state = deepcopy(committed_state)
                self._set_status(import_id, source, dataset.data_scope,
                    expected_revision, state, 'FAILED')
            except Exception:
                pass
            raise

    def _settings(self, path, format, mapping):
        if not isinstance(format, str) or format not in _FORMATS:
            raise InvalidImport('unsupported format')
        Path(path).resolve(strict=True)
        return _mapping(mapping, format)

    def _active_dataset(self, dataset_id):
        record = self.registry.get(dataset_id)
        if record is None or not record.active:
            raise ScopeMismatch('dataset is absent or inactive')
        if record.friend_id not in (None, self.scope.friend_id):
            raise ScopeMismatch('dataset owner differs from current friend')
        if record.data_scope not in ('PRIVATE_FRIEND', 'PUBLIC_DATASET'):
            raise ScopeMismatch('dataset scope is invalid')
        if record.local_use_allowed is False:
            raise ScopeMismatch('dataset local use is not permitted')
        return record

    def revoke_import(self, import_id):
        from persona_incremental import revoke_import
        return revoke_import(self.scope, import_id)

    def _begin(self, expected_revision, dataset_id, data_scope):
        if self.store.readonly:
            raise Unavailable('dataset sidecar is read-only')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            refreshed = FriendScope.load(self.scope.friend_id,
                project_root=self.scope.project_root, friend_dir=self.scope.friend_dir,
                allowed_pack_versions=self.scope.allowed_pack_versions)
            if refreshed.friend_dir != self.scope.friend_dir:
                raise ScopeMismatch('friend identity changed during import')
            self._check_revision(expected_revision)
            current = self._active_dataset(dataset_id)
            if current.data_scope != data_scope:
                raise ScopeMismatch('dataset privacy scope changed')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _check_revision(self, expected):
        if self.store.revision != expected:
            raise Conflict('scope revision changed during import')

    def _commit_batch(self, batch, dataset_id, data_scope, revision, state, index, import_id):
        self._begin(revision, dataset_id, data_scope)
        try:
            if self.db.execute('SELECT 1 FROM source_import_tombstones WHERE friend_id=? AND import_id=?',
                               (self.scope.friend_id, import_id)).fetchone():
                raise Conflict('Import is revoked and cannot be replayed')
            inserted = duplicates = 0
            for item in batch:
                (friend_id, import_id, dataset_id, record_id, seq, speaker_id, role,
                 direction, event_time, recorded_at, text, message_type, media_id,
                 fingerprint, origin, raw_json) = item
                exists = self.db.execute('SELECT 1 FROM imported_messages WHERE friend_id=? '
                    'AND dataset_id=? AND (content_fingerprint=? OR '
                    '(record_id=? AND message_seq=?)) LIMIT 1',
                    (friend_id, dataset_id, fingerprint, record_id, seq)).fetchone()
                if exists:
                    duplicates += 1
                    continue
                self.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,'
                    'record_id,message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,'
                    'text,message_type,media_id,content_fingerprint,source_origin,source_record_json) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (friend_id, import_id, dataset_id, record_id, seq, speaker_id, role,
                     direction, event_time, recorded_at, text, message_type, media_id,
                     fingerprint, origin, raw_json))
                inserted += 1
            state = dict(state)
            state.update(next_index=index, accepted=state['accepted'] + inserted,
                         duplicates=state['duplicates'] + duplicates)
            now = _now()
            self.db.execute('UPDATE dataset_imports SET checkpoint_json=?,status=?,updated_at=? '
                'WHERE friend_id=? AND import_id=?',
                (json.dumps(state, separators=(',', ':')), 'RUNNING', now,
                 self.scope.friend_id, import_id))
            self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                             (self.scope.friend_id,))
            self.db.execute('COMMIT')
            return inserted, duplicates
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _set_status(self, import_id, dataset_id, data_scope, revision, state, status):
        self._begin(revision, dataset_id, data_scope)
        try:
            self.db.execute('UPDATE dataset_imports SET checkpoint_json=?,status=?,updated_at=? '
                'WHERE friend_id=? AND import_id=?',
                (json.dumps(state, separators=(',', ':')), status, _now(),
                 self.scope.friend_id, import_id))
            self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                             (self.scope.friend_id,))
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise


def _format_for(path, mapping):
    value = mapping.get('format') if isinstance(mapping, dict) else None
    if value:
        return value
    suffix = Path(path).suffix.lower()
    return {'.json': 'json', '.jsonl': 'jsonl', '.ndjson': 'jsonl',
            '.csv': 'csv', '.txt': 'txt'}.get(suffix, '')


def _cancelled(cancel):
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    if hasattr(cancel, 'is_set'):
        return bool(cancel.is_set())
    return bool(cancel)


def _category(state, value, index):
    categories = state.setdefault('rejection_categories', {})
    categories[value] = categories.get(value, 0) + 1
    rows = state.setdefault('rejected_rows', [])
    if len(rows) < MAX_REJECTION_INDICES:
        rows.append({'row': index, 'category': value})


def _bounded_lines(source):
    index = 0
    while True:
        line = source.readline(MAX_RECORD_BYTES + 1)
        if not line:
            return
        index += 1
        oversized = len(line) > MAX_RECORD_BYTES
        if oversized and not line.endswith(b'\n'):
            while line and not line.endswith(b'\n'):
                line = source.readline(MAX_RECORD_BYTES + 1)
        yield index, b'' if oversized else line, oversized


def _csv_records(source):
    """Yield bounded logical CSV records, retaining quoted physical newlines."""
    buffer = bytearray()
    size = 0
    oversized = False
    quoted = False
    quote_pending = False
    field_start = True
    while True:
        chunk = source.read(8192)
        if not chunk:
            break
        for char in chunk:
            size += 1
            if not oversized:
                if size <= MAX_RECORD_BYTES:
                    buffer.append(char)
                else:
                    oversized = True
                    buffer.clear()
            if quote_pending:
                quote_pending = False
                if char == 34:
                    continue
                quoted = False
            if char == 34:
                if quoted:
                    quote_pending = True
                elif field_start:
                    quoted = True
            elif char == 10 and not quoted:
                yield bytes(buffer), oversized
                buffer.clear()
                size = 0
                oversized = False
                field_start = True
                continue
            if not quoted:
                field_start = char == 44
    if size:
        yield bytes(buffer), oversized
