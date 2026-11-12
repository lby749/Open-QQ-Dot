"""Independent protocol tests use FAKE subprocesses, not provider accounts."""
import base64
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock, patch

import pytest

from agent_tool_adapters import (
    AgentAdapterError, LocalOpenCode, available_adapters, mcp_call_tool,
    mcp_list_tools, run_acp, run_headless, run_agent,
)


@pytest.fixture
def rpc_executable(tmp_path):
    file = tmp_path / 'rpc_server.py'
    file.write_text(r'''import json,sys
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 method=q['method']; param=q.get('params') or {}
 if method=='initialize': result={'protocolVersion':param.get('protocolVersion'), 'capabilities':{}, 'agentCapabilities':{}}
 elif method=='session/new': result={'sessionId':'test-session'}
 elif method=='session/resume': result={'sessionId':param['sessionId']}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'test-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Real JSON-RPC mock reply'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 elif method=='tools/list': result={'tools':[{'name':'readme','inputSchema':{'type':'object'}}]}
 elif method=='tools/call': result={'content':[{'type':'text','text':'okay'}], 'isError':False}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':result}),flush=True)
''', encoding='utf-8')
    return [os.sys.executable, '-u', str(file)]


def test_builtin_registry_has_seven_adapters():
    records = available_adapters()
    assert set(r['backend'] for r in records) == {'opencode', 'cursor', 'qwen_code', 'zcode', 'kimi_code', 'acp', 'mcp'}
    assert next(x for x in records if x['backend'] == 'kimi_code')['transport'] == 'acp'


def test_acp_starts_session_and_streams_text(tmp_path, rpc_executable):
    r = run_acp(rpc_executable, 'Hello', tmp_path, timeout=5)
    assert r['session_id'] == 'test-session'
    assert r['output'] == 'Real JSON-RPC mock reply'
    assert r['verified'] is False


def test_acp_resume_and_protocol_v2(tmp_path, rpc_executable):
    r = run_acp(rpc_executable, 'Hello', tmp_path, timeout=5, protocol_version=2,
                session_id='test-session')
    assert r['output'] == 'Real JSON-RPC mock reply'


def test_mcp_real_stdio_handshake_list_and_call(tmp_path, rpc_executable):
    assert mcp_list_tools(rpc_executable, tmp_path)[0]['name'] == 'readme'
    assert mcp_call_tool(rpc_executable, tmp_path, 'readme', {})['content'][0]['text'] == 'okay'


def test_rpc_rejects_reverse_permissions(tmp_path):
    script = tmp_path/'reverse.py'
    script.write_text('''import sys,json
for l in sys.stdin:
 req=json.loads(l)
 if req.get("method")=="initialize":
  print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"protocolVersion":1}}),flush=True)
 elif req.get("method")=="session/new":
  print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"sessionId":"s"}}),flush=True)
 elif req.get("method")=="session/prompt":
  print(json.dumps({"jsonrpc":"2.0","id":901,"method":"session/request_permission","params":{}}),flush=True)
  reply=json.loads(sys.stdin.readline())
  print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":{"stopReason":"end_turn"}}),flush=True)
  if "error" in reply:
   print(json.dumps({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"Denied"}}}}),flush=True)
''',encoding='utf-8')
    with pytest.raises(AgentAdapterError):
        # No completed agent text, but the permission request was not granted.
        run_acp([os.sys.executable, '-u', str(script)], 'dangerous task', tmp_path, timeout=5)


def test_headless_cursor_uses_no_shell_and_no_force_by_default(tmp_path, monkeypatch):
    script = tmp_path / 'cursor_fixture.py'
    script.write_text('import json\nprint(json.dumps({"result":"Cursor mock completed", "session_id":"cur"}))\n', 'utf-8')
    r=run_headless('cursor','test',tmp_path, command=[os.sys.executable, str(script)])
    assert r['output'] == 'Cursor mock completed'
    assert r['verified'] is False


def test_qwen_reads_prompt_from_stdin(tmp_path, monkeypatch):
    script = tmp_path / 'qwen_fixture.py'
    script.write_text('import sys,json\np=sys.stdin.read()\nprint(json.dumps([{"type":"result","result":p,"usage":{"total_tokens":6}}]))\n', 'utf-8')
    result=run_headless('qwen_code','private instruction',tmp_path, command=[os.sys.executable, str(script)])
    assert result['output']=='private instruction'
    assert result['tokens_used']==6


