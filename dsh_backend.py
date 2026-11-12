"""DeepSeek Harness (dsh) backend: native desktop client and headless CLI execution.

Supports invoking DeepSeek Harness directly through the installed desktop client runtime
(D:\\dsh\\resources\\runtime\\cli\\bin\\dsh.cmd) using the user's logged-in desktop account,
as well as optional dual-mode execution via standard DeepSeek API endpoints.
"""
from __future__ import annotations

import os
import re
import shutil
from task_runtime import run_process, current_execution, reported_usage
import subprocess
from pathlib import Path

ROOT = Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))
DEFAULT_MODELS = (
    'DeepSeek-V41-Flash',
    'DeepSeek-V4-Pro',
    'deepseek-chat',
    'deepseek-reasoner',
)
EFFORTS = ('off', 'low', 'medium', 'high', 'max')


def cli_path(custom_path: str | None = None) -> str | None:
    """Locate the dsh CLI executable on the local Windows system."""
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_file():
            # If user selected DeepSeek Harness.exe, redirect to internal CLI
            if p.name.lower() in ('deepseek harness.exe', 'deepseek.exe'):
                cli_sibling = p.parent / 'resources' / 'runtime' / 'cli' / 'bin' / 'dsh.cmd'
                if cli_sibling.is_file():
                    return str(cli_sibling.resolve())
            return str(p.resolve())
        if p.is_dir():
            for name in ('dsh.cmd', 'dsh.exe', 'dsh.bat', 'dsh'):
                candidate = p / name
                if candidate.is_file():
                    return str(candidate.resolve())
            for name in ('resources/runtime/cli/bin/dsh.cmd', 'resources/runtime/cli/bin/dsh'):
                candidate = p / name
                if candidate.is_file():
                    return str(candidate.resolve())

    candidates = [
        Path(r'D:\dsh\resources\runtime\cli\bin\dsh.cmd'),
        Path(r'D:\dsh\resources\runtime\cli\bin\dsh'),
        Path(r'C:\Program Files\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'),
        Path(r'C:\Users\Administrator\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd'),
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())

    return shutil.which('dsh')


def desktop_patch_path() -> Path | None:
    """Return the desktop profile's cordis.patch.yml if available."""
    desktop_patch = Path.home() / '.dsh' / 'profiles' / 'desktop' / 'cordis.patch.yml'
    return desktop_patch if desktop_patch.is_file() else None


def fetch_available_models(custom_path: str | None = None) -> list[str]:
    """Read the installed Harness catalogue, rather than this project's defaults."""
    return list(fetch_model_catalog(custom_path)['models'])


def _read_cli_config(cli):
    command = [cli, 'headless', '--dump-config']
    patch = desktop_patch_path()
    if patch:
        command.extend(['--patch', str(patch)])
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('DSH 未能读取当前配置，请检查程序路径与桌面端配置')
    return result.stdout


def _model_patch(config, model, effort, provider=None):
    import yaml
    from yaml.nodes import MappingNode, SequenceNode, ScalarNode
    # Compose/serialize preserves !!js expressions and scalar types without executing constructors.
    tree = yaml.compose(config)
    def find(node):
        if isinstance(node, MappingNode):
            fields = {key.value: value for key, value in node.value}
            if getattr(fields.get('id'), 'value', None) == 'agent-default-model':
                return fields.get('config')
            children = (value for _, value in node.value)
        else:
            children = node.value if isinstance(node, SequenceNode) else ()
        for child in children:
            found = find(child)
            if found is not None:
                return found
    current = find(tree)
    if not isinstance(current, MappingNode):
        raise ValueError('DSH 当前配置缺少默认模型设置，未执行任务')
    overrides = {'provider': provider, 'model': model, 'reasoningEffort': effort}
    for key, value in overrides.items():
        if key == 'reasoningEffort' and value is None:
            current.value = [(k, v) for k, v in current.value if k.value != key]
        if value:
            current.value = [(k, v) for k, v in current.value if k.value != key]
            current.value.append((ScalarNode('tag:yaml.org,2002:str', key),
                                  ScalarNode('tag:yaml.org,2002:str', str(value))))
    if not any(key.value == 'provider' and value.value for key, value in current.value):
        raise ValueError('DSH 当前配置缺少模型来源，请先在桌面端选择来源')
    patch = yaml.compose("- id: agent-default-model\n  name: '@deepseek-ai/dsh-agent-default-model'\n  config: {}\n")
    patch.value[0].value[-1] = (patch.value[0].value[-1][0], current)
    return yaml.serialize(patch, allow_unicode=True)


