import json
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_group
import mobile_collaboration as collab
from task_manager import TaskManager


@pytest.fixture
def house(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    for owner in ('12345', '67890'):
        directory = tmp_path/'friends'/owner
        directory.mkdir(parents=True)
        (directory/'friend.json').write_text(json.dumps({'qq_id': owner, 'model_api': {'provider': 'codex', 'active_providers': ['codex', 'antigravity']}}), 'utf-8')
    return tmp_path


def test_rooms_members_and_available_providers_remain_friend_scoped(house):
    room = collab.action(house, '12345', dict(action='create', title='协作验收'))['room']
    rid = room['id']
    collab.action(house, '12345', dict(action='add', room_id=rid, provider='codex', name='执行者', role='完成任务'))
    assert collab.action(house, '67890', dict(action='list'))['rooms'] == []
    with pytest.raises(FileNotFoundError):
        collab.action(house, '67890', dict(action='get', room_id=rid))
    with pytest.raises(PermissionError):
        collab.action(house, '12345', dict(action='add', room_id=rid, provider='claude', role='未启用'))
    with pytest.raises(ValueError):
        collab.action(house, '../12345', dict(action='list'))


def test_submission_is_persistent_deduplicated_and_rejects_overlapping_turns(house):
    manager = TaskManager(house/'data/tasks.sqlite3', house)
    room = collab.action(house, '12345', dict(action='create', title='一次接力'))['room']
    collab.action(house, '12345', dict(action='add', room_id=room['id'], provider='codex', role='整理证据'))
    request = dict(action='run', room_id=room['id'], text='整理项目资料', request_id='stable_request_123')
    with patch.object(TaskManager, 'get_instance', return_value=manager), patch.object(manager, 'start_worker'):
        first = collab.action(house, '12345', request)
        second = collab.action(house, '12345', request)
        assert second['duplicate'] and first['task_id'] == second['task_id']
        with pytest.raises(ValueError, match='上一轮'):
            collab.action(house, '12345', {**request, 'request_id': 'another_request_456'})
    task = manager.get_task(first['task_id'])
    assert task.owner_id == '12345' and task.max_retries == 0
    assert task.checkpoint['member_ids'] == [agent_group.load_room(house/'data', '12345', room['id'])['members'][0]['id']]
    manager.stop_worker()


def test_real_member_handoff_prompt_contains_prior_reply_once(house):
    # A prompt-contract test; actual provider execution is recorded separately.
    manager = TaskManager(house/'data/tasks.sqlite3', house)
    room = agent_group.create_room(house/'data', '12345', '接力')
    agent_group.add_member(house/'data', '12345', room['id'], 'codex', name='甲', role='先说一个词')
    agent_group.add_member(house/'data', '12345', room['id'], 'antigravity', name='乙', role='复核甲的词')
    room = agent_group.load_room(house/'data', '12345', room['id'])
    tid = manager.create_task('接力', '开始', category='data', owner_id='12345', initial_checkpoint={
        'agent_group_room': room['id'], 'member_ids': [m['id'] for m in room['members']]})
    prompts = []
    def call(prompt, *_args, **_kw):
        prompts.append(prompt)
        return ('FIRST_MEMBER_UNIQUE_REPLY' if len(prompts) == 1 else '已复核'), None
    directory, profile = collab.owner_directory(house, '12345')
    with patch('main.codex_text', side_effect=call):
        result = collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    assert prompts[1].count('FIRST_MEMBER_UNIQUE_REPLY') == 1
    assert len(json.loads(result['output'])['messages']) == 2
    manager.stop_worker()


def mother_turn(house):
    manager = TaskManager(house/'data/tasks.sqlite3', house)
    room = agent_group.create_room(house/'data', '12345', '主智能体协作')
    host = agent_group.add_member(house/'data', '12345', room['id'], 'codex', name='统筹', role='选择与汇总')
    selected = agent_group.add_member(house/'data', '12345', room['id'], 'codex', name='审查员', role='核对结论')
    unused = agent_group.add_member(house/'data', '12345', room['id'], 'antigravity', name='未参与成员', role='不应调用')
    collab.action(house, '12345', dict(action='set_host', room_id=room['id'], member_id=host['id']))
    room = agent_group.load_room(house/'data', '12345', room['id'])
    tid = manager.create_task('手机主智能体', '审查这个结论', category='data', owner_id='12345', initial_checkpoint={
        'agent_group_room': room['id'], 'member_ids': [m['id'] for m in room['members']],
        'member_config': room['members'], 'host_id': host['id']})
    return manager, room, host, selected, unused, tid


def test_mother_plan_selects_actual_model_effort_assignment_and_is_not_replayed(house, monkeypatch):
    import codex_settings
    import multi_agent_orchestrator
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low'], 'deep': ['high']})
    manager, room, host, selected, _, tid = mother_turn(house)
    calls = []
    def call(provider, prompt, directory, profile, **options):
        calls.append((provider, prompt, profile, options))
        assert options['max_retries'] == 0
        assert options['local_ok'] is False
        current = manager.get_task(tid).checkpoint
        current['budget_receipt_calls'] = len(calls)
        manager.save_checkpoint(tid, current)
        if '候选成员：' in prompt:
            return json.dumps({'summary': '由审查员核对', 'assignments': [
                {'member_id': selected['id'], 'model': 'deep', 'effort': 'high', 'task': '仅核对证据，不重复原任务'}]}), None
        if '请汇总' in prompt:
            return '已完成审查，文件执行尚未验收', None
        return '审查证据结果', None
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', call)
    directory, profile = collab.owner_directory(house, '12345')
    result = collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    assert len(calls) == 3
    assert manager.get_task(tid).checkpoint['budget_receipt_calls'] == 3
    assert '仅核对证据，不重复原任务' in calls[1][1]
    assert calls[1][2]['model_api']['codex_model'] == 'deep'
    assert calls[1][2]['model_api']['codex_effort'] == 'high'
    assert 'deep' in calls[2][1] and 'high' in calls[2][1]
    before = agent_group.load_room(house/'data', '12345', room['id'])['messages']
    collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    assert len(calls) == 3
    assert agent_group.load_room(house/'data', '12345', room['id'])['messages'] == before
    with patch.object(TaskManager, 'get_instance', return_value=manager):
        snapshot = collab.action(house, '12345', dict(action='get', room_id=room['id']))
    assert snapshot['routing']['members'][0]['model'] == 'deep'
    assert selected['id'] in snapshot['completed_members']
    assert len(json.loads(result['output'])['messages']) == 2
    manager.stop_worker()


def test_unknown_mother_and_changed_queued_member_configuration_fail_closed(house):
    manager, room, _, selected, _, tid = mother_turn(house)
    with pytest.raises(ValueError, match='当前协作群'):
        collab.action(house, '12345', dict(action='set_host', room_id=room['id'], member_id='other-friend-member'))
    saved = agent_group.load_room(house/'data', '12345', room['id'])
    saved['members'][1]['role'] = '排队后被修改'
    agent_group.save_room(house/'data', '12345', saved)
    directory, profile = collab.owner_directory(house, '12345')
    with pytest.raises(ValueError, match='模型或分工发生变化'):
        collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    manager.stop_worker()


def test_unknown_model_reply_stops_before_worker_calls(house, monkeypatch):
    import multi_agent_orchestrator
    manager, room, _, selected, _, tid = mother_turn(house)
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        return json.dumps({'summary': '无效分工', 'assignments': [
            {'member_id': selected['id'], 'model': 'not-authorized-model', 'effort': 'high', 'task': '核查'}]}), None
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', call)
    directory, profile = collab.owner_directory(house, '12345')
    with pytest.raises(ValueError, match='未配置或未列出'):
        collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    assert len(calls) == 1
    assert not [m for m in agent_group.load_room(house/'data', '12345', room['id'])['messages'] if m['role'] == 'agent']
    manager.stop_worker()


def test_unknown_model_outcome_preserves_checkpoint_without_automatic_repeat(house, monkeypatch):
    import multi_agent_orchestrator
    manager, room, _, _, _, tid = mother_turn(house)
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('未确认')
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', call)
    directory, profile = collab.owner_directory(house, '12345')
    with pytest.raises(TimeoutError):
        collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    with pytest.raises(InterruptedError, match='不自动重发'):
        collab.execute(manager.get_task(tid), directory, profile, manager, lambda _: None)
    assert len(calls) == 1
    assert manager.get_task(tid).checkpoint['collaboration_inflight'] == 'mother_plan'
    manager.stop_worker()


def test_phone_host_configuration_uses_real_scoped_http(house, monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    import vnext_bridge
    monkeypatch.setattr(vnext_bridge, '_root', lambda: house)
    monkeypatch.setattr(vnext_bridge, 'TOKEN', 'synthetic-admin')
    store = vnext_bridge.device_store(house)
    owner_device = store.pair(store.create_code(owners=('12345',), scopes=('collab:read', 'collab:write')), '合成写设备')
    readonly = store.pair(store.create_code(owners=('12345',), scopes=('collab:read',)), '合成只读设备')
    room = collab.action(house, '12345', dict(action='create', title='HTTP 主智能体'))['room']
    member = agent_group.add_member(house/'data', '12345', room['id'], 'codex', name='主持')
    server = ThreadingHTTPServer(('127.0.0.1', 0), vnext_bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(token, owner, *, path='/api/collaboration', operation='set_host', method='POST'):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        body = json.dumps({'owner': owner, 'action': operation, 'room_id': room['id'], 'member_id': member['id']}) if method == 'POST' else None
        conn.request(method, path, body, {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read())
        conn.close()
        return status, payload
    try:
        assert request(owner_device['token'], '12345', path='/api/overview', method='GET')[0] == 403
        assert request(owner_device['token'], '12345', path='/api/friends', method='GET')[0] == 403
        # A valid collaboration-only device remains authorized even when unrelated reads are forbidden.
        status, owners = request(owner_device['token'], '12345', operation='owners')
        assert status == 200 and [item['qq'] for item in owners['items']] == ['12345']
        assert request(readonly['token'], '12345')[0] == 403
        assert request(owner_device['token'], '67890')[0] == 403
        status, payload = request(owner_device['token'], '12345')
        assert status == 200 and payload['room']['host_id'] == member['id']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
