"""Real SQLite/Qt/configuration; model transport is explicitly substituted."""
import json
import os
from types import SimpleNamespace

import pytest
import agent_group
import collaboration_view


@pytest.fixture
def flow(tmp_path, monkeypatch):
    base = tmp_path / 'data'
    owner = '12345'
    directory = tmp_path / 'friends' / owner
    directory.mkdir(parents=True)
    profile = {'qq_id': owner, 'bridge_enabled': True, 'cooperation_enabled': True,
               'model_api': {'provider': 'codex', 'codex_model': 'test-model', 'codex_effort': 'low',
                             'active_providers': ['codex']}}
    (directory / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    import codex_settings
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'test-model': ['low']})
    room = agent_group.create_room(base, owner, '核对流程')
    nodes = [agent_group.add_member(base, owner, room['id'], 'codex', name, '分工', 'test-model')
             for name in ('统筹', '研究', '复核')]
    agent_group.update_room(base, owner, room['id'], host_id=nodes[0]['id'])
    agent_group.update_node(base, owner, room['id'], nodes[1]['id'], parent_id=nodes[0]['id'])
    agent_group.update_node(base, owner, room['id'], nodes[2]['id'], parent_id=nodes[1]['id'])
    return SimpleNamespace(base=base, owner=owner, directory=directory, profile=profile, room=room, nodes=nodes)


def test_disabling_parent_retains_child_setting_and_reenable_restores_it(flow):
    room = agent_group.update_node(flow.base, flow.owner, flow.room['id'], flow.nodes[1]['id'], enabled=False)
    states = agent_group.node_states(room, flow.profile)
    assert states[flow.nodes[1]['id']] == (False, '节点已关闭')
    assert states[flow.nodes[2]['id']] == (False, '上级节点不可用')
    assert room['members'][2].get('enabled', True) is True
    room = agent_group.update_node(flow.base, flow.owner, flow.room['id'], flow.nodes[1]['id'], enabled=True)
    assert all(enabled for enabled, _ in agent_group.node_states(room, flow.profile).values())
    profile = dict(flow.profile, cooperation_enabled=False)
    assert not any(enabled for enabled, _ in agent_group.node_states(room, profile).values())
    profile = dict(flow.profile, model_api={'active_providers': []})
    assert not any(enabled for enabled, _ in agent_group.node_states(room, profile).values())


def test_cycles_are_rejected_even_when_closed_without_changing_saved_configuration(flow):
    before = agent_group.load_room(flow.base, flow.owner, flow.room['id'])
    with pytest.raises(ValueError, match='循环'):
        agent_group.update_node(flow.base, flow.owner, flow.room['id'], flow.nodes[0]['id'],
                                enabled=False, parent_id=flow.nodes[2]['id'])
    assert agent_group.load_room(flow.base, flow.owner, flow.room['id']) == before


def test_hierarchical_execution_uses_actual_parent_edges_and_records_real_returns(flow, monkeypatch):
    import multi_agent_orchestrator
    calls = []
    def model(provider, prompt, directory, profile, **kwargs):
        calls.append(prompt)
        assert provider == 'codex' and kwargs['max_retries'] == 0 and kwargs['ephemeral']
        if '候选成员：' in prompt:
            candidates = json.loads(prompt.split('候选成员：')[1].split('\n模型目录：')[0])
            return json.dumps({'summary': '分工', 'assignments': [dict(member_id=m['member_id'],
                model='test-model', effort='low', task=m['name'] + '独立核对') for m in candidates]}), None
        return '实际返回内容', None
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', model)
    room = agent_group.load_room(flow.base, flow.owner, flow.room['id'])
    assert agent_group.run_configured(flow.base, flow.owner, room, '核对任务', flow.directory, flow.profile) == '实际返回内容'
    # Root plans for its direct child; that child plans for its own child, then both summarise.
    assert len(calls) == 5
    first = json.loads(calls[0].split('候选成员：')[1].split('\n模型目录：')[0])
    second = json.loads(calls[1].split('候选成员：')[1].split('\n模型目录：')[0])
    assert [x['member_id'] for x in first] == [flow.nodes[1]['id']]
    assert [x['member_id'] for x in second] == [flow.nodes[2]['id']]
    data = collaboration_view.snapshot(flow.base, owner=flow.owner)
    assert data['summary']['threads'] == 1 and data['summary']['active'] == data['summary']['waiting'] == 0
    assert data['threads'][0]['task_status'] == 'COMPLETED'
    assert collaboration_view.snapshot(flow.base, owner='99999')['threads'] == []


