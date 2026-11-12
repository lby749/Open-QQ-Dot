"""Freeze the account/provider/model used by a QQ-originated task.

The UI can switch the active account while a task is queued or running; a task
must keep the account it was created with so retries and later stages do not
silently change provider or model.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

FIELDS = ('account_id', 'provider', 'model', 'base_url', 'key_id', 'effort')


def _path(root: Path, task_id: str) -> Path:
    safe = ''.join(ch for ch in str(task_id) if ch.isalnum() or ch in '_-')[:120]
    return Path(root) / 'data' / 'vnext_task_sessions' / (safe + '.json')


def freeze(root: Path, task_id: str, qq: str) -> dict | None:
    """Snapshot the account currently routed to this friend, if any."""
    import account_service
    row = account_service.route(root, qq)
    if row is None:
        active = account_service.active_accounts(root)
        for identifier in active.values():
            row = next((item for item in account_service.list_accounts(root) if item.get('id') == identifier), None)
            if row:
                break
    if row is None:
        return None
    snapshot = {key: row.get(key, '') for key in ('id', 'provider', 'model', 'base_url', 'key_id', 'effort')}
    snapshot['account_id'] = snapshot.pop('id')
    snapshot['qq'] = str(qq)
    snapshot['task_id'] = str(task_id)
    snapshot['created_at'] = time.time()
    path = _path(root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)
    return snapshot


def read(root: Path, task_id: str) -> dict | None:
    try:
        value = json.loads(_path(root, task_id).read_text('utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def apply(root: Path, task_id: str, profile: dict) -> dict:
    """Apply a frozen snapshot to an already loaded friend profile."""
    snapshot = read(root, task_id)
    if not snapshot:
        return profile
    api = dict(profile.get('model_api') or {})
    api['provider'] = snapshot.get('provider') or api.get('provider')
    api['model'] = snapshot.get('model') or api.get('model')
    if snapshot.get('base_url'):
        api['api_base_url'] = snapshot['base_url']
    if snapshot.get('key_id'):
        api['api_key_id'] = snapshot['key_id']
    if snapshot.get('effort'):
        api['api_effort'] = snapshot['effort']
    profile['model_api'] = api
    provider = api.get('provider')
    if provider:
        entry = profile.setdefault('agent_adapters', {}).setdefault(provider, {})
        if api.get('model'):
            entry['model'] = api['model']
        if api.get('api_effort'):
            entry['effort'] = api['api_effort']
    return profile


def list_sessions(root: Path, limit: int = 20) -> list[dict]:
    folder = Path(root) / 'data' / 'vnext_task_sessions'
    rows = []
    if folder.is_dir():
        for path in sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            value = read(root, path.stem)
            if value:
                rows.append(value)
    return rows
