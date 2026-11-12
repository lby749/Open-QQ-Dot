"""Custom ACP backend: ACP stdio transport, owned here."""
from __future__ import annotations


def capabilities(config=None):
    from .capabilities import AgentCapability
    return AgentCapability(model_supported=False, effort_supported=False,
                           note='ACP 会话由 Agent 自己管理模型；没有统一的模型/思考档位枚举')


def list_models(config=None):
    return [], 'ACP 没有模型枚举接口；模型由该 Agent 自身配置'


def generate(prompt, directory, profile, *, image_path=None, text_only=False,
             output_schema=None, record_input=None):
    if image_path is not None or text_only or output_schema is not None:
        from agent_tool_adapters import AgentAdapterError
        raise AgentAdapterError('自定义 ACP 接入暂不支持图片、纯文本隔离或结构化输出')
    from ._common import prepare, finish
    from .transport_acp import run_acp

    config, workspace, execution = prepare(profile, directory, 'acp', prompt, record_input)
    command = config.get('command')
    if not isinstance(command, list):
        from agent_tool_adapters import AgentAdapterError
        raise AgentAdapterError('Generic ACP requires a trusted configured command')
    result = run_acp(command, prompt, workspace, timeout=600,
                     protocol_version=int(config.get('protocol_version', 1)))
    result['backend'] = 'acp'
    return finish(execution, result)
