"""One backend route for native settings, chat, orchestration and MCP tools."""
import json
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import agent_tool_adapters as adapters
from tests.test_agent_tool_adapters import rpc_executable


def profile_for(tmp_path, backend='acp', command=None):
    profile = {'qq_id': '12345', 'model_api': {'provider': backend},
               'computer_tools': {'enabled': True, 'commands': True, 'full_computer_access': True,
                                  'project_path': str(tmp_path)},
               'agent_adapters': {backend: {'command': command or [sys.executable]}}}
    (tmp_path / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    return profile


@pytest.mark.parametrize('backend', adapters.AGENT_BACKENDS)
def test_every_adapter_uses_main_router_and_shared_registry(tmp_path, backend):
    import main
    from model_dispatcher import DEFAULT_MODEL_SPECS
    from multi_agent_orchestrator import AGENT_SPECS
    profile = profile_for(tmp_path, backend)
    with patch('agent_backends.generate', return_value=('实际路由结果', 'session')) as run:
        result = main.codex_text('测试统一智能体路径', tmp_path, ephemeral=True, profile=profile)
    assert result == ('实际路由结果', 'session')
    assert run.call_args.args[:3] == (backend, '测试统一智能体路径', tmp_path)
    assert run.call_args.args[3]['agent_adapters'][backend] == profile['agent_adapters'][backend]
    assert backend in DEFAULT_MODEL_SPECS and backend in AGENT_SPECS


def test_real_acp_subprocess_through_main_router(tmp_path, rpc_executable):
    import main
    profile = profile_for(tmp_path, command=rpc_executable)
    text, session = main.codex_text('不要写文件，仅返回文字', tmp_path, ephemeral=True, profile=profile)
    assert text == 'Real JSON-RPC mock reply'
    assert session == 'test-session'


@pytest.mark.parametrize('change', [{'commands': False}, {'enabled': False}, {'full_computer_access': False},
                                   {'read_only': True}, {'mode': 'read_only'}])
def test_unconfined_native_agent_respects_existing_permissions(tmp_path, change):
    profile = profile_for(tmp_path)
    profile['computer_tools'].update(change)
    with patch.object(adapters, 'run_agent') as run, pytest.raises(PermissionError):
        adapters.generate_text('不应启动', tmp_path, profile)
    run.assert_not_called()


def test_custom_cursor_command_does_not_become_a_shell_command(tmp_path):
    command = [sys.executable, '-u', 'custom_agent.py']
    with patch('task_runtime.run_process', return_value=SimpleNamespace(returncode=0, stdout='{"result":"done"}')) as run:
        adapters.run_agent('cursor', 'test & echo bad', tmp_path, config={'command': command})
    argv = run.call_args.args[0]
    assert argv[:3] == [sys.executable, '-u', 'custom_agent.py']
    assert 'test & echo bad' in argv
    assert not run.call_args.kwargs.get('shell', False)


def test_mcp_configuration_reaches_claude_and_respects_permissions(tmp_path, rpc_executable):
    import claude_backend
    profile = profile_for(tmp_path)
    profile['agent_adapters']['mcp'] = {'enabled': True, 'command': rpc_executable}
    (tmp_path / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    config = json.loads(claude_backend._mcp_config(tmp_path))['mcpServers']['external_tools']
    assert config['command'] == rpc_executable[0]
    assert config['args'] == rpc_executable[1:]
    assert 'readme' in adapters.probe_adapter('mcp', profile['agent_adapters']['mcp'], tmp_path)
    profile['computer_tools']['commands'] = False
    (tmp_path / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    with pytest.raises(PermissionError):
        adapters.mcp_server_config(tmp_path)


def test_execution_permissions_respect_native_read_only_mode(tmp_path):
    from task_runtime import Execution
    profile = profile_for(tmp_path)
    profile['computer_tools']['mode'] = 'read_only'
    (tmp_path / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    manager = SimpleNamespace(workspace=tmp_path, get_task=lambda _: SimpleNamespace(owner_id='default'))
    assert Execution(manager, 'task', 'run', Event()).effective_permissions()['read_only'] is True


def test_external_master_does_not_bypass_enabled_collaboration(tmp_path):
    from task_manager import TaskManager
    profile = profile_for(tmp_path, 'qwen_code')
    profile['model_api']['multi_agent'] = {'enabled': True, 'master': 'qwen_code',
                                         'active_providers': ['qwen_code', 'codex']}
    (tmp_path / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    manager = object.__new__(TaskManager)
    manager.workspace = tmp_path
    task = SimpleNamespace(category='code', checkpoint={}, description='test collaboration')
    with patch.object(manager, '_execute_external_agent') as direct, \
            patch('multi_agent_orchestrator.orchestrate', return_value=('协同执行结果', None)) as orchestrate:
        result = manager._default_orchestrated_executor(task, lambda _: None)
    direct.assert_not_called()
    orchestrate.assert_called_once()
    assert result['output'] == '协同执行结果'
