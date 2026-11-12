"""Nonsecret account catalog for the vNext AccountService.

The catalog stores only references and routing metadata.  API keys remain in the
existing DPAPI-protected credential files; this module never reads or writes a
secret value.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path

ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
PROVIDERS = {
    'codex', 'claude', 'antigravity', 'opencode', 'cursor', 'qwen_code',
    'kimi_code', 'acp', 'custom', 'dsh', 'deepseek', 'deepseek_harness',
    'webchat_gemini', 'ollama',
}
_lock = threading.Lock()
EFFORTS = {'', 'off', 'none', 'low', 'medium', 'high', 'xhigh', 'max'}


def _path(root: Path) -> Path:
    return root / 'data' / 'vnext_accounts.json'


def _read(root: Path) -> list[dict]:
    try:
        value = json.loads(_path(root).read_text('utf-8'))
        rows = value.get('accounts') if isinstance(value, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _write(root: Path, rows: list[dict]) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'accounts': rows}, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)


def _clean(data: dict, identifier: str = '') -> dict:
    if any(key in data for key in ('api_key', 'key', 'secret', 'token', 'password')):
        raise ValueError('账号目录不接受凭据值；请使用 key_id 引用现有凭据')
    label = str(data.get('label') or '').strip()[:64]
    provider = str(data.get('provider') or '').strip()[:40]
    if not label:
        raise ValueError('账号名称不能为空')
    if provider not in PROVIDERS:
        raise ValueError('不支持的 provider')
    base = str(data.get('base_url') or '').strip()[:500]
    if base:
        from api_profiles import validate_url
        base = validate_url(base)
    model = str(data.get('model') or '').strip()[:160]
    key_id = str(data.get('key_id') or data.get('api_key_id') or '').strip()[:80]
    if key_id and not ID_PATTERN.fullmatch(key_id):
        raise ValueError('凭据引用 ID 无效')
    effort = str(data.get('effort') or data.get('api_effort') or '').strip()
    if effort not in EFFORTS:
        raise ValueError('思考档位无效')
    return {'id': identifier, 'label': label, 'provider': provider, 'base_url': base,
            'model': model, 'key_id': key_id, 'effort': effort,
            'enabled': bool(data.get('enabled', True))}


def list_accounts(root: Path) -> list[dict]:
    return _read(root)


def discover(root: Path) -> list[dict]:
    """Non-secret providers already present in friend profiles."""
    rows = []
    base = root / 'friends'
    if not base.is_dir():
        return rows
    for directory in sorted(base.iterdir(), key=lambda p: p.name):
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        try:
            profile = json.loads((directory / 'friend.json').read_text('utf-8'))
        except (OSError, ValueError):
            continue
        api = profile.get('model_api') or {}
        rows.append({'source': 'friend_profile', 'qq': directory.name,
                     'provider': str(api.get('provider') or 'unknown')[:40],
                     'model': str(api.get('model') or '')[:160],
                     'key_id': str(api.get('api_key_id') or '')[:80]})
    return rows


def add(root: Path, data: dict) -> dict:
    identifier = 'acct_' + secrets.token_hex(8)
    row = _clean(data, identifier)
    with _lock:
        rows = _read(root)
        rows.append(row)
        _write(root, rows)
    return row


def add_with_key(root: Path, data: dict, api_key: str) -> dict:
    key = str(api_key or '').strip()
    if not key:
        raise ValueError('API Key 不能为空')
    identifier = 'acct_' + secrets.token_hex(8)
    row = _clean(data, identifier)
    if row.get('key_id'):
        raise ValueError('使用 API Key 创建账号时不要同时传 key_id')
    directory = root / 'data' / 'vnext_accounts' / identifier
    directory.mkdir(parents=True, exist_ok=False)
    try:
        from api_backend import save_key
        save_key(directory, key, 'chat')
    except Exception:
        import shutil
        shutil.rmtree(directory, ignore_errors=True)
        raise
    row['key_id'] = identifier
    with _lock:
        rows = _read(root)
        rows.append(row)
        try:
            _write(root, rows)
        except Exception:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return row


def _bindings_path(root: Path) -> Path:
    return root / 'data' / 'vnext_account_bindings.json'




def _active_path(root: Path) -> Path:
    return root / 'data' / 'vnext_active_accounts.json'


def active_accounts(root: Path) -> dict[str, str]:
    """tool -> account_id, the currently enabled provider for each agent tool."""
    try:
        value = json.loads(_active_path(root).read_text('utf-8'))
        rows = value.get('active') if isinstance(value, dict) else None
        return {str(k): str(v) for k, v in rows.items()} if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def activate(root: Path, tool: str, identifier: str) -> dict:
    tool = str(tool or '').strip()
    if not tool or len(tool) > 80:
        raise ValueError('工具名称无效')
    if not any(row.get('id') == identifier for row in _read(root)):
        raise ValueError('账号不存在')
    with _lock:
        rows = active_accounts(root)
        rows[tool] = identifier
        path = _active_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'active': rows}, ensure_ascii=False, indent=2), 'utf-8')
        temp.replace(path)
    return {'tool': tool, 'account_id': identifier}


def bindings(root: Path) -> dict[str, str]:
    try:
        value = json.loads(_bindings_path(root).read_text('utf-8'))
        rows = value.get('bindings') if isinstance(value, dict) else None
        return {str(k): str(v) for k, v in rows.items() if str(k).isdigit()} if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}




def apply_to_friend(root: Path, qq: str, identifier: str) -> dict:
    """Write the selected account's non-secret connection fields into one friend profile."""
    qq = str(qq or '').strip()
    if not qq.isdigit():
        raise ValueError('QQ 号无效')
    row = next((item for item in _read(root) if item.get('id') == identifier), None)
    if row is None:
        raise ValueError('账号不存在')
    path = root / 'friends' / qq / 'friend.json'
    if not path.is_file():
        raise ValueError('好友没有本地资料')
    profile = json.loads(path.read_text('utf-8'))
    api = profile.setdefault('model_api', {})
    api['provider'] = row.get('provider') or 'custom'
    api['model'] = row.get('model') or ''
    if row.get('base_url'):
        api['api_base_url'] = row['base_url']
    if row.get('key_id'):
        api['api_key_id'] = row['key_id']
    if row.get('effort'):
        api['api_effort'] = row['effort']
    if api['provider'] in {'opencode', 'claude', 'codex', 'antigravity', 'dsh', 'kimi_code', 'qwen_code',
                           'cursor', 'acp'}:
        adapters = profile.setdefault('agent_adapters', {})
        entry = adapters.setdefault(api['provider'], {})
        entry['model'] = api['model']
        if api.get('api_effort'):
            entry['effort'] = api['api_effort']
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), 'utf-8')
    temp.replace(path)
    return {'qq': qq, 'provider': api['provider'], 'model': api['model']}




