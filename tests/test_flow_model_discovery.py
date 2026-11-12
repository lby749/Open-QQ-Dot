"""Discovery contracts: no real account, CLI, network, model call or credential access."""
import json
import os
from pathlib import Path

import pytest

from agent_model_selection import discover, signature, flow_options, validate


def test_codex_catalog_uses_real_rpc_models_and_per_model_reasoning(monkeypatch):
    from codex_app_server import CodexAppServerClient
    client = CodexAppServerClient()
    monkeypatch.setattr(client, 'start', lambda: None)
    calls = []
    def request(method, params, **kwargs):
        calls.append((method, params))
        return {'data': [{'id': 'one', 'model': 'small', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]},
                         {'id': 'two', 'model': 'deep', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]},
                         {'id': 'hidden', 'hidden': True}], 'nextCursor': None}
    monkeypatch.setattr(client, '_send_request', request)
    assert client.list_models() == {'small': ['low'], 'deep': ['high']}
    assert [method for method, _ in calls] == ['model/list']


def test_codex_discovery_cannot_fall_back_to_builtin_list(monkeypatch):
    import codex_settings
    import codex_app_server
    closed = []
    class Client:
        def __init__(self, **kwargs): pass
        def list_models(self): raise RuntimeError('查询失败')
        def close(self): closed.append(True)
    monkeypatch.setattr(codex_settings, 'cli_path', lambda _: 'test-cli')
    monkeypatch.setattr(codex_app_server, 'CodexAppServerClient', Client)
    monkeypatch.setattr(codex_settings, 'model_options', lambda *_: {'fake-model': ['fake-effort']})
    with pytest.raises(RuntimeError, match='查询失败'):
        codex_settings.fetch_available_models()
    assert closed == [True]


def test_opencode_uses_enabled_models_and_each_models_own_variants(monkeypatch, tmp_path):
    from agent_backends.transport_opencode import LocalOpenCode
    monkeypatch.setattr(LocalOpenCode, '__init__', lambda self, *a, **kw: None)
    monkeypatch.setattr(LocalOpenCode, 'list_models', lambda self: [
        {'id': 'small', 'provider': 'p', 'enabled': True, 'variants': ['low']},
        {'id': 'deep', 'provider': 'p', 'enabled': True, 'variants': ['high']},
        {'id': 'blocked', 'provider': 'p', 'enabled': False, 'variants': ['max']}])
    entry = discover({}, 'opencode', tmp_path)
    assert entry['models'] == {'p/small': ['low'], 'p/deep': ['high']}


def test_no_enum_and_claude_do_not_invent_models_or_reasoning_tiers(tmp_path):
    assert discover({}, 'qwen_code', tmp_path)['models'] == {'': []}
    with pytest.raises(PermissionError, match='禁止调用 Claude'):
        discover({}, 'claude', tmp_path)


def test_runtime_catalog_rejects_unlisted_model_and_changed_connection(monkeypatch):
    import agent_model_selection as selection
    monkeypatch.setattr(selection, 'options', lambda *a, **kw: {'codex': {}})
    profile = {'model_api': {'provider': 'codex', 'codex_model': 'small'}}
    room = {'members': [{'provider': 'codex'}], 'model_catalogs': {'codex': {
        'models': {'small': ['low']}, 'signature': signature(profile, 'codex'), 'source': 'test RPC'}}}
    result = flow_options(room, profile)
    assert validate(result, 'codex', 'small', 'low') == ('small', 'low')
    with pytest.raises(ValueError): validate(result, 'codex', 'unlisted', '')
    with pytest.raises(ValueError): validate(result, 'codex', 'small', 'high')
    with pytest.raises(PermissionError): flow_options(room, {'model_api': {'codex_path': 'different'}})


def test_node_model_and_effort_dropdowns_follow_the_discovered_catalog(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QWidget
    import agent_group
    from house_qt.cooperation_flow import CooperationFlowPage
    app = QApplication.instance() or QApplication([])
    base, owner = tmp_path/'data', '12345'
    directory = tmp_path/'friends'/owner
    directory.mkdir(parents=True)
    profile = {'model_api': {'provider': 'codex'}}
    (directory/'friend.json').write_text(json.dumps(profile), 'utf-8')
    room = agent_group.create_room(base, owner)
    member = agent_group.add_member(base, owner, room['id'], 'codex')
    agent_group.update_room(base, owner, room['id'], host_id=member['id'])
    house = QWidget()
    house.current_qq = owner
    page = CooperationFlowPage(house, base)
    page.pick((room['id'], member['id']))
    page.catalogs[(room['id'], 'codex')] = {'models': {'small': ['low'], 'deep': ['high']},
        'signature': signature(profile, 'codex'), 'source': 'test RPC'}
    page.populate_models('small', 'low')
    from qfluentwidgets import ComboBox, EditableComboBox
    assert isinstance(page.model, ComboBox) and not isinstance(page.model, EditableComboBox)
    assert [page.effort.itemData(i) for i in range(page.effort.count())] == ['', 'low']
    page.model.setCurrentIndex(page.model.findData('deep'))
    assert [page.effort.itemData(i) for i in range(page.effort.count())] == ['', 'high']
    page.save()
    saved = agent_group.load_room(base, owner, room['id'])
    assert saved['members'][0]['model'] == 'deep'
    assert saved['model_catalogs']['codex']['models'] == {'small': ['low'], 'deep': ['high']}
    page.deleteLater()
    house.deleteLater()
    app.processEvents()
