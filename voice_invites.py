"""Short-lived desktop-to-phone voice call invitations.

The desktop queues an invitation for the paired phone; the phone polls while its
app is open and shows an explicit accept/decline prompt.  No microphone is opened
until the phone accepts.  This is an in-app invitation queue, not a push service.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

TTL = 120
_LOCK = threading.Lock()


def _path(root: Path) -> Path:
    return root / 'data' / 'vnext_voice_invites.json'


def _read(root: Path) -> list[dict]:
    try:
        value = json.loads(_path(root).read_text('utf-8'))
        return value.get('invites', []) if isinstance(value, dict) else []
    except (OSError, ValueError):
        return []


def _write(root: Path, rows: list[dict]) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'invites': rows}, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)


def create(root: Path, qq: str, device_id: str = '') -> dict:
    qq = str(qq or '').strip()
    if not qq.isdigit():
        raise ValueError('QQ 号无效')
    now = time.time()
    row = {'id': 'inv_' + secrets.token_hex(8), 'qq': qq, 'device_id': str(device_id or ''),
           'created_at': now, 'expires_at': now + TTL, 'status': 'ringing'}
    with _LOCK:
        rows = [item for item in _read(root) if float(item.get('expires_at', 0)) > now and item.get('status') == 'ringing']
        rows.append(row)
        _write(root, rows)
    return row


def pending(root: Path, device_id: str = '') -> list[dict]:
    now = time.time()
    rows = [item for item in _read(root)
            if item.get('status') == 'ringing' and float(item.get('expires_at', 0)) > now
            and (not device_id or not item.get('device_id') or item.get('device_id') == device_id)]
    return rows


def respond(root: Path, invite_id: str, accept: bool) -> dict:
    with _LOCK:
        rows = _read(root)
        for item in rows:
            if item.get('id') == invite_id:
                if item.get('status') != 'ringing':
                    raise ValueError('邀请已处理')
                if float(item.get('expires_at', 0)) <= time.time():
                    item['status'] = 'expired'
                    _write(root, rows)
                    raise ValueError('邀请已过期')
                item['status'] = 'accepted' if accept else 'declined'
                item['responded_at'] = time.time()
                _write(root, rows)
                return item
    raise ValueError('邀请不存在')
