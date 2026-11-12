"""Local protocol/receipt checks; no real QQ account, network or model."""
import json
import sqlite3
import time
from types import SimpleNamespace
import pytest
import qq_group_connection as connection
import qq_official


@pytest.fixture
def setup(tmp_path, monkeypatch):
    friend = tmp_path / 'friends/12345'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text('{}', 'utf-8')
    class Client:
        sends = 0
        account = '67890'
        online = True
        joined = False
        failure = False
        def call(self, action, params=None, timeout=10):
            if action == 'get_status': return {'online': self.online, 'good': True}
            if action == 'get_login_info': return {'user_id': self.account}
            if action == 'get_stranger_info': return {'uid': 'u_SYNTHETIC_OWNER'}
            if action == 'get_group_member_info': return {'role': 'owner'}
            if action == 'get_group_member_list': return [{'user_id': 22222}] if self.joined else []
            assert action == 'send_packet'
            self.sends += 1
            packet = connection._fields(bytes.fromhex(params['data']))
            if params['cmd'] == 'OidbSvcTrpcTcp.0x8a1_7':
                assert packet[2] == [7]
                payload = connection._fields(packet[4][0])
                attributes = connection._fields(payload[2][0])
                assert attributes[2] == [b'u_SYNTHETIC_OWNER'] and attributes[10] == ['新协作群'.encode()]
                return connection._message((1, 0x8a1), (2, 7),
                    (4, connection._message((2, 11111)))).hex()
            assert params['cmd'] == 'OidbSvcTrpcTcp.0x9076_1'
            assert connection._fields(packet[4][0]) == {1: [11111], 2: [22222]}
            if self.failure: raise TimeoutError('unknown result')
            self.joined = True
            return connection._message((1, 0x9076), (2, 1)).hex()
    client = Client()
    monkeypatch.setattr(connection, 'OfficialGroupSender', lambda *_: SimpleNamespace(
        invitation=lambda: {'app_id': '33333', 'qq_id': '22222'}))
    service = connection.GroupConnection(tmp_path, '12345', '67890', client)
    monkeypatch.setattr(service, 'group_bots', lambda _: [{'qq_id': '22222'}] if client.joined else [])
    return service, client


def test_official_bot_is_verified_from_robot_directory_and_duplicate_is_not_sent(setup):
    service, client = setup
    assert service.add_bot('11111', '33333', 'one')['state'] == 'VERIFIED'
    assert service.add_bot('11111', '33333', 'two')['state'] == 'VERIFIED'
    assert client.sends == 1


def test_create_group_has_stable_receipt_and_is_not_recreated(setup):
    service, client = setup
    assert service.create_group('新协作群', 'create')['group_id'] == '11111'
    assert service.create_group('新协作群', 'create')['state'] == 'VERIFIED'
    assert client.sends == 1


def test_unknown_side_effect_is_durable_and_never_resent(setup):
    service, client = setup
    client.failure = True
    with pytest.raises(TimeoutError): service.add_bot('11111', '33333', 'stable')
    assert service.add_bot('11111', '33333', 'stable')['state'] == 'UNCERTAIN'
    assert client.sends == 1
    with pytest.raises(ValueError, match='其他群操作'):
        service.add_bot('44444', '33333', 'stable')


@pytest.mark.parametrize('change', ['offline', 'account'])
def test_account_mismatch_and_offline_prevent_side_effects(setup, change):
    service, client = setup
    if change == 'offline': client.online = False
    else: client.account = '99999'
    with pytest.raises((ConnectionError, PermissionError)):
        service.add_bot('11111', '33333', 'denied')
    assert client.sends == 0


@pytest.mark.parametrize('data', [b'\x00', b'\x08\x80', b'\x0f', b'\x0a\x06a', b'\x08' + b'\xff' * 10])
def test_truncated_or_unbounded_wire_responses_are_rejected(data):
    with pytest.raises(ValueError): connection._fields(data)