def test_late_model_return_is_not_delivered_after_parent_is_closed(flow, monkeypatch):
    import multi_agent_orchestrator
    def model(*args, **kwargs):
        agent_group.update_node(flow.base, flow.owner, flow.room['id'], flow.nodes[0]['id'], enabled=False)
        return '迟到结果不得显示或发送', None
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', model)
    room = agent_group.load_room(flow.base, flow.owner, flow.room['id'])
    with pytest.raises(PermissionError):
        agent_group.run_configured(flow.base, flow.owner, room, '任务', flow.directory, flow.profile)
    assert not any('迟到结果' in m['content'] for m in agent_group.load_room(flow.base, flow.owner, flow.room['id'])['messages'])
    assert collaboration_view.snapshot(flow.base, owner=flow.owner)['threads'][0]['task_status'] == 'FAILED'


def test_private_entry_does_not_fall_back_to_other_friend_or_duplicate_enabled_roots(flow):
    agent_group.update_room(flow.base, flow.owner, flow.room['id'], entry_kind='private')
    assert agent_group.entry_flow(flow.base, flow.owner, 'private')['id'] == flow.room['id']
    assert agent_group.entry_flow(flow.base, '99999', 'private') is None
    other = agent_group.create_room(flow.base, flow.owner, '第二入口')
    member = agent_group.add_member(flow.base, flow.owner, other['id'], 'codex')
    other = agent_group.load_room(flow.base, flow.owner, other['id'])
    other['host_id'] = member['id']
    other.update(flow_version=1, entry_kind='private')
    agent_group.save_room(flow.base, flow.owner, other)
    with pytest.raises(ValueError, match='多个启用流程'):
        agent_group.entry_flow(flow.base, flow.owner, 'private')


def test_empty_draft_root_does_not_take_over_workbench_or_private_chat(flow):
    draft = agent_group.create_room(flow.base, flow.owner, '未配置的入口')
    agent_group.update_room(flow.base, flow.owner, draft['id'], entry_kind='private', flow_version=1)
    assert agent_group.entry_flow(flow.base, flow.owner, 'private') is None


def test_configuration_tree_is_separate_editable_and_grays_disabled_branch(flow):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QWidget
    from house_qt.cooperation_flow import CooperationFlowPage
    app = QApplication.instance() or QApplication([])
    house = QWidget()
    house.current_qq = flow.owner
    page = CooperationFlowPage(house, base=flow.base)
    page.resize(1000, 700)
    page.pick((flow.room['id'], flow.nodes[1]['id']))
    page.enabled.setChecked(False)
    page.save()
    disabled = [node for _, node in page.tree.boxes if node.get('disabled')]
    assert {node['index'][1] for node in disabled} == {flow.nodes[1]['id'], flow.nodes[2]['id']}
    assert all(page.tree.color(node).name() == '#94a3b8' for node in disabled)
    house.current_qq = '99999'
    page.refresh()
    app.processEvents()
    assert page.tree.rows == [] and page.rooms == {} and not page.editor.isEnabled()
    page.deleteLater()
    house.deleteLater()


def test_configuration_children_fork_left_and_right_without_moving_execution_tree(flow):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from house_qt.cooperation_flow import FlowTree
    from house_qt.collaboration import TreeView
    app = QApplication.instance() or QApplication([])
    def node(name, children=()):
        return dict(type='agent', index=name, label=name, short=name, agent_id=name,
                    detail='', brief='配置', children=list(children))
    root = node('主节点', [node('研究'), node('复核')])
    tree = FlowTree(lambda _: None)
    tree.resize(800, 500)
    tree.set_thread({'tree': [root]}, {})
    assert tree.centre(1).x() < tree.centre(0).x() < tree.centre(2).x()
    assert tree.centre(1).y() == tree.centre(2).y() > tree.centre(0).y()
    assert not tree.boxes[1][0].intersects(tree.boxes[2][0])
    original = TreeView()
    original.set_thread({'tree': root}, {})
    assert original.centre(1).y() < original.centre(2).y()
    tree.deleteLater()
    original.deleteLater()
    app.processEvents()
