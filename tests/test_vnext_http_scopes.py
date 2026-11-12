"""Real HTTP/SQLite authorization checks with synthetic identities only."""
import http.client
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import pytest

import vnext_bridge
import voice_invites


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(vnext_bridge, '_root', lambda: tmp_path)
    monkeypatch.setattr(vnext_bridge, 'TOKEN', 'synthetic-admin-token')
    for owner in ('12345', '67890'):
        directory = tmp_path / 'friends' / owner
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text(json.dumps({'basic_info': {'name': '好友'+owner}}), 'utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    with sqlite3.connect(data / 'tasks.sqlite3') as db:
        db.execute('CREATE TABLE tasks(task_id TEXT,title TEXT,status TEXT,created_at TEXT,updated_at TEXT,owner_id TEXT)')
        db.execute("INSERT INTO tasks VALUES('owned','我的任务','DONE','','','12345')")
        db.executemany("INSERT INTO tasks VALUES(?,?,'DONE','','','67890')", [(f'other-{i}', '其他好友任务') for i in range(60)])
    store = vnext_bridge.device_store(tmp_path)
    own = store.pair(store.create_code(owners=('12345',), scopes=('chat:read','tasks:read','voice:stream')), '合成设备')
    no_scope = store.pair(store.create_code(owners=('12345',), scopes=('agents:read',)), '合成只读设备')
    no_owner = store.pair(store.create_code(scopes=('chat:read','tasks:read')), '合成未绑定设备')
    server = ThreadingHTTPServer(('127.0.0.1', 0), vnext_bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, token='', body=None, origin='', length=None):
        headers = {'Authorization': 'Bearer '+token} if token else {}
        if origin:
            headers['Origin'] = origin
        if length is not None:
            headers['Content-Length'] = length
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request('POST' if body is not None else 'GET', path, body=payload, headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result
    yield tmp_path, own, no_scope, no_owner, request
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_absent_admin_configuration_does_not_enable_anonymous_access(endpoint, monkeypatch):
    _, _, _, _, request = endpoint
    monkeypatch.setattr(vnext_bridge, 'TOKEN', '')
    assert request('/api/health')[0] == 200
    assert request('/api/friends')[0] == 401
    assert request('/api/accounts/add', body={'id': 'forged', 'admin': True})[0] == 401
    assert request('/api/device/pairing-code', body={})[0] == 401


def test_device_reads_only_its_owner_and_filters_before_task_limit(endpoint):
    _, own, _, _, request = endpoint
    token = own['token']
    code, rows = request('/api/friends', token)
    assert code == 200 and [row['qq'] for row in rows['items']] == ['12345']
    code, rows = request('/api/tasks', token)
    assert code == 200 and [row['id'] for row in rows['items']] == ['owned']
    code, report = request('/api/overview', token)
    assert code == 200 and report['friends'] == report['tasks'] == 1
    assert 'root' not in report and report['mail_messages'] is None and report['memories'] is None
    assert request('/api/overview', 'synthetic-admin-token')[1]['tasks'] == 61


def test_missing_scope_or_owner_never_falls_back_to_global_data(endpoint):
    _, _, no_scope, no_owner, request = endpoint
    assert request('/api/tasks', no_scope['token'])[0] == 403
    assert request('/api/friends', no_scope['token'])[0] == 403
    assert request('/api/friends', no_owner['token'])[1]['items'] == []
    assert request('/api/tasks', no_owner['token'])[1]['items'] == []


@pytest.mark.parametrize('path', ['/api/accounts', '/api/providers', '/api/devices', '/api/accounts/route?qq=67890',
                                  '/api/providers/route?qq=67890', '/api/accounts?admin=true', '/api/devices?owner=12345'])
def test_device_cannot_read_global_administrative_catalogues(endpoint, path):
    _, own, _, _, request = endpoint
    assert request(path, own['token'])[0] == 403


def test_health_read_scope_does_not_grant_unscoped_global_health_data_or_writes(endpoint, monkeypatch):
    from health_bridge import HealthBridge
    root, _, _, _, request = endpoint
    store = vnext_bridge.device_store(root)
    phone = store.pair(store.create_code(owners=('12345',), scopes=('health:read',)), '健康合成设备')
    health = HealthBridge(root/'data/health_bridge.json')
    health.report('garmin', supported=True, note='其他设备的私有健康备注')
    original = health.path.read_bytes()
    assert request('/api/health-bridge', phone['token'])[0] == 403
    for path in ('/api/health/report', '/api/health/events'):
        assert request(path, phone['token'], {'adapter':'garmin','supported':False,'admin':True})[0] == 403
    assert health.path.read_bytes() == original
    assert request('/api/health-bridge', 'synthetic-admin-token')[0] == 200


def test_device_identity_is_authenticated_once_and_revocation_applies_to_next_request(endpoint, monkeypatch):
    root, own, _, _, request = endpoint
    store = vnext_bridge.device_store(root)
    original = store.authenticate
    calls = []
    def checked(token):
        calls.append(token)
        return original(token)
    monkeypatch.setattr(store, 'authenticate', checked)
    assert request('/api/tasks?owner=67890&admin=true', own['token'])[1]['items'][0]['id'] == 'owned'
    assert len(calls) == 1
    store.revoke(own['device_id'])
    assert request('/api/tasks', own['token'])[0] == 401


def test_writes_enforce_permissions_before_account_or_model_calls(endpoint, monkeypatch):
    import account_service
    import main
    from health_bridge import HealthBridge
    called = []
    def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError('未经授权的请求不应到达业务操作')
    monkeypatch.setattr(account_service, 'add', forbidden)
    monkeypatch.setattr(account_service, 'add_with_key', forbidden)
    monkeypatch.setattr(main, 'make_reply', forbidden)
    monkeypatch.setattr(HealthBridge, 'report', forbidden)
    _, own, _, _, request = endpoint
    assert request('/api/accounts/add', own['token'], {'admin': True, 'id': 'forged'})[0] == 403
    assert request('/api/voice/call', own['token'], {'qq': '67890', 'text': '不得跨好友调用模型'})[0] == 403
    assert request('/api/health/report', own['token'], {'adapter': 'forged'})[0] == 403
    assert called == []


def test_voice_invitation_isolation_includes_owner_and_target_device(endpoint):
    root, own, _, _, request = endpoint
    mine = voice_invites.create(root, '12345', own['device_id'])
    other_owner = voice_invites.create(root, '67890')
    voice_invites.create(root, '12345', 'different-device')
    code, rows = request('/api/voice/invites', own['token'])
    assert code == 200 and [row['id'] for row in rows['items']] == [mine['id']]
    assert request('/api/voice/invites/respond', own['token'], {'id': other_owner['id'], 'accept': True})[0] == 403
    assert request('/api/voice/invites/respond', own['token'], {'id': mine['id'], 'accept': False})[0] == 200


def test_untrusted_browser_origin_and_negative_request_length_are_rejected(endpoint):
    _, _, _, _, request = endpoint
    assert request('/api/friends', 'synthetic-admin-token', origin='https://untrusted.invalid')[0] == 403
    assert request('/api/accounts/add', 'synthetic-admin-token', {}, origin='https://untrusted.invalid')[0] == 403
    assert request('/api/device/pair', body={}, length='-1')[0] == 400
    assert request('/api/friends', 'invalid-é')[0] == 401