@pytest.mark.parametrize('first', ['agent_tool_adapters', 'agent_backends.transport_opencode', 'agent_backends.transport_acp'])
def test_compatibility_imports_use_the_production_transport_in_any_import_order(first):
    import subprocess
    code = (f'import {first}\n'
        'import agent_tool_adapters as old\n'
        'from agent_backends import transport_opencode as http, transport_acp as rpc\n'
        'assert old.LocalOpenCode is http.LocalOpenCode\n'
        'assert old.StdioJsonRpc is rpc.StdioJsonRpc\n'
        'assert old.run_acp is rpc.run_acp\n')
    result = subprocess.run([os.sys.executable, '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def _opencode_server(routes):
    """A fake loopback OpenCode server: routes maps (method, path) to (status, body)."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, method):
            length = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split('?')[0]
            requests.append((method, path, self.headers.get('Authorization'), payload))
            status, body = routes.get((method, path), (404, {'error': 'not found'}))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply('GET')

        def do_POST(self):
            self._reply('POST')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, requests


def test_opencode_v1_http_protocol(tmp_path):
    routes = {
        ('GET', '/global/health'): (200, {'healthy': True}),
        ('GET', '/path'): (200, {'directory': str(tmp_path.resolve())}),
        ('POST', '/session'): (200, {'id': 'ses_test'}),
        ('POST', '/session/ses_test/message'): (200, {'parts': [{'type': 'text', 'text': 'OpenCode mock result'}]}),
    }
    server, requests = _opencode_server(routes)
    try:
        client = LocalOpenCode('http://127.0.0.1:' + str(server.server_port), password='private')
        r = client.run('task', workspace=tmp_path)
        assert client.api == 1
        assert r['output'] == 'OpenCode mock result'
        assert r['session_id'] == 'ses_test'
        assert requests[0][2] == 'Basic ' + base64.b64encode(b'opencode:private').decode()
        assert next(p for m, path, _a, p in requests if path == '/session/ses_test/message')['parts'][0]['text'] == 'task'
        with pytest.raises(AgentAdapterError):
            client.run('cross-user task', workspace=tmp_path.parent)
    finally:
        server.shutdown()
        server.server_close()


def test_opencode_v2_http_protocol_binds_the_session_to_the_task_workspace(tmp_path, monkeypatch):
    """2026-10-26 user: the installed OpenCode 2.0.32 answers /api/*, not the v1 web API."""
    monkeypatch.setattr(LocalOpenCode, 'POLL_SECONDS', 0)
    routes = {
        ('GET', '/api/info'): (200, {'version': '2.0.32', 'pid': 4242}),
        ('GET', '/api/location'): (200, {'directory': 'C:\\somewhere-else'}),
        ('POST', '/api/session'): (200, {'data': {'id': 'ses_v2'}}),
        ('POST', '/api/session/ses_v2/prompt'): (200, {'data': {'id': 'msg_1'}}),
        ('GET', '/api/session/ses_v2/message'): (200, {'data': [
            {'type': 'user', 'text': 'task'},
            {'type': 'assistant', 'content': [{'type': 'text', 'text': 'v2 结果'}]},
            {'type': 'idle', 'outcome': 'succeeded'}]}),
    }
    server, requests = _opencode_server(routes)
    try:
        client = LocalOpenCode('http://127.0.0.1:' + str(server.server_port), password='private')
        result = client.run('task', workspace=tmp_path)
        assert client.api == 2
        assert result['output'] == 'v2 结果'
        assert result['session_id'] == 'ses_v2'
        created = next(payload for method, path, _a, payload in requests if path == '/api/session')
        assert created['location']['directory'] == str(tmp_path.resolve())
        prompt = next(payload for method, path, _a, payload in requests if path.endswith('/prompt'))
        assert prompt['text'] == 'task'
        assert requests[0][2].startswith('Basic ')
    finally:
        server.shutdown()
        server.server_close()


def test_opencode_v2_reports_the_provider_error_instead_of_empty_text(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalOpenCode, 'POLL_SECONDS', 0)
    routes = {
        ('GET', '/api/info'): (200, {'version': '2.0.32'}),
        ('POST', '/api/session'): (200, {'data': {'id': 'ses_v2'}}),
        ('POST', '/api/session/ses_v2/prompt'): (200, {'data': {'id': 'msg_1'}}),
        ('GET', '/api/session/ses_v2/message'): (200, {'data': [
            {'type': 'assistant', 'content': [], 'finish': 'error'},
            {'type': 'idle', 'outcome': 'failed',
             'error': {'type': 'provider.invalid-request', 'message': '模型未开通，请检查隐私设置'}}]}),
    }
    server, _requests = _opencode_server(routes)
    try:
        client = LocalOpenCode('http://127.0.0.1:' + str(server.server_port), password='private')
        with pytest.raises(AgentAdapterError) as err:
            client.run('task', workspace=tmp_path)
        assert '模型未开通' in str(err.value)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('url',['https://example.com','http://localhost:4096','http://127.0.0.1:4096/evil',
                                  'http://127.0.0.1:4096@evil.com','file:///etc/passwd'])
def test_opencode_refuses_non_loopback_or_ambiguous_url(url):
    with pytest.raises(AgentAdapterError): LocalOpenCode(url)


def test_no_trusted_command_for_generic_acp(tmp_path):
    with pytest.raises(AgentAdapterError): run_agent('acp','test',tmp_path)
    with pytest.raises(AgentAdapterError): run_agent('mcp','test',tmp_path)


def test_task_manager_permission_fail_closed():
    from task_manager import TaskManager
    class Ex:
        def effective_permissions(self): return {'enabled':True,'commands':True,'full_computer_access':False}
    task=Mock(category='code')
    mgr=object.__new__(TaskManager)
    with pytest.raises(PermissionError):
        mgr._execute_external_agent(task,Ex(),{},lambda msg:None,'qwen_code')


def test_headless_provider_failure_does_not_mark_success(tmp_path, monkeypatch):
    bin_dir=tmp_path/'bin';bin_dir.mkdir()
    executable=bin_dir/'qwen'
    executable.write_text('#!'+os.sys.executable+'\nimport sys\nprint("private secret",file=sys.stderr)\nsys.exit(7)\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH',str(bin_dir)+os.pathsep+os.environ.get('PATH',''))
    with pytest.raises(AgentAdapterError) as err:
        run_headless('qwen_code','test',tmp_path)
    assert 'private secret' not in str(err.value)


def test_task_manager_executes_backend_and_forwards_to_verifier(tmp_path):
    from task_manager import TaskManager
    class Ex:
        def effective_permissions(self):
            return {'enabled':True,'commands':True,'full_computer_access':True,'read_only':False}
        def workspace(self): return tmp_path
        def before_call(self, prompt): self.prompt=prompt
        def check(self): pass
        def after_call(self, output, usage): self.output=output
    task=Mock(category='code', description='implement feature',
              checkpoint={'target_files':['feature.py'],'test_command':'pytest -q'},
              max_duration_seconds=60)
    manager=object.__new__(TaskManager)
    execution=Ex()
    with patch('agent_tool_adapters.run_agent', return_value={
        'output':'provider said complete','tokens_used':12,'session_id':'s'}) as agent:
        result=manager._execute_external_agent(task,execution,
                {'agent_adapters':{'qwen_code':{}}},lambda m:None,'qwen_code')
    from task_code_review import review_instruction
    assert agent.call_args.args[:2] == ('qwen_code', review_instruction('code') + 'implement feature')
    assert agent.call_args.args[1].count('【交付前自审】') == 1
    assert result['output']=='provider said complete'
    assert result['verification_status']=='unverified_pending_task_verifier'
    assert result['test_command']=='pytest -q'
    assert execution.output=='provider said complete'


def test_open_code_shared_session_identifier_not_used_by_generic_dispatch(tmp_path):
    with patch('agent_backends.transport_opencode.LocalOpenCode') as opener:
        opener.return_value.run.return_value={'output':'test','session_id':'new'}
        run_agent('opencode','task',tmp_path,config={'session_id':'somebody-elses-session'})
        assert opener.return_value.run.call_args.kwargs.get('session_id','') == ''


def test_zcode_headless_plan_and_explicit_yolo(tmp_path):
    script = tmp_path / 'fake_zcode.py'
    log = tmp_path / 'args.json'
    script.write_text(
        "import json,sys\n"
        "args=sys.argv[1:]\n"
        "open(" + repr(str(log)) + ",'w',encoding='utf-8').write(json.dumps(args))\n"
        "mode='yolo' if 'yolo' in args else 'plan'\n"
        "print(json.dumps({'sessionId':'zcode-session','response':'ZCode '+mode+' completed',"
        "'usage':{'total_tokens':7}}))\n",
        encoding='utf-8')
    command = [os.sys.executable, str(script)]
    result = run_headless('zcode', 'inspect only', tmp_path, timeout=10, command=command)
    assert result['output'] == 'ZCode plan completed'
    assert result['session_id'] == 'zcode-session'
    assert result['tokens_used'] == 7
    args = json.loads(log.read_text('utf-8'))
    assert '--prompt' in args and 'inspect only' in args
    assert args[args.index('--mode') + 1] == 'plan'
    assert '--json' in args and '--no-color' in args
    result = run_headless('zcode', 'change file', tmp_path, timeout=10, allow_write=True, command=command)
    assert result['output'] == 'ZCode yolo completed'
    args = json.loads(log.read_text('utf-8'))
    assert args[args.index('--mode') + 1] == 'yolo'
