import json
from pathlib import Path

import pytest

from device_gateway import DeviceStore


def test_pairing_is_one_use_and_persists_only_hash(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    code = store.create_code()
    result = store.pair(code, 'Test Phone')
    assert result['device_id'].startswith('dev_')
    assert result['token']
    assert 'chat:read' in result['scopes']
    raw = (tmp_path / 'devices.json').read_text('utf-8')
    assert result['token'] not in raw
    with pytest.raises(ValueError):
        store.pair(code, 'Second Phone')


def test_authenticate_revoke_and_scope_filter(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    code = store.create_code()
    result = store.pair(code, 'Phone', scopes=('chat:read', 'not-a-scope'))
    assert result['scopes'] == ['chat:read']
    device = store.authenticate(result['token'])
    assert device and device['id'] == result['device_id']
    assert store.authenticate('wrong-token') is None
    assert store.revoke(result['device_id']) is True
    assert store.authenticate(result['token']) is None
    assert store.revoke('missing') is False


def test_expired_code_is_rejected(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    code = store.create_code(ttl=30)
    # Force expiry without sleeping.
    with store._locked() as db:
        db.execute('UPDATE codes SET expires=0')
    with pytest.raises(ValueError):
        store.pair(code, 'Late Phone')


def test_listing_never_contains_tokens(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    result = store.pair(store.create_code(), 'Phone')
    listing = json.dumps(store.list_devices(), ensure_ascii=False)
    assert result['token'] not in listing
    assert 'Phone' in listing


def test_pairing_code_survives_gui_server_instance_boundary_and_scopes_are_bounded(tmp_path):
    path = tmp_path / 'devices.json'
    gui = DeviceStore(path)
    code = gui.create_code(scopes=('agents:read', 'collab:read', 'collab:write'), owners=('12345',))
    server = DeviceStore(path)
    paired = server.pair(code, '手机')
    auth = DeviceStore(path).authenticate(paired['token'])
    assert auth['owners'] == ['12345'] and 'collab:write' in auth['scopes']
    assert code.encode() not in path.with_suffix('.pairing.sqlite3').read_bytes()
    with pytest.raises(ValueError):
        gui.pair(code, '重复配对')
    read_code = gui.create_code()
    with pytest.raises(ValueError, match='权限'):
        server.pair(read_code, '提权尝试', scopes=('collab:write',))


def test_expired_device_token_is_rejected(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    code = store.create_code()
    paired = store.pair(code, 'expiring phone')
    assert paired['device_id']
    data = __import__('json').loads((tmp_path / 'devices.json').read_text('utf-8'))
    data['devices'][0]['expires_at'] = 1
    (tmp_path / 'devices.json').write_text(__import__('json').dumps(data), 'utf-8')
    assert store.authenticate(paired['token']) is None


def test_refresh_rotates_token_and_extends_expiry(tmp_path):
    store = DeviceStore(tmp_path / 'devices.json')
    code = store.create_code()
    paired = store.pair(code, 'rotate phone')
    refreshed = store.refresh(paired['token'])
    assert refreshed and refreshed['token'] != paired['token']
    assert refreshed['expires_at'] > 0
    assert store.authenticate(paired['token']) is None
    assert store.authenticate(refreshed['token']) is not None
    assert store.refresh('not-a-token') is None
