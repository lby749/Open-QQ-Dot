"""DeepSeek Harness (dsh) backend: native desktop client and headless CLI execution.

Supports invoking DeepSeek Harness directly through the installed desktop client runtime
(D:\\dsh\\resources\\runtime\\cli\\bin\\dsh.cmd) using the user's logged-in desktop account,
as well as optional dual-mode execution via standard DeepSeek API endpoints.
"""
from __future__ import annotations

import json
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
    """Fetch live available models for DeepSeek Harness (DeepSeek )."""
    return list(DEFAULT_MODELS)


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
    from api_backend import key_path

    kfile = key_path(directory, 'dsh')
    has_api_key = kfile.is_file() and bool(kfile.read_text('utf-8').strip())

    # If CLI is available and user didn't force a remote API key, run native CLI
    if cli:
        cmd = [cli, 'headless']
        patch = desktop_patch_path()
        if patch:
            cmd.extend(['--patch', str(patch)])

        temp_patch: Path | None = None
        chosen_model = api.get('model') or (api.get('dsh_settings') or {}).get('model')
        chosen_effort = '' if _no_effort else (api.get('api_effort') or (api.get('dsh_settings') or {}).get('effort'))
        if chosen_model or chosen_effort:
            try:
                import tempfile
                cfg_lines = []
                if chosen_model:
                    # Keep the desktop's configured provider; model names cannot identify an API route.
                    provider = (api.get('dsh_settings') or {}).get('provider')
                    if provider:
                        cfg_lines.append('    provider: ' + json.dumps(str(provider)))
                    cfg_lines.append('    model: ' + json.dumps(str(chosen_model)))
                if chosen_effort:
                    cfg_lines.append(f"    reasoningEffort: {chosen_effort}")
                if cfg_lines:
                    tfile = tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False, encoding='utf-8')
                    tfile.write("- id: agent-default-model\n  name: '@deepseek-ai/dsh-agent-default-model'\n  config:\n" + "\n".join(cfg_lines) + "\n")
                    tfile.close()
                    temp_patch = Path(tfile.name)
                    cmd.extend(['--patch', str(temp_patch)])
            except Exception:
                pass

        # The task goes through stdin ("-"): dsh.cmd runs under cmd.exe, which cuts an argument at its first line
        # break (and at 8191 characters), so the model only ever saw a prompt's first line.
        env = dict(os.environ)
        from workspace_tools import enabled
        permission = enabled(directory)
        if not (permission.get('enabled') is True and permission.get('commands') is True and permission.get('full_computer_access') is True and permission.get('mode') != 'read_only'):
            raise PermissionError(
                'DSH 原生执行还没有可验证的目录隔离：需要「好友 → 本地电脑工作台与项目权限」里允许命令并开放完全访问，'
                '或填写 DeepSeek API Key 让这条消息走兼容 API。'
                + ('' if has_api_key else '（当前没有可用的 DSH API Key，所以没有自动改走 API。）'))
        mode = str(approval_mode or permission.get('approval_mode') or 'danger-full-access')
        if mode not in ('read-only', 'workspace-write', 'danger-full-access'):
            raise ValueError('DSH 权限配置无效，请选择只读、工作区写入或完全访问。')
        env['DSH_PERMISSION_MODE'] = mode
        cmd.extend(['--permission-mode', env['DSH_PERMISSION_MODE'], '--approval-policy', 'never'])
        cmd.append('-')
        env.update(load_credentials_env())

        if has_api_key:
            env['DEEPSEEK_API_KEY'] = kfile.read_text('utf-8').strip()

        execution = current_execution.get()
        from workspace_tools import workspace
        working_directory = execution.workspace() if execution else workspace(directory)
        working_directory.mkdir(parents=True, exist_ok=True)
        import round_clock
        round_clock.check()
        if execution:
            execution.before_call(prompt)
        try:
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
                    return generate(prompt, directory, api, dsh_path, True)
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
    if 'api_base_url' not in api_cfg:
        api_cfg['api_base_url'] = 'https://api.deepseek.com'
    if 'api_protocol' not in api_cfg:
        api_cfg['api_protocol'] = 'openai-completions'
    text = api_backend.generate(prompt, directory, api_cfg)
    return text, {'provider': 'dsh', 'native': False}
