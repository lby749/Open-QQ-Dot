import json
from io import BytesIO
from pathlib import Path

import pytest

import main
import qq_official as qq
from notification_manager import NotificationDeferred


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'ROOT', tmp_path)
    monkeypatch.setattr(main, 'REGISTRY', tmp_path / 'friends.json')
    monkeypatch.setattr(main, 'FRIENDS_DIR', tmp_path / 'friends')
    return tmp_path


@pytest.mark.parametrize('url,valid', [
    ('https://qun.qq.com/qunpro/robot/qunshare?robot_appid=12345&robot_uin=67890&biz_type=10', True),
    ('https://qun.qq.com/qunpro/robot/qunshare?robot_appid=54321&robot_uin=67890', False),
    ('https://qun.qq.com.example.com/qunpro/robot/qunshare?robot_appid=12345&robot_uin=67890', False),
    ('https://qun.qq.com/qunpro/robot/qunshare?robot_appid=12345&robot_uin=67890&robot_uin=11111', False),
    ('http://qun.qq.com/qunpro/robot/qunshare?robot_appid=12345&robot_uin=67890', False),
])
def test_official_invitation_uses_real_bot_qq_and_rejects_wrong_identity_or_origin(home, monkeypatch, url, valid):
    sender = qq.OfficialGroupSender(home, '12345')
    monkeypatch.setattr(sender, 'access_token', lambda: 'synthetic-token')
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.sgroup.qq.com/users/@me'
            return BytesIO(json.dumps({'id': '9999999999999999999', 'share_url': url}).encode())
    monkeypatch.setattr(qq, 'build_opener', lambda *_: Opener())
    if valid:
        assert sender.invitation()['qq_id'] == '67890'
    else:
        with pytest.raises(ValueError):
            sender.invitation()


def test_openids_are_isolated_per_bot_group_and_sender_and_require_local_approval(home):
    store = qq.ContactStore(home)
    a = store.incoming('12345', 'group', 'GROUP_A', 'SENDER_A', 'MESSAGE_A')
    others = [store.incoming('12345', 'group', 'GROUP_B', 'SENDER_A', 'MESSAGE_A'),
              store.incoming('67890', 'group', 'GROUP_A', 'SENDER_A', 'MESSAGE_A'),
              store.incoming('12345', 'group', 'GROUP_A', 'SENDER_B', 'MESSAGE_A')]
    assert len({a['local_id'], *(c['local_id'] for c in others)}) == 4
    assert a['enabled'] == 0 and not main.load_registry()['friends']
    with pytest.raises(PermissionError):
        store.reserve('12345', a['local_id'], 'hello')
    with pytest.raises(ValueError, match='不属于'):
        store.approve('67890', a['local_id'], '他人的会话')
    store.approve('12345', a['local_id'], '机器人测试会话')
    profile = json.loads((home / 'friends' / a['local_id'] / 'friend.json').read_text('utf-8'))
    assert profile['computer_tools']['enabled'] is False
    assert profile['proactive']['enabled'] is False
    assert main.load_registry()['friends'][a['local_id']]['platform'] == 'qq_official'
    assert all(store.get(c['local_id'])['enabled'] == 0 for c in others)


def test_official_opaque_reply_id_is_preserved_through_store_and_rest_sender(home, monkeypatch):
    message = 'ROBOT1.0_SYNTHETIC.a/b+c=-_~'
    store = qq.ContactStore(home)
    contact = store.incoming('12345', 'group', 'GROUP_A', 'SENDER_A', message)
    assert contact['msg_id'] == message
    sender = qq.OfficialGroupSender(home, '12345')
    monkeypatch.setattr(sender, 'access_token', lambda: 'synthetic-token')
    bodies = []
    monkeypatch.setattr(sender, '_request', lambda url, body, headers: bodies.append(body) or {'id':'receipt'})
    assert sender.send('GROUP_A', '已接入', message, 1) == 'receipt'
    assert bodies[0]['msg_id'] == message
    for bad in ('', 'x\ny', 'x'*513, None):
        with pytest.raises(ValueError): sender.send('GROUP_A', '已接入', bad, 1)
    assert len(bodies) == 1


