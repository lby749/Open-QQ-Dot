"""Experience views must not fall back to global data when a friend is omitted."""
import json
import os
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import experience_service
import vnext_bridge


@pytest.fixture
def house(tmp_path):
    for owner in ('12345', '67890'):
        directory = tmp_path / 'friends' / owner
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text('{}', 'utf-8')
    (tmp_path / 'data').mkdir()
    with sqlite3.connect(tmp_path / 'data/retrieval_engine.sqlite3') as db:
        db.execute('''CREATE TABLE task_experiences (
            experience_id TEXT PRIMARY KEY,task_id TEXT,owner_id TEXT,agent_provider TEXT,
            model_name TEXT,problem_statement TEXT,action_taken TEXT,result_status TEXT,
            confidence_status TEXT,lifecycle_state TEXT,created_at TEXT,is_shared INTEGER,
            root_cause_or_condition TEXT)''')
        db.execute('CREATE VIRTUAL TABLE task_experiences_fts USING fts5(experience_id UNINDEXED,problem_statement,action_taken,root_cause_or_condition,tools_used)')
        for identifier, owner, shared in (('one', '12345', 0), ('two', '67890', 1), ('legacy', 'user', 0)):
            db.execute('INSERT INTO task_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, 'task-'+identifier, owner, 'fixture', '', 'risk '+identifier, 'action '+identifier,
                 'unverified', 'low_unverified', 'CANDIDATE', '2026-11-11', shared, '风险'+identifier))
            db.execute('INSERT INTO task_experiences_fts VALUES (?,?,?,?,?)',
                (identifier, 'risk '+identifier, 'action '+identifier, '风险'+identifier, ''))
    return tmp_path


def test_snapshot_and_both_search_paths_are_owner_bound(house):
    snapshot = experience_service.snapshot(house, owner='12345')
    assert snapshot['total'] == 1 and snapshot['by_state'] == {'CANDIDATE': 1}
    assert [row['id'] for row in snapshot['recent']] == ['one']
    assert [row['id'] for row in experience_service.search(house, 'risk', owner='12345')] == ['one']
    assert [row['id'] for row in experience_service.search(house, '风险', owner='12345')] == ['one']
    assert experience_service.snapshot(house, owner='99999')['total'] == 0
    assert experience_service.snapshot(house, admin=True)['total'] == 3
    assert len(experience_service.search(house, 'risk', admin=True)) == 3


@pytest.mark.parametrize('owner', ['', None, '../12345', '12345/67890', '12345\n', True])
def test_missing_or_malformed_owner_is_not_global_access(house, owner):
    with pytest.raises(PermissionError):
        experience_service.snapshot(house, owner=owner)
    with pytest.raises(PermissionError):
        experience_service.search(house, 'risk', owner=owner)
    with pytest.raises(PermissionError):
        experience_service.record_candidate(house, '有效测试问题', '有效测试做法', owner=owner)


def test_untrusted_route_query_does_not_establish_identity(house):
    assert vnext_bridge.route('/api/experiences', house, 'owner=12345')[0] == 403
    assert vnext_bridge.route('/api/experiences/search', house, 'q=risk&owner=67890', experience_owner='12345')[0] == 403


def test_http_requires_authenticated_owner_and_scope_even_without_legacy_token(house, monkeypatch):
    monkeypatch.setattr(vnext_bridge, '_root', lambda: house)
    monkeypatch.setattr(vnext_bridge, 'TOKEN', '')
    store = vnext_bridge.device_store(house)
    token = store.pair(store.create_code(scopes=('tasks:read',), owners=('12345',)), 'fixture')['token']
    no_scope = store.pair(store.create_code(scopes=('agents:read',), owners=('12345',)), 'no-scope')['token']
    no_owner = store.pair(store.create_code(scopes=('tasks:read',)), 'no-owner')['token']
    server = ThreadingHTTPServer(('127.0.0.1', 0), vnext_bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, credential='', body=None):
        headers = {'Authorization': 'Bearer '+credential} if credential else {}
        payload = None if body is None else json.dumps(body).encode()
        try:
            response = urlopen(Request(base+path, payload, headers=headers), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)
    try:
        assert request('/api/experiences?owner=12345')[0] == 401
        for credential in (no_scope, no_owner):
            assert request('/api/experiences?owner=12345', credential)[0] == 403
        assert request('/api/experiences?owner=67890', token)[0] == 403
        assert request('/api/experiences', token)[0] == 403
        status, data = request('/api/experiences?owner=12345', token)
        assert status == 200 and data['total'] == 1
        assert [row['id'] for row in data['recent']] == ['one']
        status, data = request('/api/experiences/search?owner=12345&q=risk', token)
        assert status == 200 and [row['id'] for row in data['items']] == ['one']
        assert request('/api/experiences/candidate', body={'admin': True, 'owner': '12345', 'problem': '测试问题', 'action': '测试做法'})[0] == 401
        admin = 'fixture-admin-token-' + 'x'*32
        monkeypatch.setattr(vnext_bridge, 'TOKEN', admin)
        status, data = request('/api/experiences', admin)
        assert status == 200 and data['total'] == 3
        seen = []
        monkeypatch.setattr(experience_service, 'record_candidate', lambda *args, **kwargs: seen.append(kwargs['owner']) or {'owner': kwargs['owner']})
        status, data = request('/api/experiences/candidate', admin, {'owner': '12345', 'problem': '测试问题', 'action': '测试做法'})
        assert status == 200 and seen == ['12345']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_desktop_friend_switch_and_empty_selection_never_show_global_experiences(house, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    import house_qt.vnext_pages as pages
    from house_qt.common import init_ui_thread
    app = QApplication.instance() or QApplication([])
    init_ui_thread()
    monkeypatch.setattr(pages, 'ROOT', house)
    monkeypatch.setattr(pages, 'toast', lambda *args: None)
    parent = SimpleNamespace(current_qq='12345')
    page = pages.ExperienceVNextPage(parent)
    try:
        page.refresh()
        assert page.results.rowCount() == 1 and page.results.item(0, 0).text() == 'risk one'
        page.problem.setText('好友一尚未保存的问题')
        parent.current_qq = '67890'
        page.refresh()
        assert page.problem.text() == ''
        assert page.results.rowCount() == 1 and page.results.item(0, 0).text() == 'risk two'
        parent.current_qq = ''
        page.refresh()
        assert page.results.rowCount() == 0 and not page.record_button.isEnabled()
        page.problem.setText('未选好友问题')
        page.action.setText('不应创建记录')
        page.record()
        assert experience_service.snapshot(house, admin=True)['total'] == 3
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
