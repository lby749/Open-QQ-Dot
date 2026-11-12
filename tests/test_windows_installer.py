import hashlib
import json
import tempfile
import time
from pathlib import Path
import zipfile

import pytest
import windows_installer as installer
from windows_installer import install_source


def package(folder, files):
    archive = Path(folder) / 'payload.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        for name, content in files.items():
            output.writestr(name, content)
    return archive


def test_update_preserves_private_state_and_backs_up_changed_source():
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / '中文目录 with spaces'
        (target / 'friends').mkdir(parents=True)
        (target / 'friends/private.txt').write_text('private', 'utf-8')
        (target / 'main.py').write_text('previous source', 'utf-8')
        archive = package(folder, {'main.py': 'updated source', 'assets/icon.txt': 'icon'})
        install_source(archive, target)
        assert (target / 'friends/private.txt').read_text('utf-8') == 'private'
        assert (target / 'main.py').read_text('utf-8') == 'updated source'
        assert (target / 'installation-backup/main.py').read_text('utf-8') == 'previous source'
        install_source(archive, target)
        assert (target / 'installation-backup/main.py').read_text('utf-8') == 'previous source'


@pytest.mark.parametrize('name', ['../outside.py', 'C:/outside.py', '/outside.py',
                                 'friends/person.txt', 'data/auth.json', '.venv/pip.py',
                                 'napcat/config.json', 'auth.json', 'data.sqlite3'])
def test_invalid_payload_is_rejected_before_any_write(name):
    with tempfile.TemporaryDirectory() as folder:
        archive = package(folder, {'main.py': 'safe', name: 'unsafe'})
        target = Path(folder) / 'install'
        with pytest.raises(ValueError):
            install_source(archive, target)
        assert not (target / 'main.py').exists()


