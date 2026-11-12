"""Fresh source snapshots and short, ordered source mutation locks.

Readers do not hold locks while calculating results. Supported writers take the
same friend lock before SQLite; database backups do not copy the commit nonce.
"""
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import fields, is_dataclass
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading
import time
import uuid


_locks = {}
_registry_lock = threading.Lock()
HISTORY_TABLES = ('messages', 'memories', 'memory_entries', 'memory_versions')
PERSONA_TABLES = ('dataset_registry', 'dataset_imports', 'imported_messages',
    'interest_entries', 'topic_knowledge', 'knowledge_entities',
    'knowledge_relations', 'sticker_assets', 'sticker_metadata',
    'source_import_tombstones', 'dialogue_sessions', 'dialogue_turns',
    'dialogue_pairs', 'persona_analysis_runs', 'persona_claims',
    'persona_claim_evidence', 'sticker_examples', 'artifact_versions', 'scope_state', 'pack_bindings')


def _conflict(message):
    from persona_store import Conflict
    return Conflict(message)


@contextmanager
def source_lock(directory, *, publication=False):
    directory = Path(directory).resolve()
    with _registry_lock:
        lock, local = _locks.setdefault(str(directory), (threading.RLock(), threading.local()))
    with lock:
        if publication and getattr(local, 'publication', False):
            raise _conflict('scope publication is already in progress')
        if publication:
            local.publication = True
        try:
            with _source_file_lock(directory, local):
                yield
        finally:
            if publication:
                local.publication = False


@contextmanager
def _source_file_lock(directory, local):
    if getattr(local, 'depth', 0):
        local.depth += 1
        try:
            yield
        finally:
            local.depth -= 1
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.source.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _conflict('source lock is busy') from None
                time.sleep(.01)
        local.depth = 1
        try:
            yield
        finally:
            local.depth = 0
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

_recovery = threading.local()


@contextmanager
def source_recovery(directory):
    """Only persistent operation recovery may write while an intent is pending."""
    key = str(Path(directory).resolve())
    previous = getattr(_recovery, 'directories', ())
    _recovery.directories = (*previous, key)
    try:
        yield
    finally:
        _recovery.directories = previous


def require_available(directory):
    path = Path(directory).resolve() / 'history.sqlite3'
    if str(path.parent) in getattr(_recovery, 'directories', ()) or not path.is_file():
        return
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_operations'").fetchone() and db.execute(
                "SELECT 1 FROM source_operations WHERE state='PENDING' LIMIT 1").fetchone():
            raise _conflict('source operation pending; reconcile before writing')


def _rotate(path):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='ascii') as stream:
            stream.write(uuid.uuid4().hex)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except PermissionError:
        # Windows can deny an atomic rename while another reader/AV holds the
        # target; a direct rewrite has the same result and keeps the lock usable.
        path.write_text(temporary.read_text(encoding='ascii'), encoding='ascii')
    finally:
        temporary.unlink(missing_ok=True)