def fetch_model_catalog(custom_path=None, *, provider=None, base_url='', api_key=''):
    cli = cli_path(custom_path)
    if not cli:
        if not api_key:
            raise RuntimeError('未找到 DSH CLI；请配置程序路径，或填写 API 地址与密钥')
        from api_profiles import discover_models
        models = discover_models(base_url or 'https://api.deepseek.com', 'openai-completions', api_key)
        return {'models': {model: [] for model in models}, 'source': 'DSH API 返回的模型目录；思考档位由服务默认配置决定'}
    import yaml
    # BaseLoader treats the Harness !!js expressions as inert strings, never executable YAML.
    try:
        tree = yaml.load(_read_cli_config(cli), Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        raise ValueError('DSH 返回的配置格式无法识别') from None
    entries = {}
    def visit(value):
        if isinstance(value, dict):
            if value.get('id') in ('agent-default-model', 'llm-pi-ai'):
                entries[value['id']] = value.get('config') or {}
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(tree)
    provider = provider or entries.get('agent-default-model', {}).get('provider')
    providers = entries.get('llm-pi-ai', {}).get('providers') or {}
    if not provider and len(providers) == 1:
        provider = next(iter(providers))
    if provider not in providers:
        raise ValueError('DSH 未返回当前模型来源的目录，请先在桌面端选择模型来源')
    config = providers[provider]
    models = {}
    for item in config.get('models') or []:
        if not isinstance(item, dict) or not item.get('id') or item.get('enabled') == 'false':
            continue
        overrides = (config.get('modelOverrides') or {}).get(item['id']) or {}
        efforts = overrides.get('reasoningEfforts', item.get('reasoningEfforts'))
        models[item['id']] = list(efforts) if isinstance(efforts, dict) else []
    if not models:
        raise ValueError('DSH 当前来源没有返回模型，未使用预置列表代替')
    return {'models': models, 'default_model': entries.get('agent-default-model', {}).get('model', ''),
            'source': 'DSH 当前来源 ' + str(provider) + ' 的配置目录；未声明的思考档位沿用工具默认'}


def load_credentials_env() -> dict[str, str]:
    """Read credentials from ~/.dsh/.credentials.yaml into an environment mapping."""
    env = {}
    cred_file = Path.home() / '.dsh' / '.credentials.yaml'
    if not cred_file.is_file():
        return env
    try:
        content = cred_file.read_text(encoding='utf-8', errors='ignore')
        # match refs: { KEY: value }
        for match in re.finditer(r'([A-Za-z0-9_]+)\s*:\s*([a-zA-Z0-9_\-]+)', content):
            k, v = match.group(1), match.group(2)
            if k not in ('version', 'records', 'kind', 'payload', 'id', 'token', 'issuer', 'refs'):
                env[k] = v
        # token for platform
        token_match = re.search(r'token:\s*([a-zA-Z0-9_\-]+)', content)
        if token_match:
            env['DEEPSEEK_ACCOUNT_TOKEN'] = token_match.group(1)
    except Exception:
        pass
    return env


def generate(prompt: str, directory: Path, api: dict | None = None,
             dsh_path: str | None = None, _no_effort: bool = False,
             approval_mode: str | None = None) -> tuple[str, dict]:
    """Execute a task with DeepSeek Harness, either via native client CLI or API fallback."""
    api = api or {}
    custom = dsh_path or api.get('dsh_path')
    cli = cli_path(custom)
    from api_backend import key_path, load_key

    kfile = key_path(directory, 'dsh')
    has_api_key = kfile.is_file()
    dsh_config = api.get('dsh_settings') or {}
    chosen_model = dsh_config.get('model') or api.get('model')
    chosen_effort = '' if _no_effort else (dsh_config.get('effort') or api.get('api_effort'))

    if cli:
        from workspace_tools import enabled
        permission = enabled(directory)
        native_allowed = (permission.get('enabled') is True and permission.get('commands') is True
                          and permission.get('full_computer_access') is True and permission.get('mode') != 'read_only')
        if not native_allowed:
            if has_api_key:
                cli = None  # Explicit API credentials work without granting native host access.
            else:
                raise PermissionError(
                    'DSH 原生执行还没有可验证的目录隔离：需要「好友 → 本地电脑工作台与项目权限」里允许命令并开放完全访问，'
                    '或填写 DeepSeek API Key 让这条消息走兼容 API。'
                    '（当前没有可用的 DSH API Key，所以没有自动改走 API。）')

    # If CLI is available and user didn't force a remote API key, run native CLI
    if cli:
        cmd = [cli, 'headless']
        patch = desktop_patch_path()
        if patch:
            cmd.extend(['--patch', str(patch)])

        # The task goes through stdin ("-"): dsh.cmd runs under cmd.exe, which cuts an argument at its first line
        # break (and at 8191 characters), so the model only ever saw a prompt's first line.
        env = dict(os.environ)
        mode = str(approval_mode or permission.get('approval_mode') or 'danger-full-access')
        if mode not in ('read-only', 'workspace-write', 'danger-full-access'):
            raise ValueError('DSH 权限配置无效，请选择只读、工作区写入或完全访问。')
        env['DSH_PERMISSION_MODE'] = mode
        env.update(load_credentials_env())

        if has_api_key:
            env['DEEPSEEK_API_KEY'] = load_key(directory, 'dsh')

        execution = current_execution.get()
        from workspace_tools import workspace
        working_directory = execution.workspace() if execution else workspace(directory)
        working_directory.mkdir(parents=True, exist_ok=True)
        import round_clock
        round_clock.check()
        if execution:
            execution.before_call(prompt)
        temp_patch: Path | None = None
        try:
            if chosen_model or chosen_effort or dsh_config.get('provider'):
                import tempfile
                # Cordis replaces the plugin config: retain the resolved desktop settings first.
                patch_text = _model_patch(_read_cli_config(cli), chosen_model, None if _no_effort else chosen_effort,
                                          dsh_config.get('provider'))
                with tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False, encoding='utf-8') as tfile:
                    temp_patch = Path(tfile.name)
                    tfile.write(patch_text)
                cmd.extend(['--patch', str(temp_patch)])
            # Launcher overlays must precede app flags, which start Harness's headless argument parser.
            cmd.extend(['--permission-mode', env['DSH_PERMISSION_MODE'], '--approval-policy', 'never', '-'])
            res = run_process(
                cmd,
                input=prompt,
                encoding='utf-8',
                errors='replace',
                capture_output=True,
                text=True,
                timeout=round_clock.clamp(300),
                cwd=str(working_directory.resolve()),
                env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            )
            if execution:
                execution.after_call(res.stdout, usage=reported_usage(res.stdout))
            output = (res.stdout or '').strip()
            err = (res.stderr or '').strip()
            if res.returncode == 0 and output:
                # filter out json events if any
                lines = [line for line in output.splitlines() if not line.strip().startswith('{"type":')]
                final_text = '\n'.join(lines).strip() or output
                return final_text, {'provider': 'dsh', 'native': True}
            elif res.returncode == 0:
                raise InterruptedError('DSH 未返回结果，执行状态尚未确认；请先核对产物，不自动重复发送任务。')
            elif res.returncode != 0 and 'MISSING_CREDENTIAL' in (output + err) and has_api_key:
                # Fall through to API mode
                pass
            elif res.returncode != 0:
                msg = err or output or f'DeepSeek Harness exit code {res.returncode}'
                if 'UNSUPPORTED_REASONING_EFFORT' in msg and chosen_effort:
                    # 2026-10-26: this model has no effort tiers (DeepSeek-V41-Flash); retry once without it
                    # instead of leaving the user with a model-capability error.
                    return generate(prompt, directory, api, dsh_path, True, approval_mode=approval_mode)
                if 'MISSING_CREDENTIAL' in msg and not has_api_key:
                    raise RuntimeError("DeepSeek Harness 尚未在桌面端登录或未检测到可用凭据。请在桌面端登录账号，或在设置中填写 API Key。")
                raise RuntimeError(f"DeepSeek Harness invocation error: {msg}")
        except subprocess.TimeoutExpired as exc:
            raise InterruptedError('DSH 调用超时，执行状态尚未确认；请先核对产物，不自动重复发送任务。') from exc
        finally:
            if temp_patch and temp_patch.is_file():
                try:
                    temp_patch.unlink(missing_ok=True)
                except Exception:
                    pass

    # API fallback
    import api_backend
    api_cfg = dict(api)
    api_cfg['api_key_id'] = 'dsh'
    # Legacy single-source DSH settings used the flat API fields. Once a
    # dedicated DSH connection exists, never borrow another provider's URL.
    legacy = api if not dsh_config and api.get('provider') in ('dsh', 'deepseek_harness') else {}
    api_cfg['api_base_url'] = dsh_config.get('base_url') or legacy.get('api_base_url') or 'https://api.deepseek.com'
    api_cfg['api_protocol'] = dsh_config.get('protocol') or legacy.get('api_protocol') or 'openai-completions'
    if chosen_model:
        api_cfg['model'] = chosen_model
    api_cfg['api_effort'] = chosen_effort or ('off' if _no_effort else 'low')
    text = api_backend.generate(prompt, directory, api_cfg)
    return text, {'provider': 'dsh', 'native': False}
