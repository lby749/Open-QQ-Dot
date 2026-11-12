"""Funnel configuration boundaries; no real VPN, installer, login or external mutation."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import house_settings
import house_web
import network


def config(port=8790, listen='443'):
    site = 'test.example.ts.net:' + listen
    return {'TCP': {listen: {'HTTPS': True}}, 'Web': {site: {'Handlers': {
        '/': {'Proxy': f'http://127.0.0.1:{port}'}}}}, 'AllowFunnel': {site: True}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.delenv('QQ_ALLOW_FUNNEL', raising=False)
    monkeypatch.setattr(network, '_tailscale', lambda: 'tailscale-test')
    monkeypatch.setattr(network, '_funnel_reachable', lambda url: True)
    def run(*args):
        return SimpleNamespace(stdout=json.dumps({'BackendState': 'Running'} if args[1] == 'status' else config()),
                               stderr='', returncode=0)
    call = Mock(side_effect=run)
    monkeypatch.setattr(network, '_tailscale_run', call)
    return call


def test_existing_funnel_reused_and_setting_survives_restart(setup):
    assert not house_web.funnel_allowed()
    assert network.configure_funnel()['state'] == 'ready'
    assert house_web.funnel_allowed()
    assert all(call.args[1] != 'funnel' for call in setup.call_args_list)
    assert house_settings.load()['tailscale_funnel_enabled'] is True


def test_unrelated_and_foreground_listener_never_overwritten(setup):
    old = config(9999)
    old['Foreground'] = {'other': config(9998, '8443')}
    setup.side_effect = [SimpleNamespace(stdout='{"BackendState":"Running"}'),
                         SimpleNamespace(stdout=json.dumps(old), returncode=0),
                         SimpleNamespace(stdout='', stderr='', returncode=0),
                         SimpleNamespace(stdout=json.dumps(config(8790, '10000')), returncode=0)]
    assert network.configure_funnel()['public'] == 'https://test.example.ts.net:10000/'
    assert setup.call_args_list[2].args == ('tailscale-test', 'funnel', '--bg', '--yes', '--https=10000',
                                          'http://127.0.0.1:8790')


def test_all_ports_busy_fails_closed(setup):
    old = config(9999)
    old['TCP'].update({'8443': {}, '10000': {}})
    setup.side_effect = [SimpleNamespace(stdout='{"BackendState":"Running"}'),
                         SimpleNamespace(stdout=json.dumps(old), returncode=0)]
    with pytest.raises(RuntimeError, match='未覆盖'):
        network.configure_funnel()
    assert not house_web.funnel_allowed()


def test_auth_timeout_is_not_reported_connected(setup):
    setup.side_effect = [SimpleNamespace(stdout='{"BackendState":"Running"}'),
                         SimpleNamespace(stdout='{}', returncode=0),
                         subprocess.TimeoutExpired('tailscale', 20,
                             output=b'Visit https://login.tailscale.com/f/funnel?node=test')]
    result = network.configure_funnel()
    assert result['state'] == 'authorize'
    assert not house_web.funnel_allowed()


def test_disable_closes_gate_before_stopping_only_our_listener(setup):
    network.configure_funnel()
    setup.reset_mock()
    result = network.disable_funnel()
    assert result['public'] is None and not house_web.funnel_allowed()
    assert setup.call_args.args == ('tailscale-test', 'funnel', '--bg', '--yes', '--https=443', '--set-path=/', 'off')


def test_explicit_environment_off_takes_precedence(setup, monkeypatch):
    network.configure_funnel()
    monkeypatch.setenv('QQ_ALLOW_FUNNEL', '0')
    assert not house_web.funnel_allowed()


def test_login_is_a_pending_state_not_success(setup, monkeypatch):
    setup.side_effect = [SimpleNamespace(stdout='{"BackendState":"NeedsLogin"}')]
    assert network.configure_funnel()['state'] == 'login'
    assert not house_web.funnel_allowed()


def test_failed_cli_does_not_open_gate(setup):
    setup.side_effect = [SimpleNamespace(stdout='{"BackendState":"Running"}'),
                         SimpleNamespace(stdout='{}', returncode=0),
                         SimpleNamespace(stdout='', stderr='feature not available', returncode=1),
                         SimpleNamespace(stdout='{}', returncode=0)]
    with pytest.raises(RuntimeError, match='尚未建立'):
        network.configure_funnel()
    assert not house_web.funnel_allowed()


def test_configured_but_unreachable_funnel_is_not_reported_ready(setup, monkeypatch):
    monkeypatch.setattr(network, '_funnel_reachable', lambda url: False)
    assert network.configure_funnel()['state'] == 'unreachable'
    assert not house_web.funnel_allowed()


@pytest.mark.parametrize('body,denied,expected', [
    ('<title>Open QQ Dot · 好友小屋</title>', False, True),
    ('<title>Other service</title>', False, False),
    (json.dumps({'error': 'Tailscale Funnel 未启用'}), True, True),
    (json.dumps({'error': 'Forbidden'}), True, False),
])
def test_funnel_probe_checks_app_identity_and_keeps_certificate_verification(monkeypatch, body, denied, expected):
    import io
    import ssl
    import urllib.request
    import urllib.error
    probe = network._funnel_reachable
    def open_url(url, timeout, context):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        if denied:
            raise urllib.error.HTTPError(url, 403, 'Forbidden', {}, io.BytesIO(body.encode('utf-8')))
        response = io.BytesIO(body.encode('utf-8'))
        response.status = 200
        return response
    monkeypatch.setattr(urllib.request, 'urlopen', open_url)
    assert probe('https://test.example.ts.net/') is expected