class SourceConnection(sqlite3.Connection):
    """Existing execute/transaction API, with source lock before database locks."""
    def bind_source(self, path):
        self.source_path = Path(path)
        self._source_guard = None
        self._source_before = None

    def _revision(self):
        try:
            return super().execute('SELECT identity,revision FROM source_state WHERE singleton=1').fetchone()
        except sqlite3.OperationalError:
            return None

    def _acquire(self):
        if not hasattr(self, 'source_path') or self._source_guard is not None:
            return
        guard = source_lock(self.source_path.parent)
        guard.__enter__()
        try:
            require_available(self.source_path.parent)
        except Exception:
            guard.__exit__(None, None, None)
            raise
        self._source_guard = guard
        row = self._revision()
        self._source_before = tuple(row) if row else None

    def _release(self, committed=False):
        guard = getattr(self, '_source_guard', None)
        if guard is None:
            return
        try:
            row = self._revision()
            current = tuple(row) if row else None
            if committed and current != self._source_before:
                _rotate(self.source_path.with_name('.' + self.source_path.name + '.source-nonce'))
        finally:
            self._source_guard = None
            guard.__exit__(None, None, None)

    def execute(self, sql, parameters=()):
        verb = sql.lstrip().split(None, 1)[0].upper()
        write = (verb == 'BEGIN' and any(word in sql.upper() for word in ('IMMEDIATE', 'EXCLUSIVE')) or
                 verb in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP'})
        if write:
            self._acquire()
        try:
            cursor = super().execute(sql, parameters)
        except Exception:
            if not self.in_transaction:
                self._release()
            raise
        if verb in {'COMMIT', 'END', 'ROLLBACK'} or write and not self.in_transaction:
            self._release(verb != 'ROLLBACK')
        return cursor

    def executemany(self, sql, parameters):
        self._acquire()
        try:
            return super().executemany(sql, parameters)
        finally:
            if not self.in_transaction:
                self._release(True)

    def executescript(self, sql):
        self._acquire()
        try:
            return super().executescript(sql)
        finally:
            if not self.in_transaction:
                self._release(True)

    def commit(self):
        super().commit()
        self._release(True)

    def rollback(self):
        try:
            super().rollback()
        finally:
            self._release()

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self._release(args[0] is None)

    def close(self):
        try:
            super().close()
        finally:
            guard = getattr(self, '_source_guard', None)
            if guard:
                self._source_guard = None
                guard.__exit__(None, None, None)


def state_statements(tables):
    result = ['CREATE TABLE IF NOT EXISTS source_state ('
              'singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL, '
              'revision INTEGER NOT NULL DEFAULT 0, clear_epoch INTEGER NOT NULL DEFAULT 0)',
              'INSERT OR IGNORE INTO source_state(singleton,identity) VALUES(1,lower(hex(randomblob(16))))']
    for table in tables:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            result.append(f'CREATE TRIGGER IF NOT EXISTS source_{table}_{operation.lower()} '
                f'AFTER {operation} ON {table} BEGIN '
                'UPDATE source_state SET revision=revision+1 WHERE singleton=1; END')
    return tuple(result)


def initialize_history(store):
    with source_lock(store.path.parent), store.db:
        for sql in state_statements(HISTORY_TABLES):
            store.db.execute(sql)
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_library'").fetchone():
            for sql in state_statements(('topic_library',)):
                store.db.execute(sql)
        for operation, key in (('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')):
            store.db.execute(f'CREATE TRIGGER IF NOT EXISTS source_memory_meta_{operation.lower()} '
                f'AFTER {operation} ON metadata WHEN {key}.key IN '
                "('memory_revision','activity_memory_reset_seq') BEGIN "
                'UPDATE source_state SET revision=revision+1 WHERE singleton=1; END')
            store.db.execute(f'CREATE TRIGGER IF NOT EXISTS source_identity_research_{operation.lower()} '
                f'AFTER {operation} ON metadata WHEN {key}.key IN '
                "('qq_id','daily_research') BEGIN "
                'UPDATE source_state SET revision=revision+1 WHERE singleton=1; END')
        store.db.execute('CREATE TABLE IF NOT EXISTS source_operations ('
            'id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, result INTEGER)')

        store.db.execute('CREATE TABLE IF NOT EXISTS incremental_cursors ('
                'friend_id TEXT NOT NULL, processing_version TEXT NOT NULL, source_epoch INTEGER NOT NULL, '
                'cursor INTEGER NOT NULL, PRIMARY KEY(friend_id,processing_version,source_epoch))')
        store.db.execute('CREATE TABLE IF NOT EXISTS incremental_projections ('
                'friend_id TEXT NOT NULL, processing_version TEXT NOT NULL, source_epoch INTEGER NOT NULL, '
                'message_seq INTEGER NOT NULL, source_hash TEXT NOT NULL, source_kind TEXT NOT NULL, '
                'direction TEXT NOT NULL, PRIMARY KEY(friend_id,processing_version,source_epoch,message_seq))')
        store.db.execute('CREATE TABLE IF NOT EXISTS incremental_receipts ('
                'id TEXT PRIMARY KEY, friend_id TEXT NOT NULL, processing_version TEXT NOT NULL, '
                'source_epoch INTEGER NOT NULL, snapshot_hash TEXT NOT NULL, from_cursor INTEGER NOT NULL, '
                'to_cursor INTEGER NOT NULL, count INTEGER NOT NULL, projection_hash TEXT NOT NULL, committed_at TEXT NOT NULL)')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'


def _legacy_state(db, tables):
    # Hash committed logical rows without loading the
    # full database into memory or opening a writable SQLite connection.
    digest = hashlib.sha256()
    for table in sorted(set(HISTORY_TABLES + PERSONA_TABLES + ('topic_library',)) & tables):
        digest.update(table.encode())
        for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                default=lambda value: {'blob': value.hex()}).encode())
            digest.update(b'\n')
    return digest.hexdigest()


