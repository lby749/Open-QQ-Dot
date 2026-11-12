"""Small Windows launcher; installs dependencies, then starts the named local interpreter."""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


def prepare_runtime(home):
    """Use the venv interpreter: copying it breaks Conda/standalone DLL discovery."""
    python = Path(home) / '.venv' / 'Scripts' / 'python.exe'
    if not python.is_file():
        raise RuntimeError('Python 环境尚未准备好，请运行一键安装器修复。')
    return python


def child_environment(home):
    """External Python must not inherit the frozen launcher's DLL/runtime paths."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('_PYI_') and key != '_MEIPASS2'}
    bundled = getattr(sys, '_MEIPASS', None)
    if bundled:
        prefix = os.path.normcase(os.path.abspath(bundled))
        env['PATH'] = os.pathsep.join(part for part in env.get('PATH', '').split(os.pathsep)
            if os.path.normcase(os.path.abspath(part)) != prefix
            and not os.path.normcase(os.path.abspath(part)).startswith(prefix + os.sep))
    env.update(QQ_BRIDGE_HOME=str(home), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if getattr(sys, 'frozen', False):
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        if os.name == 'nt':
            ctypes.windll.kernel32.SetDllDirectoryW(None)
    return env


def main():
    home = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    installed = home / '.venv' / 'Scripts' / 'python.exe'
    env = child_environment(home)
    ready = installed.is_file()
    if ready:
        probe = subprocess.run([str(installed), '-X', 'utf8', '-c',
            'import sqlite3, ctypes, ssl, PySide6, qfluentwidgets, PIL, psutil, websockets, jsonschema, numpy, pypdf, ijson, qrcode, botpy, cryptography'],
            cwd=home, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        ready = probe.returncode == 0
    if not ready:
        if '--prepare-only' in sys.argv:
            raise RuntimeError('基础依赖校验失败（包括 SQLite、SSL 和界面组件）。')
        answer = ctypes.windll.user32.MessageBoxW(None,
            '首次启动需要安装 Python 依赖和原版 Fluent 界面组件。不会下载本地模型。\n现在安装吗？', 'Open QQ Dot', 0x24)
        if answer != 6:
            return
        bundled = Path(getattr(sys, '_MEIPASS', home))
        uv = bundled / 'uv.exe'
        if uv.is_file():
            helper = home / '.bootstrap'
            helper.mkdir(exist_ok=True)
            target = helper / 'uv.exe'
            if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(uv.read_bytes()).digest():
                shutil.copy2(uv, target)
            for license_file in (bundled / 'licenses' / 'uv').glob('LICENSE*'):
                (helper / 'licenses').mkdir(exist_ok=True)
                shutil.copy2(license_file, helper / 'licenses' / license_file.name)
        powershell = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        code = subprocess.call([powershell, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', str(home / 'setup_environment.ps1')], cwd=home, env=env)
        if code:
            raise RuntimeError('环境安装失败，请查看安装窗口中的下载或依赖校验信息，也可运行一键安装器修复。')
    python = prepare_runtime(home)
    if '--prepare-only' in sys.argv:
        return
    script = home / ('quota-whale/quota_whale.py' if '--quota-widget' in sys.argv else 'launcher.py')
    subprocess.Popen([str(python), '-X', 'utf8', str(script)], cwd=home, env=env,
                     creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if '--prepare-only' in sys.argv:
            home = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
            (home / 'bootstrap-check.log').write_text(str(exc), encoding='utf-8')
        else:
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Open QQ Dot 启动失败', 0x10)
        sys.exit(1)
