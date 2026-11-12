import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import ValidationError
import runtime_capabilities
import workspace_tools
from topic_library import call_tool


def test_runtime_discovery_is_bound_and_whitelisted(tmp_path):
    for name in ('123', '456'):
        directory = tmp_path/name
        directory.mkdir()
        (directory/'friend.json').write_text(json.dumps({
            'model_api': {'provider': 'codex', 'codex_model': 'model-'+name,
                          'api_key': 'DO_NOT_LEAK', 'token': 'DO_NOT_LEAK'},
            'computer_tools': {'enabled': False, 'project_path': 'DO_NOT_LEAK'},
            'unknown_settings': {'session': 'DO_NOT_LEAK'}}), encoding='utf-8')
        for section in ('config', 'models', 'tool_models', 'tools'):
            result = call_tool(directory, 'inspect_runtime', {'section': section, 'limit': 1})
            text = json.dumps(result)
            assert 'DO_NOT_LEAK' not in text
            assert result['scope'] == name
            assert len(result['items']) <= 1
        result = runtime_capabilities.call(directory, {'section': 'models'})
        assert result['items'][0]['codex_model'] == 'model-'+name
        with pytest.raises(ValidationError):
            runtime_capabilities.call(directory, {'section': 'models', 'owner': 'other'})


def test_file_pages_preserve_content_and_bounds(tmp_path):
    (tmp_path/'friend.json').write_text(json.dumps({'computer_tools': {
        'enabled': True, 'mode': 'read_only', 'project_path': str(tmp_path)}}), encoding='utf-8')
    (tmp_path/'data.txt').write_bytes('第一行\n第二行\n第三行\n'.encode('utf-8'))
    first = call_tool(tmp_path, 'desk_read_file', {'path': 'data.txt', 'max_lines': 2})
    second = call_tool(tmp_path, 'desk_read_file', {'path': 'data.txt', 'start_line': first['next_line']})
    assert first['text'] + second['text'] == '第一行\n第二行\n第三行\n'
    assert second['next_line'] is None
    for arguments in ({'start_line': 0}, {'max_lines': 201}):
        with pytest.raises((ValueError, ValidationError)):
            workspace_tools.call(tmp_path, 'desk_read_file', {'path': 'data.txt', **arguments})


def test_quota_snapshot_expires_per_window():
    path = Path(__file__).resolve().parents[1]/'quota-whale/quota_sources.py'
    spec = importlib.util.spec_from_file_location('quota_snapshot_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.now(timezone.utc)
    old = now-timedelta(days=3)
    assert module._window_usage({'window_minutes': 300, 'used_percent': 97}, old, now) is None
    assert module._window_usage({'window_minutes': 10080, 'used_percent': 81}, old, now) == 81
    assert module._window_usage({'window_minutes': 300, 'used_percent': 97,
        'resets_at': now.timestamp()-1}, now-timedelta(minutes=1), now) is None
