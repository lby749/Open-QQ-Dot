"""Qwen Code backend: headless transport, owned here."""
from __future__ import annotations


def capabilities(config=None):
    from .capabilities import AgentCapability
    return AgentCapability(model_supported=True, effort_supported=False,
                           note='Qwen Code 用 --model 选择模型；没有统一思考档位枚举')


def list_models(config=None):
    return [], 'Qwen Code 没有模型枚举命令；请手动填写 --model 支持的名称'


def generate(prompt, directory, profile, *, image_path=None, text_only=False,
             output_schema=None, record_input=None):
    if image_path is not None or text_only or output_schema is not None:
        from agent_tool_adapters import AgentAdapterError
        raise AgentAdapterError('Qwen Code 接入暂不支持图片、纯文本隔离或结构化输出')
    from ._common import prepare, finish
    from .transport_headless import run_headless

    config, workspace, execution = prepare(profile, directory, 'qwen_code', prompt, record_input)
    result = run_headless('qwen_code', prompt, workspace, timeout=600, allow_write=True,
                          model=config.get('model') or '',
                          **({'command': config['command']} if config.get('command') else {}))
    return finish(execution, result)