def test_link_requires_both_real_transports_and_replays_no_challenge(setup, monkeypatch):
    service, client = setup
    client.joined = True
    monkeypatch.setattr(service, 'group_bots', lambda _: pytest.fail('link must verify the native roster without a second web login'))
    events, sent = [], []
    def reply(channel, text, message, sequence):
        events.extend([
            dict(self_id='99999', group_id='11111', user_id='22222'),
            dict(self_id='67890', group_id='99999', user_id='22222'),
            dict(self_id='67890', group_id='11111', user_id='99999'),
            dict(self_id='67890', group_id='11111', user_id='22222')])
        for event in events:
            event.update(message_type='group', message=[{'type': 'text', 'data': {'text': text}}])
        return 'official-receipt'
    monkeypatch.setattr(connection, 'OfficialGroupSender', lambda *_: SimpleNamespace(
        invitation=lambda: {'app_id': '33333', 'qq_id': '22222'}, send=reply))
    def challenge(group, segments):
        text = segments[-1]['data']['text']
        sent.append(text)
        assert connection.capture_link(service.home, '33333', 'OPENID', 'event-1', text)
        assert connection.capture_link(service.home, '33333', 'OPENID', 'event-1', text)
        return {'message_id': 'native-receipt'}
    client.send_group_segments = challenge
    client.next_event = lambda timeout: events.pop(0) if events else None
    result = service.link_bots('11111', ['33333'], 'link')
    assert result['state'] == 'VERIFIED' and result['channels'] == {'33333': 'OPENID'}
    assert service.link_bots('11111', ['33333'], 'link')['channels'] == result['channels']
    assert len(sent) == 1


def test_expired_challenge_and_wrong_app_never_reply(setup, monkeypatch):
    service, _ = setup
    nonce = 'a' * 32
    with sqlite3.connect(service.path) as db:
        db.execute('INSERT INTO links(nonce,owner,self_id,group_id,app,bot,state,expires) VALUES(?,?,?,?,?,?,?,?)',
                   (nonce, '12345', '67890', '11111', '33333', '22222', 'WAITING', time.time()-1))
    monkeypatch.setattr(connection, 'OfficialGroupSender', lambda *_: pytest.fail('No send allowed'))
    assert not connection.capture_link(service.home, '33333', 'OPENID', 'event', '〔QQDOT:' + nonce + '〕')
    assert not connection.capture_link(service.home, '44444', 'OPENID', 'event', '〔QQDOT:' + nonce + '〕')


def test_native_challenge_send_timeout_is_not_repeated(setup):
    service, client = setup
    client.joined = True
    calls = []
    def send(*args):
        calls.append(args)
        raise TimeoutError('receipt unknown')
    client.send_group_segments = send
    with pytest.raises(TimeoutError): service.link_bots('11111', ['33333'], 'link')
    assert service.link_bots('11111', ['33333'], 'link')['state'] == 'UNCERTAIN'
    assert service.link_bots('11111', ['33333'], 'changed-operation-id')['state'] == 'UNCERTAIN'
    assert len(calls) == 1


def test_model_group_tools_are_hidden_without_owner_scope_and_cannot_expand_targets(setup, monkeypatch):
    import agent_group
    import house_settings
    import qq_link
    service, _ = setup
    monkeypatch.setattr(house_settings, 'root', lambda: service.home)
    directory = service.home / 'friends/12345'
    assert connection.tools(directory) == []
    room = agent_group.create_room(service.home/'data', '12345')
    host = agent_group.add_member(service.home/'data', '12345', room['id'], 'codex')
    worker = agent_group.add_member(service.home/'data', '12345', room['id'], 'codex')
    agent_group.update_room(service.home/'data', '12345', room['id'], host_id=host['id'])
    monkeypatch.setattr(qq_official, 'bots', lambda _: [{'app_id':'33333'}])
    connection.save_setup(service.home/'data', '12345', room['id'], {'enabled':True,
        'self_id':'67890', 'group_id':'11111', 'members':['44444'], 'bots':{worker['id']:'33333'}})
    assert connection.tools(directory)[0]['name'] == 'qq_manage_group'
    assert connection.tools(service.home/'friends/99999') == []
    assert connection.tools(service.home/'workspace/12345') == []
    monkeypatch.setattr(qq_link, 'QQClient', lambda: SimpleNamespace(close=lambda: None))
    assert connection.tools(directory)[0]['inputSchema']['properties']['action']['enum'] == ['create','connect']
    assert not hasattr(connection.GroupConnection, 'invite_members')
    with pytest.raises(ValueError, match='仅支持'):
        connection.call(directory, {'room_id':room['id'], 'operation_id':'test', 'action':'invite', 'members':['55555']})
