"""Device-authorized, friend-scoped edits for background model and literal quota reply."""
import hashlib
import json
import re

import agent_model_selection as selection
from house_status import write_profile
from mobile_collaboration import owner_directory
from source_state import source_lock


class SettingsConflict(ValueError):
    pass


def _model_id(value):
    return (isinstance(value, str) and len(value) <= 200
            and not any(char in value for char in ('://', '\\', '\n', '\r'))
            and not value.lower().startswith(('sk-', 'ghp_', 'github_pat_', 'bearer ')))


def catalog(profile):
    from multi_agent_orchestrator import AGENT_SPECS
    rows = []
    for provider, spec in AGENT_SPECS.items():
        try:
            entry = selection.options(profile, [provider])[provider]
            models = [{'id': model, 'efforts': [level for level in levels if isinstance(level, str)
                         and re.fullmatch(r'[A-Za-z0-9_.-]{0,32}', level)]}
                      for model, levels in entry['models'].items() if _model_id(model)]
            if not models:
                continue
            rows.append({'provider': provider, 'name': spec['name'], 'models': models,
                         'default_model': entry['default_model'] if _model_id(entry['default_model']) else '',
                         'default_effort': entry['default_effort'] if any(entry['default_effort'] in model['efforts'] for model in models) else ''})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return rows


def _snapshot(owner, profile):
    route = profile.get('background_model') or {}
    public_route = {}
    if isinstance(route, dict) and route:
        public_route = {'provider': route.get('provider') if isinstance(route.get('provider'), str)
                        and re.fullmatch(r'[A-Za-z0-9_]{1,64}', route['provider']) else '',
                        'model': route.get('model') if _model_id(route.get('model')) else '',
                        'effort': route.get('effort') if isinstance(route.get('effort'), str)
                        and re.fullmatch(r'[A-Za-z0-9_.-]{0,32}', route['effort']) else ''}
    raw = (profile.get('model_api') or {}).get('quota_reply', '')
    value = {'owner': owner, 'background_model': public_route, 'quota_reply': raw if isinstance(raw, str) else '',
             'catalog': catalog(profile), 'note': '模型候选来自配置或本机缓存，未在此查询中调用模型。'}
    value['revision'] = hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    return value


def snapshot(root, owner):
    directory, _ = owner_directory(root, owner)
    with source_lock(directory):
        _, profile = owner_directory(root, owner)
        return _snapshot(owner, profile)


def update(root, owner, body):
    if set(body) - {'action', 'owner', 'revision', 'background_model', 'quota_reply'}:
        raise ValueError('此入口仅可修改后台整理模型与额度回复')
    revision = body.get('revision')
    route, text = body.get('background_model'), body.get('quota_reply')
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
        raise ValueError('请先读取当前好友设置再保存')
    if not isinstance(route, dict) or set(route) - {'provider', 'model', 'effort'}:
        raise ValueError('后台模型设置格式无效')
    if any(not isinstance(value, str) for value in route.values()):
        raise ValueError('模型来源、名称与思考强度须为文字')
    if not isinstance(text, str) or (text.strip() and len(text) > 1200):
        raise ValueError('额度回复须为最多 1200 字的原文；留空不额外回复')
    directory, _ = owner_directory(root, owner)
    with source_lock(directory):
        _, profile = owner_directory(root, owner)
        if _snapshot(owner, profile)['revision'] != revision:
            raise SettingsConflict('此好友的设置或模型候选已更新，请重新读取后再保存')
        clean = {}
        if route:
            provider = selection.provider_name(route.get('provider') or '')
            allowed = {entry['provider'] for entry in catalog(profile)}
            if provider not in allowed:
                raise ValueError('请选择已配置的普通模型来源')
            model, effort = selection.validate(selection.options(profile, [provider]), provider,
                                               route.get('model') or '', route.get('effort') or '')
            clean = {'provider': provider, 'model': model, 'effort': effort}
        profile['background_model'] = clean
        profile.setdefault('model_api', {})['quota_reply'] = text if text.strip() else ''
        write_profile(directory / 'friend.json', profile)
        return _snapshot(owner, profile)
