"""Background adapter for local, bounded classification of stored messages."""
from collections import deque
from contextlib import closing
from datetime import datetime
import atexit
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time


MAX_BATCH = 16
# 2026-10-10 user: off. Every stored message woke the local 9B (and unsure ones a Codex review), but nothing in the
# chat read the labels. True turns it back on.
ENABLED = False
_LABELS = {
    'intent': {'share', 'inquire', 'banter', 'comfort', 'complain', 'refuse', 'confirm', 'respond', 'other', '分享', '询问', '调侃', '安慰', '抱怨', '拒绝', '确认', '回应', '其他'},
    'emotion': {'happy', 'funny', 'surprised', 'angry', 'sad', 'annoyed', 'calm', 'other', '开心', '好笑', '惊讶', '生气', '难过', '烦躁', '平静', '其他'},
    'tone': {'casual', 'serious', 'cold', 'intimate', 'perfunctory', '随意', '认真', '冷淡', '亲密', '敷衍'},
    'initiative': {'proactive', 'passive', '主动', '被动'},
}
_LOG = logging.getLogger(__name__)
_REGISTRY_LOCK = threading.Lock()
_WORKERS = {}
_REVIEW_REASONS = {'insufficient_context', 'multiple_interpretations', 'tone_ambiguity', 'unclear_reference', 'very_short_message', 'uncertain', 'clear', '上下文不足', '多种解释', '语气歧义', '指代不明', '极短消息', '不确定', '明确'}


def _root(project_root=None):
    return Path(project_root or os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent)).resolve()


def _runtime_path(project_root):
    return Path(project_root) / 'build' / 'message-classifier-runtime' / 'runtime.json'


def _get_classifier(project_root):
    from message_classifier import get_classifier
    return get_classifier(project_root)


def _valid_result(value):
    return isinstance(value, dict) and set(value) == set(_LABELS) and all(
        isinstance(value[key], str) and value[key] in allowed for key, allowed in _LABELS.items())


