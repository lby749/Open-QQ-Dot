"""Read-only ExperienceEngine projection for the vNext client.

The real state machine, verification gate and hybrid search already live in
`local_retrieval_engine.TaskExperienceStore`.  This module does not duplicate that
logic; it exposes a small sanitized snapshot and delegates search to the existing
store when explicitly requested.
"""
from __future__ import annotations

import sqlite3
import re
from pathlib import Path

LIFECYCLES = ('CANDIDATE', 'EXECUTED', 'RESULT_RECORDED', 'VALIDATED', 'FAILED', 'PARTIAL', 'INCONCLUSIVE', 'STALE')


def _owner(owner, admin=False):
    if admin is True and owner in (None, ''):
        return None
    if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_:@.-]{1,80}', owner) or '..' in owner:
        raise PermissionError('请指定已授权的好友；经验库不默认开放全局数据')
    return owner


def _connect(root: Path):
    path = root / 'data' / 'retrieval_engine.sqlite3'
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def snapshot(root: Path, limit: int = 20, *, owner: str = '', admin: bool = False) -> dict:
    owner = _owner(owner, admin)
    where, params = (' WHERE owner_id=?', (owner,)) if owner else ('', ())
    con = _connect(root)
    if con is None:
        return {'available': False, 'total': 0, 'by_state': {}, 'recent': [],
                'note': 'retrieval_engine.sqlite3 不存在'}
    try:
        total = int(con.execute('SELECT count(*) FROM task_experiences' + where, params).fetchone()[0])
        by_state = {row['lifecycle_state']: int(row['n']) for row in con.execute(
            'SELECT lifecycle_state, count(*) AS n FROM task_experiences' + where + ' GROUP BY lifecycle_state', params)}
        by_confidence = {row['confidence_status']: int(row['n']) for row in con.execute(
            'SELECT confidence_status, count(*) AS n FROM task_experiences' + where + ' GROUP BY confidence_status', params)}
        rows = con.execute(
            f'''SELECT experience_id, task_id, owner_id, agent_provider, model_name,
                      problem_statement, action_taken, result_status, confidence_status,
                      lifecycle_state, created_at
               FROM task_experiences{where} ORDER BY created_at DESC LIMIT ?''',
            params + (max(1, min(100, limit)),)).fetchall()
        recent = [{
            'id': row['experience_id'],
            'task_id': row['task_id'],
            'owner': row['owner_id'],
            'provider': row['agent_provider'],
            'model': row['model_name'],
            'problem': (row['problem_statement'] or '')[:180],
            'action': (row['action_taken'] or '')[:180],
            'result': row['result_status'],
            'confidence': row['confidence_status'],
            'state': row['lifecycle_state'],
            'created_at': row['created_at'],
        } for row in rows]
        return {'available': True, 'total': total, 'by_state': by_state,
                'by_confidence': by_confidence, 'recent': recent,
                'verified': int(con.execute(
                    'SELECT count(*) FROM task_experiences' + (where + ' AND ' if where else ' WHERE ') +
                    "lifecycle_state='VALIDATED' AND confidence_status='high_verified'", params).fetchone()[0]),
                'scope': owner or 'admin-global',
                'note': '现有 TaskExperienceStore 的只读投影；验证与签名由原模块负责'}
    except sqlite3.Error as exc:
        return {'available': False, 'total': 0, 'by_state': {}, 'recent': [],
                'note': f'读取失败：{type(exc).__name__}'}
    finally:
        con.close()


def record_candidate(root: Path, problem: str, action: str, cause: str = '', owner: str = '') -> dict:
    """Record an explicitly unverified suggestion; never a validated success."""
    problem = str(problem or '').strip()[:600]
    action = str(action or '').strip()[:600]
    cause = str(cause or '').strip()[:300]
    owner = _owner(owner)
    if len(problem) < 4 or len(action) < 4:
        raise ValueError('问题与做法至少各 4 个字符')
    path = root / 'data' / 'retrieval_engine.sqlite3'
    if not path.is_file():
        raise ValueError('经验库不存在')
    from local_retrieval_engine import get_local_retrieval_engine, TaskExperienceStore
    engine = get_local_retrieval_engine(path)
    store = TaskExperienceStore(engine)
    exp = store.record_experience(
        task_id='', owner_id=owner, project_id='vnext', agent_provider='user',
        model_name='', tools_used=[], problem_statement=problem,
        environment_conditions={}, action_taken=action,
        result_status='unverified_suggestion', root_cause_or_condition=cause)
    return {'id': exp.experience_id, 'state': exp.lifecycle_state.value,
            'confidence': exp.confidence_status.value, 'owner': owner,
            'problem': problem, 'action': action, 'cause': cause,
            'note': '用户候选，未经验证；不能作为已验证成功'}


def search(root: Path, query: str, owner: str = '', limit: int = 10, *, admin: bool = False) -> list[dict]:
    # Lexical only. Richer hybrid retrieval remains in TaskExperienceStore.
    owner = _owner(owner, admin)
    text = str(query or '').strip()[:200]
    if len(text) < 2:
        return []
    con = _connect(root)
    if con is None:
        return []
    try:
        params: list[object] = [text]
        owner_sql = ''
        if owner:
            owner_sql = ' AND e.owner_id = ?'
            params.append(owner)
        params.append(max(1, min(30, int(limit))))
        rows = con.execute(
            f'''SELECT e.experience_id, e.task_id, e.owner_id, e.agent_provider, e.model_name,
                       e.problem_statement, e.action_taken, e.result_status, e.confidence_status,
                       e.lifecycle_state, e.created_at
                FROM task_experiences_fts f JOIN task_experiences e ON e.experience_id = f.experience_id
                WHERE task_experiences_fts MATCH ? AND e.lifecycle_state != 'STALE'{owner_sql}
                ORDER BY rank LIMIT ?''', params).fetchall()
        if not rows:
            # Default FTS5 tokenization treats a Chinese phrase as one token; fall
            # back to a bounded substring match rather than returning a false empty.
            like = f'%{text}%'
            like_params: list[object] = [like, like, like]
            like_owner = ''
            if owner:
                like_owner = ' AND owner_id = ?'
                like_params.append(owner)
            like_params.append(max(1, min(30, int(limit))))
            rows = con.execute(
                f'''SELECT experience_id, task_id, owner_id, agent_provider, model_name,
                           problem_statement, action_taken, result_status, confidence_status,
                           lifecycle_state, created_at
                    FROM task_experiences
                    WHERE (problem_statement LIKE ? OR action_taken LIKE ? OR root_cause_or_condition LIKE ?)
                      AND lifecycle_state != 'STALE'{like_owner}
                    ORDER BY created_at DESC LIMIT ?''', like_params).fetchall()
        return [{
            'id': row['experience_id'], 'task_id': row['task_id'], 'owner': row['owner_id'],
            'provider': row['agent_provider'], 'model': row['model_name'],
            'problem': (row['problem_statement'] or '')[:180],
            'action': (row['action_taken'] or '')[:180],
            'result': row['result_status'], 'confidence': row['confidence_status'],
            'state': row['lifecycle_state'], 'created_at': row['created_at'],
        } for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()
