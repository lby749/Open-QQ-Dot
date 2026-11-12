"""Provider/account routing foundation for vNext.

This is intentionally read-only and does not duplicate credentials.  It reads the
existing per-friend profiles and agent registry, then returns an explicit route
decision that the desktop/mobile UI can render.  Credentials remain in the
existing DPAPI-backed stores.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

AGENT_PROVIDERS = {'opencode', 'cursor', 'qwen_code', 'kimi_code', 'acp'}
API_PROVIDERS = {'custom', 'dsh', 'deepseek', 'deepseek_harness'}
KNOWN_PROVIDERS = AGENT_PROVIDERS | API_PROVIDERS | {'codex', 'claude', 'antigravity', 'webchat_gemini'}


@dataclass(frozen=True)
class Account:
    qq: str
    name: str
    provider: str
    model: str
    api_key_id: str
    effort: str
    approval: str


@dataclass(frozen=True)
class Route:
    qq: str
    provider: str
    model: str
    transport: str
    reason: str


def _profile(path: Path) -> dict:
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _approval(profile: dict, provider: str) -> str:
    api = profile.get('model_api') or {}
    approvals = api.get('provider_approvals') or {}
    value = approvals.get(provider) if isinstance(approvals, dict) else None
    if isinstance(value, str) and value:
        return value
    return str((profile.get('computer_tools') or {}).get('approval_mode') or 'never')


def accounts(root: Path) -> list[Account]:
    result = []
    base = root / 'friends'
    if not base.is_dir():
        return result
    for directory in sorted(base.iterdir(), key=lambda p: p.name):
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        profile = _profile(directory / 'friend.json')
        api = profile.get('model_api') or {}
        basic = profile.get('basic_info') or {}
        provider = str(api.get('provider') or 'unknown')[:40]
        result.append(Account(
            qq=directory.name,
            name=str(basic.get('name') or directory.name)[:80],
            provider=provider,
            model=str(api.get('model') or '')[:160],
            api_key_id=str(api.get('api_key_id') or '')[:80],
            effort=str(api.get('api_effort') or api.get('effort') or '')[:20],
            approval=_approval(profile, provider)[:40],
        ))
    return result


def provider_summary(root: Path) -> dict:
    rows = accounts(root)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.provider] = counts.get(row.provider, 0) + 1
    installed = {}
    try:
        from agent_tool_adapters import available_adapters
        installed = {item['backend']: item.get('installed') for item in available_adapters()}
    except Exception:
        pass
    return {'providers': [{'provider': key, 'friends': value, 'kind':
                           'agent' if key in AGENT_PROVIDERS else 'api' if key in API_PROVIDERS else 'builtin',
                           'installed': installed.get(key)} for key, value in sorted(counts.items())],
            'accounts': [asdict(row) for row in rows]}


def route(root: Path, qq: str, purpose: str = 'chat') -> Route:
    try:
        import account_service
        bound = account_service.route(root, str(qq))
    except Exception:
        bound = None
    if bound and bound.get('enabled', True):
        provider = str(bound.get('provider') or '')
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f'未知 provider: {provider}')
        transport = 'agent' if provider in AGENT_PROVIDERS else 'http' if provider in API_PROVIDERS else 'cli'
        return Route(qq=str(qq), provider=provider, model=str(bound.get('model') or '')[:160],
                     transport=transport, reason=f'{purpose}: account_catalog binding {bound.get("id")}')
    profile_path = root / 'friends' / str(qq) / 'friend.json'
    profile = _profile(profile_path)
    if not profile:
        raise ValueError('好友 profile 不存在')
    api = profile.get('model_api') or {}
    provider = str(api.get('provider') or '')
    if not provider:
        raise ValueError('好友未配置模型 provider')
    if provider in AGENT_PROVIDERS:
        try:
            from agent_tool_adapters import available_adapters
            found = {item['backend']: item for item in available_adapters()}.get(provider)
        except Exception:
            found = None
        if not found:
            raise ValueError(f'Agent provider 未注册: {provider}')
        if found.get('installed') is False:
            raise ValueError(f'Agent provider 未安装: {provider}')
        transport = str(found.get('transport') or 'unknown')
    elif provider in API_PROVIDERS or provider in {'codex', 'claude', 'antigravity', 'webchat_gemini'}:
        transport = 'http' if provider not in {'codex', 'claude', 'antigravity'} else 'cli'
    else:
        raise ValueError(f'未知 provider: {provider}')
    return Route(qq=str(qq), provider=provider, model=str(api.get('model') or '')[:160],
                 transport=transport, reason=f'{purpose}: explicit friend profile')
