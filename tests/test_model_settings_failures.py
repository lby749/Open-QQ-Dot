"""Regression cases reproduced during release UI/configuration acceptance."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_dsh_native_model_override_retains_the_resolved_provider_and_config(tmp_path, monkeypatch):
    import dsh_backend
    import workspace_tools
    import yaml
    config = '''
- id: agent-default-model
  config:
    provider: connected-source
    model: old-model
    reasoningEffort: high
    temperature: 0.3
    enabled: true
    customExpression: !!js 'not_evaluated()'
'''
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: 'fixture-dsh')
    monkeypatch.setattr(dsh_backend, 'desktop_patch_path', lambda: None)
    monkeypatch.setattr(dsh_backend, '_read_cli_config', lambda _: config)
    monkeypatch.setattr(dsh_backend, 'load_credentials_env', lambda: {})
    monkeypatch.setattr(workspace_tools, 'enabled', lambda _: {
        'enabled': True, 'commands': True, 'full_computer_access': True})
    monkeypatch.setattr(workspace_tools, 'workspace', lambda _: tmp_path)
    paths = []
    def run(command, **kwargs):
        assert command[-1] == '-', 'stdin task marker must follow every CLI option'
        assert command.index('--patch') < command.index('--permission-mode')
        paths.append(Path(command[command.index('--patch') + 1]))
        patch = paths[-1].read_text('utf-8')
        values = yaml.load(patch, Loader=yaml.BaseLoader)[0]['config']
        assert values == {'provider': 'connected-source', 'model': 'selected-model',
            'reasoningEffort': 'low', 'temperature': '0.3', 'enabled': 'true',
            'customExpression': 'not_evaluated()'}
        config_node = dict((k.value, v) for k, v in yaml.compose(patch).value[0].value)['config']
        tags = {key.value: value.tag for key, value in config_node.value}
        assert tags['enabled'] == 'tag:yaml.org,2002:bool'
        assert tags['temperature'] == 'tag:yaml.org,2002:float'
        assert tags['customExpression'] == 'tag:yaml.org,2002:js'
        return SimpleNamespace(returncode=0, stdout='真实协议测试替身', stderr='')
    monkeypatch.setattr(dsh_backend, 'run_process', run)
    assert dsh_backend.generate('一次请求', tmp_path, {'dsh_settings': {
        'model': 'selected-model', 'effort': 'low'}})[0] == '真实协议测试替身'
    assert len(paths) == 1 and not paths[0].exists()


def test_dsh_retry_without_unsupported_effort_preserves_the_explicit_permission(tmp_path, monkeypatch):
    import dsh_backend
    import workspace_tools
    import yaml
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: 'fixture-dsh')
    monkeypatch.setattr(dsh_backend, 'desktop_patch_path', lambda: None)
    monkeypatch.setattr(dsh_backend, '_read_cli_config', lambda _:
        '- id: agent-default-model\n  config: {provider: selected, model: old, reasoningEffort: high}')
    monkeypatch.setattr(dsh_backend, 'load_credentials_env', lambda: {})
    monkeypatch.setattr(workspace_tools, 'enabled', lambda _: {
        'enabled': True, 'commands': True, 'full_computer_access': True})
    monkeypatch.setattr(workspace_tools, 'workspace', lambda _: tmp_path)
    calls = []
    def run(command, **kwargs):
        config = yaml.safe_load(Path(command[command.index('--patch')+1]).read_text('utf-8'))[0]['config']
        assert command[command.index('--permission-mode')+1] == 'read-only'
        calls.append(config)
        if len(calls) == 1:
            assert config['reasoningEffort'] == 'high'
            return SimpleNamespace(returncode=1, stdout='', stderr='UNSUPPORTED_REASONING_EFFORT')
        assert 'reasoningEffort' not in config
        assert config['provider'] == 'selected' and config['model'] == 'chosen'
        return SimpleNamespace(returncode=0, stdout='结果', stderr='')
    monkeypatch.setattr(dsh_backend, 'run_process', run)
    assert dsh_backend.generate('一次请求', tmp_path,
        {'dsh_settings': {'model': 'chosen', 'effort': 'high'}}, approval_mode='read-only')[0] == '结果'
    assert len(calls) == 2


def test_dsh_model_override_rejects_a_missing_provider_before_task_dispatch(tmp_path, monkeypatch):
    import dsh_backend
    import workspace_tools
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: 'fixture-dsh')
    monkeypatch.setattr(dsh_backend, 'desktop_patch_path', lambda: None)
    monkeypatch.setattr(dsh_backend, '_read_cli_config', lambda _: '- id: agent-default-model\n  config: {model: old}')
    monkeypatch.setattr(dsh_backend, 'load_credentials_env', lambda: {})
    monkeypatch.setattr(workspace_tools, 'enabled', lambda _: {
        'enabled': True, 'commands': True, 'full_computer_access': True})
    monkeypatch.setattr(workspace_tools, 'workspace', lambda _: tmp_path)
    monkeypatch.setattr(dsh_backend, 'run_process', lambda *a, **kw: pytest.fail('no valid provider'))
    with pytest.raises(ValueError, match='缺少模型来源'):
        dsh_backend.generate('一次请求', tmp_path, {'dsh_settings': {'model': 'selected-model'}})


def test_settings_snapshot_does_not_require_an_unrelated_dsh_account(tmp_path, monkeypatch):
    import house_web
    import dsh_backend
    (tmp_path/'friend.json').write_text(json.dumps({'model_api': {
        'provider': 'codex', 'dsh_settings': {'model': 'saved-dsh-model'}}}), 'utf-8')
    monkeypatch.setattr(house_web, '_directory', lambda _: tmp_path)
    monkeypatch.setattr(dsh_backend, 'fetch_available_models',
                        lambda *a, **kw: pytest.fail('configuration GET must not invoke a CLI'))
    result = house_web.model('fixture', {})
    assert result['provider'] == 'codex'
    assert 'saved-dsh-model' in result['options']['dsh_models']
    assert result['options']['dsh_models_source'] == 'saved_and_defaults'


def test_custom_discovery_uses_the_same_endpoint_and_protocol_as_execution(tmp_path, monkeypatch):
    import api_backend
    import api_profiles
    from agent_model_selection import discover
    calls = []
    monkeypatch.setattr(api_backend, 'load_key', lambda *a: 'synthetic-test-key')
    monkeypatch.setattr(api_profiles, 'discover_models', lambda *args: calls.append(args) or ['actual-model'])
    profile = {'model_api': {'provider': 'custom', 'api_key_id': 'private-source',
        'api_base_url': 'https://selected.example/v1', 'api_protocol': 'openai-responses',
        'model': 'actual-model', 'api_effort': 'off'}}
    assert discover(profile, 'custom', tmp_path)['models'] == {'actual-model': []}
    assert calls == [('https://selected.example/v1', 'openai-responses', 'synthetic-test-key')]


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_dsh_native_reads_the_encrypted_key_via_the_credential_store(tmp_path, monkeypatch):
    import api_backend
    import dsh_backend
    import workspace_tools
    api_backend.save_key(tmp_path, 'synthetic-dsh-key', 'dsh')
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: 'fixture-dsh')
    monkeypatch.setattr(dsh_backend, 'desktop_patch_path', lambda: None)
    monkeypatch.setattr(dsh_backend, 'load_credentials_env', lambda: {})
    monkeypatch.setattr(workspace_tools, 'enabled', lambda _: {
        'enabled': True, 'commands': True, 'full_computer_access': True})
    monkeypatch.setattr(workspace_tools, 'workspace', lambda _: tmp_path)
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs['env']['DEEPSEEK_API_KEY'])
        return SimpleNamespace(returncode=0, stdout='结果', stderr='')
    monkeypatch.setattr(dsh_backend, 'run_process', run)
    assert dsh_backend.generate('一次请求', tmp_path)[0] == '结果'
    assert calls == ['synthetic-dsh-key']


def test_dsh_api_uses_its_own_connection_and_nested_model_choice(tmp_path, monkeypatch):
    import api_backend
    import dsh_backend
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: None)
    calls = []
    monkeypatch.setattr(api_backend, 'generate', lambda prompt, directory, config:
                        calls.append(config) or '结果')
    dsh_backend.generate('一次请求', tmp_path, {
        'model': 'unrelated-custom-model', 'api_base_url': 'https://unrelated.example/v1',
        'api_effort': 'high', 'api_protocol': 'anthropic-messages',
        'dsh_settings': {'base_url': 'https://dsh.example/v1', 'model': 'dsh-model', 'effort': 'low'}})
    assert calls[0]['api_base_url'] == 'https://dsh.example/v1'
    assert calls[0]['api_protocol'] == 'openai-completions'
    assert calls[0]['model'] == 'dsh-model'
    assert calls[0]['api_effort'] == 'low'


def test_dsh_remote_model_change_preserves_its_connection_and_other_providers(tmp_path, monkeypatch):
    import house_web
    import codex_settings
    profile = {'research_effort': 'high', 'research_fast': False, 'model_api': {
        'provider': 'dsh', 'codex_model': 'saved-unavailable-model', 'codex_effort': 'high',
        'dsh_settings': {'model': 'old-dsh-model', 'effort': 'low',
            'base_url': 'https://dsh.example/v1', 'path': 'trusted-dsh.cmd', 'provider': 'trusted'}}}
    (tmp_path/'friend.json').write_text(json.dumps(profile), 'utf-8')
    monkeypatch.setattr(house_web, '_directory', lambda _: tmp_path)
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'other-model': ['low']})
    house_web.post_model('fixture', {'provider': 'dsh', 'dsh_model': 'next-dsh-model', 'dsh_effort': 'low'})
    saved = json.loads((tmp_path/'friend.json').read_text('utf-8'))
    assert saved['model_api']['dsh_settings'] == {**profile['model_api']['dsh_settings'], 'model': 'next-dsh-model'}
    assert saved['model_api']['codex_model'] == 'saved-unavailable-model'
    assert saved['research_effort'] == 'high' and saved['research_fast'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_dsh_can_use_explicit_api_key_without_granting_native_full_access(tmp_path, monkeypatch):
    import api_backend
    import dsh_backend
    import workspace_tools
    api_backend.save_key(tmp_path, 'synthetic-dsh-key', 'dsh')
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: 'fixture-dsh')
    monkeypatch.setattr(workspace_tools, 'enabled', lambda _: {'enabled': False})
    monkeypatch.setattr(dsh_backend, 'run_process', lambda *a, **kw: pytest.fail('native execution was not authorized'))
    monkeypatch.setattr(api_backend, 'generate', lambda *a, **kw: 'API 结果')
    text, metadata = dsh_backend.generate('只回复', tmp_path)
    assert text == 'API 结果' and metadata['native'] is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_dsh_actual_http_transport_reads_protected_key_and_selected_connection(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import api_backend
    import dsh_backend
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append((self.path, self.headers.get('Authorization'),
                json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': '已验证'},
                                           'finish_reason': 'stop'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    api_backend.save_key(tmp_path, 'synthetic-dsh-http-key', 'dsh')
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *a: None)
    try:
        text, metadata = dsh_backend.generate('只回复', tmp_path, {
            'model': 'unrelated-model', 'api_base_url': 'https://unrelated.example/v1',
            'dsh_settings': {'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                             'model': 'selected-model', 'effort': 'off'}})
        assert text == '已验证' and metadata['native'] is False
        assert len(received) == 1
        path, authorization, request = received[0]
        assert path == '/v1/chat/completions'
        assert authorization == 'Bearer synthetic-dsh-http-key'
        assert request['model'] == 'selected-model'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
