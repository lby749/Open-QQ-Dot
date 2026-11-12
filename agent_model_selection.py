"""Validated, per-call model choices shared by planners and collaboration rooms."""
from copy import deepcopy


def signature(profile, provider):
    import hashlib
    import json
    provider = provider_name(provider)
    api = profile.get('model_api') or {}
    adapter = (profile.get('agent_adapters') or {}).get(provider) or {}
    # A catalogue belongs to this connection; saving a different account/path invalidates it.
    return hashlib.sha256(json.dumps([api, adapter], sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def discover(profile, provider, directory):
    """Reuse backend enumeration; never turn a static list or configured model into live evidence."""
    import time
    import os
    provider = provider_name(provider)
    api = profile.get('model_api') or {}
    config = (profile.get('agent_adapters') or {}).get(provider) or {}
    note, models = '', {}
    if provider == 'claude':
        raise PermissionError('本轮禁止调用 Claude，未查询该工具')
    if provider == 'codex':
        from codex_settings import fetch_available_models
        models = fetch_available_models(api.get('codex_path') or None)
        note = 'Codex CLI 实际模型目录'
    elif provider == 'opencode':
        from agent_backends.transport_opencode import LocalOpenCode
        items = LocalOpenCode(config.get('url') or '', password=os.environ.get('OPENCODE_SERVER_PASSWORD', '')).list_models()
        models = {f"{m['provider']}/{m['id']}": list(m.get('variants') or []) for m in items if m.get('enabled')}
        note = 'OpenCode 当前服务的模型及对应思考档位'
    elif provider == 'antigravity':
        from antigravity_backend import fetch_available_models
        models = {key: ([key.rsplit('-', 1)[1]] if key.rsplit('-', 1)[-1] in ('low', 'medium', 'high') else [])
                  for key, _ in fetch_available_models(api.get('antigravity_path') or None)}
        note = 'Antigravity CLI 当前模型；强度编码在模型名称中'
    elif provider == 'custom':
        from api_profiles import discover_models
        from api_backend import load_key, DEFAULT_BASE_URL, PROTOCOLS
        identifiers = discover_models(api.get('base_url') or DEFAULT_BASE_URL,
            api.get('protocol') or next(iter(PROTOCOLS)), load_key(directory, api.get('api_key_id') or 'chat'))
        models = {key: [] for key in identifiers}
        note = 'API 实际模型目录；服务未提供可验证的思考档位，沿用服务默认'
    else:
        from agent_tool_adapters import AGENT_BACKENDS
        if provider in AGENT_BACKENDS:
            from agent_backends.capabilities import list_models, capability
            identifiers, note = list_models(provider, config)
            levels = list(capability(provider, config).efforts)
            models = {key: list(levels) for key in identifiers}
        if not models:
            models = {'': []}
            note = '此接入未提供模型与思考档位枚举；只能沿用工具自己的默认配置'
    if not models:
        raise ValueError('当前工具未返回可选择的模型')
    return {'models': models, 'source': note, 'signature': signature(profile, provider), 'at': time.time()}


def flow_options(room, profile):
    providers = list(dict.fromkeys(m['provider'] for m in room['members']))
    result = options(profile, providers, members=room['members'])
    for value in providers:
        provider = provider_name(value)
        saved = (room.get('model_catalogs') or {}).get(provider)
        if saved:
            if saved.get('signature') != signature(profile, provider):
                raise PermissionError('模型连接设置已改变，请重新提取此流程的模型与思考档位')
            models = saved['models']
            model, effort = current(profile, provider)
            result[provider] = {'models': models, 'default_model': model if model in models else next(iter(models)),
                'default_effort': effort, 'evidence': saved['source']}
    return result


def provider_name(value):
    return {'api': 'custom', 'deepseek': 'custom', 'deepseek_harness': 'dsh'}.get(value, value)


def current(profile, provider):
    provider = provider_name(provider)
    api = profile.get('model_api') or {}
    if provider.startswith('webchat_'):
        return (api.get('webchat_models') or {}).get(provider, ''), (api.get('webchat_efforts') or {}).get(provider, '')
    if provider == 'dsh':
        config = api.get('dsh_settings') or {}
        return config.get('model') or api.get('model') or 'DeepSeek-V41-Flash', config.get('effort') or 'low'
    if provider in ('codex', 'claude', 'antigravity'):
        defaults = {'codex': 'gpt-6-luna', 'claude': 'sonnet', 'antigravity': 'gemini-3.8-flash-high'}
        return api.get(provider + '_model') or defaults[provider], api.get(provider + '_effort') or 'low'
    if provider == 'custom':
        return api.get('model') or 'DeepSeek-V41-Flash', api.get('api_effort') or 'low'
    config = (profile.get('agent_adapters') or {}).get(provider) or {}
    return config.get('model') or '', config.get('effort') or config.get('variant') or ''


def options(profile, providers, *, members=()):
    """Read configuration/cache only; this does not prove login, quota or live availability."""
    result = {}
    for value in providers:
        provider = provider_name(value)
        from agent_tool_adapters import AGENT_BACKENDS
        if provider not in ('codex', 'claude', 'antigravity', 'dsh', 'custom',
                            'webchat_deepseek', 'webchat_chatgpt', 'webchat_gemini') and provider not in AGENT_BACKENDS:
            raise ValueError('未知智能体，未回退到其他模型')
        model, effort = current(profile, provider)
        configured = [model] + [m.get('model') for m in members
                               if provider_name(m['provider']) == provider and m.get('model')]
        if provider.startswith('webchat_'):
            import web_chat
            source = provider[len('webchat_'):]
            models = {key: list(web_chat.efforts_for(source)) for key in web_chat.models_for(source)}
            models.setdefault(model, list(web_chat.efforts_for(source)))
        elif provider == 'codex':
            from codex_settings import model_options
            models = {key: list(levels) for key, levels in model_options().items()}
        elif provider == 'dsh':
            from dsh_backend import DEFAULT_MODELS, EFFORTS
            models = {key: list(EFFORTS[:-1] if 'flash' in key.lower() else EFFORTS)
                      for key in dict.fromkeys([*DEFAULT_MODELS, *configured])}
        elif provider in ('custom', 'claude', 'antigravity'):
            # For these backends, only configured models are advertised; no network discovery on each turn.
            from api_backend import EFFORTS
            levels = list(EFFORTS) if provider == 'custom' else (
                ['low', 'medium', 'high', 'max'] if provider == 'claude' else ['low', 'medium', 'high'])
            models = {key: ([key.rsplit('-', 1)[1]] if provider == 'antigravity'
                           and key.rsplit('-', 1)[-1] in levels else levels) for key in configured}
        else:
            from agent_backends.capabilities import capability
            config = (profile.get('agent_adapters') or {}).get(provider) or {}
            caps = capability(provider, config)
            levels = list(caps.efforts) if caps.effort_supported else []
            if caps.effort_supported and effort and effort not in levels:
                levels.append(effort)
            models = {key: list(levels) for key in configured}
            if provider == 'opencode':
                models.update({key: list(values) for key, values in (config.get('model_options') or {}).items()})
        result[provider] = {'models': models, 'default_model': model, 'default_effort': effort,
                            'evidence': '配置或本机缓存；未发起模型调用'}
    return result


def validate(catalog, provider, model='', effort=''):
    provider = provider_name(provider)
    entry = catalog[provider]
    model = model or entry['default_model']
    if model not in entry['models']:
        raise ValueError('主智能体选择了未配置或未列出的模型')
    levels = entry['models'][model]
    if effort and effort not in levels:
        raise ValueError('该模型不支持主智能体选择的思考强度')
    if not effort and levels:
        effort = entry['default_effort'] if entry['default_effort'] in levels else ('low' if 'low' in levels else levels[0])
    return model, effort


def apply(profile, provider, model='', effort=''):
    """Change one call's copy; never change the friend's saved defaults or credentials."""
    provider = provider_name(provider)
    result = deepcopy(profile)
    api = result.setdefault('model_api', {})
    api.update(provider=provider, multi_agent_enabled=False, multi_agent={'enabled': False})
    if provider.startswith('webchat_'):
        if model:
            api.setdefault('webchat_models', {})[provider] = model
        if effort:
            api.setdefault('webchat_efforts', {})[provider] = effort
        return result
    if provider == 'dsh':
        config = api.setdefault('dsh_settings', {})
        model_key, effort_key = 'model', 'effort'
    elif provider in ('codex', 'claude', 'antigravity'):
        config, model_key, effort_key = api, provider + '_model', provider + '_effort'
    elif provider == 'custom':
        config, model_key, effort_key = api, 'model', 'api_effort'
    else:
        config = result.setdefault('agent_adapters', {}).setdefault(provider, {})
        model_key, effort_key = 'model', 'variant' if provider == 'opencode' else 'effort'
    if model:
        config[model_key] = model
    if effort:
        config[effort_key] = effort
    return result
