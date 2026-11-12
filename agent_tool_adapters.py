"""Explicitly approved external *agent* execution adapters for Open QQ Dot.

An agent is NOT an API model. This module does not automatically authorize CLI
processes, third-party sessions or their tool calls. TaskManager must enforce
owner, workspace, budget and verification before/after calling ``run_agent``.

Supported: OpenCode local HTTP, Cursor headless CLI, Qwen Code headless CLI,
ZCode headless CLI, Kimi Code/other ACP stdio, and MCP stdio tool discovery and invocation.
No dependency on the provider's Python SDK; native executables must be installed
and logged in separately. No untrusted command line or remote URL overrides.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


class AgentAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentAdapterSpec:
    name: str
    transport: str
    executable: str = ''
    args: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ('code', 'file', 'conversation')
    session_support: bool = False


BUILTINS = {
    'opencode': AgentAdapterSpec('OpenCode', 'http', 'opencode', session_support=True),
    'cursor': AgentAdapterSpec('Cursor Agent CLI', 'headless', 'agent'),
    'qwen_code': AgentAdapterSpec('千问', 'headless', 'qwen', session_support=True),
    'zcode': AgentAdapterSpec('ZCode CLI', 'headless', 'zcode', session_support=True),
    'kimi_code': AgentAdapterSpec('Kimi Code ACP', 'acp', 'kimi', ('acp',), session_support=True),
    'acp': AgentAdapterSpec('自定义智能体（ACP）', 'acp', session_support=True),
    'mcp': AgentAdapterSpec('工具服务（MCP）', 'mcp', capabilities=('tool_discovery', 'tool_call')),
}
BACKEND_NAMES = frozenset(BUILTINS)
AGENT_BACKENDS = tuple(key for key in BUILTINS if key != 'mcp')


def available_adapters() -> list[dict]:
    """Installation availability only. Installed ≠ authenticated ≠ operational."""
    from agent_backends.zcode_backend import cli_command
    return [{'backend': key, 'display_name': spec.name, 'transport': spec.transport,
             'installed': bool(_opencode_cli()) if key == 'opencode' else
                          bool(cli_command()) if key == 'zcode' else
                          bool(shutil.which(spec.executable)) if spec.executable else None,
             'session_support': spec.session_support, 'capabilities': list(spec.capabilities)}
            for key, spec in BUILTINS.items()]


def _workspace(path: str | Path) -> Path:
    p = Path(path).resolve(strict=True)
    if not p.is_dir():
        raise AgentAdapterError('Agent workspace must be an existing directory')
    return p


def _validate_program(program: str) -> str:
    """Never use shell=True; explicit trusted command only, no user prompts as executables."""
    if not isinstance(program, str) or not program or any(c in program for c in '\n\r\0'):
        raise AgentAdapterError('Invalid executable')
    found = shutil.which(program)
    if not found:
        raise AgentAdapterError('Agent executable is not installed or not on PATH')
    return found


def _parse_output(stdout: str, backend: str) -> tuple[str, str, int]:
    """Extract actual provider result, fail closed on error envelopes."""
    raw = stdout.strip()
    if not raw:
        raise AgentAdapterError(f'{backend} exited without a result')
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # Some CLIs emit plain text in older versions. Do not mistake a crash for success.
        if raw.startswith('{') or raw.startswith('['):
            raise AgentAdapterError('Invalid JSON result from agent') from None
        return raw, '', 0
    if isinstance(obj, list):
        errors = [e for e in obj if isinstance(e, dict) and e.get('type') == 'result' and e.get('is_error')]
        if errors:
            raise AgentAdapterError('Qwen Code returned an error result')
        results = [e for e in obj if isinstance(e, dict) and e.get('type') == 'result']
        if not results:
            raise AgentAdapterError('Agent returned no final result event')
        obj = results[-1]
    if not isinstance(obj, dict) or obj.get('is_error') is True or obj.get('subtype') in ('error', 'failure'):
        raise AgentAdapterError('Agent returned an error envelope')
    text = obj.get('response') or obj.get('result') or obj.get('text') or obj.get('output')
    if not isinstance(text, str) or not text.strip():
        raise AgentAdapterError('Agent returned no final text')
    usage = obj.get('usage') or {}
    tokens = 0
    if isinstance(usage, dict):
        for key in ('total_tokens', 'totalTokens'):
            if type(usage.get(key)) is int:
                tokens = usage[key]
                break
        if not tokens:
            tokens = sum(usage.get(k, 0) for k in ('input_tokens', 'output_tokens') if type(usage.get(k)) is int)
    return text, str(obj.get('session_id') or obj.get('sessionId') or ''), tokens


def run_headless(backend: str, instruction: str, workspace: str | Path, *,
                 timeout: float = 600, allow_write: bool = False, command: list[str] | None = None,
                 env: dict | None = None) -> dict:
    """Official non-interactive CLI, bounded execution, no shell, no YOLO defaults."""
    if backend not in ('cursor', 'qwen_code', 'zcode'):
        raise AgentAdapterError('Unsupported headless backend')
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 50000:
        raise AgentAdapterError('Invalid instruction')
    cwd = _workspace(workspace)
    if not 1 <= timeout <= 3600:
        raise AgentAdapterError('Invalid execution timeout')
    try:
        program = _validate_program(command[0] if command else BUILTINS[backend].executable)
    except AgentAdapterError:
        if backend != 'cursor' or command:
            raise
        program = _validate_program('cursor-agent')
    if backend == 'cursor':
        # Cursor -p accepts the prompt as argv, and --force explicitly allows edits.
        # Never use --force unless TaskManager authorized code writes.
        argv = [program, *(command[1:] if command else []), '-p', instruction, '--output-format', 'json']
        if allow_write:
            argv.append('--force')
        data = None
    elif backend == 'zcode':
        # ZCode's official non-interactive mode.  It has no read-only switch:
        # plan only plans; yolo may execute tools and write files, so it is only
        # selected after TaskManager's explicit write authorization.
        argv = [program, *(command[1:] if command else []),
                '--prompt', instruction, '--cwd', str(cwd),
                '--mode', 'yolo' if allow_write else 'plan',
                '--json', '--no-color']
        data = None
    else:
        # Qwen supports prompt via stdin; avoids exposing prompts in process argv.
        argv = [program, *(command[1:] if command else []), '--output-format', 'json']
        data = instruction
    try:
        from task_runtime import run_process
        run = run_process(argv, input=data, cwd=str(cwd), capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise InterruptedError(f'{backend} 调用超时，执行结果尚未确认；请核对产物，不自动重复提交。') from exc
    if run.returncode != 0:
        # stderr can contain secrets or the original prompt; do not forward it to QQ/logs.
        raise AgentAdapterError(f'{backend} exited with code {run.returncode}; inspect provider logs locally')
    output, session, tokens = _parse_output(run.stdout, backend)
    return {'output': output, 'session_id': session, 'tokens_used': tokens,
            'backend': backend, 'verified': False, 'transport': 'headless'}


def _opencode_cli() -> str:
    from agent_backends.transport_opencode import _opencode_cli as find
    return find()


def __getattr__(name):
    """Legacy imports resolve to the production transport, including direct transport imports.

    Lazy resolution avoids a cycle: transports import the error and permission
    helpers above. There is only one HTTP / JSON-RPC implementation to test.
    """
    if name in {'LocalOpenCode', 'OPENCODE_DEFAULT_URL', 'opencode_discovery', '_loopback_url',
                '_message_text', '_opencode_model', '_opencode_variant', '_opencode_error'}:
        from agent_backends import transport_opencode
        return getattr(transport_opencode, name)
    if name in {'StdioJsonRpc', 'run_acp'}:
        from agent_backends import transport_acp
        return getattr(transport_acp, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def mcp_list_tools(command: list[str], workspace: str | Path, *, timeout: float = 15) -> list[dict]:
    from agent_backends.transport_acp import StdioJsonRpc
    with StdioJsonRpc(command, workspace, timeout=timeout) as rpc:
        rpc.request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'Open QQ Dot', 'version': '1.0'}})
        rpc._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        tools, cursors, cursor = [], set(), None
        deadline = time.monotonic() + timeout
        for _ in range(100):
            if time.monotonic() >= deadline:
                raise AgentAdapterError('MCP 工具目录读取超时')
            result = rpc.request('tools/list', {'cursor': cursor}) if cursor else rpc.request('tools/list')
            if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
                raise AgentAdapterError('MCP server returned no tools list')
            tools.extend(result['tools'])
            if len(tools) > 10000:
                raise AgentAdapterError('MCP 工具目录超过数量限制')
            cursor = result.get('nextCursor')
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise AgentAdapterError('MCP 工具目录分页游标无效或重复')
            cursors.add(cursor)
        raise AgentAdapterError('MCP 工具目录超过分页限制')



def mcp_call_tool(command: list[str], workspace: str | Path, name: str, arguments: dict, *,
                  timeout: float = 30) -> dict:
    from agent_backends.transport_acp import StdioJsonRpc
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        raise AgentAdapterError('Invalid MCP tool invocation')
    with StdioJsonRpc(command, workspace, timeout=timeout) as rpc:
        rpc.request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'Open QQ Dot', 'version': '1.0'}})
        rpc._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        result = rpc.request('tools/call', {'name': name, 'arguments': arguments})
        if not isinstance(result, dict) or result.get('isError'):
            raise AgentAdapterError('MCP server reported tool failure')
        return result


def run_agent(backend: str, instruction: str, workspace: str | Path, *,
              timeout: float = 600, allow_write: bool = False, config: dict | None = None) -> dict:
    """Dispatch only explicitly named/trusted backends; returns unverified output."""
    if backend not in BACKEND_NAMES or backend == 'mcp':
        raise AgentAdapterError('Invalid standalone agent backend')
    config = config or {}
    command = config.get('command')
    if command is not None:
        validate_command(command)
    # This config must come from trusted host settings, NOT user request/checkpoint.
    if backend == 'zcode':
        from agent_backends.zcode_backend import run
        return run(instruction, workspace, timeout=timeout, allow_write=allow_write, config=config)
    if backend in ('cursor', 'qwen_code'):
        return run_headless(backend, instruction, workspace, timeout=timeout, allow_write=allow_write,
                            **({'command': command} if command else {}))
    if backend == 'opencode':
        from agent_backends.transport_opencode import LocalOpenCode as _LocalOpenCode
        password = os.environ.get('OPENCODE_SERVER_PASSWORD', '')
        return _LocalOpenCode(config.get('url') or '', password=password,
                              model=config.get('model') or '',
                              variant=config.get('effort') or config.get('variant') or '').run(
            instruction, workspace=workspace, timeout=timeout)
    if backend == 'kimi_code':
        argv = command or ['kimi', 'acp']
    else:
        # Generic ACP requires trusted static administrator configuration.
        argv = config.get('command')
        if not isinstance(argv, list):
            raise AgentAdapterError('Generic ACP requires a trusted configured command')
    # A shared profile session_id is not owner-scoped. Always allocate a new
    # task-local session until durable user-bound session storage is available.
    from agent_backends.transport_acp import run_acp as _run_acp
    result = _run_acp(argv, instruction, workspace, timeout=timeout,
                      protocol_version=int(config.get('protocol_version', 1)))
    result['backend'] = backend
    return result


def validate_command(command):
    if (not isinstance(command, list) or not command or len(command) > 64 or
            any(not isinstance(arg, str) or len(arg) > 4096 or any(c in arg for c in '\r\n\0') for arg in command)
            or not command[0].strip()):
        raise ValueError('启动命令须为程序路径与参数组成的非空字符串数组')


def require_native_permissions(profile):
    from task_runtime import current_execution
    execution = current_execution.get()
    permissions = execution.effective_permissions() if execution else (profile or {}).get('computer_tools', {})
    if not (permissions.get('enabled') and permissions.get('commands') and
            permissions.get('full_computer_access') and not permissions.get('read_only') and
            permissions.get('mode') != 'read_only'):
        raise PermissionError('请先在项目权限中授权命令执行及完整电脑访问；外部工具没有经过验证的工作区沙箱')
    if execution:
        execution.check()


def generate_text(prompt, directory, profile, *, image_path=None, text_only=False, output_schema=None,
                  record_input=None):
    """Use the same trusted adapter path from chat, console and orchestration."""
    if image_path is not None or text_only or output_schema is not None:
        raise AgentAdapterError('此接入暂不支持图片、强制纯文本隔离或结构化输出协议，请选择其他智能体')
    require_native_permissions(profile)
    from task_runtime import current_execution
    import workspace_tools
    backend = profile['model_api']['provider']
    config = (profile.get('agent_adapters') or {}).get(backend) or {}
    execution = current_execution.get()
    workspace = (execution.workspace() if execution else
                 (profile.get('computer_tools') or {}).get('project_path') or workspace_tools.workspace(directory))
    Path(workspace).mkdir(parents=True, exist_ok=True)
    if record_input:
        record_input(prompt, {'provider': backend, 'input_scope': 'initial_user_message.text',
                              'tool_followups_recorded': False})
    result = run_agent(backend, prompt, workspace, config=config, allow_write=True)
    if execution:
        execution.check()
    return result['output'], result.get('session_id') or None


def probe_adapter(backend, config, workspace):
    """Explicit desktop connectivity check; does not submit an agent task."""
    if backend == 'opencode':
        from agent_backends.transport_opencode import LocalOpenCode
        client = LocalOpenCode(config.get('url') or '',
                               password=os.environ.get('OPENCODE_SERVER_PASSWORD', ''),
                               model=config.get('model') or '',
                               variant=config.get('variant') or '')
        info = client.info()
        where = client.server_workspace()
        version = info.get('version')
        return ((f'服务连接正常（OpenCode {version}，{client.base_url()}）；'
                 f'服务工作目录：{where}') if version else
                ('服务连接正常（v1 服务）；服务工作目录：' + where))
    command = config.get('command')
    if command:
        validate_command(command)
    elif backend == 'zcode':
        from agent_backends.zcode_backend import cli_command
        command = cli_command(config)
    else:
        spec = BUILTINS[backend]
        command = [spec.executable, *spec.args] if spec.executable else None
    if not command:
        raise ValueError('请先设置程序路径和启动参数')
    if backend in ('cursor', 'qwen_code', 'zcode'):
        try:
            found = _validate_program(command[0])
        except AgentAdapterError:
            if backend != 'cursor' or config.get('command'):
                raise
            found = _validate_program('cursor-agent')
        return '已检测到程序：' + found + '；登录状态需通过实际任务确认'
    if backend == 'mcp':
        tools = mcp_list_tools(command, workspace)
        return '工具服务握手成功，可用工具：' + ('、'.join(str(t.get('name')) for t in tools) or '无')
    from agent_backends.transport_acp import StdioJsonRpc
    with StdioJsonRpc(command, workspace, timeout=15) as rpc:
        version = int(config.get('protocol_version', 1))
        initialization = ({'protocolVersion': 2, 'capabilities': {},
                           'info': {'name': 'Open QQ Dot', 'title': 'Open QQ Dot', 'version': '1'}}
                          if version == 2 else {'protocolVersion': 1, 'clientCapabilities': {}})
        rpc.request('initialize', initialization)
    return '智能体协议握手成功；尚未提交执行任务'


def mcp_server_configs(directory):
    """Every enabled MCP tool server of this friend, keyed by a CLI-safe name.

    2026-10-26 user: 工具服务不可能只接一个。旧配置（单个 command）仍然照常读，
    新的 `servers` 列表可以同时接多个；每个都要单独校验命令，不能因为一个坏了就全部不接。
    """
    try:
        profile = json.loads((Path(directory) / 'friend.json').read_text('utf-8'))
    except (OSError, ValueError):
        return {}
    config = (profile.get('agent_adapters') or {}).get('mcp') or {}
    raw = config.get('servers')
    entries = []
    if isinstance(raw, list) and raw:
        for item in raw:
            if isinstance(item, dict):
                entries.append(item)
    elif config.get('command'):
        # 兼容旧配置：单个服务器。
        entries.append({'name': 'external_tools', 'command': config.get('command'),
                        'enabled': config.get('enabled', True)})
    if not any(item.get('enabled', True) for item in entries):
        return {}
    require_native_permissions(profile)
    servers = {}
    for index, item in enumerate(entries):
        if not item.get('enabled', True):
            continue
        command = item.get('command')
        try:
            validate_command(command)
        except (TypeError, ValueError, AgentAdapterError):
            continue  # 一个坏的不影响其它的
        name = re.sub(r'[^0-9A-Za-z_-]', '_', str(item.get('name') or f'tool_{index + 1}'))[:32] or f'tool_{index + 1}'
        if name in ('qq_topic_library', 'qq_phone', 'agent_mailbox'):
            name = 'external_' + name
        stem = name
        suffix = 2
        while name in servers:
            name = f'{stem}_{suffix}'
            suffix += 1
        servers[name] = {'command': _validate_program(command[0]), 'args': list(command[1:])}
    return servers


def mcp_server_config(directory):
    """Backwards-compatible single server (the first enabled one)."""
    servers = mcp_server_configs(directory)
    return next(iter(servers.values()), None)
