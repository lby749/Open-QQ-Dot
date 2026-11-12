"""Read-only, directory-bound runtime discovery; no credential or model calls."""
import json
from pathlib import Path

TOOLS = [{'name': 'inspect_runtime', 'description': '按需查询当前好友的运行配置、已选模型、可用工具和工具模型。不返回凭据；配置不代表已经通过实际调用验证。',
          'inputSchema': {'type': 'object', 'properties': {
              'section': {'type': 'string', 'enum': ['config', 'models', 'model_choices', 'tools', 'tool_models']},
              'query': {'type': 'string', 'maxLength': 120},
              'offset': {'type': 'integer', 'minimum': 0},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30}},
              'required': ['section'], 'additionalProperties': False}}]


def _fields(value, fields):
    # Explicit field allowlist; nested dictionaries and URLs are never copied.
    return {key: val for key in fields if isinstance(value, dict) and key in value
            and isinstance((val := value[key]), (str, bool, int, float))
            and (not isinstance(val, str) or
                 len(val) <= 160 and not any(mark in val for mark in ('://', '\\', '/', '\n')))}


def call(directory, arguments):
    from jsonschema import validate
    validate(arguments, TOOLS[0]['inputSchema'])
    profile = json.loads((Path(directory)/'friend.json').read_text('utf-8'))
    section = arguments['section']
    api = profile.get('model_api') or {}
    if section == 'config':
        rows = [{'kind': 'computer_tools', **_fields(profile.get('computer_tools'), (
            'enabled', 'commands', 'mode', 'read_only', 'full_computer_access', 'approval_mode'))},
            {'kind': 'model_api', **_fields(api, ('provider', 'context_tokens', 'prompt_cache_mode'))}]
    elif section == 'models':
        provider = api.get('provider', 'codex')
        fields = {'codex': ('codex_model', 'codex_effort'),
                  'antigravity': ('antigravity_model', 'antigravity_effort')}.get(provider, ('model', 'effort'))
        rows = [{'kind': 'selected', 'provider': provider, **_fields(api, fields),
                 'evidence': '当前配置，未在本次查询中调用模型'}]
    elif section == 'model_choices':
        from agent_model_selection import options
        multi = api.get('multi_agent') or {}
        providers = (multi.get('handover_order') or api.get('handover_order') or
                     multi.get('active_providers') or api.get('active_providers') or [api.get('provider', 'codex')])
        rows = [{'provider': provider, 'model': model, 'efforts': levels, 'evidence': item['evidence']}
                for provider, item in options(profile, providers).items()
                for model, levels in item['models'].items()]
    elif section == 'tools':
        from topic_library import model_tools
        rows = [{'name': tool['name'], 'description': tool.get('description', '')[:160]}
                for tool in model_tools(directory)]
    else:
        rows = [{'kind': str(kind), **_fields(value, ('enabled', 'mode', 'model', 'dimension', 'pooling', 'device'))}
                for kind, value in (profile.get('retrieval_api') or {}).items() if isinstance(value, dict)]
        rows += [{'kind': kind, **_fields(profile.get(kind), ('enabled', 'model', 'provider', 'device'))}
                 for kind in ('voice_input', 'voice_reply', 'local_chat') if profile.get(kind)]
    query = arguments.get('query', '').casefold()
    rows = [row for row in rows if query in json.dumps(row, ensure_ascii=False).casefold()]
    offset, limit = arguments.get('offset', 0), arguments.get('limit', 20)
    end = offset + limit
    return {'scope': Path(directory).name, 'read_only': True, 'total': len(rows),
            'items': rows[offset:end], 'next_offset': end if end < len(rows) else None}