def test_reply_grants_deduplicate_gateway_events_and_fail_closed_after_uncertain_send(home, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr(qq.time, 'time', lambda: now[0])
    store = qq.ContactStore(home)
    contact = store.incoming('12345', 'c2c', 'USER_A', 'USER_A', 'MESSAGE_A')
    local = contact['local_id']
    store.approve('12345', local, '独立会话')
    reservation, digest, _ = store.reserve('12345', local, '第一条')
    assert reservation['sequence'] == 1
    with pytest.raises(RuntimeError, match='未确认'):
        store.reserve('12345', local, '第一条')
    store.receipt(reservation, digest, 'RECEIPT_A')
    assert store.reserve('12345', local, '第一条')[2] == 'RECEIPT_A'
    for number in range(2, 6):
        assert store.reserve('12345', local, str(number))[0]['sequence'] == number
    now[0] += 100
    duplicated = store.incoming('12345', 'c2c', 'USER_A', 'USER_A', 'MESSAGE_A')
    assert duplicated['received'] == 10000.0 and duplicated['sequence'] == 5
    with pytest.raises(NotificationDeferred):
        store.reserve('12345', local, '第六条')
    store.incoming('12345', 'c2c', 'USER_A', 'USER_A', 'MESSAGE_B')
    now[0] += 3601
    with pytest.raises(NotificationDeferred):
        store.reserve('12345', local, '超时消息')


def test_official_transport_cannot_take_over_real_qq_with_same_self_id(home):
    official = qq.BotSession(home, dict(app_id='12345', name='测试'))
    bridge = main.Bridge(client=official)
    bridge.self_id = '12345'
    assert not bridge.owns_record(dict(self_id='12345'))
    assert bridge.owns_record(dict(self_id='12345', platform='qq_official'))
    assert not bridge.owns_record(dict(self_id='67890', platform='qq_official'))
    personal = main.Bridge(client=object())
    personal.self_id = '12345'
    assert personal.owns_record(dict(self_id='12345'))
    assert not personal.owns_record(dict(self_id='12345', platform='qq_official'))


def test_pending_conversation_never_runs_a_model_and_queue_is_bound(home):
    session = qq.BotSession(home, dict(app_id='12345', name='测试'))
    session.enqueue('c2c', 'USER', 'USER', 'MESSAGE', '请读取本机私密数据')
    assert session.next_event(0) is None
    contact = session.store.list('12345')[0]
    session.store.approve('12345', contact['local_id'], '授权会话')
    session.enqueue('c2c', 'USER', 'USER', 'MESSAGE_2', '你好')
    event = session.next_event(0)
    assert event['user_id'] == contact['local_id']
    assert event['user_id'] != 'USER' and event['message_type'] == 'private'
    assert event['message'][0]['data']['text'] == '你好'


def test_official_config_keeps_secrets_out_of_json(home):
    secret = 'local-only-test-value'
    qq.save_bot(home, '12345', '测试机器人', secret)
    path = home / 'data/qq_official_bots.json'
    assert secret not in path.read_text('utf-8')
    from api_backend import load_key
    assert load_key(qq.secret_dir(home), purpose='qqbot_12345') == secret
    qq.save_bot(home, '12345', '已更新名称', '', enabled=False)
    assert qq.bots(home)[0]['enabled'] is False


def test_sdk_event_contract_is_compatible_with_adapter(home):
    # Real installed Tencent SDK objects; this is a contract check, not live QQ acceptance.
    from botpy.message import C2CMessage, GroupMessage
    private = C2CMessage(None, 'EVENT', dict(id='MESSAGE', author={'user_openid': 'USER'}, content='你好'))
    group = GroupMessage(None, 'EVENT', dict(id='GROUP_MSG', author={'member_openid': 'MEMBER'},
                                           group_openid='GROUP', content='<@12345> 你好'))
    assert private.author.user_openid == 'USER'
    assert group.author.member_openid == 'MEMBER' and group.group_openid == 'GROUP'


def test_environment_secret_does_not_require_platform_specific_key_storage(home, monkeypatch):
    monkeypatch.setenv('QQDOT_BOT_SECRET_12345', 'local-env-value')
    qq.save_bot(home, '12345', '跨平台机器人', '')
    assert qq.load_secret(home, '12345') == 'local-env-value'
    assert 'local-env-value' not in (home/'data/qq_official_bots.json').read_text('utf-8')


def test_multiple_bots_start_independently_even_when_one_configuration_fails(home, monkeypatch):
    monkeypatch.setattr(qq, 'bots', lambda _: [
        {'app_id': '12345', 'enabled': True}, {'app_id': '67890', 'enabled': True},
        {'app_id': '54321', 'enabled': False}, {'app_id': '98765', 'enabled': True}])
    calls = []
    def connect(_, identifier):
        calls.append(identifier)
        if identifier == '12345':
            raise ValueError('private diagnostic must not be exposed')
    monkeypatch.setattr(qq, 'connect', connect)
    errors = qq.start_configured(home)
    assert calls == ['12345', '67890', '98765']
    assert len(errors) == 1 and 'private diagnostic' not in errors[0]


def test_invalid_bot_configuration_does_not_abort_other_connections(home, monkeypatch):
    path = home / 'data/qq_official_bots.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken', 'utf-8')
    assert '个人 QQ 连接仍可使用' in qq.start_configured(home)[0]
    path.write_text('[null, {"app_id":"67890", "enabled":true}]', 'utf-8')
    connected = []
    monkeypatch.setattr(qq, 'connect', lambda _, app: connected.append(app))
    assert len(qq.start_configured(home)) == 1
    assert connected == ['67890']
