"""Read-only, bounded, authenticated-UI snapshots of the operational data stores.

The dashboard never returns message bodies, secrets, tool outputs, full prompts or
filesystem paths. A missing database is *unknown* rather than a zero count.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

from usage_accounting import normalize_usage


def _query(path: Path, sql: str, params=()):
    if not path.is_file():
        return None
    # mode=ro: rendering the UI must never create or migrate databases.
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.35)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _read(path, query, params=()):
    try:
        return _query(path, query, params), None
    except (sqlite3.Error, OSError, ValueError) as ex:
        return None, type(ex).__name__


def system_snapshot(base: Path, owner: str | None = None) -> dict:
    """One friend's rows when owner is given; the whole database only when it is not.

    2026-10-26 user: these counters sat on friend-scoped pages while reading every friend's rows.
    """
    base = Path(base)
    report = {'tasks': None, 'task_items': [], 'approvals': None, 'approval_items': [],
              'agents': None, 'agent_items': [], 'mail': None, 'mail_items': [],
              'sessions': None, 'warnings': []}
    def run(db, sql, params=()):
        rows, error = _read(base / db, sql, params)
        if error: report['warnings'].append(db + ': ' + error)
        return rows

    one = str(owner) if owner else None
    by_owner = ' WHERE owner_id=?' if one else ''
    args = (one,) if one else ()

    rows = run('tasks.sqlite3', 'SELECT status, COUNT(*) AS count FROM tasks'
               + by_owner + ' GROUP BY status', args)
    if rows is not None:
        report['tasks'] = {str(r['status']): r['count'] for r in rows}
        report['task_items'] = run('tasks.sqlite3', '''SELECT task_id, title, category, status,
            owner_id, priority, updated_at, created_at FROM tasks''' + by_owner +
            ' ORDER BY updated_at DESC LIMIT 80', args) or []
    rows = run('approvals.sqlite3', 'SELECT status, COUNT(*) AS count FROM approval_requests'
               + by_owner + ' GROUP BY status', args)
    if rows is not None:
        report['approvals'] = {str(r['status']): r['count'] for r in rows}
        report['approval_items'] = run('approvals.sqlite3', """SELECT req_id, task_id, category,
            owner_id, created_at, status FROM approval_requests WHERE status='pending'""" +
            (' AND owner_id=?' if one else '') +
            ' ORDER BY created_at ASC LIMIT 40', args) or []
    rows = run('agent_mail.sqlite3', '''SELECT a.agent_id, a.name, a.provider,
        a.project_id, a.capabilities, a.wake, a.last_active_at,
        (SELECT COUNT(*) FROM agent_sessions s WHERE s.agent_id=a.agent_id AND s.expires_at>?) AS sessions
        FROM agents a''' + (' WHERE a.owner_id=?' if one else '') +
               ' ORDER BY a.last_active_at DESC LIMIT 120', (time.time(), *args))
    if rows is not None:
        total = run('agent_mail.sqlite3', 'SELECT COUNT(*) AS count FROM agents'
                    + (' WHERE owner_id=?' if one else ''), args)
        report['agents'] = total[0]['count'] if total else None
        for r in rows:
            try: caps = json.loads(r['capabilities'])
            except (ValueError, TypeError): caps = []
            if not isinstance(caps, list): caps = []
            report['agent_items'].append({**{k:r[k] for k in ('agent_id','name','provider','project_id','wake','last_active_at','sessions')},
                                          'capabilities': [str(c)[:64] for c in caps[:8]]})
        report['sessions'] = sum(int(r.get('sessions') or 0) for r in rows)
    # Mail is owned by the agent that received it, never by the message row itself.
    mail_where = ' WHERE a.owner_id=?' if one else ''
    rows = run('agent_mail.sqlite3', 'SELECT r.state, COUNT(*) AS count FROM recipients r'
               ' JOIN agents a ON a.agent_id=r.agent_id' + mail_where + ' GROUP BY r.state', args)
    if rows is not None:
        report['mail'] = {str(r['state']): r['count'] for r in rows}
        report['mail_items'] = run('agent_mail.sqlite3', '''SELECT m.message_id, m.thread_id,
            m.from_agent, m.subject, m.kind, m.created_at, r.agent_id AS to_agent,
            r.state, r.delivery_count FROM messages m JOIN recipients r ON r.message_id=m.message_id
            JOIN agents a ON a.agent_id=r.agent_id''' + mail_where +
            ' ORDER BY m.created_at DESC LIMIT 80', args) or []
    return report


def usage_snapshot(base: Path, qq_id: str | None = None, limit: int = 500) -> dict:
    """Only provider-reported input and cached token totals are considered.
    Missing/invalid usage records are not incorrectly counted as cache misses.
    """
    base = Path(base)
    result = {'available': False, 'calls': 0, 'input_tokens': 0, 'output_tokens': 0,
              'cached_input_tokens': 0, 'unknown': 0, 'by_model': [], 'by_friend': []}
    friend_root = base.parent / 'friends'
    if not friend_root.is_dir():
        return result
    if qq_id is not None and (not isinstance(qq_id, str) or not qq_id.isdigit()):
        return result
    friend_list = [qq_id] if qq_id is not None else [p.name for p in friend_root.iterdir() if p.is_dir() and p.name.isdigit()]
    accum = {}
    for friend in friend_list[:250]:
        # Strict numeric directory identifiers; no user-controlled path traversal.
        if not friend.isdigit(): continue
        path = friend_root / friend / 'history.sqlite3'
        rows, error = _read(path, 'SELECT model, tier, usage FROM usage_calls ORDER BY seq DESC LIMIT ?', (min(limit, 2000),))
        if rows is None and error == 'OperationalError':
            # Older stores did not record the provider tier.
            rows, error = _read(path, 'SELECT model, NULL AS tier, usage FROM usage_calls ORDER BY seq DESC LIMIT ?', (min(limit, 2000),))
        if rows is None: continue
        sums = {'friend': friend, 'calls': 0, 'input_tokens': 0, 'cached_input_tokens': 0}
        for item in rows:
            try:
                u = normalize_usage(json.loads(item['usage']), item['tier'])
                ins, cached, out = (int(u[k]) for k in ('input_tokens','cached_input_tokens','output_tokens'))
                if min(ins,cached,out)<0 or cached>ins: raise ValueError()
            except (ValueError, KeyError, TypeError):
                result['unknown'] += 1
                continue
            result['calls'] += 1
            result['input_tokens'] += ins
            result['output_tokens'] += out
            result['cached_input_tokens'] += cached
            sums['calls'] += 1; sums['input_tokens'] += ins; sums['cached_input_tokens'] += cached
            model = str(item['model'] or 'unknown')[:100]
            a = accum.setdefault(model, {'model': model, 'calls':0, 'input_tokens':0, 'cached_input_tokens':0})
            a['calls'] += 1; a['input_tokens'] += ins; a['cached_input_tokens'] += cached
        if sums['calls']: result['by_friend'].append(sums)
    result['available'] = result['calls']>0
    result['by_model'] = sorted(accum.values(), key=lambda r:r['input_tokens'], reverse=True)[:40]
    return result
