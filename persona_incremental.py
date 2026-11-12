"""Deterministic source receipts, precise local Import revocation, and recovery."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from persona_store import Conflict, PersonaStore, ScopeMismatch
from source_state import snapshot, require_snapshot, source_lock, source_recovery, _refs


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class IncrementalBatch:
    friend_id: str
    processing_version: str
    source_epoch: int
    source_token: str
    from_cursor: int
    to_cursor: int
    projections: tuple[dict, ...]


class IncrementalUpdate:
    """Uses history storage; receipt/cursor/projection commit in one transaction."""
    def __init__(self, scope, store, processing_version='UPDATE-001-v1'):
        if store.path.parent.resolve() != scope.friend_dir:
            raise ScopeMismatch('incremental history must belong to the verified friend')
        if not isinstance(processing_version, str) or not processing_version.strip():
            raise ValueError('processing version required')
        self.scope, self.store, self.version = scope, store, processing_version

    def prepare(self, limit=256):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError('incremental limit must be 1..256')
        token = snapshot(self.scope)
        db = self.store.db
        epoch = db.execute('SELECT clear_epoch FROM source_state WHERE singleton=1').fetchone()[0]
        row = db.execute('SELECT cursor FROM incremental_cursors WHERE friend_id=? AND processing_version=? '
                         'AND source_epoch=?', (self.scope.friend_id, self.version, epoch)).fetchone()
        cursor = row[0] if row else 0
        rows = db.execute('SELECT seq,qq_id,direction,body,created_at FROM messages WHERE seq>? '
                          'ORDER BY seq LIMIT ?', (cursor, limit)).fetchall()
        projections = tuple(dict(friend_id=self.scope.friend_id, processing_version=self.version,
            source_epoch=epoch, message_seq=row['seq'], source_hash=_hash(dict(row)),
            source_kind='HISTORY_MESSAGE', direction=row['direction']) for row in rows)
        require_snapshot(self.scope, token)
        return IncrementalBatch(self.scope.friend_id, self.version, epoch, token, cursor,
                                rows[-1]['seq'] if rows else cursor, projections)

    def commit(self, batch):
        if (not isinstance(batch, IncrementalBatch) or batch.friend_id != self.scope.friend_id or
                batch.processing_version != self.version):
            raise ScopeMismatch('incremental batch belongs to another scope/version')
        receipt_id = _hash([batch.friend_id, batch.processing_version, batch.source_epoch,
                            batch.from_cursor, batch.to_cursor, batch.source_token, batch.projections])
        db = self.store.db
        with source_lock(self.scope.friend_dir), db:
            db.execute('BEGIN IMMEDIATE')
            require_snapshot(self.scope, batch.source_token)
            old = db.execute('SELECT * FROM incremental_receipts WHERE id=?', (receipt_id,)).fetchone()
            if old:
                return dict(old)
            epoch = db.execute('SELECT clear_epoch FROM source_state WHERE singleton=1').fetchone()[0]
            row = db.execute('SELECT cursor FROM incremental_cursors WHERE friend_id=? AND processing_version=? '
                'AND source_epoch=?', (self.scope.friend_id, self.version, epoch)).fetchone()
            if epoch != batch.source_epoch or (row[0] if row else 0) != batch.from_cursor:
                raise Conflict('incremental cursor changed')
            # A batch is task-owned, not a caller-supplied projection authoring API.
            current = self.prepare(max(1, len(batch.projections)))
            if current != batch:
                raise Conflict('incremental batch differs from current source')
            if not batch.projections:
                return None
            db.executemany('INSERT INTO incremental_projections VALUES(?,?,?,?,?,?,?)',
                [tuple(row[key] for key in ('friend_id', 'processing_version', 'source_epoch',
                    'message_seq', 'source_hash', 'source_kind', 'direction')) for row in batch.projections])
            db.execute('INSERT INTO incremental_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                (receipt_id, batch.friend_id, batch.processing_version, epoch, batch.source_token,
                 batch.from_cursor, batch.to_cursor, len(batch.projections), _hash(batch.projections), _now()))
            db.execute('INSERT INTO incremental_cursors VALUES(?,?,?,?) ON CONFLICT '
                '(friend_id,processing_version,source_epoch) DO UPDATE SET cursor=excluded.cursor',
                (batch.friend_id, batch.processing_version, epoch, batch.to_cursor))
            return dict(db.execute('SELECT * FROM incremental_receipts WHERE id=?', (receipt_id,)).fetchone())

    def process(self, limit=256):
        return self.commit(self.prepare(limit))


def run_history_operation(store, kind, apply):
    """Intent survives failures between the two independent database commits."""
    if kind not in ('CLEAR_MESSAGES', 'RESET_MEMORY'):
        raise ValueError('unsupported source operation')
    with source_lock(store.path.parent), source_recovery(store.path.parent):
        reconcile(store)
        operation_id = uuid.uuid4().hex
        with store.db:
            store.db.execute('INSERT INTO source_operations VALUES(?,?,?,NULL)',
                             (operation_id, kind, 'PENDING'))
        result = apply(operation_id)
        reconcile(store)
        return result


def _invalidated(refs, kind, import_id=None):
    for ref in refs:
        value = ref if isinstance(ref, dict) else vars(ref)
        if value.get('data_scope') == 'PUBLIC_PACK':
            continue
        if import_id is not None and value.get('import_id') == import_id:
            return True
        if kind == 'CLEAR_MESSAGES' and value.get('message_seq') is not None and not all(
                value.get(key) for key in ('dataset_id', 'import_id', 'record_id')):
            return True
        if kind == 'CLEAR_MESSAGES' and value.get('message_seq') is not None and str(
                value.get('dataset_id', '')).startswith('legacy:memory'):
            return True
        if kind == 'RESET_MEMORY' and str(value.get('dataset_id', '')).startswith(
                ('legacy:memory', 'manual:memory')):
            return True
    return False


def _invalidate_sidecar(directory, kind, import_id=None):
    path = directory / 'persona.sqlite3'
    if not path.is_file():
        return
    from source_state import SourceConnection
    with closing(sqlite3.connect(path, isolation_level=None, factory=SourceConnection)) as db:
        db.bind_source(path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            before_changes = db.total_changes
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'scope_state' in tables and [row[0] for row in db.execute(
                    'SELECT friend_id FROM scope_state')] != [directory.name]:
                raise ScopeMismatch('cleanup sidecar belongs to another friend')
            targets = {'interest_entries': ('source_refs_json', 'valid'),
                'topic_knowledge': ('source_refs_json', 'valid'),
                'knowledge_entities': ('source_refs_json', 'valid'),
                'knowledge_relations': ('source_refs_json', 'valid'),
                'semantic_documents': ('source_refs_json', 'valid'),
                'sticker_assets': ('source', 'valid'), 'persona_claims': ('claim_json', 'state')}
            for table, (column, status) in targets.items():
                if table not in tables:
                    continue
                for row in db.execute(f'SELECT rowid,* FROM {table} WHERE {status}=?',
                                      ('VALIDATED' if status == 'state' else 1,)).fetchall():
                    refs = tuple(_refs(json.loads(row[column])))
                    memory_projection = (kind == 'RESET_MEMORY' and table == 'semantic_documents'
                                         and row['namespace'] == 'memory')
                    if memory_projection or _invalidated(refs, kind, import_id):
                        db.execute(f'UPDATE {table} SET {status}=? WHERE rowid=?',
                                   ('INVALID' if status == 'state' else 0, row['rowid']))
            if 'persona_analysis_runs' in tables:
                for row in db.execute('SELECT id,result_json FROM persona_analysis_runs '
                        "WHERE state IN ('DRAFT','COMPLETED')").fetchall():
                    if _invalidated(tuple(_refs(json.loads(row['result_json']))), kind, import_id):
                        db.execute("UPDATE persona_analysis_runs SET state='STALE' WHERE id=?", (row['id'],))
                        db.execute("UPDATE persona_claims SET state='INVALID' WHERE run_id=?", (row['id'],))
            if 'artifact_versions' in tables:
                for row in db.execute("SELECT id,manifest_relpath FROM artifact_versions WHERE state!='INVALID'").fetchall():
                    manifest_path = directory / row['manifest_relpath']
                    if not manifest_path.resolve().is_relative_to(directory.resolve()):
                        raise ScopeMismatch('artifact manifest escaped friend directory')
                    if manifest_path.is_file() and _invalidated(tuple(_refs(json.loads(
                            manifest_path.read_text('utf-8')))), kind, import_id):
                        db.execute("UPDATE artifact_versions SET state='INVALID' WHERE id=?", (row['id'],))
            if 'dialogue_turns' in tables:
                affected = {row['session_id'] for row in db.execute('SELECT session_id,bubbles_json FROM dialogue_turns')
                    if _invalidated(tuple(_refs(json.loads(row['bubbles_json']))), kind, import_id)}
                for session_id in affected:
                    if 'sticker_examples' in tables:
                        db.execute('DELETE FROM sticker_examples WHERE session_id=?', (session_id,))
                    db.execute('DELETE FROM dialogue_pairs WHERE session_id=?', (session_id,))
                    db.execute('DELETE FROM dialogue_turns WHERE session_id=?', (session_id,))
                    db.execute('DELETE FROM dialogue_sessions WHERE id=?', (session_id,))
            if 'sticker_examples' in tables:
                db.execute('DELETE FROM sticker_examples WHERE asset_id IN '
                           '(SELECT asset_id FROM sticker_assets WHERE valid=0)')
            if 'knowledge_relations' in tables:
                db.execute('UPDATE knowledge_relations SET valid=0 WHERE valid=1 AND (source_entity IN '
                    '(SELECT id FROM knowledge_entities WHERE valid=0) OR target_entity IN '
                    '(SELECT id FROM knowledge_entities WHERE valid=0))')
            if 'analysis_jobs' in tables:
                for row in db.execute("SELECT id,source_refs_json FROM analysis_jobs WHERE state IN ('queued','running','paused')").fetchall():
                    if _invalidated(json.loads(row['source_refs_json']), kind, import_id):
                        db.execute("UPDATE analysis_jobs SET state='failed',error_code='STALE_SOURCE',version=version+1 WHERE id=?",
                                   (row['id'],))
            if 'scope_state' in tables and db.total_changes > before_changes:
                db.execute('UPDATE scope_state SET revision=revision+1')
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise


def reconcile(store):
    with source_lock(store.path.parent), source_recovery(store.path.parent):
        for operation in store.db.execute("SELECT * FROM source_operations WHERE state='PENDING' ORDER BY rowid").fetchall():
            if operation['result'] is None:
                # No history mutation committed yet. Reapply the same intent.
                if operation['kind'] == 'CLEAR_MESSAGES':
                    store._clear_messages(operation['id'])
                else:
                    from memory_management import _reset
                    _reset(store, operation['id'])
            _invalidate_sidecar(store.path.parent, operation['kind'])
            with store.db:
                store.db.execute("UPDATE source_operations SET state='COMPLETED' WHERE id=?", (operation['id'],))


def revoke_import(scope, import_id):
    if not isinstance(import_id, str) or not import_id:
        raise ValueError('exact Import id required')
    with source_lock(scope.friend_dir):
        store = PersonaStore(scope.friend_id, scope.friend_dir, project_root=scope.project_root)
        try:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('SELECT data_scope FROM dataset_imports WHERE friend_id=? AND import_id=?',
                                   (scope.friend_id, import_id)).fetchone()
            if row is None:
                raise ScopeMismatch('Import not present in this friend sidecar')
            changed = store.db.execute('INSERT OR IGNORE INTO source_import_tombstones VALUES(?,?,?)',
                (scope.friend_id, import_id, _now())).rowcount == 1
            if changed:
                store.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (scope.friend_id,))
            store.db.execute('COMMIT')
            # Tombstone already denies reads even if materialized cleanup is interrupted.
            _invalidate_sidecar(scope.friend_dir, 'REVOKE_IMPORT', import_id)
            return changed
        except Exception:
            if store.db.in_transaction:
                store.db.execute('ROLLBACK')
            raise
        finally:
            store.close()
