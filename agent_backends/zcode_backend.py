"""ZCode's bundled CLI, using existing desktop installation and provider settings."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile


def cli_command(config=None):
    config = config or {}
    if config.get('command'):
        return list(config['command'])
    installed = shutil.which('zcode')
    if installed:
        return [installed]
    candidates = [Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ZCode']
    if os.name == 'nt':
        import winreg
        for hive, branch in ((winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
                             (winreg.HKEY_LOCAL_MACHINE, r'Software\Microsoft\Windows\CurrentVersion\Uninstall'),
                             (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')):
            try:
                with winreg.OpenKey(hive, branch) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                name = str(winreg.QueryValueEx(key, 'DisplayName')[0])
                                if not name.startswith('ZCode'):
                                    continue
                                for field in ('InstallLocation', 'DisplayIcon'):
                                    try:
                                        value = str(winreg.QueryValueEx(key, field)[0]).rsplit(',', 1)[0].strip('"')
                                        path = Path(value)
                                        candidates.append(path.parent if field == 'DisplayIcon' else path)
                                    except OSError:
                                        pass
                        except OSError:
                            continue
            except OSError:
                continue
    node = shutil.which('node')
    for home in candidates:
        entry = home / 'resources/glm/zcode.cjs'
        if entry.is_file() and node:
            return [node, str(entry)]
    return []


@contextmanager
def environment(command, config):
    """Select a model per call, without changing the desktop provider or credentials."""
    env = dict(os.environ)
    entry = next((Path(arg) for arg in command if str(arg).endswith('zcode.cjs')), None)
    builtin = (entry.parent.parent / 'config/provider/zcode-builtin.json') if entry else None
    personal = Path(env.get('ZCODE_PERSONAL_PROVIDER_CONFIG_FILE') or Path.home() / '.zcode/v2/provider_config.json')
    if builtin and builtin.is_file():
        env.setdefault('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE', str(builtin))
        env.setdefault('ZCODE_PERSONAL_PROVIDER_CONFIG_FILE', str(personal))
    model = str(config.get('model') or '')
    if not model:
        yield env
        return
    source = json.loads(personal.read_text('utf-8'))
    data = copy.deepcopy(source)
    settings = data.setdefault('config', {})
    rules = (settings.get('providerConfigRules') or {}).get('providerRules') or []
    provider = config.get('provider') or (settings.get('defaultModelSelection') or {}).get('providerId')
    if not provider and len(rules) == 1:
        provider = rules[0]['providerId']
    if not provider:
        raise ValueError('ZCode 有多个模型来源，请明确选择 provider，避免调用错误的账号或模型。')
    rule = next((r for r in rules if r.get('providerId') == provider), None)
    if not rule:
        raise ValueError('所选 ZCode provider 尚未在桌面端配置，未提交模型调用。')
    # Some desktop versions save access overrides but omit the matching template's API definition.
    if rule and not rule.get('config', {}).get('api') and builtin and builtin.is_file():
        def templates(value):
            if isinstance(value, dict):
                if value.get('templateId') == provider and (value.get('config') or {}).get('api'):
                    yield value['config']
                for child in value.values():
                    yield from templates(child)
            elif isinstance(value, list):
                for child in value:
                    yield from templates(child)
        template = next(templates(json.loads(builtin.read_text('utf-8'))), None)
        if template:
            rule['config']['api'] = template['api']
            rule['config']['personalModelIds'] = list(dict.fromkeys([*rule['config'].get('personalModelIds', []), model]))
    settings['defaultModelSelection'] = {'providerId': provider, 'modelId': model}
    with tempfile.TemporaryDirectory(prefix='openqq-zcode-model-', ignore_cleanup_errors=True) as folder:
        path = Path(folder) / 'provider.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        env['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE'] = str(path)
        yield env


def run(instruction, workspace, *, timeout=600, allow_write=False, config=None):
    from agent_tool_adapters import AgentAdapterError, run_headless
    import round_clock
    round_clock.check()
    timeout = round_clock.clamp(timeout)
    config = config or {}
    command = cli_command(config)
    if not command:
        raise AgentAdapterError('未找到 ZCode CLI；已安装桌面版时还需要可用的 Node.js，或填写独立 CLI 路径。')
    with environment(command, config) as env:
        return run_headless('zcode', instruction, workspace, timeout=timeout,
                            allow_write=allow_write, command=command, env=env)


def capabilities(config=None):
    from .capabilities import AgentCapability
    return AgentCapability(model_supported=True, effort_supported=False,
                           note='ZCode 通过独立调用配置选择模型，沿用桌面凭据；思考参数取决于模型来源，不伪造档位。')


def list_models(config=None):
    return [], '填写 ZCode 模型来源支持的真实模型 ID；不会替换桌面默认模型。'


def generate(prompt, directory, profile, *, image_path=None, text_only=False,
             output_schema=None, record_input=None):
    from agent_tool_adapters import AgentAdapterError
    if image_path is not None or text_only or output_schema is not None:
        raise AgentAdapterError('ZCode 当前接入暂不支持图片、纯文本工具隔离或结构化输出。')
    from ._common import prepare, finish
    config, workspace, execution = prepare(profile, directory, 'zcode', prompt, record_input)
    return finish(execution, run(prompt, workspace, allow_write=True, config=config))
