"""OpenCode backend: loopback HTTP transport, owned here."""
from __future__ import annotations

import os


def capabilities(config=None):
    from .capabilities import AgentCapability
    return AgentCapability(model_supported=True, effort_supported=True,
                           note='模型与思考档位来自 OpenCode 服务')


def list_models(config=None):
    import os
    from .transport_opencode import LocalOpenCode
    config = config or {}
    client = LocalOpenCode(config.get('url') or '',
                           password=os.environ.get('OPENCODE_SERVER_PASSWORD', ''))
    items = client.list_models()
    return [f"{item['provider']}/{item['id']}" for item in items
            if item.get('enabled')], 'OpenCode /api/model'


def generate(prompt, directory, profile, *, image_path=None, text_only=False,
             output_schema=None, record_input=None):
    if image_path is not None or text_only or output_schema is not None:
        from agent_tool_adapters import AgentAdapterError
        raise AgentAdapterError('OpenCode 接入暂不支持图片、纯文本隔离或结构化输出')
    from ._common import prepare, finish
    from .transport_opencode import LocalOpenCode

    config, workspace, execution = prepare(profile, directory, 'opencode', prompt, record_input)
    try:
        result = LocalOpenCode(config.get('url') or '',
                               password=os.environ.get('OPENCODE_SERVER_PASSWORD', ''),
                               model=config.get('model') or '',
                               variant=config.get('effort') or config.get('variant') or '').run(
            prompt, workspace=workspace, timeout=600)
    except Exception as exc:
        from .transport_opencode import run_cli
        try:
            result = {'output': run_cli(prompt, workspace, config.get('model') or '', timeout=600),
                      'session_id': ''}
        except Exception:
            raise exc
    return finish(execution, result)
