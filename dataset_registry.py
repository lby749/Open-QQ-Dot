"""Per-friend dataset metadata and explicit permission registry."""
from source_state import source_read
from dataclasses import dataclass
from datetime import datetime

from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import FriendScope


_FIELDS = ('dataset_id', 'name', 'data_scope', 'source', 'homepage', 'repository',
           'local_path', 'version', 'usage_scope', 'license_code', 'license_data',
           'license_image', 'local_use_allowed', 'derived_stats_allowed',
           'model_analysis_allowed', 'raw_corpus_redistribution_allowed',
           'image_redistribution_allowed', 'contains_personal_data', 'modality',
           'notes')
_PERMISSIONS = ('local_use_allowed', 'derived_stats_allowed', 'model_analysis_allowed',
                'raw_corpus_redistribution_allowed', 'image_redistribution_allowed')
_BOOL_FIELDS = _PERMISSIONS + ('contains_personal_data',)


class InvalidDataset(ValueError):
    pass


def _text(value, field, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataset(field + ' must be a nonempty string' + (' or None' if optional else ''))


def _optional_text(value, field):
    _text(value, field, optional=True)


def _permission(value, field):
    if value is not None and type(value) is not bool:
        raise InvalidDataset(field + ' must be bool or None (unknown)')


@dataclass(frozen=True, kw_only=True)
class DatasetRecord:
    dataset_id: str
    name: str
    data_scope: str
    usage_scope: str
    source: str | None = None
    homepage: str | None = None
    repository: str | None = None
    local_path: str | None = None
    version: str | None = None
    license_code: str | None = None
    license_data: str | None = None
    license_image: str | None = None
    local_use_allowed: bool | None = None
    derived_stats_allowed: bool | None = None
    model_analysis_allowed: bool | None = None
    raw_corpus_redistribution_allowed: bool | None = None
    image_redistribution_allowed: bool | None = None
    contains_personal_data: bool | None = None
    modality: str | None = None
    notes: str | None = None
    friend_id: str | None = None
    active: bool = True
    revision: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self):
        for name in ('dataset_id', 'name', 'usage_scope'):
            _text(getattr(self, name), name)
        if self.data_scope not in ('PRIVATE_FRIEND', 'PUBLIC_DATASET'):
            raise ScopeMismatch('dataset data_scope must be PRIVATE_FRIEND or PUBLIC_DATASET')
        for name in ('source', 'homepage', 'repository', 'local_path', 'version',
                     'license_code', 'license_data', 'license_image', 'modality', 'notes'):
            _optional_text(getattr(self, name), name)
        for name in _BOOL_FIELDS:
            _permission(getattr(self, name), name)
        if type(self.active) is not bool or type(self.revision) is not int or self.revision < 0:
            raise InvalidDataset('active/revision metadata is invalid')


@dataclass(frozen=True, kw_only=True)
class DatasetPermissions:
    data_scope: str
    local_use_allowed: bool | None
    derived_stats_allowed: bool | None
    model_analysis_allowed: bool | None
    raw_corpus_redistribution_allowed: bool | None
    image_redistribution_allowed: bool | None
    license_code: str | None
    license_data: str | None
    license_image: str | None

    def __post_init__(self):
        if self.data_scope not in ('PRIVATE_FRIEND', 'PUBLIC_DATASET'):
            raise ScopeMismatch('dataset data_scope must be PRIVATE_FRIEND or PUBLIC_DATASET')
        for name in _PERMISSIONS:
            _permission(getattr(self, name), name)
        for name in ('license_code', 'license_data', 'license_image'):
            _optional_text(getattr(self, name), name)

    @property
    def cloud_model_analysis_allowed(self):
        return self.data_scope == 'PUBLIC_DATASET' and self.model_analysis_allowed is True and bool(self.license_data)

    @property
    def local_research_allowed(self):
        return self.local_use_allowed is not False

    @property
    def raw_corpus_redistribution_effective(self):
        return (self.data_scope == 'PUBLIC_DATASET' and
                self.raw_corpus_redistribution_allowed is True and bool(self.license_data))

    @property
    def image_redistribution_effective(self):
        return (self.data_scope == 'PUBLIC_DATASET' and
                self.image_redistribution_allowed is True and bool(self.license_image))