class MessageClassificationQueue:
    def __init__(self, project_root, *, batch_size=MAX_BATCH, gather_seconds=0.02):
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
            raise ValueError('batch_size must be between 1 and 16')
        self.project_root = _root(project_root)
        self.batch_size = batch_size
        self.gather_seconds = gather_seconds
        self._queue = queue.Queue(maxsize=2048)
        self._pending = 0
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._classifier = None
        self._backlog = deque()
        self._thread = threading.Thread(target=self._run, name='message-classifier', daemon=True)
        self._thread.start()

    def enqueue(self, database_path, seq, *, refresh=False):
        from house_settings import local_models_enabled
        if local_models_enabled(self.project_root) and not _runtime_path(self.project_root).is_file():
            return False
        item = (Path(database_path).resolve(), int(seq), bool(refresh))
        with self._condition:
            self._pending += 1
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()
            _LOG.warning('message classification queue full; item deferred')
            return False
        return True

    def flush(self, timeout=0.25):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout=0.25):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(max(0.0, timeout))
        if not self._thread.is_alive() and self._classifier is not None:
            try:
                self._classifier.close()
            except Exception as exc:
                _LOG.warning('message classifier close failed: %s', type(exc).__name__)
        return not self._thread.is_alive()

    def _next(self, timeout=None):
        if self._backlog:
            return self._backlog.popleft()
        return self._queue.get(timeout=timeout)

    def _batch(self, first):
        path = first[0]
        batch = [first]
        deadline = time.monotonic() + self.gather_seconds
        while len(batch) < self.batch_size and not self._stop.is_set():
            candidate = None
            for index, item in enumerate(self._backlog):
                if item is None:
                    continue
                if item[0] == path:
                    candidate = item
                    del self._backlog[index]
                    break
            if candidate is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    self._backlog.append(None)
                    break
                if item[0] == path:
                    candidate = item
                else:
                    self._backlog.append(item)
                    continue
            batch.append(candidate)
        return batch

    def _run(self):
        while True:
            try:
                first = self._next()
            except queue.Empty:
                continue
            if first is None:
                self._queue.task_done()
                break
            batch = self._batch(first)
            try:
                self._process(batch)
            except Exception as exc:
                _LOG.warning('message classification batch failed: %s', type(exc).__name__)
            finally:
                for _ in batch:
                    self._queue.task_done()
                    with self._condition:
                        self._pending -= 1
                        self._condition.notify_all()
            if self._stop.is_set() and self._queue.empty() and not self._backlog:
                break

    def _process(self, items):
        merged = {}
        for path, seq, refresh in items:
            key = (path, seq)
            merged[key] = merged.get(key, False) or refresh
        items = [(path, seq, refresh) for (path, seq), refresh in merged.items()]
        by_path = items[0][0]
        loaded = []
        with closing(sqlite3.connect(by_path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            for _, seq, refresh in items:
                row = db.execute('SELECT seq,body,direction FROM messages WHERE seq=?', (seq,)).fetchone()
                tagged = db.execute('SELECT 1 FROM metadata WHERE key=?',
                    (f'message_classification:{seq}',)).fetchone()
                if row is None or (tagged and not refresh):
                    continue
                review_row = db.execute('SELECT value FROM metadata WHERE key=?',
                    (f'classification_review:{seq}',)).fetchone()
                try:
                    if review_row and json.loads(review_row['value']).get('manual_locked'):
                        continue
                except (TypeError, ValueError):
                    pass
                previous = db.execute('SELECT seq,body,direction FROM messages WHERE seq<? ORDER BY seq DESC LIMIT 3',
                                      (seq,)).fetchall()
                following = db.execute('SELECT seq,body,direction FROM messages WHERE seq>? '
                    'ORDER BY seq LIMIT 1', (seq,)).fetchone()
                loaded.append((seq, row['body'], row['direction'], tuple(
                    {'text': item['body'], 'direction': item['direction'], 'seq': item['seq']}
                    for item in reversed(previous)),
                    ({'text': following['body'], 'direction': following['direction'],
                      'seq': following['seq']} if following else None), refresh))
        if not loaded:
            return
        started = time.perf_counter()
        try:
            if self._classifier is None:
                self._classifier = _get_classifier(self.project_root)
            results = self._classifier.classify_batch_with_review(
                [item[1] for item in loaded], directions=[item[2] for item in loaded],
                contexts=[item[3] for item in loaded],
                following_contexts=[(item[4],) if item[4] else () for item in loaded])
            if not isinstance(results, list) or len(results) != len(loaded) or any(
                    not isinstance(value, dict) or set(value) != {'labels', 'needs_review', 'reason'} or
                    not _valid_result(value['labels']) or type(value['needs_review']) is not bool or
                    value['reason'] not in _REVIEW_REASONS for value in results):
                raise ValueError('classifier returned an invalid result batch')
        except Exception as exc:
            _LOG.warning('message classification failed: %s', type(exc).__name__)
            return

        with closing(sqlite3.connect(by_path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                review_events = []
                review_changed = False
                for (seq, body, direction, context, following, refresh), detail in zip(loaded, results):
                    result = detail['labels']
                    current = db.execute('SELECT seq,body,direction FROM messages WHERE seq=?',
                                         (seq,)).fetchone()
                    if current is None or (current['body'], current['direction']) != (body, direction):
                        continue
                    current_following = db.execute('SELECT seq,body,direction FROM messages WHERE seq>? '
                        'ORDER BY seq LIMIT 1', (seq,)).fetchone()
                    current_following = ({'text': current_following['body'],
                        'direction': current_following['direction'], 'seq': current_following['seq']}
                        if current_following else None)
                    if current_following != following:
                        continue
                    review_key = f'classification_review:{seq}'
                    review_row = db.execute('SELECT value FROM metadata WHERE key=?', (review_key,)).fetchone()
                    try:
                        old_review = json.loads(review_row['value']) if review_row else {}
                    except (TypeError, ValueError):
                        old_review = {}
                    if old_review.get('manual_locked'):
                        continue
                    from classification_review import input_hash, _snapshot, _snapshot_matches
                    if not _snapshot_matches(db, seq, _snapshot(seq, body, direction, context, following)):
                        continue
                    digest = input_hash(body, direction, context, following)
                    terminal_same = old_review.get('input_hash') == digest and old_review.get('status') in (
                        'RESOLVED', 'HUMAN_REVIEW', 'MANUAL', 'REVIEWING')
                    if detail['needs_review']:
                        if old_review.get('input_hash') != digest:
                            review = {'input_hash': digest, 'snapshot': _snapshot(seq, body, direction, context, following),
                                'qwen_labels': result, 'qwen_reason': detail['reason'], 'qwen_self_assessment': 'uncalibrated',
                                'status': 'PENDING', 'attempt': 0, 'luna_result': None}
                        else:
                            review = old_review
                        if not terminal_same:
                            db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) '
                                'DO UPDATE SET value=excluded.value',
                                (review_key, json.dumps(review, ensure_ascii=False, separators=(',', ':'))))
                            review_changed = True
                        if not terminal_same and review.get('attempt', 0) == 0 and review.get('status') == 'PENDING':
                            review_events.append({'seq': seq, 'input_hash': digest, 'snapshot': review['snapshot'],
                                                  'labels': result, 'reason': detail['reason']})
                    elif old_review and old_review.get('input_hash') != digest and old_review.get('status') != 'MANUAL':
                        # A refreshed, now-clear local classification invalidates the previous review.
                        db.execute('DELETE FROM metadata WHERE key=?', (review_key,))
                        review_changed = True
                    preserve_resolved = bool(terminal_same)
                    key = f'message_classification:{seq}'
                    exists = db.execute('SELECT 1 FROM metadata WHERE key=?', (key,)).fetchone()
                    if not preserve_resolved and not (exists and not refresh):
                        value = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
                        db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) '
                            'DO UPDATE SET value=excluded.value', (key, value))
                if review_events:
                    from classification_review import enqueue_review
                    # Queue only after commit below; collect it outside this transaction.
                    pending_reviews = review_events
                else:
                    pending_reviews = []
        if review_changed:
            from classification_review import _write_friend_doc
            _write_friend_doc(by_path)
        if pending_reviews:
            from classification_review import enqueue_review
            enqueue_review(by_path, pending_reviews, self.project_root)
        _LOG.info('message classification batch count=%s elapsed_ms=%.1f', len(loaded),
                  (time.perf_counter() - started) * 1000)


