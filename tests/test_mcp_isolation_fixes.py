import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
import agent_group as groups
import agent_tool_adapters as adapters


def test_recent_messages_once_and_old_facts_kept(tmp_path):
    room = groups.create_room(tmp_path, '123', '测试')
    member = {'name': '执行', 'role': '执行', 'id': 'worker'}
    for i in range(45):
        groups.append_message(tmp_path, '123', room['id'], f'MARKER_{i:03d}', name='甲')
    room = groups.load_room(tmp_path, '123', room['id'])
    # Exercise old files as well as new IDs.
    for item in room['memory']:
        item.pop('id', None)
    memory = groups.shared_memory(tmp_path, '123', room, '好友持久事实')
    for prompt in (groups.prompt_for(room, member, memory=memory),
                   groups.host_prompt(room, member, '开始', memory=memory),
                   groups.summary_prompt(room, member, '开始', memory=memory)):
        for i in range(45):
            assert prompt.count(f'MARKER_{i:03d}') == 1
        assert '好友持久事实' in prompt


@pytest.mark.parametrize('owner,room', [('..', 'good'), ('123', '../other'), ('', 'good')])
def test_group_rejects_path_escape(tmp_path, owner, room):
    with pytest.raises(ValueError):
        groups.load_room(tmp_path, owner, room)


def test_real_stdio_survives_bad_requests_and_rejects_ungranted_tool(tmp_path):
    (tmp_path / 'friend.json').write_text('{}', encoding='utf-8')
    requests = ['{bad-json}', '[]', json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize',
        'params': {'protocolVersion': 'unsupported'}}),
        json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                    'params': {'name': 'not_allowed', 'arguments': {}}}),
        json.dumps({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list', 'params': []}),
        json.dumps({'jsonrpc': '2.0', 'id': 5, 'method': 'ping'})]
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'topic_library.py'), str(tmp_path)],
        input='\n'.join(requests)+'\n', capture_output=True, text=True, encoding='utf-8', timeout=15,
        env={**os.environ, 'QQ_BRIDGE_HOME': str(tmp_path)})
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row.get('error', {}).get('code') for row in rows[:2]] == [-32700, -32600]
    assert rows[2]['result']['protocolVersion'] == '2024-11-05'
    assert rows[3]['result']['isError'] is True
    assert rows[4]['error']['code'] == -32602
    assert rows[5] == {'jsonrpc': '2.0', 'id': 5, 'result': {}}


def test_mcp_pagination_and_repeated_cursor(tmp_path):
    class Rpc:
        pages = []
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def _send(self, *args): pass
        def request(self, method, params=None):
            return {} if method == 'initialize' else self.pages.pop(0)
    with patch('agent_backends.transport_acp.StdioJsonRpc', Rpc):
        Rpc.pages = [{'tools': [{'name': 'one'}], 'nextCursor': 'next'}, {'tools': [{'name': 'two'}]}]
        assert [t['name'] for t in adapters.mcp_list_tools(['mock'], tmp_path)] == ['one', 'two']
        Rpc.pages = [{'tools': [], 'nextCursor': 'loop'}, {'tools': [], 'nextCursor': 'loop'}]
        with pytest.raises(adapters.AgentAdapterError, match='游标'):
            adapters.mcp_list_tools(['mock'], tmp_path)


def test_external_mcp_cannot_replace_internal_services(tmp_path, monkeypatch):
    config = {'computer_tools': {'enabled': True, 'commands': True, 'full_computer_access': True},
              'agent_adapters': {'mcp': {'servers': [
                  {'name': name, 'command': [sys.executable]} for name in
                  ('qq_phone', 'external_qq_phone', 'qq_topic_library', 'agent_mailbox')]}}}
    (tmp_path/'friend.json').write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(adapters, 'require_native_permissions', lambda profile: None)
    servers = adapters.mcp_server_configs(tmp_path)
    assert len(servers) == 4
    assert not set(servers) & {'qq_phone', 'qq_topic_library', 'agent_mailbox'}


def test_parallel_group_writes_keep_every_reply(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    room = groups.create_room(tmp_path, '123')
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: groups.append_message(tmp_path, '123', room['id'], str(i)), range(60)))
    saved = groups.load_room(tmp_path, '123', room['id'])
    assert {m['content'] for m in saved['messages']} == {str(i) for i in range(60)}
    assert len(saved['messages']) == len(saved['memory']) == 60


def test_real_paginated_mcp_server(tmp_path):
    server = tmp_path/'server.py'
    server.write_text('''import json,sys
for line in sys.stdin:
    r=json.loads(line)
    if 'id' not in r: continue
    result={}
    if r['method']=='tools/list':
        page=int(r.get('params',{}).get('cursor','0'))
        result={'tools':[{'name':'page_'+str(page)}]}
        if page<2: result['nextCursor']=str(page+1)
    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''', encoding='utf-8')
    assert [x['name'] for x in adapters.mcp_list_tools([sys.executable, str(server)], tmp_path)] == [
        'page_0', 'page_1', 'page_2']


@pytest.mark.parametrize('script', ['topic_library.py', 'phone_tools.py'])
def test_each_local_server_recovers_and_keeps_friend_tool_scope(tmp_path, script):
    root = Path(__file__).resolve().parents[1]
    for owner in ('123', '456'):
        directory = tmp_path/owner
        directory.mkdir()
        profile = {'function_apis': [{'name': 'api_only_'+owner, 'enabled': True,
                   'inputSchema': {'type': 'object', 'properties': {}}}]}
        (directory/'friend.json').write_text(json.dumps(profile), encoding='utf-8')
        payload = '{bad}\n'+json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n'
        run = subprocess.run([sys.executable, '-X', 'utf8', str(root/script), str(directory)],
            input=payload, capture_output=True, text=True, encoding='utf-8', timeout=15,
            env={**os.environ,'QQ_BRIDGE_HOME':str(tmp_path)})
        assert run.returncode == 0, run.stderr
        responses = [json.loads(x) for x in run.stdout.splitlines()]
        assert responses[0]['error']['code'] == -32700
        if script == 'topic_library.py':
            names = {t['name'] for t in responses[1]['result']['tools']}
            assert 'api_only_'+owner in names
            assert 'api_only_'+('456' if owner == '123' else '123') not in names


def test_real_bound_mailbox_recovers_but_unbound_cannot_start(tmp_path, monkeypatch):
    import agent_messaging
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    mailbox = agent_messaging.AgentMailbox()
    identity = mailbox.register_agent('123', 'audit', 'worker', 'codex')
    token = agent_messaging.issue_agent_token(identity)
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-X', 'utf8', str(root/'agent_mailbox_mcp.py')]
    env = {**os.environ, 'AGENT_MAIL_TOKEN': token}
    payload = '{bad}\n'+json.dumps({'jsonrpc':'2.0','id':9,'method':'ping'})+'\n'
    result = subprocess.run(command, input=payload, capture_output=True, text=True,
                            encoding='utf-8', timeout=15, env=env)
    assert result.returncode == 0
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]['error']['code'] == -32700
    assert rows[1]['id'] == 9 and rows[1]['result'] == {}
    refused = subprocess.run(command, input='', capture_output=True, timeout=15,
                             env={**env, 'AGENT_MAIL_TOKEN': ''})
    assert refused.returncode != 0
