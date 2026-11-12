"""Real local HTTP/file permissions, with synthetic owners and no model processes."""
import http.client
import json
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

import codex_settings
from device_gateway import DEFAULT_SCOPES, DeviceStore
import mobile_settings
import vnext_bridge


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.setattr(vnext_bridge, '_root', lambda: tmp_path)
    monkeypatch.setattr(vnext_bridge, 'TOKEN', 'synthetic-admin-only')
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low'], 'deep': ['high']})
    def no_process(*args, **kwargs):
        raise AssertionError('设置读写不能启动模型、CLI、QQ或下载')
    monkeypatch.setattr(subprocess, 'Popen', no_process)
    for owner in ('12345', '67890'):
        directory = tmp_path/'friends'/owner
        directory.mkdir(parents=True)
        profile = {'qq_id': owner, 'style': '保留此好友人设', 'other': {'keep': True},
                   'model_api': {'provider': 'codex', 'codex_model': 'fast', 'codex_effort': 'low',
                       'api_key': 'synthetic-private-value', 'api_base_url': 'https://synthetic-secret.invalid/v1'},
                   'agent_adapters': {'zcode': {'model': 'zcode-configured', 'command': ['private-command-path']}}}
        (directory/'friend.json').write_text(json.dumps(profile, ensure_ascii=False), 'utf-8')
    store = vnext_bridge.device_store(tmp_path)
    def pair(scopes, owners=('12345',)):
        return store.pair(store.create_code(scopes=scopes, owners=owners), '合成设备')
    devices = {'old': pair(DEFAULT_SCOPES), 'read': pair(('settings:read',)),
               'write': pair(('settings:read','settings:write')), 'both': pair(('settings:read','settings:write'), ('12345','67890'))}
    server = ThreadingHTTPServer(('127.0.0.1', 0), vnext_bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(kind, action, owner='12345', **body):
        token = devices[kind]['token'] if kind in devices else kind
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=4)
        conn.request('POST', '/api/settings', json.dumps(dict(body, action=action, owner=owner)),
                     {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result
    yield tmp_path, store, devices, request
    server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_old_pairings_are_not_silently_granted_settings_and_writes_need_owner(service):
    root, store, devices, request = service
    assert request('old', 'get')[0] == 403
    fresh = store.refresh(devices['old']['token'])
    assert not {'settings:read', 'settings:write'} & set(store.authenticate(fresh['token'])['scopes'])
    with pytest.raises(ValueError, match='好友范围'):
        store.create_code(scopes=('settings:write',), owners=())
    assert request('invalid-token', 'get')[0] == 401


def test_public_catalog_and_saved_fields_never_include_credentials_or_other_friend(service):
    _, _, _, request = service
    code, snapshot = request('read', 'get')
    assert code == 200 and snapshot['owner'] == '12345'
    text = json.dumps(snapshot)
    assert all(secret not in text for secret in ('synthetic-private-value','synthetic-secret.invalid','private-command-path'))
    assert request('read', 'owners', owner='')[1]['items'][0]['qq'] == '12345'
    assert request('read', 'get', owner='67890')[0] == 403
    assert next(row for row in snapshot['catalog'] if row['provider'] == 'zcode')['models'] == [
        {'id': 'zcode-configured', 'efforts': []}]


def test_save_merges_only_current_owner_settings_and_preserves_literal_text(service):
    root, _, _, request = service
    code, before = request('write', 'get')
    path = root/'friends/12345/friend.json'
    other_before = (root/'friends/67890/friend.json').read_bytes()
    profile = json.loads(path.read_text('utf-8'))
    profile['other']['written_elsewhere'] = True
    path.write_text(json.dumps(profile), 'utf-8')
    literal = '模型额度用完了。\n{task_id} 原样保留'
    status, result = request('write', 'save', revision=before['revision'],
        background_model={'provider':'codex','model':'deep','effort':'high'}, quota_reply=literal)
    assert status == 200 and result['background_model']['model'] == 'deep'
    saved = json.loads(path.read_text('utf-8'))
    assert saved['other']['written_elsewhere'] and saved['style'] == profile['style']
    assert saved['model_api']['codex_model'] == 'fast' and saved['model_api']['quota_reply'] == literal
    assert (root/'friends/67890/friend.json').read_bytes() == other_before


def test_read_only_device_and_forged_admin_cannot_write(service):
    root, _, _, request = service
    before = (root/'friends/12345/friend.json').read_bytes()
    revision = request('read', 'get')[1]['revision']
    assert request('read', 'save', revision=revision, background_model={}, quota_reply='x', admin=True)[0] == 403
    assert request('old', 'save', revision=revision, background_model={}, quota_reply='x', admin=True)[0] == 403
    assert (root/'friends/12345/friend.json').read_bytes() == before


def test_stale_revision_and_cross_owner_async_payload_are_rejected_without_write(service):
    root, _, _, request = service
    first = request('both', 'get')[1]
    assert request('both', 'save', revision=first['revision'], background_model={}, quota_reply='第一条')[0] == 200
    assert request('both', 'save', revision=first['revision'], background_model={}, quota_reply='过期草稿')[0] == 409
    other_before = (root/'friends/67890/friend.json').read_bytes()
    assert request('both', 'save', owner='67890', revision=first['revision'], background_model={}, quota_reply='不能串入')[0] == 409
    assert (root/'friends/67890/friend.json').read_bytes() == other_before


@pytest.mark.parametrize('route,text,extra', [
    ({'provider':'codex','model':'unknown-model','effort':'high'}, 'x', {}),
    ({'provider':'codex','model':'fast','effort':'high'}, 'x', {}),
    ({'provider':'zcode','model':'zcode-configured','effort':'high'}, 'x', {}),
    ({}, '字'*1201, {}), ({}, 'x', {'model_api': {'provider':'forged'}}),
    ({'provider':'codex','api_key':'synthetic-forged'}, 'x', {})])
def test_invalid_settings_never_partially_write(service, route, text, extra):
    root, _, _, request = service
    revision = request('write', 'get')[1]['revision']
    path = root/'friends/12345/friend.json'
    before = path.read_bytes()
    assert request('write', 'save', revision=revision, background_model=route, quota_reply=text, **extra)[0] == 400
    assert path.read_bytes() == before


def test_reply_limit_and_inherit_roundtrip(service):
    _, _, _, request = service
    revision = request('write', 'get')[1]['revision']
    code, saved = request('write', 'save', revision=revision, background_model={}, quota_reply='字'*1200)
    assert code == 200 and len(saved['quota_reply']) == 1200 and saved['background_model'] == {}
    code, saved = request('write', 'save', revision=saved['revision'], background_model={}, quota_reply=' \n ')
    assert code == 200 and saved['quota_reply'] == ''
