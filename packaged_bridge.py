"""Process control for the packaged QQ friend house."""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))
PID_FILE = ROOT / "bridge.pid"
LOG_FILE = ROOT / "bridge.log"
ERROR_FILE = ROOT / "bridge.err.log"


def _is_local_bridge(process):
    command = process.cmdline()
    if not process.is_running():
        return False
    home = ROOT.resolve()
    if 'run' in command and any(Path(arg).name.casefold() == 'main.py' and
                               Path(arg).resolve() == home / 'main.py' for arg in command):
        return True
    if '--bridge-run' not in command:
        return False
    return (any(Path(arg).name.casefold() == 'launcher.py'
                and Path(arg).resolve() == home / 'launcher.py' for arg in command)
            or Path(process.exe()).resolve() == home / 'Open QQ Dot.exe')


def bridge_process():
    try:
        process = psutil.Process(int(PID_FILE.read_text(encoding="utf-8").strip()))
        if _is_local_bridge(process):
            return process
    except (OSError, ValueError, psutil.Error):
        pass
    # Recover a bridge launched by python.exe/another supported entry point,
    # including one whose launcher exited before persisting its PID.
    for process in psutil.process_iter():
        try:
            if _is_local_bridge(process):
                PID_FILE.write_text(str(process.pid), encoding='utf-8')
                return process
        except (OSError, ValueError, psutil.Error):
            continue
    return None


def start():
    running = bridge_process()
    if running:
        return f"Already running, PID {running.pid}"
    for other in psutil.process_iter(["name", "cmdline"]):
        try:
            command = other.info['cmdline'] or []
            if '--bridge-run' not in command:
                continue
            script = next((Path(arg) for arg in command if Path(arg).name.casefold() == 'launcher.py'), None)
            if script and (script.parent / 'packaged_bridge.py').is_file():
                # Switching installs must not leave the user stuck on a stale bridge
                # from another folder.  Stop only the old launcher child, not the
                # whole other app, and continue with this install.
                try:
                    other.terminate()
                    other.wait(timeout=5)
                except psutil.TimeoutExpired:
                    other.kill()
                    other.wait(timeout=5)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    command = ([sys.executable] if getattr(sys, "frozen", False)
               else [sys.executable, str(ROOT / "launcher.py")])
    process = subprocess.Popen([*command, "--bridge-run"], cwd=ROOT,
                               env=os.environ.copy(), creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    time.sleep(2)
    if process.poll() is not None:
        raise RuntimeError("桥接启动失败：" + ERROR_FILE.read_text(encoding="utf-8", errors="replace")[-500:])
    return f"Started, PID {process.pid}"


def stop():
    process = bridge_process()
    if process is None:
        PID_FILE.unlink(missing_ok=True)
        return "not running"
    descendants = process.children(recursive=True)
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        pass
    _, alive = psutil.wait_procs([*descendants, process], timeout=5)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if psutil.pid_exists(process.pid):
        raise RuntimeError("桥接进程未能停止")
    PID_FILE.unlink(missing_ok=True)
    return "本目录桥接及其后台进程已停止"


def invoke(action: str) -> tuple[int, str]:
    try:
        if action == "start":
            return 0, start()
        if action == "stop":
            return 0, stop()
        from main import bind_friend, status_text
        if action == "status":
            running = bridge_process()
            first = f"Running, PID {running.pid}" if running else "not running"
            recent = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-8:] if LOG_FILE.exists() else []
            return 0, "\n".join([first, status_text(), *recent])
        if action.startswith("bind -QQ "):
            qq_id = action.removeprefix("bind -QQ ").strip()
            bind_friend(qq_id, False, None)
            return 0, "friend. "
        raise ValueError("未知操作")
    except Exception as exc:
        return 1, str(exc)
