"""Test-only environment defaults.

Fake model executables are local processes but production keeps a proxy guard.
Supply a loopback proxy on every platform: unit tests must not depend on the
developer's system proxy or make real network requests.
"""
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_claude_processes_in_unit_tests(monkeypatch):
    """Backend contracts must use test doubles, never a developer's installed account."""
    original = subprocess.Popen
    def guarded(command, *args, **kwargs):
        values = command if isinstance(command, (list, tuple)) else [command]
        if any(Path(str(value).strip('"')).name.lower() in ('claude', 'claude.exe', 'claude.cmd') for value in values):
            raise RuntimeError('单元测试禁止启动真实 Claude；请替换后端实际调用的进程接口')
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', guarded)


@pytest.fixture(autouse=True)
def _loopback_proxy_for_fake_cloud_clis(monkeypatch):
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY',
                 'https_proxy', 'http_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    for name in ('NO_PROXY', 'no_proxy'):
        monkeypatch.setenv(name, '127.0.0.1,localhost,::1')