def export_catalog(root: Path) -> dict:
    """Non-secret catalog export, suitable for another machine/install."""
    return {'version': 1, 'accounts': [{key: row.get(key, '') for key in
            ('label', 'provider', 'base_url', 'model', 'effort', 'enabled')}
            for row in _read(root)]}


def import_catalog(root: Path, payload: dict, replace: bool = False) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get('accounts'), list):
        raise ValueError('账号文件格式无效')
    rows = [] if replace else _read(root)
    seen = {(row.get('provider'), row.get('label'), row.get('model')) for row in rows}
    added = 0
    with _lock:
        for item in payload['accounts']:
            if not isinstance(item, dict):
                continue
            clean = _clean(item, 'acct_' + secrets.token_hex(8))
            signature = (clean['provider'], clean['label'], clean['model'])
            if signature in seen:
                continue
            rows.append(clean)
            seen.add(signature)
            added += 1
        _write(root, rows)
    return {'added': added, 'total': len(rows)}


def bind(root: Path, qq: str, identifier: str) -> dict:
    qq = str(qq or '').strip()
    if not (qq.isdigit() and 5 <= len(qq) <= 15):
        raise ValueError('QQ 号无效')
    if not any(row.get('id') == identifier for row in _read(root)):
        raise ValueError('账号不存在')
    with _lock:
        rows = bindings(root)
        rows[qq] = identifier
        path = _bindings_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'bindings': rows}, ensure_ascii=False, indent=2), 'utf-8')
        temp.replace(path)
    return {'qq': qq, 'account_id': identifier}