def _worker(project_root=None, create=False):
    root = _root(project_root)
    with _REGISTRY_LOCK:
        worker = _WORKERS.get(root)
        if worker is None or not worker._thread.is_alive():
            if not create:
                return None
            worker = MessageClassificationQueue(root)
            _WORKERS[root] = worker
        return worker


def enqueue_for_database(database_path, seq, project_root=None, *, refresh=False):
    root = _root(project_root)
    if not ENABLED or not _runtime_path(root).is_file():
        return False
    return _worker(root, create=True).enqueue(database_path, seq, refresh=refresh)


def flush_for_database(database_path, timeout=0.25, project_root=None):
    worker = _worker(project_root)
    return True if worker is None else worker.flush(timeout)


def close_queue(project_root=None, timeout=0.25):
    root = _root(project_root)
    worker = _worker(root)
    if worker is None:
        return True
    stopped = worker.close(timeout)
    if stopped:
        with _REGISTRY_LOCK:
            if _WORKERS.get(root) is worker:
                del _WORKERS[root]
    return stopped


def backfill_database(database_path, *, after_seq=None, limit=32, project_root=None):
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError('limit must be between 1 and 256')
    root = _root(project_root)
    if not _runtime_path(root).is_file():
        return {'cursor': after_seq or 0, 'queued': 0, 'scanned': 0, 'enabled': False}
    db_path = Path(database_path).resolve()
    with closing(sqlite3.connect(db_path, timeout=5)) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='message_classification_backfill_seq'").fetchone()
        saved_cursor = int(row[0]) if row else 0
        cursor = max(0, int(after_seq)) if after_seq is not None else 0
        rows = db.execute('SELECT seq FROM messages WHERE seq>? AND NOT EXISTS '
            "(SELECT 1 FROM metadata x WHERE x.key='message_classification:'||messages.seq) "
            'ORDER BY seq LIMIT ?', (cursor, limit)).fetchall()
    if not rows:
        return {'cursor': max(saved_cursor, cursor), 'queued': 0, 'scanned': 0, 'enabled': True}
    worker = _worker(root, create=True)
    last, queued = cursor, 0
    for (seq,) in rows:
        if not worker.enqueue(db_path, seq):
            break
        queued += 1
        last = seq
    if last != cursor:
        with closing(sqlite3.connect(db_path, timeout=5)) as db:
            with db:
                db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) '
                    'DO UPDATE SET value=excluded.value',
                    ('message_classification_backfill_seq', str(last)))
    return {'cursor': last, 'queued': queued, 'scanned': last - cursor, 'enabled': True}


def _shutdown_all():
    with _REGISTRY_LOCK:
        workers = tuple(_WORKERS.items())
    for root, worker in workers:
        if worker.close(0.5):
            with _REGISTRY_LOCK:
                if _WORKERS.get(root) is worker:
                    del _WORKERS[root]


atexit.register(_shutdown_all)
