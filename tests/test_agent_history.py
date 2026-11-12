from pathlib import Path
import json
import pytest
import agent_history
import workspace_tools


def test_agent_history_is_per_friend_and_filterable(tmp_path):
    agent_history.record(tmp_path, 'codex', 'session_a', 'assistant', '修复了登录 bug', 'task-1')
    agent_history.record(tmp_path, 'claude', 'session_b', 'assistant', '登录 bug 需要补测试', 'task-1')
    rows = agent_history.search(tmp_path, '登录 bug')
    assert {row['agent_id'] for row in rows} == {'codex', 'claude'}
    only = agent_history.search(tmp_path, '登录 bug', ['claude'])
    assert len(only) == 1 and only[0]['agent_id'] == 'claude'


def test_agent_history_tool_requires_opt_in(tmp_path):
    friend = tmp_path / 'friend'
    friend.mkdir()
    (friend / 'friend.json').write_text(json.dumps({'computer_tools': {'enabled': True}, 'agent_history_sharing': False}), encoding='utf-8')
    agent_history.record(friend, 'codex', 's', 'assistant', 'secret plan')
    with pytest.raises(PermissionError):
        workspace_tools.call(friend, 'desk_search_agent_history', {'query': 'secret'})
    (friend / 'friend.json').write_text(json.dumps({'computer_tools': {'enabled': True}, 'agent_history_sharing': True}), encoding='utf-8')
    found = workspace_tools.call(friend, 'desk_search_agent_history', {'query': 'secret'})
    assert found['matches'][0]['agent_id'] == 'codex'


def test_agent_history_defaults_on_when_field_missing(tmp_path):
    friend = tmp_path / 'friend'
    friend.mkdir()
    (friend / 'friend.json').write_text('{"computer_tools":{"enabled":true}}', encoding='utf-8')
    agent_history.record(friend, 'codex', 's', 'assistant', 'default sharing')
    found = workspace_tools.call(friend, 'desk_search_agent_history', {'query': 'sharing'})
    assert found['matches']