def resources(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    payload = folder / "application.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("main.py", "print('ok')")
        archive.writestr("requirements-ui.txt", "websockets\n")
    (folder / "application.sha256").write_text(hashlib.sha256(payload.read_bytes()).hexdigest(), "utf-8")
    (folder / "uv.exe").write_bytes(b"uv")
    return folder


def test_preflight_reports_missing_payload_and_writable_target(tmp_path):
    target = tmp_path / "install"
    check = installer.preflight(target, tmp_path / "resources")
    assert check["ok"] is False
    assert any("application.zip" in item for item in check["errors"])
    good = resources(tmp_path / "resources")
    check = installer.preflight(target, good)
    assert check["ok"] is True and check["free_gb"] > 0


def test_install_marker_roundtrip(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    installer.write_marker(target, "abc123")
    marker = installer.read_marker(target)
    assert marker["payload_sha256"] == "abc123" and marker["version"]
    assert Path(target / installer.MARKER).is_file()


def test_run_install_repairs_environment_and_honours_options(tmp_path, monkeypatch):
    resource_dir = resources(tmp_path / "resources")
    target = tmp_path / "app"
    (target / "friends").mkdir(parents=True)
    (target / "friends" / "private.txt").write_text("keep", "utf-8")
    (target / ".venv" / "Scripts").mkdir(parents=True)
    (target / ".venv" / "Scripts" / "python.exe").write_bytes(b"python")
    calls = []
    monkeypatch.setattr(installer, "_run_logged", lambda *args, **kwargs: calls.append(args) or 0)
    monkeypatch.setattr(installer, "verify_install", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(installer, "create_shortcuts", lambda *args, **kwargs: [])
    monkeypatch.setattr(installer, "set_autostart", lambda *args, **kwargs: True)
    monkeypatch.setattr(installer, "launch_app", lambda *args, **kwargs: None)
    steps = []
    installer.run_install(resource_dir, target, lambda text: None, launch=False,
                          options={"shortcut": True, "autostart": True},
                          on_step=lambda index, label: steps.append(index))
    assert steps == [0, 1, 2, 3]
    assert (target / "friends" / "private.txt").read_text("utf-8") == "keep"
    assert (target / installer.MARKER).is_file()
    assert calls and any("pip" in " ".join(map(str, call[0])) for call in calls)


def test_run_install_rejects_tampered_payload(tmp_path):
    resource_dir = resources(tmp_path / "resources")
    (resource_dir / "application.sha256").write_text("0" * 64, "utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        installer.run_install(resource_dir, tmp_path / "app", lambda text: None)


def test_first_install_keeps_python_bins_and_registry_private(tmp_path, monkeypatch):
    resource_dir = resources(tmp_path / 'resources')
    target = tmp_path / 'fresh-app'
    commands = []
    def run(command, *_args, **_kwargs):
        commands.append(command)
        if command[1] == 'venv':
            python = target / '.venv/Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'fixture')
        return 0
    monkeypatch.setattr(installer, 'detect_host_python', lambda *_args: '')
    monkeypatch.setattr(installer, '_run_logged', run)
    monkeypatch.setattr(installer, 'verify_install', lambda *_args: (True, ''))
    installer.run_install(resource_dir, target, lambda _: None, launch=False,
                          options={'shortcut': False, 'runtimes': False})
    python_install = next(c for c in commands if c[1:3] == ['python', 'install'])
    assert '--no-bin' in python_install and '--no-registry' in python_install
    assert '--force' not in python_install


def test_run_install_honours_cancel_before_writing(tmp_path):
    import threading
    resource_dir = resources(tmp_path / "resources")
    event = threading.Event()
    event.set()
    with pytest.raises(installer.InstallCancelled):
        installer.run_install(resource_dir, tmp_path / "app", lambda text: None,
                              launch=False, options={}, cancel_event=event)
    assert not (tmp_path / "app").exists()


def test_install_runtime_copies_bundled_runtime(tmp_path):
    resource_dir = resources(tmp_path / "resources")
    source = resource_dir / "qq-runtime"
    source.mkdir()
    (source / "index.js").write_text("// runtime", "utf-8")
    (source / "node.exe").write_bytes(b"node")
    for name in ('guild1.db', 'guild1.db-wal', 'guild1.db-shm', 'state/token.json',
                 'napcat/config/webui.json', 'napcat/config/napcat_123456.json', '.env', 'auth.json'):
        private = source / name
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text('private data must not be copied', 'utf-8')
    code = source / 'napcat/napcat.mjs'
    code.write_text('// runtime entry', 'utf-8')
    target = tmp_path / "app"
    target.mkdir()
    assert installer.install_runtime(resource_dir, target, lambda text: None) is True
    assert (target / "qq-runtime" / "index.js").is_file()
    assert installer.runtime_ready(target) is True
    assert (target / 'qq-runtime/napcat/napcat.mjs').is_file()
    assert not (target / 'qq-runtime/guild1.db').exists()
    assert not (target / 'qq-runtime/napcat/config').exists()
    assert not (target / 'qq-runtime/state').exists()
    assert not (target / 'qq-runtime/.env').exists()
    # A second run keeps the existing runtime and reports that.
    messages = []
    assert installer.install_runtime(resource_dir, target, messages.append) is False
    assert any("已存在" in message for message in messages)


def test_install_runtime_rejects_unsafe_zip_member(tmp_path):
    resource_dir = resources(tmp_path / "resources")
    import hashlib as _hashlib
    archive = resource_dir / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("index.js", "// runtime")
        package.writestr("node.exe", b"node")
        package.writestr("../escape.txt", b"bad")
    (resource_dir / "runtime.sha256").write_text(_hashlib.sha256(archive.read_bytes()).hexdigest(), "utf-8")
    target = tmp_path / "app"
    with pytest.raises(installer.InstallError, match="不安全路径"):
        installer.install_runtime(resource_dir, target, lambda text: None)


def test_default_destination_prefers_existing_portable_install(tmp_path, monkeypatch):
    executable = tmp_path / "Downloads" / "Open QQ Dot Setup.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"setup")
    monkeypatch.setattr(installer.sys, "executable", str(executable))
    portable = executable.parent / "OpenQQDot"
    (portable / "friends.json").parent.mkdir(parents=True)
    (portable / "friends.json").write_text("{}", "utf-8")
    assert installer.default_destination() == portable
    (portable / "friends.json").unlink()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert installer.default_destination() == tmp_path / "local" / "Programs" / "OpenQQDot"


def test_uninstall_removes_program_and_keeps_private_data(tmp_path):
    target = tmp_path / "app"
    (target / "friends" / "123").mkdir(parents=True)
    (target / "friends" / "123" / "friend.json").write_text("keep", "utf-8")
    (target / "data").mkdir()
    (target / "data" / "history.sqlite3").write_text("keep", "utf-8")
    (target / "Open QQ Dot.exe").write_bytes(b"exe")
    (target / "house_qt").mkdir()
    installer.write_marker(target, "0" * 64)
    result = installer.uninstall(target, lambda text: None, keep_data=True)
    assert not (target / "Open QQ Dot.exe").exists()
    assert not (target / "house_qt").exists()
    assert (target / "friends" / "123" / "friend.json").is_file()
    assert (target / "data" / "history.sqlite3").is_file()
    assert not (target / installer.MARKER).exists()
    assert "friends" in result["kept"] and "data" in result["kept"]


def test_uninstall_refuses_a_non_install_directory(tmp_path):
    target = tmp_path / "not-ours"
    target.mkdir()
    with pytest.raises(installer.InstallError, match="不是 Open QQ Dot"):
        installer.uninstall(target, lambda text: None)


def test_install_requirements_prefers_domestic_mirror_and_falls_back(tmp_path, monkeypatch):
    calls = []

    def fake_run_logged(command, destination, env, log, emit, cancel_event=None, on_progress=None):
        calls.append((env.get("UV_DEFAULT_INDEX"), command))
        return 1 if len(calls) == 1 else 0

    monkeypatch.setattr(installer, "_run_logged", fake_run_logged)
    code = installer._install_requirements(
        "uv", tmp_path / "python.exe", tmp_path / "requirements.txt", tmp_path,
        {"UV_DEFAULT_INDEX": "https://pypi.org/simple"}, tmp_path / "install.log", lambda text: None)
    assert code == 0
    assert calls[0][0].startswith("https://pypi.tuna.tsinghua.edu.cn/")
    assert calls[1][0].startswith("https://mirrors.aliyun.com/")


def test_install_requirements_can_install_qfluent_without_deps(tmp_path, monkeypatch):
    seen = []

    def fake_run_logged(command, destination, env, log, emit, cancel_event=None, on_progress=None):
        seen.append(command)
        return 0

    monkeypatch.setattr(installer, "_run_logged", fake_run_logged)
    installer._install_requirements(
        "uv", tmp_path / "python.exe", tmp_path / "qfluent.txt", tmp_path, {}, tmp_path / "install.log",
        lambda text: None, extra_args=("--no-deps",))
    assert "--no-deps" in seen[0]


def test_existing_python_313_is_reused_instead_of_triggering_a_download(tmp_path, monkeypatch):
    from types import SimpleNamespace
    host = str(tmp_path/'host-python.exe')
    monkeypatch.setenv('TEMP', str(tmp_path/'unrelated-temp'))
    monkeypatch.setattr(installer.subprocess, 'run', lambda *_args, **_kw:
                        SimpleNamespace(stdout=' -V:3.13 * '+host+'\n', returncode=0))
    monkeypatch.setattr(installer.os.path, 'isfile', lambda path: path == host)
    monkeypatch.setattr(installer.shutil, 'which', lambda *_: None)
    monkeypatch.setattr(installer, '_python_reports', lambda path, version: path == host and version == '3.13')
    assert installer.detect_host_python(tmp_path/'uv.exe', {}, lambda _:None) == host


def test_host_python_probe_checks_real_sqlite_ssl_and_architecture():
    import sys
    assert installer._python_reports(sys.executable, f'{sys.version_info.major}.{sys.version_info.minor}')


def test_uninstall_retries_a_transient_locked_directory(tmp_path, monkeypatch):
    target = tmp_path / "app"
    (target / "house_qt").mkdir(parents=True)
    (target / "house_qt" / "window.py").write_text("x", "utf-8")
    installer.write_marker(target, "0" * 64)
    monkeypatch.setattr(installer, "_stop_installation_processes", lambda destination: [])
    real_rmtree = installer.shutil.rmtree
    calls = {"count": 0}

    def flaky(path, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("file is in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(installer.shutil, "rmtree", flaky)
    installer.uninstall(target, lambda text: None)
    assert not (target / "house_qt").exists()
    assert calls["count"] >= 2
