import json
from pathlib import Path

import pytest

import account_service


def test_add_list_remove_without_storing_a_secret(tmp_path):
    row = account_service.add(tmp_path, {'label': 'Codex work', 'provider': 'codex',
                                         'model': 'm', 'key_id': 'key-1'})
    assert row['id'].startswith('acct_')
    assert account_service.list_accounts(tmp_path)[0]['label'] == 'Codex work'
    raw = (tmp_path / 'data' / 'vnext_accounts.json').read_text('utf-8')
    assert 'api_key' not in raw
    assert account_service.remove(tmp_path, row['id']) is True
    assert account_service.list_accounts(tmp_path) == []


def test_rejects_invalid_provider_and_secret_fields(tmp_path):
    with pytest.raises(ValueError):
        account_service.add(tmp_path, {'label': 'x', 'provider': 'not-real'})
    with pytest.raises(ValueError):
        account_service.add(tmp_path, {'label': 'x', 'provider': 'codex', 'api_key': 'nope'})


def test_discover_reads_only_profiles(tmp_path):
    directory = tmp_path / 'friends' / '123'
    directory.mkdir(parents=True)
    (directory / 'friend.json').write_text(json.dumps({'model_api': {'provider': 'claude', 'model': 'm'}}), 'utf-8')
    rows = account_service.discover(tmp_path)
    assert rows[0]['provider'] == 'claude'
    assert 'api_key' not in rows[0]


def test_add_with_key_never_writes_plaintext_and_removes_secret(tmp_path):
    from api_backend import load_key
    row = account_service.add_with_key(tmp_path, {'label': 'Secure', 'provider': 'codex', 'model': 'm'}, 'sk-test-secret')
    raw = (tmp_path / 'data' / 'vnext_accounts.json').read_text('utf-8')
    assert 'sk-test-secret' not in raw
    assert row['key_id'] == row['id']
    assert load_key(tmp_path / 'data' / 'vnext_accounts' / row['id'], 'chat') == 'sk-test-secret'
    assert account_service.remove(tmp_path, row['id']) is True
    assert not (tmp_path / 'data' / 'vnext_accounts' / row['id']).exists()


def test_probe_uses_stored_dpapi_key(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen['auth'] = self.headers.get('Authorization')
            body = json.dumps({'data': [{'id': 'model-a'}, {'id': 'model-b'}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        row = account_service.add_with_key(tmp_path, {'label': 'Probe', 'provider': 'custom',
                                                      'base_url': f'http://127.0.0.1:{server.server_port}'}, 'sk-probe')
        result = account_service.probe(tmp_path, row['id'])
        assert result['ok'] is True
        assert result['model_count'] == 2
        assert seen['auth'] == 'Bearer sk-probe'
    finally:
        server.shutdown()


def test_bind_friend_to_account_and_route(tmp_path):
    row = account_service.add(tmp_path, {'label': 'Bound', 'provider': 'codex'})
    assert account_service.bind(tmp_path, '123456789', row['id'])['qq'] == '123456789'
    routed = account_service.route(tmp_path, '123456789')
    assert routed and routed['id'] == row['id']
    assert account_service.route(tmp_path, '999999999') is None
    with pytest.raises(ValueError):
        account_service.bind(tmp_path, 'not-qq', row['id'])


def test_chat_uses_stored_key_and_returns_model_text(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            seen['body'] = json.loads(self.rfile.read(length))
            seen['auth'] = self.headers.get('Authorization')
            body = json.dumps({'choices': [{'message': {'content': 'mock reply'}}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        row = account_service.add_with_key(tmp_path, {'label': 'Chat', 'provider': 'custom',
                                                      'base_url': f'http://127.0.0.1:{server.server_port}',
                                                      'model': 'mock-model'}, 'sk-chat')
        result = account_service.chat(tmp_path, row['id'], 'hello')
        assert result['ok'] is True and result['content'] == 'mock reply'
        assert seen['auth'] == 'Bearer sk-chat'
        assert seen['body']['model'] == 'mock-model'
        assert seen['body']['messages'][0]['content'] == 'hello'
    finally:
        server.shutdown()


def test_activate_and_apply_to_friend_profile(tmp_path):
    friend = tmp_path / 'friends' / '12345'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text('{}', 'utf-8')
    row = account_service.add(tmp_path, {'label': 'OpenCode free', 'provider': 'opencode',
                                         'model': 'opencode-go/mimo-v2.6-flash'})
    account_service.activate(tmp_path, 'opencode', row['id'])
    assert account_service.active_accounts(tmp_path)['opencode'] == row['id']
    result = account_service.apply_to_friend(tmp_path, '12345', row['id'])
    assert result['provider'] == 'opencode'
    profile = json.loads((friend / 'friend.json').read_text('utf-8'))
    assert profile['model_api']['model'] == 'opencode-go/mimo-v2.6-flash'
    assert profile['agent_adapters']['opencode']['model'] == 'opencode-go/mimo-v2.6-flash'


def test_export_import_catalog_is_nonsecret_and_deduplicated(tmp_path):
    first = account_service.add(tmp_path, {'label': 'OpenCode', 'provider': 'opencode',
                                           'model': 'opencode-go/mimo-v2.6-flash'})
    payload = account_service.export_catalog(tmp_path)
    assert 'api_key' not in json.dumps(payload)
    assert payload['accounts'][0]['label'] == 'OpenCode'
    target = tmp_path / 'target'; target.mkdir()
    result = account_service.import_catalog(target, payload)
    assert result['added'] == 1
    again = account_service.import_catalog(target, payload)
    assert again['added'] == 0


def test_clone_copies_metadata_without_secret(tmp_path):
    row = account_service.add(tmp_path, {'label': 'Base', 'provider': 'opencode',
                                         'model': 'opencode-go/mimo-v2.6-flash'})
    copy = account_service.clone(tmp_path, row['id'])
    assert copy['id'] != row['id']
    assert copy['label'].endswith('副本')
    assert copy['model'] == row['model']
    assert 'api_key' not in json.dumps(copy)