class DatasetRegistry:
    def __init__(self, scope, readonly=False):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('DatasetRegistry requires a verified FriendScope')
        verified = FriendScope.load(scope.friend_id, project_root=scope.project_root,
                                    friend_dir=scope.friend_dir,
                                    allowed_pack_versions=scope.allowed_pack_versions)
        self.scope = verified
        self.friend_id = verified.friend_id
        self.readonly = readonly
        self.store = PersonaStore(self.friend_id, verified.friend_dir, readonly,
                                  project_root=verified.project_root)
        self.db = self.store.db

    @property
    def revision(self):
        return self.db.execute('SELECT revision FROM scope_state WHERE friend_id=?',
                               (self.friend_id,)).fetchone()[0]

    @source_read
    def list(self, limit=100, after_dataset_id=None, include_inactive=False):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('limit must be an integer from 1 to 500')
        if type(include_inactive) is not bool:
            raise ValueError('include_inactive must be bool')
        if after_dataset_id is not None:
            _text(after_dataset_id, 'after_dataset_id')
        where = 'friend_id=?'
        args = [self.friend_id]
        if not include_inactive:
            where += ' AND active=1'
        if after_dataset_id is not None:
            where += ' AND dataset_id>?'
            args.append(after_dataset_id)
        rows = self.db.execute('SELECT * FROM dataset_registry WHERE ' + where +
                               ' ORDER BY dataset_id LIMIT ?', (*args, limit))
        return tuple(self._record(row) for row in rows)

    @source_read
    def get(self, dataset_id):
        _text(dataset_id, 'dataset_id')
        row = self.db.execute('SELECT * FROM dataset_registry WHERE friend_id=? AND dataset_id=?',
                              (self.friend_id, dataset_id)).fetchone()
        return None if row is None else self._record(row)

    def register(self, record, expected_revision):
        record = self._validate_record(record, new=True)
        self._begin(expected_revision)
        try:
            now = datetime.now().astimezone().isoformat()
            values = [self.friend_id] + [getattr(record, key) for key in _FIELDS]
            values += [int(record.active), 1, now, now]
            columns = ('friend_id',) + _FIELDS + ('active', 'revision', 'created_at', 'updated_at')
            self.db.execute('INSERT INTO dataset_registry(' + ','.join(columns) + ') VALUES(' +
                            ','.join('?' for _ in values) + ')', values)
            self._commit_revision()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return self.get(record.dataset_id)

    def update(self, record, expected_revision):
        record = self._validate_record(record)
        self._begin(expected_revision)
        try:
            old = self.get(record.dataset_id)
            if old is None:
                raise KeyError(record.dataset_id)
            if old.data_scope != record.data_scope:
                raise ScopeMismatch('data_scope is immutable; register a new dataset_id for a new source')
            now = datetime.now().astimezone().isoformat()
            assignments = ','.join(key + '=?' for key in _FIELDS[1:])
            values = [getattr(record, key) for key in _FIELDS[1:]]
            values.extend((int(record.active), old.revision + 1, now,
                           self.friend_id, record.dataset_id, old.revision))
            cursor = self.db.execute('UPDATE dataset_registry SET ' + assignments +
                ',active=?,revision=?,updated_at=? WHERE friend_id=? AND dataset_id=? AND revision=?', values)
            if cursor.rowcount != 1:
                raise Conflict('dataset record changed')
            self._commit_revision()
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise
        return self.get(record.dataset_id)

    def deactivate(self, dataset_id, expected_revision):
        old = self.get(dataset_id)
        if old is None:
            raise KeyError(dataset_id)
        return self.update(DatasetRecord(**{**old.__dict__, 'active': False}), expected_revision)

    def get_permissions(self, dataset_id):
        record = self.get(dataset_id)
        if record is None or not record.active:
            return DatasetPermissions(data_scope='PRIVATE_FRIEND', **{
                name: False for name in _PERMISSIONS}, license_code=None,
                license_data=None, license_image=None)
        return DatasetPermissions(data_scope=record.data_scope,
            **{name: getattr(record, name) for name in _PERMISSIONS},
            license_code=record.license_code, license_data=record.license_data,
            license_image=record.license_image)

    def _validate_record(self, record, new=False):
        if not isinstance(record, DatasetRecord):
            raise InvalidDataset('record must be DatasetRecord')
        if record.friend_id not in (None, self.friend_id):
            raise ScopeMismatch('dataset belongs to another friend')
        if new and record.revision not in (0,):
            raise InvalidDataset('new records start at revision zero')
        return record

    def _begin(self, expected_revision):
        if self.readonly:
            raise Unavailable('dataset registry is read-only')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected_revision must be a nonnegative integer')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.revision != expected_revision:
                raise Conflict('catalog revision changed')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def _commit_revision(self):
        self.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?',
                         (self.friend_id,))

    @staticmethod
    def _record(row):
        values = dict(row)
        values['active'] = bool(values['active'])
        for name in _BOOL_FIELDS:
            if values[name] is not None:
                values[name] = bool(values[name])
        return DatasetRecord(**values)

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
