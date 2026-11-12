"""Optional runtime installation: which runtime a model needs, mirrors, fallback, verify."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import local_runtimes as lr

_ensure_windows_cpp_runtime = lr.ensure_windows_cpp_runtime


@pytest.fixture(autouse=True)
def no_system_runtime_install(monkeypatch):
    monkeypatch.setattr(lr, 'ensure_windows_cpp_runtime', lambda root, emit: True)


def make_app(tmp_path):
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (tmp_path / ".bootstrap").mkdir()
    (tmp_path / ".bootstrap" / "uv.exe").write_bytes(b"")
    return tmp_path


def test_embedding_and_rerank_share_one_torch_runtime():
    assert lr.runtimes_for([("embedding", "small"), ("rerank", "base")]) == ["embedding"]
    assert lr.runtimes_for([("tts", "small"), ("asr", "small"), ("llm", "base")]) == ['asr']


def test_install_skips_when_imports_already_work(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: True)
    monkeypatch.setattr(lr, "_run", lambda *a, **k: pytest.fail("must not run uv"))
    outcome = lr.install("embedding", app)
    assert outcome["installed"] is True
    assert outcome["restart"] is False


def test_install_uses_domestic_torch_mirror_and_verifies(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    state = {"ready": False}
    calls = []

    def fake_run(argv, root, emit, cancel_event):
        calls.append(argv)
        state["ready"] = True
        return 0

    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: state["ready"])
    monkeypatch.setattr(lr, "_run", fake_run)
    outcome = lr.install("embedding", app)

    assert outcome == {"installed": True, "restart": True, "message": outcome["message"]}
    argv = calls[0]
    # Aliyun and SJTU publish flat wheel directories, so they are --find-links sources.
    assert "https://mirrors.aliyun.com/pytorch-wheels/cpu/" in argv
    assert argv[argv.index("--index-url") + 1] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert any(arg.startswith("torch") for arg in argv)
    assert "transformers>=4.51,<5" in argv  # embedding_provider is written against 4.x
    assert "-r" not in argv  # never the pinned requirements file: it would downgrade numpy
    assert not any(arg.endswith("requirements-embedding.txt") for arg in argv)


def test_install_falls_back_to_the_next_mirror(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    state = {"ready": False}
    seen = []

    def fake_run(argv, root, emit, cancel_event):
        seen.append(next((argv[i + 1] for i, a in enumerate(argv)
                          if a in ("--find-links", "--index-url") and "pytorch" in argv[i + 1]),
                         ""))
        if len(seen) == 1:
            return 1
        state["ready"] = True
        return 0

    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: state["ready"])
    monkeypatch.setattr(lr, "_run", fake_run)
    outcome = lr.install("embedding", app)
    assert outcome["installed"] is True
    assert seen[0].startswith("https://mirrors.aliyun.com/")
    assert seen[1].startswith("https://mirror.sjtu.edu.cn/")


def test_install_reports_failure_when_every_mirror_fails(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: False)
    monkeypatch.setattr(lr, "_run", lambda *a, **k: 1)
    outcome = lr.install("embedding", app)
    assert outcome["installed"] is False
    assert "失败" in outcome["message"]


def test_cancel_stops_installation(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    event = threading.Event()
    event.set()
    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: False)
    monkeypatch.setattr(lr, "_run", lambda *a, **k: -2)
    outcome = lr.install("embedding", app, cancel_event=event)
    assert outcome["installed"] is False
    assert "取消" in outcome["message"]


def test_unknown_runtime_is_rejected(tmp_path):
    with pytest.raises(lr.RuntimeUnavailable):
        lr.install("video", tmp_path)


def test_deep_import_failure_is_rechecked_after_dependencies_change(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    outcomes = iter((1, 0))
    monkeypatch.setattr(lr.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=next(outcomes)))
    assert lr.imports_ready('embedding', app, deep=True) is False
    assert lr.imports_ready('embedding', app, deep=True) is True


def test_shallow_status_respects_actual_import_failure_and_repair(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    outcomes = iter((0, 1, 0))
    monkeypatch.setattr(lr.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=next(outcomes)))
    assert lr.imports_ready('embedding', app) is True
    assert lr.imports_ready('embedding', app, deep=True) is False
    assert lr.describe('embedding', app)['needs_install'] is True
    assert lr.imports_ready('embedding', app, deep=True) is True
    assert lr.describe('embedding', app)['needs_install'] is False


def test_install_does_not_trust_package_presence_without_a_working_import(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    state = {'ready': False}
    monkeypatch.setattr(lr, 'imports_ready', lambda kind, root=None, deep=False: state['ready'] if deep else True)
    def install(*_args):
        state['ready'] = True
        return 0
    monkeypatch.setattr(lr, '_run', install)
    assert lr.install('embedding', app)['restart'] is True


@pytest.mark.skipif(lr.os.name != 'nt', reason='Windows runtime contract')
def test_windows_cpp_runtime_only_uses_installed_packages(tmp_path, monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(lr.subprocess, 'run', run)
    assert _ensure_windows_cpp_runtime(tmp_path, lambda *_: None)
    assert len(calls) == 1
    assert calls[0][0][0][0] == str(lr.venv_python(tmp_path))


@pytest.mark.skipif(lr.os.name != 'nt', reason='Windows runtime contract')
def test_windows_cpp_runtime_stays_in_venv_and_preserves_existing_dlls(tmp_path, monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(lr.subprocess, 'run', run)
    assert _ensure_windows_cpp_runtime(tmp_path, lambda *_: None)
    script = calls[0][0][0][-1]
    assert "distribution('msvc-runtime')" in script and "find_spec('torch')" in script
    assert 'is_relative_to(Path(sys.prefix).resolve())' in script
    assert 'if not destination.exists()' in script
    assert 'http' not in script and 'powershell' not in script


def test_successful_install_with_failed_import_is_not_retried_as_a_mirror_error(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []
    monkeypatch.setattr(lr, 'imports_ready', lambda *a, **k: False)
    monkeypatch.setattr(lr, '_run', lambda *a: calls.append(a) or 0)
    outcome = lr.install('embedding', app)
    assert not outcome['installed'] and '导入检查失败' in outcome['message']
    assert len(calls) == 1


def test_missing_venv_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "imports_ready", lambda kind, root=None, deep=False: False)
    with pytest.raises(lr.RuntimeUnavailable):
        lr.install("embedding", tmp_path)


def test_cuda_variant_is_opt_in(tmp_path, monkeypatch):
    import house_settings

    monkeypatch.setattr(house_settings, "load", lambda root=None: {"embedding_runtime": "cuda"})
    monkeypatch.setattr(lr.shutil, "which", lambda name: "C:/Windows/nvidia-smi.exe")
    assert lr.variant(tmp_path) == "cuda"

    monkeypatch.setattr(house_settings, "load", lambda root=None: {"embedding_runtime": "cpu"})
    assert lr.variant(tmp_path) == "cpu"

    monkeypatch.setattr(house_settings, "load", lambda root=None: {"embedding_runtime": "cuda"})
    monkeypatch.setattr(lr.shutil, "which", lambda name: None)
    assert lr.variant(tmp_path) == "cpu"
