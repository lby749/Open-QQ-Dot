"""One-click preparation chooses the correct action per model source."""
import agent_launcher
import pytest


def test_web_source_opens_login_page(monkeypatch):
    opened = []
    monkeypatch.setattr(agent_launcher.webbrowser, 'open', lambda url: opened.append(url))
    message = agent_launcher.prepare('webchat_chatgpt')
    assert opened == ['https://chatgpt.com']
    assert '登录' in message


def test_installed_cli_opens_terminal(monkeypatch):
    opened = []
    monkeypatch.setattr(agent_launcher, '_which', lambda names: 'C:/tools/claude.cmd')
    monkeypatch.setattr(agent_launcher, '_open_console', lambda command: opened.append(command))
    message = agent_launcher.prepare('claude')
    assert opened == ['C:/tools/claude.cmd']
    assert '登录' in message


def test_custom_sources_explain_configuration():
    for provider in ('custom', 'acp', 'mcp'):
        message = agent_launcher.prepare(provider)
        assert '不需要下载' in message


def test_missing_unknown_source_fails_clearly(monkeypatch):
    monkeypatch.setattr(agent_launcher, '_which', lambda names: '')
    monkeypatch.setattr(agent_launcher, '_desktop', lambda provider: '')
    try:
        agent_launcher.prepare('zcode')
    except RuntimeError as exc:
        assert '没有检测到' in str(exc)
    else:
        raise AssertionError('unknown source should not silently succeed')


def test_npm_metadata_requires_registry_integrity(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"version": "1.2.3", "dist.integrity": "sha512-abc"}'
        stderr = ''

    monkeypatch.setattr(agent_launcher.shutil, 'which', lambda names: 'npm')
    monkeypatch.setattr(agent_launcher.subprocess, 'run', lambda *a, **k: Result())
    assert agent_launcher._npm_metadata('@example/tool') == ('1.2.3', 'sha512-abc')

    class Bad(Result):
        stdout = '{"version": "1.2.3", "dist.integrity": ""}'

    monkeypatch.setattr(agent_launcher.subprocess, 'run', lambda *a, **k: Bad())
    try:
        agent_launcher._npm_metadata('@example/tool')
    except RuntimeError as exc:
        assert 'integrity' in str(exc)
    else:
        raise AssertionError('missing npm integrity was accepted')


def test_codex_standalone_install_without_node(monkeypatch, tmp_path):
    import codex_settings
    from io import BytesIO
    script = b'official installer fixture'
    monkeypatch.setattr(agent_launcher.urllib.request, 'urlopen', lambda *a, **k: BytesIO(script))
    monkeypatch.setattr(agent_launcher.hashlib, 'sha256', lambda data: type('Hash', (), {
        'hexdigest': lambda self: '9c7ec5ecb7c90718808d3ab1fc8b285f51f8c28f90c1505aadf333f96645c706'})())
    executable = tmp_path / 'Programs/OpenAI/Codex/bin/codex.exe'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setattr(codex_settings.shutil, 'which', lambda *a: None)
    calls = []
    def run(command, **kwargs):
        assert command[0] == 'powershell.exe'
        assert kwargs['env']['CODEX_NON_INTERACTIVE'] == '1'
        assert agent_launcher.Path(command[-1]).read_bytes() == script
        calls.append(command)
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(agent_launcher.subprocess, 'run', run)
    assert agent_launcher._install_codex() == str(executable.resolve())
    assert len(calls) == 1


def test_download_tls_preserves_certificate_and_hostname_validation():
    import ssl
    import certifi
    from pathlib import Path
    from unittest.mock import patch
    from tls_support import client_context
    context = ssl.create_default_context()
    with patch('ssl.create_default_context', return_value=context) as defaults:
        result = client_context()
    defaults.assert_called_once_with()
    assert result is context  # Retain the system/operator trust loaded by the default context.
    assert result.verify_mode == ssl.CERT_REQUIRED and result.check_hostname
    assert result.cert_store_stats()['x509_ca'] > 0
    assert Path(certifi.where()).is_file()


def test_download_tls_still_rejects_an_untrusted_server(tmp_path):
    import http.server
    import ssl
    import threading
    import urllib.error
    import urllib.request
    from tls_support import client_context, ensure_certificate
    cert, key = ensure_certificate(tmp_path)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(urllib.error.URLError) as rejected:
            urllib.request.urlopen(f'https://127.0.0.1:{server.server_port}/',
                                   context=client_context(), timeout=3)
        assert isinstance(rejected.value.reason, ssl.SSLCertVerificationError)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_codex_rejects_changed_installer_before_execution(monkeypatch):
    from io import BytesIO
    monkeypatch.setattr(agent_launcher.urllib.request, 'urlopen', lambda *a, **k: BytesIO(b'changed'))
    monkeypatch.setattr(agent_launcher.subprocess, 'run', lambda *a, **k: pytest.fail('executed unverified installer'))
    with pytest.raises(RuntimeError, match='校验失败'):
        agent_launcher._install_codex()


def test_codex_download_network_error_is_readable_in_chinese(monkeypatch):
    from urllib.error import URLError
    def offline(*args, **kwargs):
        raise URLError('timed out')
    monkeypatch.setattr(agent_launcher.urllib.request, 'urlopen', offline)
    with pytest.raises(RuntimeError, match='检查网络与代理'):
        agent_launcher._install_codex()
