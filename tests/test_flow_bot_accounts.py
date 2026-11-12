import json
import os

import pytest

import agent_group
import qq_official


def room_with_members(tmp_path, count):
    base = tmp_path/'data'
    room = agent_group.create_room(base, '12345')
    host = agent_group.add_member(base, '12345', room['id'], 'codex', '主账号')
    agent_group.update_room(base, '12345', room['id'], host_id=host['id'], entry_kind='group')
    members = [agent_group.add_member(base, '12345', room['id'], 'codex', f'成员{i}') for i in range(count)]
    return base, room['id'], host, members


def test_five_online_slots_preserve_disabled_nodes_and_can_be_reassigned(tmp_path):
    base, identifier, host, members = room_with_members(tmp_path, 6)
    room = agent_group.load_room(base, '12345', identifier)
    assert len(room['members']) == 7
    assert len(agent_group.active_bot_members(room)) == 5
    assert members[-1]['enabled'] is False
    with pytest.raises(ValueError, match='5 个机器人'):
        agent_group.update_node(base, '12345', identifier, members[-1]['id'], enabled=True)
    agent_group.update_node(base, '12345', identifier, members[0]['id'], enabled=False)
    agent_group.update_node(base, '12345', identifier, members[-1]['id'], enabled=True)
    room = agent_group.load_room(base, '12345', identifier)
    assert len(agent_group.active_bot_members(room)) == 5
    agent_group.update_node(base, '12345', identifier, host['id'], enabled=False)
    assert agent_group.active_bot_members(agent_group.load_room(base, '12345', identifier)) == []


def test_changing_bot_requires_reconnection_and_cannot_reuse_an_active_account(tmp_path, monkeypatch):
    base, identifier, host, members = room_with_members(tmp_path, 2)
    monkeypatch.setattr(qq_official, 'bots', lambda _: [{'app_id': key, 'enabled': True} for key in ('11111', '22222')])
    room = agent_group.load_room(base, '12345', identifier)
    room['qq_group'] = {'enabled': True, 'self_id': '34567', 'group_id': '45678',
                        'bots': {members[0]['id']: {'app_id': '11111', 'group_openid': 'OLD_OPENID'}}}
    agent_group.save_room(base, '12345', room)
    agent_group.update_node(base, '12345', identifier, members[0]['id'], qq_bot_app_id='22222')
    room = agent_group.load_room(base, '12345', identifier)
    assert room['qq_group']['enabled'] is False
    assert room['qq_group_setup']['bots'][members[0]['id']] == '22222'
    before = json.dumps(room, sort_keys=True)
    with pytest.raises(ValueError, match='不能重复'):
        agent_group.update_node(base, '12345', identifier, members[1]['id'], qq_bot_app_id='22222')
    assert json.dumps(agent_group.load_room(base, '12345', identifier), sort_keys=True) == before


def test_sixth_bot_cannot_write_a_secret_before_the_limit_check(tmp_path, monkeypatch):
    rows = [{'app_id': str(10000+i), 'name': str(i), 'enabled': True} for i in range(5)]
    monkeypatch.setattr(qq_official, 'bots', lambda _: rows)
    import api_backend
    writes = []
    monkeypatch.setattr(api_backend, 'save_key', lambda *args: writes.append(args))
    with pytest.raises(ValueError, match='5 个官方机器人'):
        qq_official.save_bot(tmp_path, '22222', '第六个', 'synthetic-secret')
    assert not writes


@pytest.mark.parametrize('parent_switch', [False, True])
def test_reenabling_node_or_ancestor_cannot_activate_duplicate_bot_accounts(tmp_path, parent_switch):
    base, identifier, host, members = room_with_members(tmp_path, 3)
    room = agent_group.load_room(base, '12345', identifier)
    room['members'][2]['enabled'] = False
    if parent_switch:
        room['members'][3]['parent_id'] = members[1]['id']
        second = members[2]['id']
    else:
        second = members[1]['id']
    room['qq_group_setup'] = {'bots': {members[0]['id']: '11111', second: '11111'}}
    agent_group.save_room(base, '12345', room)
    before = json.dumps(room, sort_keys=True)
    with pytest.raises(ValueError, match='不能重复'):
        agent_group.update_node(base, '12345', identifier, members[1]['id'], enabled=True)
    assert json.dumps(agent_group.load_room(base, '12345', identifier), sort_keys=True) == before


def test_node_ui_shows_account_group_and_online_slots(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QWidget
    from house_qt.cooperation_flow import CooperationFlowPage
    base, identifier, host, members = room_with_members(tmp_path, 1)
    directory = tmp_path/'friends/12345'
    directory.mkdir(parents=True)
    (directory/'friend.json').write_text(json.dumps({'model_api': {'provider': 'codex'}}), encoding='utf-8')
    monkeypatch.setattr(qq_official, 'bots', lambda _: [{'app_id': '11111', 'name': '研究机器人', 'enabled': True}])
    room = agent_group.load_room(base, '12345', identifier)
    room['qq_group'] = {'enabled': True, 'self_id': '34567', 'group_id': '45678',
                        'bots': {members[0]['id']: {'app_id': '11111', 'group_openid': 'GROUP_OPENID'}}}
    agent_group.save_room(base, '12345', room)
    app = QApplication.instance() or QApplication([])
    house = QWidget()
    house.current_qq = '12345'
    page = CooperationFlowPage(house, base)
    page.pick((identifier, members[0]['id']))
    assert page.bot_account.currentData() == '11111'
    assert '45678' in page.qq_destination.text() and '34567' in page.qq_destination.text()
    assert '1/5' in page.qq_destination.text()
    assert not page.connect_button.isHidden()
    page.pick((identifier, host['id']))
    assert page.bot_account.isHidden()
    page.deleteLater()
    house.deleteLater()
    app.processEvents()
