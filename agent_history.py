"""Per-friend, read-only index of agent conversation turns.

Agents may retrieve snippets from other agents of the same friend when the
friend's `agent_history_sharing` switch is on. Cross-friend access is impossible
because the database lives inside the friend directory.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB_NAME = 'agent_history.db'
MAX_TEXT = 1200


def _connect(directory: Path):
    path = Path(directory) / DB_NAME
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_agent_history_agent ON agent_history(agent_id, created_at)')
    return conn


def record(directory, agent_id: str, session_id: str, role: str, text: str, task_id: str = ''):
    text = str(text or '').strip()
    if not text:
        return
    conn = _connect(directory)
    try:
        with conn:
            conn.execute('INSERT INTO agent_history(agent_id,session_id,role,text,task_id,created_at) '
                         'VALUES(?,?,?,?,?,?)',
                         (str(agent_id or 'unknown')[:80], str(session_id or '')[:100], str(role or '')[:20],
                          text[:MAX_TEXT], str(task_id or '')[:120],
                          datetime.now().astimezone().isoformat()))
    finally:
        conn.close()


def search(directory, query: str, agent_ids=None, limit: int = 8):
    query = str(query or '').strip()
    if not query:
        raise ValueError('query 不能为空')
    limit = max(1, min(int(limit or 8), 30))
    conn = _connect(directory)
    try:
        sql = 'SELECT agent_id,session_id,role,text,task_id,created_at FROM agent_history WHERE text LIKE ?'
        args = ['%' + query + '%']
        if agent_ids:
            ids = [str(item)[:80] for item in agent_ids if str(item).strip()]
            if ids:
                sql += ' AND agent_id IN (' + ','.join('?' for _ in ids) + ')'
                args.extend(ids)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        return [{'agent_id': row['agent_id'], 'session_id': row['session_id'], 'role': row['role'],
                 'text': row['text'], 'task_id': row['task_id'], 'time': row['created_at']}
                for row in conn.execute(sql, args)]
    finally:
        conn.close()