def route(root: Path, qq: str) -> dict | None:
    identifier = bindings(root).get(str(qq or '').strip())
    if not identifier:
        return None
    return next((row for row in _read(root) if row.get('id') == identifier), None)


def probe(root: Path, identifier: str, timeout: float = 8.0) -> dict:
    row = next((item for item in _read(root) if item.get('id') == identifier), None)
    if row is None:
        raise ValueError('账号不存在')
    base = str(row.get('base_url') or '').strip().rstrip('/')
    if not base:
        raise ValueError('账号没有 base_url')
    from api_backend import load_key
    key = load_key(root / 'data' / 'vnext_accounts' / identifier, 'chat')
    import json
    import urllib.request
    endpoint = base + ('/models' if base.endswith('/v1') else '/v1/models')
    request = urllib.request.Request(endpoint, headers={
        'Authorization': f'Bearer {key}', 'User-Agent': 'Open-QQ-Dot', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}', 'endpoint': endpoint}
    models = [item.get('id') for item in payload.get('data', []) if isinstance(item, dict) and item.get('id')]
    return {'ok': True, 'endpoint': endpoint, 'models': models[:20], 'model_count': len(models)}


def chat(root: Path, identifier: str, prompt: str, timeout: float = 30.0) -> dict:
    row = next((item for item in _read(root) if item.get('id') == identifier), None)
    if row is None:
        raise ValueError('账号不存在')
    prompt = str(prompt or '').strip()[:4000]
    if not prompt:
        raise ValueError('消息不能为空')
    base = str(row.get('base_url') or '').strip().rstrip('/')
    if not base:
        raise ValueError('账号没有 base_url')
    from api_backend import load_key
    key = load_key(root / 'data' / 'vnext_accounts' / identifier, 'chat')
    import json
    import urllib.request
    endpoint = base + ('/chat/completions' if base.endswith('/v1') else '/v1/chat/completions')
    payload = json.dumps({'model': row.get('model') or 'default',
                          'messages': [{'role': 'user', 'content': prompt}],
                          'max_tokens': 256}).encode('utf-8')
    request = urllib.request.Request(endpoint, data=payload, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
        'User-Agent': 'Open-QQ-Dot', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except Exception as exc:
        raise RuntimeError(f'{type(exc).__name__}') from None
    choices = data.get('choices') or []
    content = ''
    if choices and isinstance(choices[0], dict):
        content = str((choices[0].get('message') or {}).get('content') or '')
    return {'ok': True, 'endpoint': endpoint, 'model': row.get('model') or '', 'content': content[:2000]}




def clone(root: Path, identifier: str) -> dict:
    """Duplicate non-secret routing metadata under a new id."""
    row = next((item for item in _read(root) if item.get('id') == identifier), None)
    if row is None:
        raise ValueError('账号不存在')
    payload = {key: row.get(key, '') for key in ('provider', 'base_url', 'model', 'key_id', 'effort', 'enabled')}
    payload['label'] = (str(row.get('label') or '账号') + ' 副本')[:64]
    payload['id'] = 'acct_' + secrets.token_hex(8)
    clean = _clean(payload, payload['id'])
    with _lock:
        rows = _read(root); rows.append(clean); _write(root, rows)
    return clean


def remove(root: Path, identifier: str) -> bool:
    with _lock:
        rows = _read(root)
        kept = [row for row in rows if row.get('id') != identifier]
        if len(kept) == len(rows):
            return False
        _write(root, kept)
        bound = bindings(root)
        bound = {qq: value for qq, value in bound.items() if value != identifier}
        path = _bindings_path(root)
        if path.is_file():
            path.write_text(json.dumps({'bindings': bound}, ensure_ascii=False, indent=2), 'utf-8')
        target = root / 'data' / 'vnext_accounts' / identifier
        if target.is_dir() and target.resolve().is_relative_to((root / 'data' / 'vnext_accounts').resolve()):
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        return True
