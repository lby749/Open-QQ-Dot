"""Create a resumable local BGE-M3 index from Qwen rows that need no review."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from embedding_provider import DIMENSION, METADATA, EmbeddingProvider
from persona_types import DataScope, FriendScope, PrivacyView, SourceOrigin, SourceRef

SOURCE_DB = PROJECT_ROOT / 'data' / 'datasets' / 'annotations.sqlite3'
OUTPUT_DB = PROJECT_ROOT / 'data' / 'datasets' / 'bge-m3-clean-vectors.sqlite3'
COMPLETE = SOURCE_DB.with_name('COMPLETE.json')
REQUEST_SIZE = 64
SELECT = """SELECT data_source,split,source_file,source_record_id,conversation_id,
speaker_id,message_seq,text,annotation_id FROM annotations
WHERE run_id=? AND intent IS NOT NULL AND needs_review=0 AND pipeline_needs_review=0
ORDER BY data_source,split,conversation_id,message_seq,annotation_id"""


@contextmanager
def _exclusive_lock(path: Path):
    lock = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise RuntimeError(f'an embedding run already owns {path}') from exc
    finally:
        lock.close()


def _init_db(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS runs(
        run_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL, model TEXT NOT NULL,
        dimension INTEGER NOT NULL, version TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS vectors(
        run_id TEXT NOT NULL, annotation_id TEXT NOT NULL,
        data_source TEXT NOT NULL, split TEXT NOT NULL,
        source_file TEXT NOT NULL, source_record_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, speaker_id TEXT NOT NULL,
        message_seq INTEGER NOT NULL, text_hash TEXT NOT NULL,
        vector BLOB NOT NULL,
        PRIMARY KEY(run_id,annotation_id),
        FOREIGN KEY(run_id) REFERENCES runs(run_id));
      CREATE INDEX IF NOT EXISTS vectors_source
        ON vectors(run_id,data_source,split,conversation_id,message_seq);
    ''')
    db.commit()


def _fingerprint(source, source_run_id):
    digest = hashlib.sha256()
    digest.update(json.dumps({'source_run_id': source_run_id, 'model': METADATA.model,
        'dimension': METADATA.dimension, 'version': METADATA.version},
        sort_keys=True, separators=(',', ':')).encode())
    cursor = source.execute(SELECT, (source_run_id,))
    total = 0
    while rows := cursor.fetchmany(2048):
        for row in rows:
            digest.update(json.dumps(tuple(row), ensure_ascii=False,
                separators=(',', ':')).encode('utf-8'))
            digest.update(b'\n')
            total += 1
    return digest.hexdigest(), total


def _scope():
    registry = json.loads((PROJECT_ROOT / 'friends.json').read_text('utf-8'))
    # EmbeddingProvider currently requires a verified friend scope as a runtime
    # guard. SourceRefs below remain PUBLIC_DATASET and vectors are stored in a
    # separate public index; this scope does not own or reclassify the data.
    for friend_id in sorted(registry.get('friends', {})):
        try:
            return FriendScope.load(friend_id, project_root=PROJECT_ROOT)
        except Exception:
            continue
    raise RuntimeError('no verifiable local provider scope is available')


def _refs(rows):
    now = datetime.now(timezone.utc).isoformat()
    return [SourceRef(dataset_id=row['data_source'],
        import_id=f"qwen-run:{row['split']}:{row['source_file']}",
        record_id=row['source_record_id'], message_seq=None,
        speaker_id=row['speaker_id'] or 'unknown', origin=SourceOrigin.PUBLIC_DATASET,
        data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
        friend_id=None, event_time=None, recorded_at=now)
        for row in rows]


def _vector_bytes(vector):
    if len(vector) != DIMENSION or not all(math.isfinite(x) for x in vector):
        raise RuntimeError('BGE returned a malformed vector')
    norm_squared = sum(x*x for x in vector)
    if abs(norm_squared - 1.0) > 1e-4:
        raise RuntimeError('BGE returned a non-normalized vector')
    return struct.pack('<' + 'f' * DIMENSION, *vector)


def run():
    if not SOURCE_DB.is_file() or not COMPLETE.is_file():
        raise RuntimeError('completed Qwen source database or marker is missing')
    marker = json.loads(COMPLETE.read_text('utf-8-sig'))
    source_run_id = marker['run_id']
    OUTPUT_DB.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(OUTPUT_DB.with_suffix(OUTPUT_DB.suffix + '.lock')):
        with closing(sqlite3.connect(f'file:{SOURCE_DB.as_posix()}?mode=ro', uri=True,
                                     timeout=30)) as source, \
             closing(sqlite3.connect(OUTPUT_DB, timeout=30)) as target:
            source.row_factory = sqlite3.Row
            target.row_factory = sqlite3.Row
            source.execute('BEGIN')
            status = source.execute('SELECT status FROM runs WHERE run_id=?',
                                    (source_run_id,)).fetchone()
            if not status or status['status'] != 'complete':
                raise RuntimeError('Qwen source run is not complete')
            total_source, pending = source.execute(
                'SELECT COUNT(*),SUM(intent IS NULL) FROM annotations WHERE run_id=?',
                (source_run_id,)).fetchone()
            if total_source != marker['total'] or pending:
                raise RuntimeError('Qwen completion marker does not match its database')
            source_fingerprint, total = _fingerprint(source, source_run_id)
            target.execute('PRAGMA journal_mode=WAL')
            _init_db(target)
            run_id = hashlib.sha256((source_fingerprint + METADATA.version).encode()).hexdigest()
            existing = target.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if existing and (existing['source_fingerprint'] != source_fingerprint or
                    existing['source_run_id'] != source_run_id or
                    existing['model'] != METADATA.model or
                    existing['dimension'] != METADATA.dimension or
                    existing['version'] != METADATA.version):
                raise RuntimeError('embedding checkpoint metadata mismatch; refusing resume')
            target.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?)',
                (run_id, source_run_id, source_fingerprint, METADATA.model,
                 METADATA.dimension, METADATA.version, datetime.now(timezone.utc).isoformat(),
                 'running'))
            target.commit()
            done = target.execute('SELECT COUNT(*) FROM vectors WHERE run_id=?',
                                  (run_id,)).fetchone()[0]
            if done > total:
                raise RuntimeError('embedding checkpoint exceeds eligible source rows')
            provider = EmbeddingProvider(_scope())
            started = time.monotonic()
            processed_this_run = 0
            cursor = source.execute(SELECT, (source_run_id,))
            while rows := cursor.fetchmany(REQUEST_SIZE):
                missing = [row for row in rows if target.execute(
                    'SELECT 1 FROM vectors WHERE run_id=? AND annotation_id=?',
                    (run_id, row['annotation_id'])).fetchone() is None]
                if not missing:
                    continue
                refs = _refs(missing)
                result = provider.embed_texts([row['text'] for row in missing], source_refs=refs)
                if len(result.vectors) != len(missing) or result.version != METADATA.version:
                    raise RuntimeError('BGE response metadata/count mismatch')
                target.executemany('''INSERT OR IGNORE INTO vectors VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    [(run_id, row['annotation_id'], row['data_source'], row['split'],
                      row['source_file'], row['source_record_id'], row['conversation_id'],
                      row['speaker_id'], row['message_seq'],
                      hashlib.sha256(row['text'].encode('utf-8')).hexdigest(),
                      _vector_bytes(vector)) for row, vector in zip(missing, result.vectors)])
                target.commit()
                processed_this_run += len(missing)
                elapsed = max(0.001, time.monotonic() - started)
                indexed = target.execute('SELECT COUNT(*) FROM vectors WHERE run_id=?',
                                         (run_id,)).fetchone()[0]
                print(f'indexed={indexed}/{total} session={processed_this_run} '
                      f'rate={processed_this_run/elapsed:.1f}/s', flush=True)
            indexed = target.execute('SELECT COUNT(*) FROM vectors WHERE run_id=?',
                                     (run_id,)).fetchone()[0]
            if indexed != total:
                raise RuntimeError(f'final index count mismatch: {indexed}/{total}')
            target.execute("UPDATE runs SET status='complete' WHERE run_id=?", (run_id,))
            target.commit()
            print(json.dumps({'status': 'complete', 'run_id': run_id,
                'source_run_id': source_run_id, 'eligible': total, 'indexed': indexed,
                'database': str(OUTPUT_DB), 'model': METADATA.model,
                'dimension': METADATA.dimension, 'version': METADATA.version,
                'worker': provider.stats()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        print(f'embed_clean: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
