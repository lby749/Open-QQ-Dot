"""Small OS-specific startup helpers; no model or GUI initialization."""
import contextlib
import importlib
import io
import os
from pathlib import Path
import sys


def shell_command(command, platform=None, backend=None):
    if (platform or sys.platform)=='win32':
        backend = (backend or 'powershell').casefold()
        if backend == 'cmd':
            return ['cmd.exe', '/d', '/s', '/c', command]
        if backend == 'pwsh':
            import shutil
            if shutil.which('pwsh'):
                return ['pwsh.exe', '-NoProfile', '-NonInteractive', '-Command',
                        '[Console]::OutputEncoding=[Text.UTF8Encoding]::new();' + command]
        return ['powershell.exe','-NoProfile','-NonInteractive','-Command',
                '[Console]::OutputEncoding=[Text.UTF8Encoding]::new();'+command]
    return ['/bin/sh','-c',command]


def state_directory():
    if sys.platform=='darwin':
        return Path.home()/'Library/Application Support/Open QQ Dot'
    return Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))/'open-qq-dot'


def acquire_posix_lock(name):
    """The open file owns the lock; stale files after a crash do not block startup."""
    import fcntl
    directory=state_directory(); directory.mkdir(parents=True,exist_ok=True)
    handle=os.fdopen(os.open(directory/(name+'.lock'),os.O_RDWR|os.O_CREAT,0o600),'a+b')
    try:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def startup_report():
    from house_settings import python_executable
    modules=('sqlite3','ssl','ctypes','psutil','websockets','PySide6','qfluentwidgets')
    dependencies={}
    for name in modules:
        try:
            # Resolve native DLLs too, while keeping stdout as one JSON report.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                importlib.import_module('PySide6.QtWidgets' if name=='PySide6' else name)
            dependencies[name]=True
        except (ImportError, OSError):
            dependencies[name]=False
    return {'platform':sys.platform,'python':sys.version.split()[0],'child_python':python_executable(),
            'dependencies':dependencies,'ready':all(dependencies.values()),
            'model_requests':0,'starts_services':False}
