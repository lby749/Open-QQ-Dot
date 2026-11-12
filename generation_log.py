"""Per-friend generation records (4stage; Langfuse-style traces kept in the friend's own SQLite).

Every model generation (reply, proactive chat, care, continuation, timing) gets one row: which blocks went in
(with a digest per block, so two versions can be compared), the program prompt version, what the provider actually
received, the retrieval hits with their scores, the response, the time it took, errors, and how many bubbles were
then delivered. The Current Input Context page lists and compares them. Only the newest KEEP rows are kept."""
import hashlib
import json
import time
from datetime import datetime

KEEP = 300
TABLE = ('CREATE TABLE IF NOT EXISTS generation_log(id INTEGER PRIMARY KEY, time TEXT NOT NULL, mode TEXT NOT NULL, '
         'version TEXT NOT NULL, blocks TEXT NOT NULL, full TEXT NOT NULL, sent TEXT, retrieval TEXT, provider TEXT, '
         'response TEXT, error TEXT, ms INTEGER, delivered INTEGER NOT NULL DEFAULT 0)')
_OPEN = 'generation_log_open'
_started = {}


def _digest(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]


def start(store, mode, full, retrieval=None):
    """New row for an assembled prompt; returns its id (also remembered as the friend's open generation)."""
    from prompt_context import PROMPT_VERSION, split_blocks
    blocks, seen = [], {}
    for title, block in split_blocks(full):
        key = title or '开头'
        seen[key] = seen.get(key, 0) + 1
        blocks.append({'key': key if seen[key] == 1 else f'{key}#{seen[key]}', 'chars': len(block),
                       'digest': _digest(block)})
    with store.db:
        store.db.execute(TABLE)
        row = store.db.execute(
            'INSERT INTO generation_log(time,mode,version,blocks,full,retrieval) VALUES(?,?,?,?,?,?)',
            (datetime.now().astimezone().isoformat(timespec='seconds'), mode, PROMPT_VERSION,
             json.dumps(blocks, ensure_ascii=False), full, json.dumps(retrieval or [], ensure_ascii=False)))
        store.db.execute('DELETE FROM generation_log WHERE id <= ?', (row.lastrowid - KEEP,))
        store.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (_OPEN, str(row.lastrowid)))
    _started[str(store.path)] = time.monotonic()
    return row.lastrowid


def _update(store, sql, values):
    open_id = store.get(_OPEN)
    if not open_id:
        return
    with store.db:
        store.db.execute(TABLE)
        store.db.execute(f'UPDATE generation_log SET {sql} WHERE id=?', (*values, int(open_id)))


def sent(store, text, provider=None):
    """What the provider actually received (a continuing session gets only the new part)."""
    _update(store, 'sent=?, provider=COALESCE(?, provider)', (text, provider))


def finish(store, response=None, error=None, tool_calls=()):
    """tool_calls: the model's own lookups this turn (app-server mcpToolCall items), added to the retrieval list."""
    began = _started.pop(str(store.path), None)
    _update(store, 'response=?, error=?, ms=?', (response, None if error is None else str(error)[:300],
                                                  None if began is None else round((time.monotonic() - began) * 1000)))
    lookups = [{'label': '模型自己查：' + str(item.get('tool', '')), 'tool': True, 'status': item.get('status'),
                'query': str((item.get('arguments') or {}).get('query', ''))[:80],
                'hits': [[str(item.get('result') or item.get('error') or '')[:200], None]]} for item in tool_calls]
    if lookups and store.get(_OPEN):
        row = store.db.execute('SELECT retrieval FROM generation_log WHERE id=?', (int(store.get(_OPEN)),)).fetchone()
        _update(store, 'retrieval=?', (json.dumps(json.loads((row and row['retrieval']) or '[]') + lookups,
                                                  ensure_ascii=False),))


def delivered(store):
    """One more bubble of the open generation reached QQ."""
    _update(store, 'delivered=delivered+1', ())


def recent(store, limit=50):
    store.db.execute(TABLE)
    return [dict(row) for row in store.db.execute(
        'SELECT id,time,mode,version,ms,error,delivered,length(full) AS chars FROM generation_log ORDER BY id DESC LIMIT ?',
        (limit,))]


def get(store, row_id):
    store.db.execute(TABLE)
    row = store.db.execute('SELECT * FROM generation_log WHERE id=?', (row_id,)).fetchone()
    return dict(row) if row else None


def compare(old, new):
    """Block-by-block difference of two records: [(key, old chars or None, new chars or None, changed)]."""
    before = {b['key']: b for b in json.loads(old['blocks'])}
    after = {b['key']: b for b in json.loads(new['blocks'])}
    keys = list(dict.fromkeys([*before, *after]))
    return [(key, before.get(key, {}).get('chars'), after.get(key, {}).get('chars'),
             before.get(key, {}).get('digest') != after.get(key, {}).get('digest')) for key in keys]
