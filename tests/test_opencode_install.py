"""OpenCode one-click launcher: local-app reuse and verified installer download."""
import hashlib
import http.server
import os
import threading
from pathlib import Path

import opencode_install


class _InstallerHandler(http.server.BaseHTTPRequestHandler):
    payload = b'not-a-real-openCode-installer' * 1024

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


def test_existing_desktop_is_launched_without_download(monkeypatch, tmp_path):
    exe = tmp_path / 'Programs' / 'ai.opencode.desktop' / 'OpenCode.exe'
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'exe')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    launched = []
    monkeypatch.setattr(opencode_install.subprocess, 'Popen', lambda argv, **kw: launched.append(argv))
    message = opencode_install.install_and_launch()
    assert launched == [[str(exe)]]
    assert '自行登录' in message


def test_download_verifies_sha256(monkeypatch, tmp_path):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _InstallerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    digest = hashlib.sha256(_InstallerHandler.payload).hexdigest()
    monkeypatch.setattr(opencode_install, '_release_info',
                        lambda: (f'http://127.0.0.1:{server.server_port}/setup.exe', digest, 'v-test'))
    try:
        target = tmp_path / 'setup.exe'
        path, version = opencode_install.download_installer(target)
        assert path.read_bytes() == _InstallerHandler.payload
        assert version == 'v-test'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_download_rejects_wrong_digest(monkeypatch, tmp_path):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _InstallerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(opencode_install, '_release_info',
                        lambda: (f'http://127.0.0.1:{server.server_port}/setup.exe', '0' * 64, 'v-test'))
    try:
        target = tmp_path / 'setup.exe'
        try:
            opencode_install.download_installer(target)
        except opencode_install.OpenCodeInstallError as exc:
            assert '校验失败' in str(exc)
        else:
            raise AssertionError('bad digest was accepted')
        assert not target.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
