"""Review a completed public-dataset run once, retaining the original local labels."""
from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from classification_review import _PROFILE, _SCHEMA, _prompt, _redact, _valid_reply
from message_classification import _valid_result
from dataset_annotation.offline_feed import _exclusive_lock
from dataset_annotation.quota_guard import QuotaGuard

FIELDS = ('intent', 'emotion', 'tone', 'initiative')


def _payload(row):
    def turn(value):
        return {key: value.get(key) for key in ('text', 'speaker', 'seq', 'direction')}
    return {'current': {'text': row['text'], 'speaker': row['speaker_id'], 'direction': row['direction']},
            'direction_policy': row['direction_policy'],
            'context': [turn(x) for x in json.loads(row['context_json'])],
            'following': [turn(x) for x in json.loads(row['following_json'])],
            'local_labels': {key: row[key] for key in FIELDS},
            'uncertainty_reason': row['reason'], 'pipeline_needs_review': bool(row['pipeline_needs_review'])}


def _hash(row):
    return hashlib.sha256(json.dumps(_payload(row), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _json_block(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    longest = current = 0
    for char in text:
        current = current + 1 if char == '`' else 0
        longest = max(longest, current)
    fence = '`' * max(3, longest + 1)
    return [f'{fence}json', text, fence]


def _paused(quota_path):
    try:
        state = json.loads(Path(quota_path).read_text('utf-8'))
    except (OSError, ValueError):
        return False
    return str(state.get('status', '')).startswith('PAUSED')


def _model(prompt, directory):
    import main
    profile = {**_PROFILE, 'model_api': {**_PROFILE['model_api'], 'codex_effort': 'low'}}
    output, _ = main.codex_text(prompt, directory, ephemeral=True, text_only=True,
                                profile=profile, output_schema=_SCHEMA)
    return json.loads(output)


def _document(db, run_id, output):
    lines = ['# Public Dataset Uncertain Review', '', f'Run: `{run_id}`', '', 'Original Qwen labels preserved; review labels saved separately.', '']
    rows = db.execute('SELECT * FROM annotation_reviews WHERE run_id=? ORDER BY annotation_id,input_hash', (run_id,))
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
        lines += [f"## {row['annotation_id']} · {row['status']}", '',
                  f"- Input fingerprint: `{row['input_hash']}`; attempt={row['attempt']}",
                  f"- Review labels: `{row['labels_json']}`", f"- Reason: {row['reason'] or ''}",
                  '- Full source: ', '```json', row['origins_json'], '```', '']
    lines[4:4] = [f'Status counts: {json.dumps(counts, ensure_ascii=False)}', '']
    (output / 'review_results.md').write_text('\n'.join(lines), encoding='utf-8')
    # Human rows are explicitly listed; complete records remain in review_results.
    human = ['# Pending Human Review', '', f'Run: `{run_id}`', '']
    for row in db.execute("SELECT * FROM annotation_reviews WHERE run_id=? AND status='HUMAN_REVIEW' "
                          'ORDER BY annotation_id', (run_id,)):
        source = json.loads(row['origins_json'])
        context = json.loads(source.get('context_json') or '[]')
        following = json.loads(source.get('following_json') or '[]')
        qwen_labels = {field: source.get(field) for field in FIELDS}
        human += [f"## {row['annotation_id']}", '',
            f"- Status: `{row['status']}`; attempt={row['attempt']}",
            f"- annotation_id: `{row['annotation_id']}`",
            f"- input_hash / submit_manual.expected_hash: `{row['input_hash']}`",
            f"- Qwen initial labels:", *_json_block(qwen_labels),
            f"- Qwen initial reason: {source.get('reason') or '(none)'}",
            f"- Luna review labels: {row['labels_json'] or '(none)'}",
            f"- Luna review reason: {row['reason'] or '(none)'}", '',
            '### Current Message', *_json_block({'speaker': source.get('speaker_id'),
                'direction': source.get('direction'), 'seq': source.get('message_seq'),
                'text': source.get('text')}), '',
            '### Previous 3 Turns (actual source context)', *_json_block(context), '',
            '### Following Context (when present in source)', *_json_block(following), '',
            '### Source Origin', *_json_block({key: source.get(key) for key in
                ('data_source', 'split', 'source_file', 'source_record_id',
                 'conversation_id', 'message_seq', 'speaker_id', 'direction_policy')}), '',
            '### Full Source Record (raw JSON)', *_json_block(row['origins_json']), '']
    (output / 'human_review.md').write_text('\n'.join(human), encoding='utf-8')
    return counts


def submit_manual(db_path, run_id, annotation_id, expected_hash, labels, *, output=None):
    if not _valid_result(labels):
        raise ValueError('invalid four-field labels')
    path = Path(db_path).resolve()
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM annotations WHERE run_id=? AND annotation_id=?',
                             (run_id, annotation_id)).fetchone()
            if row is None or _hash(row) != expected_hash:
                raise ValueError('review input changed')
            changed = db.execute("UPDATE annotation_reviews SET status='MANUAL',manual_locked=1,labels_json=?,reason='Manual confirmation' "
                                 'WHERE run_id=? AND annotation_id=? AND input_hash=?',
                                 (json.dumps(labels, ensure_ascii=False), run_id, annotation_id, expected_hash)).rowcount
            if not changed:
                raise ValueError('review record missing')
        target = Path(output or path.parent / 'luna-review' / run_id)
        target.mkdir(parents=True, exist_ok=True)
        _document(db, run_id, target)


def run(db_path, run_id, *, output=None, batch_size=256, model_call=None, quota_path=None,
        quota_checker=None, progress=print, clock=time.monotonic):
    if type(batch_size) is not int or not 1 <= batch_size <= 256:
        raise ValueError('batch_size must be between 1 and 256')
    path = Path(db_path).resolve()
    marker = json.loads(path.with_name('COMPLETE.json').read_text('utf-8'))
    if (not isinstance(marker, dict) or marker.get('run_id') != run_id
            or type(marker.get('total')) is not int or marker['total'] < 0
            or type(marker.get('processed')) is not int or marker['processed'] != marker['total']):
        raise ValueError('COMPLETE.json must identify this completed run')
    if marker.get('db') and Path(marker['db']).resolve() != path:
        raise ValueError('COMPLETE.json database mismatch')
    output = Path(output or path.parent / 'luna-review' / run_id).resolve()
    quota_path = quota_path or PROJECT_ROOT / 'docs' / 'quota_state.json'
    with _exclusive_lock(path.with_suffix('.review.lock')), closing(sqlite3.connect(path, timeout=30)) as db, \
            (QuotaGuard(PROJECT_ROOT) if quota_checker is None else nullcontext(None)) as guard:
        db.row_factory = sqlite3.Row
        complete = db.execute("SELECT 1 FROM runs WHERE run_id=? AND status='complete'", (run_id,)).fetchone()
        total, pending = db.execute('SELECT COUNT(*),SUM(intent IS NULL) FROM annotations WHERE run_id=?',
                                   (run_id,)).fetchone()
        if not complete or total != marker['total'] or pending:
            raise ValueError('run is not fully complete in annotations database')
        if db.execute("SELECT 1 FROM annotations WHERE run_id=? AND upper(data_source) NOT IN ('MPDD','DIALOGUE-MPDD','CPED') LIMIT 1",
                      (run_id,)).fetchone():
            raise ValueError('review accepts only the public MPDD/Dialogue-MPDD/CPED dataset sources')
        output.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(output / 'history.sqlite3')) as usage_db:
            tables = {x[0] for x in usage_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'usage_calls', 'sqlite_sequence'}:
                raise ValueError('output must have an independent usage-only history.sqlite3')
        db.execute('''CREATE TABLE IF NOT EXISTS annotation_reviews(
            run_id TEXT NOT NULL, annotation_id TEXT NOT NULL, input_hash TEXT NOT NULL,
            status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            labels_json TEXT, reason TEXT, manual_locked INTEGER NOT NULL DEFAULT 0,
            origins_json TEXT NOT NULL, PRIMARY KEY(run_id,annotation_id,input_hash))''')
        with db:
            db.execute("UPDATE annotation_reviews SET status='HUMAN_REVIEW',reason='Review process interrupted, retained used attempt' "
                       "WHERE run_id=? AND status='REVIEWING' AND manual_locked=0", (run_id,))
            for row in db.execute('SELECT * FROM annotations WHERE run_id=? AND '
                                  '(needs_review=1 OR pipeline_needs_review=1)', (run_id,)).fetchall():
                db.execute('INSERT OR IGNORE INTO annotation_reviews(run_id,annotation_id,input_hash,status,origins_json) '
                           "VALUES(?,?,?,'PENDING',?)", (run_id, row['annotation_id'], _hash(row),
                                                       json.dumps(dict(row), ensure_ascii=False)))
        stopped = False
        _document(db, run_id, output)
        last_document = clock()
        while True:
            if _paused(quota_path) or not (quota_checker or guard.allowed)():
                stopped = True
                break
            with db:
                db.execute('BEGIN IMMEDIATE')
                rows = db.execute("SELECT a.*,r.input_hash FROM annotations a JOIN annotation_reviews r "
                                  'ON a.run_id=r.run_id AND a.annotation_id=r.annotation_id '
                                  "WHERE r.run_id=? AND r.status='PENDING' AND r.attempt=0 AND r.manual_locked=0 "
                                  'AND NOT EXISTS (SELECT 1 FROM annotation_reviews locked WHERE '
                                  'locked.run_id=a.run_id AND locked.annotation_id=a.annotation_id AND locked.manual_locked=1) '
                                  'ORDER BY a.annotation_id LIMIT ?', (run_id, batch_size)).fetchall()
                claimed = []
                for row in rows:
                    if _hash(row) != row['input_hash']:
                        db.execute("UPDATE annotation_reviews SET status='HUMAN_REVIEW',reason='Source input changed' "
                                   'WHERE run_id=? AND annotation_id=? AND input_hash=?',
                                   (run_id, row['annotation_id'], row['input_hash']))
                        continue
                    db.execute("UPDATE annotation_reviews SET status='REVIEWING',attempt=1 "
                               "WHERE run_id=? AND annotation_id=? AND input_hash=? AND status='PENDING' AND attempt=0",
                               (run_id, row['annotation_id'], row['input_hash']))
                    claimed.append(row)
            if not rows:
                break
            if not claimed:
                continue
            if clock() - last_document >= 60:
                _document(db, run_id, output)
                last_document = clock()
            items = [{'seq': index, **_payload(row)} for index, row in enumerate(claimed, 1)]
            for item in items:
                for turn in [item['current'], *item['context'], *item['following']]:
                    turn['text'] = _redact(str(turn['text'])[:800])
                    turn['speaker'] = _redact(str(turn['speaker']))
            prompt = (_prompt([]).split('\n', 1)[0] + '\nThis is public corpus data; speaker represents speaker identity only, direction is synthetic mapping, '
                      'and initiative cannot be determined solely by in/out direction. There is no standard answer key. Each element in items is an independent message; '
                      'each item must be evaluated individually without merging multiple current messages or inferring from other items; '
                      'each message may only use its own context and following, and the output must match the same seq.\n' +
                      json.dumps({'items': items}, ensure_ascii=False))
            try:
                reply = (model_call or (lambda p: _model(p, output)))(prompt)
                if isinstance(reply, str):
                    reply = json.loads(reply)
                if (not _valid_reply(reply, list(range(1, len(claimed) + 1)))
                        or any(len(x['reason']) > 120 for x in reply['results'])):
                    raise ValueError('invalid Luna review response')
                results = {x['seq']: x for x in reply['results']}
            except Exception as exc:
                results = {}
                error = f'Review failed: {type(exc).__name__}'
                stopped = any(word in str(exc).lower() for word in
                              ('quota', 'usage_limit', 'usage limit', 'rate_limit', 'rate limit', '额度'))
            with db:
                db.execute('BEGIN IMMEDIATE')
                for index, row in enumerate(claimed, 1):
                    result = results.get(index)
                    current = db.execute('SELECT * FROM annotations WHERE run_id=? AND annotation_id=?',
                                         (run_id, row['annotation_id'])).fetchone()
                    if current is None or _hash(current) != row['input_hash']:
                        status, labels, reason = 'HUMAN_REVIEW', None, 'Source input changed during review'
                    else:
                        status = 'RESOLVED' if result and result['status'] == 'RESOLVED' else 'HUMAN_REVIEW'
                        labels = result['labels'] if result else None
                        reason = result['reason'][:120] if result else error
                    db.execute('UPDATE annotation_reviews SET status=?,labels_json=?,reason=? '
                               "WHERE run_id=? AND annotation_id=? AND input_hash=? AND status='REVIEWING' "
                               'AND attempt=1 AND manual_locked=0',
                               (status, json.dumps(labels, ensure_ascii=False), reason,
                                run_id, row['annotation_id'], row['input_hash']))
            counts = dict(db.execute('SELECT status,COUNT(*) FROM annotation_reviews WHERE run_id=? GROUP BY status', (run_id,)))
            if clock() - last_document >= 60:
                _document(db, run_id, output)
                last_document = clock()
            progress(f'review counts={counts}')
            if stopped:
                break
        return {'run_id': run_id, 'paused': stopped, 'counts': _document(db, run_id, output), 'output': str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output')
    parser.add_argument('--batch-size', type=int, default=256)
    args = parser.parse_args(argv)
    try:
        result = run(args.db, args.run_id, output=args.output, batch_size=args.batch_size)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 3 if result['paused'] else 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f'review_uncertain: {exc}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
