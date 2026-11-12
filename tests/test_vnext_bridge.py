import json
import sqlite3
from pathlib import Path

import vnext_bridge


def make_root(tmp_path: Path) -> Path:
    friend = tmp_path / 'friends' / '123456'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text(json.dumps({
        'basic_info': {'name': '测试好友'},
        'model_api': {'provider': 'codex', 'model': 'gpt-test'},
        'voice_reply': {'mode': 'off'},
    }), 'utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    con = sqlite3.connect(data / 'tasks.sqlite3')
    con.execute('CREATE TABLE tasks (task_id TEXT, title TEXT, status TEXT, created_at TEXT, updated_at TEXT)')
    con.execute("INSERT INTO tasks VALUES ('t1','测试任务','DONE','2026-01-01','2026-01-02')")
    con.commit()
    con.close()
    con = sqlite3.connect(data / 'agent_mail.sqlite3')
    con.execute('CREATE TABLE messages (id TEXT)')
    con.execute("INSERT INTO messages VALUES ('m1')")
    con.commit()
    con.close()
    return tmp_path


def test_bridge_reads_friends_and_counts(tmp_path):
    root = make_root(tmp_path)
    data = vnext_bridge.overview(root)
    assert data['friends'] == 1
    assert data['tasks'] == 1
    assert data['mail_messages'] == 1
    assert data['read_only'] is True
    assert data['agents'] >= 1


def test_bridge_never_returns_profile_secrets(tmp_path):
    root = make_root(tmp_path)
    friend = root / 'friends' / '123456' / 'friend.json'
    profile = json.loads(friend.read_text('utf-8'))
    profile['api_key'] = 'secret-should-not-leak'
    friend.write_text(json.dumps(profile), 'utf-8')
    payload = json.dumps(vnext_bridge.friends(root), ensure_ascii=False)
    assert 'secret-should-not-leak' not in payload
    assert '测试好友' in payload


def test_bridge_routes(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(vnext_bridge, '_root', lambda: root)
    status, health = vnext_bridge.route('/api/health', root)
    assert status == 200 and health['ok'] is True
    status, listing = vnext_bridge.route('/api/friends', root)
    assert status == 200 and listing['items'][0]['qq'] == '123456'
    status, tasks = vnext_bridge.route('/api/tasks', root)
    assert status == 200 and tasks['items'][0]['title'] == '测试任务'
    status, missing = vnext_bridge.route('/api/missing', root)
    assert status == 404
    status, devices = vnext_bridge.route('/api/devices', root)
    assert status == 200 and devices['items'] == []
    status, providers = vnext_bridge.route('/api/providers', root)
    assert status == 200 and providers['accounts'][0]['qq'] == '123456'
    status, experiences = vnext_bridge.route('/api/experiences', root, experience_owner='123456')
    assert status == 200 and 'total' in experiences
    status, accounts = vnext_bridge.route('/api/accounts', root)
    assert status == 200 and accounts['items'] == []
    status, health = vnext_bridge.route('/api/health-bridge', root)
    assert status == 200 and health['has_real_events'] is False
    status, found = vnext_bridge.route('/api/experiences/search', root, 'q=x', experience_owner='123456')
    assert status == 200 and found['items'] == []