def _database_state(path, catalog=True):
    if not path.is_file():
        return 'missing'
    if path.resolve() != path or path.stat().st_nlink != 1:
        from persona_store import ScopeMismatch
        raise ScopeMismatch('source database redirects or shares a hardlink')
    with path.open('rb') as stream:
        header = stream.read(20)
    with _registry_lock:
        entry = _locks.get(str(path.parent))
        own_lock = entry is not None and getattr(entry[1], 'depth', 0) > 0
    if header[18:20] != b'\x01\x01' or any(Path(str(path) + suffix).exists()
            for suffix in ('-wal', '-shm')) or (not own_lock and Path(str(path) + '-journal').exists()):
        raise _conflict('源资料数据库正在写入或日志模式不兼容，请稍后重新读取资料')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=0)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'identity': _legacy_state(db, tables) if 'source_state' not in tables else
                  tuple(db.execute('SELECT * FROM source_state WHERE singleton=1').fetchone())}
        if 'metadata' in tables:
            result['memory'] = db.execute("SELECT key,value FROM metadata WHERE key IN "
                "('qq_id','memory_revision','activity_memory_reset_seq','daily_research') ORDER BY key").fetchall()
        if 'source_operations' in tables:
            pending = db.execute("SELECT id,kind FROM source_operations WHERE state='PENDING'").fetchall()
            if pending:
                raise _conflict('source operation pending; reconcile before reading')
        if 'source_import_tombstones' in tables:
            result['tombstones'] = db.execute('SELECT * FROM source_import_tombstones ORDER BY friend_id,import_id').fetchall()
        if catalog and 'scope_state' in tables:
            result['catalog'] = db.execute('SELECT * FROM scope_state').fetchall()
        if catalog and 'embedding_index_metadata' in tables:
            result['index'] = db.execute('SELECT friend_id,generation,revision,model,dimension,version '
                "FROM embedding_index_metadata WHERE state='ACTIVE' ORDER BY friend_id").fetchall()
        result['nonce'] = _file_hash(path.with_name('.' + path.name + '.source-nonce'))
        return result


def snapshot(scope, *, catalog=True):
    directory = scope.friend_dir if hasattr(scope, 'friend_dir') else Path(scope)
    directory = Path(directory).resolve()
    return _digest({'directory': str(directory), 'history': _database_state(directory / 'history.sqlite3'),
        'persona': _database_state(directory / 'persona.sqlite3', catalog),
        'profile': _file_hash(directory / 'friend.json'),
        'profile_revision': _file_hash(directory / '.profile.source-nonce'),
        'persona_pointer': _file_hash(directory / 'persona_versions' / 'active.json')})


def require_snapshot(scope, expected, *, catalog=True):
    if not expected:
        raise _conflict('旧记录缺少源资料校验信息，需要重新读取后再操作')
    if snapshot(scope, catalog=catalog) != expected:
        raise _conflict('源资料在处理期间发生变化，旧结果已拒绝，请按最新资料继续')


