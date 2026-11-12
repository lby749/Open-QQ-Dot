import io
import json
from types import SimpleNamespace

import pytest

import app_update
import house_settings


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / 'release_source.json'
    path.write_text(json.dumps({'repository': 'example/application'}), 'utf-8')
    monkeypatch.setattr(app_update, 'SOURCE', path)
    return path


def install_response(monkeypatch, releases):
    requests = []
    def opened(request, timeout):
        requests.append(request)
        assert timeout == 10
        return io.BytesIO(json.dumps(releases).encode())
    monkeypatch.setattr(app_update, 'build_opener', lambda *_: SimpleNamespace(open=opened))
    return requests


def test_release_selection_orders_versions_and_excludes_drafts():
    releases = [{'tag_name': 'v1.10.0', 'body': '新版'}, {'tag_name': 'v1.9.0'},
                {'tag_name': 'v9.0.0', 'draft': True}, {'tag_name': 'v2.0.0-beta.1', 'prerelease': True},
                {'tag_name': 'not-a-version'}, {'tag_name': 'v1.0.0'}]
    selected = app_update.choose_release(releases, 'example/application', '1.0.0')
    assert selected['version'] == '1.10.0'
    assert selected['url'] == 'https://github.com/example/application/releases/tag/v1.10.0'
    assert app_update.choose_release([{'tag_name': 'v1.0.0'}], 'example/application', '1.0.0') is None


def test_beta_users_receive_beta_and_stable_updates():
    rows = [{'tag_name': 'v1.0.0-beta.10', 'prerelease': True}, {'tag_name': 'v1.0.0-beta.2'}]
    assert app_update.choose_release(rows, 'example/application', '1.0.0-beta.1')['version'] == '1.0.0-beta.10'
    assert app_update.choose_release(rows + [{'tag_name': 'v1.0.0'}], 'example/application', '1.0.0-beta.1')['version'] == '1.0.0'


def test_auto_cache_is_shared_and_manual_check_bypasses_throttle(source, tmp_path, monkeypatch):
    requests = install_response(monkeypatch, [{'tag_name': 'v1.0.1', 'body': '中文更新内容'}])
    first = app_update.check(tmp_path, current='1.0.0')
    assert first['status'] == 'available'
    app_update.mark_notified(tmp_path, '1.0.1')
    cached = app_update.check(tmp_path, current='1.0.0')
    assert cached['cached'] and cached['notified_version'] == '1.0.1'
    assert len(requests) == 1
    app_update.check(tmp_path, force=True, current='1.0.0')
    assert len(requests) == 2
    assert requests[0].full_url.startswith('https://api.github.com/repos/example/application/releases?')
    assert 'Authorization' not in requests[0].headers


def test_disabled_automatic_check_keeps_manual_check_available(source, tmp_path, monkeypatch):
    house_settings.save({'app_auto_check_updates': False, 'other_setting': 'preserved'}, tmp_path)
    requests = install_response(monkeypatch, [])
    assert app_update.check(tmp_path)['status'] == 'disabled'
    assert not requests
    assert app_update.check(tmp_path, force=True)['status'] == 'latest'
    assert house_settings.load(tmp_path)['other_setting'] == 'preserved'


@pytest.mark.parametrize('repository', ['', 'https://evil.invalid/project', '../private', 'owner/repo/path'])
def test_invalid_source_never_makes_network_request(source, tmp_path, monkeypatch, repository):
    source.write_text(json.dumps({'repository': repository}), 'utf-8')
    requests = install_response(monkeypatch, [])
    assert app_update.check(tmp_path, force=True)['status'] == 'unconfigured'
    assert not requests


def test_cached_url_is_rebuilt_from_trusted_source(source, tmp_path, monkeypatch):
    install_response(monkeypatch, [{'tag_name': 'v1.0.1'}])
    app_update.check(tmp_path, current='1.0.0')
    cached = app_update.state(tmp_path)
    cached['release']['url'] = 'file:///private/credentials'
    app_update._save(tmp_path, cached)
    result = app_update.check(tmp_path, current='1.0.0')
    assert result['release']['url'] == 'https://github.com/example/application/releases/tag/v1.0.1'


def test_network_error_is_not_reported_as_latest(source, tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError('network unavailable')
    monkeypatch.setattr(app_update, 'build_opener', lambda *_: SimpleNamespace(open=failed))
    result = app_update.check(tmp_path, force=True)
    assert result['status'] == 'offline' and result['release'] is None
    assert 'network unavailable' not in json.dumps(result)


def test_redirects_are_rejected():
    assert app_update._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private') is None
