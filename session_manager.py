"""Multi-workspace and multi-session manager for Open-QQ-Dot agents.

Provides isolated multi-project and multi-conversation management per friend:
- Project workspaces: default friend folder + user-added project folders
- Independent conversation sessions under each workspace
- Clean storage under friends/<qq>/ (never pollutes target user project directories)
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):
        raise ValueError('会话或工作区标识无效')
    if value.upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
        raise ValueError('不能使用Windows设备名作为会话标识')
    return value


def _friend_dir(qq: str) -> Path:
    if not re.fullmatch(r'\d{5,12}',str(qq)):
        raise ValueError('好友标识无效')
    p = ROOT / 'friends' / str(qq)
    if not p.resolve().is_relative_to((ROOT / 'friends').resolve()) or p.is_symlink():
        raise PermissionError('好友目录不能重定向至其他目录')
    p.mkdir(parents=True, exist_ok=True)
    return p


# ================= Workspace Management =================

def _workspaces_file(qq: str) -> Path:
    return _friend_dir(qq) / 'workspaces.json'


def list_workspaces(qq: str) -> list[dict]:
    """List all workspaces for a specific friend.

    First item is always the default workspace (friends/<qq>).
    """
    default_ws = {
        'id': 'default',
        'title': '默认文件夹 (好友主目录)',
        'path': str(_friend_dir(qq).resolve()),
        'is_default': True,
        'created_at': datetime.now().astimezone().isoformat(),
    }

    path = _workspaces_file(qq)
    if not path.is_file():
        data = {'active_id': 'default', 'workspaces': [default_ws]}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return [default_ws]

    try:
        data = json.loads(path.read_text('utf-8'))
        wss = data.get('workspaces', [])
        # Ensure default workspace is always present at index 0
        cleaned = [w for w in wss if w.get('id') != 'default']
        return [default_ws] + cleaned
    except Exception:
        return [default_ws]


def _save_workspaces(qq: str, workspaces: list[dict], active_id: str | None = None):
    cur_active = active_id or get_active_workspace_id(qq)
    data = {'active_id': cur_active, 'workspaces': workspaces}
    _workspaces_file(qq).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def get_active_workspace_id(qq: str) -> str:
    path = _workspaces_file(qq)
    if path.is_file():
        try:
            return json.loads(path.read_text('utf-8')).get('active_id', 'default')
        except Exception:
            pass
    return 'default'


def set_active_workspace_id(qq: str, ws_id: str):
    _identifier(ws_id)
    wss = list_workspaces(qq)
    if ws_id not in {w['id'] for w in wss}:
        raise ValueError('工作区不存在')
    _save_workspaces(qq, wss, active_id=ws_id)


def add_workspace(qq: str, folder_path: str, title: str | None = None) -> dict:
    """Add a new project folder workspace."""
    p = Path(folder_path).resolve()
    if not p.is_dir():
        raise ValueError(f'Specified workspace directory does not exist: {folder_path}')

    ws_title = title.strip() if title else p.name or str(p)
    ws_id = f'ws_{uuid.uuid4().hex[:8]}'
    new_ws = {
        'id': ws_id,
        'title': ws_title,
        'path': str(p),
        'is_default': False,
        'created_at': datetime.now().astimezone().isoformat(),
    }

    wss = list_workspaces(qq)
    # Check if this path was already added
    for existing in wss:
        if existing.get('path') == str(p) and not existing.get('is_default'):
            set_active_workspace_id(qq, existing['id'])
            return existing

    wss.append(new_ws)
    _save_workspaces(qq, wss, active_id=ws_id)
    return new_ws


def remove_workspace(qq: str, ws_id: str) -> bool:
    """Remove a project workspace. Default workspace cannot be removed."""
    if ws_id == 'default':
        return False
    wss = list_workspaces(qq)
    filtered = [w for w in wss if w.get('id') != ws_id]
    if len(filtered) == len(wss):
        return False

    next_active = 'default'
    if get_active_workspace_id(qq) == ws_id:
        set_active_workspace_id(qq, next_active)
    _save_workspaces(qq, filtered, active_id=next_active)
    return True


# ================= Session Management under Workspace =================

def _sessions_dir(qq: str, ws_id: str = 'default') -> Path:
    ws_id = _identifier(ws_id or 'default')
    if ws_id == 'default' or not ws_id:
        p = _friend_dir(qq) / 'sessions'
    else:
        p = _friend_dir(qq) / 'workspaces' / ws_id / 'sessions'
    if not p.resolve().is_relative_to(_friend_dir(qq).resolve()) or p.is_symlink():
        raise PermissionError('会话目录越界')
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(qq,session_id,ws_id='default'):
    base=_sessions_dir(qq,ws_id).resolve()
    path=base / (_identifier(session_id)+'.json')
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        raise PermissionError('会话文件不能重定向到其他目录')
    return path


def _index_path(qq: str, ws_id: str = 'default') -> Path:
    return _sessions_dir(qq, ws_id) / 'index.json'


def list_sessions(qq: str, ws_id: str = 'default') -> list[dict]:
    """List all sessions for a specific workspace of a friend, ordered by updated_at desc."""
    path = _index_path(qq, ws_id)
    if not path.is_file():
        default_item = {
            'id': 'session_main',
            'title': '默认对话',
            'created_at': datetime.now().astimezone().isoformat(),
            'updated_at': datetime.now().astimezone().isoformat(),
            'message_count': 0,
        }
        path.write_text(json.dumps([default_item], ensure_ascii=False, indent=2), encoding='utf-8')
        return [default_item]

    try:
        items = json.loads(path.read_text('utf-8'))
        if not items:
            default_item = {
                'id': 'session_main',
                'title': '默认对话',
                'created_at': datetime.now().astimezone().isoformat(),
                'updated_at': datetime.now().astimezone().isoformat(),
                'message_count': 0,
            }
            path.write_text(json.dumps([default_item], ensure_ascii=False, indent=2), encoding='utf-8')
            return [default_item]
        return items
    except Exception:
        return []


def _save_index(qq: str, ws_id: str, sessions: list[dict]):
    _index_path(qq, ws_id).write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding='utf-8')


def get_active_session_id(qq: str, ws_id: str = 'default') -> str:
    pref_path = _sessions_dir(qq, ws_id) / 'active.txt'
    if pref_path.is_file():
        sid = pref_path.read_text('utf-8').strip()
        if sid:
            return sid
    sessions = list_sessions(qq, ws_id)
    return sessions[0]['id'] if sessions else 'session_main'


def set_active_session_id(qq: str, session_id: str, ws_id: str = 'default'):
    (_sessions_dir(qq, ws_id) / 'active.txt').write_text(_identifier(session_id), encoding='utf-8')


def load_session(qq: str, session_id: str, ws_id: str = 'default') -> dict:
    """Load session payload: {id, title, messages: [...]}."""
    s_file = _session_path(qq,session_id,ws_id)
    if s_file.is_file():
        try:
            return json.loads(s_file.read_text('utf-8'))
        except Exception:
            pass

    sessions = list_sessions(qq, ws_id)
    title = next((s['title'] for s in sessions if s['id'] == session_id), '默认对话')
    now = datetime.now().astimezone().isoformat()
    data = {
        'id': session_id,
        'title': title,
        'created_at': now,
        'updated_at': now,
        'messages': [],
    }
    s_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def create_session(qq: str, ws_id: str = 'default', title: str = '新对话') -> dict:
    """Create a new conversation session under a workspace."""
    sid = f'session_{uuid.uuid4().hex[:8]}'
    now = datetime.now().astimezone().isoformat()
    new_item = {
        'id': sid,
        'title': title.strip() or '新对话',
        'created_at': now,
        'updated_at': now,
        'message_count': 0,
    }
    sessions = list_sessions(qq, ws_id)
    sessions.insert(0, new_item)
    _save_index(qq, ws_id, sessions)

    session_payload = {
        'id': sid,
        'title': new_item['title'],
        'created_at': now,
        'updated_at': now,
        'messages': [],
    }
    (_session_path(qq,sid,ws_id)).write_text(
        json.dumps(session_payload, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    set_active_session_id(qq, sid, ws_id)
    return new_item


def rename_session(qq: str, session_id: str, new_title: str, ws_id: str = 'default') -> bool:
    new_title = new_title.strip()
    if not new_title:
        return False
    sessions = list_sessions(qq, ws_id)
    found = False
    for s in sessions:
        if s['id'] == session_id:
            s['title'] = new_title
            s['updated_at'] = datetime.now().astimezone().isoformat()
            found = True
            break
    if found:
        _save_index(qq, ws_id, sessions)
        s_file = _session_path(qq,session_id,ws_id)
        if s_file.is_file():
            try:
                data = json.loads(s_file.read_text('utf-8'))
                data['title'] = new_title
                s_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass
    return found


def delete_session(qq: str, session_id: str, ws_id: str = 'default') -> bool:
    sessions = list_sessions(qq, ws_id)
    if len(sessions) <= 1:
        clear_session(qq, session_id, ws_id)
        return False

    sessions = [s for s in sessions if s['id'] != session_id]
    _save_index(qq, ws_id, sessions)
    s_file = _session_path(qq,session_id,ws_id)
    if s_file.is_file():
        try:
            s_file.unlink()
        except Exception:
            pass

    if get_active_session_id(qq, ws_id) == session_id:
        set_active_session_id(qq, sessions[0]['id'], ws_id)
    return True


def clear_session(qq: str, session_id: str, ws_id: str = 'default'):
    """Clear all messages in a session."""
    data = load_session(qq, session_id, ws_id)
    data['messages'] = []
    data['updated_at'] = datetime.now().astimezone().isoformat()
    (_session_path(qq,session_id,ws_id)).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    sessions = list_sessions(qq, ws_id)
    for s in sessions:
        if s['id'] == session_id:
            s['message_count'] = 0
            s['updated_at'] = data['updated_at']
            break
    _save_index(qq, ws_id, sessions)


def append_message(qq: str, session_id: str, role: str, content: str,
                   thinking: str = '', tools: list | None = None, ws_id: str = 'default',
                   agent_id: str = 'session') -> dict:
    """Append a user or assistant message to the session."""
    data = load_session(qq, session_id, ws_id)
    now = datetime.now().astimezone().isoformat()
    msg = {
        'id': uuid.uuid4().hex[:12],
        'role': role,
        'content': content,
        'thinking': thinking,
        'tools': tools or [],
        'timestamp': now,
    }
    data['messages'].append(msg)
    data['updated_at'] = now

    if len(data['messages']) == 1 and role == 'user' and data['title'] in ('新对话', '默认对话', '默认主对话'):
        first_line = content.strip().splitlines()[0][:20]
        if first_line:
            data['title'] = first_line

    (_session_path(qq,session_id,ws_id)).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )

    sessions = list_sessions(qq, ws_id)
    for s in sessions:
        if s['id'] == session_id:
            s['title'] = data['title']
            s['message_count'] = len(data['messages'])
            s['updated_at'] = now
            break
    _save_index(qq, ws_id, sessions)
    try:
        import agent_history
        agent_history.record(_friend_dir(qq), agent_id, session_id, role, content)
    except Exception:
        pass
    return msg


def append_messages(qq: str, session_id: str, entries, ws_id: str = 'default') -> int:
    """Append many messages in one read/write (used by migration and import).

    ``entries`` yields ``(role, content)`` or ``(role, content, timestamp)``.
    Writing the session file and index once avoids O(n²) imports for long
    conversations while keeping the same on-disk shape as append_message().
    """
    data = load_session(qq, session_id, ws_id)
    now = datetime.now().astimezone().isoformat()
    added = 0
    for entry in entries:
        if isinstance(entry, dict):
            role, content = entry.get('role', 'user'), entry.get('content', '')
            stamp = entry.get('timestamp')
        elif isinstance(entry, (tuple, list)) and len(entry) >= 2:
            role, content = entry[0], entry[1]
            stamp = entry[2] if len(entry) > 2 else None
        else:
            continue
        text = str(content or '')
        if not text:
            continue
        item = {'id': uuid.uuid4().hex[:12], 'role': str(role or 'user'), 'content': text,
                'thinking': '', 'tools': [], 'timestamp': stamp or now}
        if len(data['messages']) == 0 and item['role'] == 'user' and data['title'] in ('新对话', '默认对话', '默认主对话'):
            first_line = text.strip().splitlines()[0][:20]
            if first_line:
                data['title'] = first_line
        data['messages'].append(item)
        added += 1
    if not added:
        return 0
    data['updated_at'] = now
    (_session_path(qq,session_id,ws_id)).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    sessions = list_sessions(qq, ws_id)
    for s in sessions:
        if s['id'] == session_id:
            s['title'] = data['title']
            s['message_count'] = len(data['messages'])
            s['updated_at'] = now
            break
    _save_index(qq, ws_id, sessions)
    return added
