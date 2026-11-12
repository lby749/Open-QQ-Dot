"""Single entry point for the packaged GUI and its bridge child."""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

from version import TITLE

def start_dotlink(home):
    if os.name!='nt':
        return '当前平台请手动启动 QQ 并登录；DotLink 使用本机配置文件中的 OneBot 配置。'
    try:
        from dotlink import ensure_started  # hidden import listed in Open QQ Dot.spec
        return ensure_started(home)
    except Exception as exc:
        return "QQ 直连启动失败：" + str(exc)[:500]


def _run_persona_worker(arguments):
    from persona_worker import main as run_worker
    raise SystemExit(run_worker(arguments))


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('OpenQQDot.Client.Desktop.1.0')
    except Exception:
        pass
    frozen = getattr(sys, "frozen", False)
    home = Path(sys.executable if frozen else __file__).resolve().parent
    if sys.stderr is None:
        # pythonw has no stderr; retain slot/startup failures instead of a silent blank window.
        sys.stderr = open(home / 'gui.log', 'a', encoding='utf-8', buffering=1)
    if sys.stdout is None:
        sys.stdout = sys.stderr
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    open_paths = []
    for argument in sys.argv[1:]:
        if argument.startswith('-'):
            continue
        candidate = Path(argument)
        try:
            if candidate.is_file():
                open_paths.append(candidate)
        except OSError:
            continue
    os.environ["QQ_BRIDGE_HOME"] = str(home)
    os.environ["QQ_BRIDGE_LAUNCHER"] = "1"  # GUI controls the bridge this launcher starts
    if '--check' in sys.argv[1:]:
        import json
        from platform_runtime import startup_report
        report=startup_report()
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--persona-worker":
        _run_persona_worker(sys.argv[2:])
    if (frozen and len(sys.argv) > 1 and
            Path(sys.argv[1]).name.casefold() == "persona_worker.py"):
        _run_persona_worker(sys.argv[2:])
    if (frozen and len(sys.argv) > 1 and
            Path(sys.argv[1]).name.casefold() == "persona_workbench_host.py"):
        from persona_workbench_host import main as run_workbench
        raise SystemExit(run_workbench(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--bridge-run":
        sys.stdout = open(home / "bridge.log", "a", encoding="utf-8", buffering=1)
        sys.stderr = open(home / "bridge.err.log", "a", encoding="utf-8", buffering=1)
        from main import Bridge
        Bridge().run()
        return

    kernel32=None
    if os.name=='nt':
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel32.CreateMutexW(None, True, "Local\\OpenQQDotGui")
        duplicate=kernel32.GetLastError()==183
    else:
        from platform_runtime import acquire_posix_lock
        mutex=acquire_posix_lock('gui')
        duplicate=mutex is None
    if duplicate:
        if os.name!='nt':
            print('Open QQ Dot 已在运行。')
            return
        if open_paths:
            try:
                from house_qt.file_drop import queue_open
                queue_open(home, open_paths)
            except Exception:
                pass
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        window = user32.FindWindowW(None, TITLE) or user32.FindWindowW(None, "Open QQ Dot")
        if window:
            user32.ShowWindow(window, 9)
            user32.SetForegroundWindow(window)
        kernel32.CloseHandle(mutex)
        return
    from packaged_bridge import start, stop

    def start_all():
        from house_settings import personal_qq_enabled
        from app_lifecycle import start_background_services
        from qq_official import start_configured
        start_background_services()
        errors = start_configured(home)
        if not personal_qq_enabled(home):
            return '\n'.join(errors)
        startup_error = '\n'.join(filter(None, [*errors, start_dotlink(home)]))
        try:
            start()
        except RuntimeError as exc:
            startup_error = "\n".join(filter(None, (startup_error, str(exc))))
        return startup_error
    try:
        from house_qt.window import run
        run(startup=start_all, open_paths=open_paths)  # the window shows at once; DotLink and the bridge start behind it
    except Exception as exc:
        if os.name=='nt':
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Open QQ Dot 启动失败", 0x10)
        else:
            print('Open QQ Dot 启动失败：'+str(exc),file=sys.stderr)
            raise
    finally:
        from qq_official import stop as stop_official
        stop_official(home)
        try:
            stop()
        finally:
            # The runtime has its own process tree and is not a bridge descendant.
            from dotlink import stop as stop_runtime
            stop_runtime(home, all_instances=False)
        if kernel32:
            kernel32.ReleaseMutex(mutex)
            kernel32.CloseHandle(mutex)
        elif mutex:
            mutex.close()


if __name__ == "__main__":
    main()