def require_sources(scope, refs, *, memory=False):
    """Validate all refs, including locally held PUBLIC_DATASET imports.

PUBLIC_PACK refs belong to immutable Pack validation, never friend history.
Manual refs retain their existing module-specific validation.
"""
    directory = scope.friend_dir
    for ref in refs:
        value = ref if isinstance(ref, Mapping) else {field.name: getattr(ref, field.name) for field in fields(ref)}
        if value.get('data_scope') == 'PUBLIC_PACK':
            continue
        if value.get('friend_id') not in (None, scope.friend_id):
            from persona_store import ScopeMismatch
            raise ScopeMismatch('source belongs to another friend')
        import_id = value.get('import_id')
        path = directory / 'persona.sqlite3'
        if import_id and path.is_file():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'source_import_tombstones' in tables and db.execute(
                        'SELECT 1 FROM source_import_tombstones WHERE friend_id=? AND import_id=?',
                        (scope.friend_id, import_id)).fetchone():
                    raise _conflict('Import source revoked')
        seq = value.get('message_seq')
        memory_source = str(value.get('dataset_id', '')).startswith(('legacy:memory', 'manual:memory'))
        if memory_source:
            path = directory / 'history.sqlite3'
            if not path.is_file():
                raise _conflict('memory source absent')
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                identifier = str(value.get('record_id', '')).removeprefix('memory_entries.')
                if not db.execute('SELECT 1 FROM memory_entries WHERE id=? AND active=1',
                                  (identifier,)).fetchone():
                    raise _conflict('memory source deleted or reset')
        if seq is not None and (memory_source or not all(
                value.get(key) for key in ('dataset_id', 'import_id', 'record_id'))):
            path = directory / 'history.sqlite3'
            if not path.is_file():
                raise _conflict('history source absent')
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('SELECT 1 FROM messages WHERE seq=?', (seq,)).fetchone() is None:
                    raise _conflict('history source deleted')
                if memory:
                    row = db.execute("SELECT value FROM metadata WHERE key='activity_memory_reset_seq'").fetchone()
                    if row and seq <= int(row[0]):
                        raise _conflict('memory source precedes reset floor')


def _refs(value):
    from persona_types import SourceRef
    if isinstance(value, SourceRef):
        yield value
    elif is_dataclass(value):
        for field in fields(value):
            yield from _refs(getattr(value, field.name))
    elif isinstance(value, Mapping):
        if {'data_scope', 'speaker_id', 'privacy_view', 'recorded_at'} <= value.keys():
            yield value
        else:
            for key, item in value.items():
                if key in ('source_refs_json', 'source_json') and isinstance(item, str):
                    item = json.loads(item)
                if key == 'source' and isinstance(item, str) and item.startswith('{'):
                    item = json.loads(item)
                yield from _refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _refs(item)


def source_read(method=None, *, locked=False):
    """Materialize generators before returning. Only bounded local context reads opt into a short lock;
    analysis, model calls and long retrievals retain optimistic validation without holding a writer lock."""
    if method is None:
        return lambda function: source_read(function, locked=locked)
    @wraps(method)
    def guarded(self, *args, **kwargs):
        scope = getattr(self, 'scope', None)
        if scope is None:
            if isinstance(self, (str, Path)):
                directory = Path(self).resolve()
            elif isinstance(self, sqlite3.Connection):
                name = self.execute('PRAGMA database_list').fetchone()[2]
                if not name:
                    return method(self, *args, **kwargs)
                directory = Path(name).parent
            else:
                directory = self.path.parent
            if getattr(getattr(self, 'db', None), 'in_transaction', False):
                require_available(self.path.parent)
                return method(self, *args, **kwargs)
            scope = SimpleNamespace(friend_dir=directory, friend_id=directory.name)
        request = args[0] if args else None
        public = getattr(getattr(request, 'filters', None), 'privacy_scope', None) == 'PUBLIC_PACK'
        if public:
            return method(self, *args, **kwargs)
        def read():
            before = snapshot(scope)
            value = method(self, *args, **kwargs)
            if isinstance(value, Iterator):
                materialized = tuple(value)
                value = iter(materialized)
            else:
                materialized = value
            require_sources(scope, tuple(_refs(materialized)))
            require_snapshot(scope, before)
            return value
        if locked:
            with source_lock(scope.friend_dir):
                return read()
        return read()
    return guarded


@contextmanager
def profile_write(path):
    path = Path(path)
    if path.name != 'friend.json':
        yield
        return
    with source_lock(path.parent):
        require_available(path.parent)
        try:
            yield
        finally:
            _rotate(path.parent / '.profile.source-nonce')
