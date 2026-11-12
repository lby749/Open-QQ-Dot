"""One-click Windows deployment for Open QQ Dot.

``install_source`` extracts a verified payload without ever touching private
state.  ``run_install`` adds the convenience layer used by the installer UI:
preflight checks, a resumable/repairable Python environment, shortcuts,
optional autostart, an install marker and launch.  ``installer_ui`` owns the
visual shell; this module stays UI-free so it can also run silently.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from house_bootstrap import child_environment

# Source and CI run under cp1252/pipes on some Windows hosts; console output must not
# turn an otherwise successful install into a UnicodeEncodeError.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is None:
        setattr(sys, _stream_name, open(os.devnull, "w", encoding="utf-8"))
    elif hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

APP_NAME = "Open QQ Dot"
MARKER = ".openqqdot-install.json"
RUNTIME_ARCHIVE = "runtime.zip"
RUNTIME_SHA = "runtime.sha256"
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
REQUIRED_IMPORTS = "sqlite3, ctypes, ssl, PySide6, qfluentwidgets, PIL, psutil, websockets, jsonschema, numpy, pypdf, ijson, qrcode, botpy, cryptography, house_qt.window"
PYTHON_CANDIDATES = ('3.13', '3.12', '3.11')
PYTHON_FALLBACK = '3.12'

# uv downloads CPython itself from GitHub Releases. Direct GitHub is unreliable on
# mainland networks and there was no mirror at all, which is why machines without a
# system Python always failed here. The Tsinghua github-release mirror does not carry
# these tags (404); the Nanjing University one does.
PYTHON_MIRRORS = (
    ('南京大学 GitHub 镜像',
     'https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/'),
    ('gh-proxy 中转',
     'https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download'),
    ('GitHub 直连', None),
)

# The "配置 Python 环境" stage used to sit at a single hardcoded 58%. These bands let
# the UI move inside that stage instead.
PROGRESS_RUNENV = (58, 70)
PROGRESS_CORE = (70, 88)
PROGRESS_QF = (88, 92)
PROGRESS_VERIFY = (92, 96)

_SIZE_RE = re.compile(r'\((\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB)\)')
_RESOLVED_RE = re.compile(r'Resolved (\d+) packages?')
_SUMMARY_RE = re.compile(r'^(Installed|Audited|Prepared|Downloaded) \d+ packages?')
_UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3}
# uv suppresses its progress bar when stdout is a pipe (the installer always uses a pipe),
# so drive the bar from the package counts uv still prints.
_RUNENV_MILESTONES = (
    ('Downloading cpython', 0.10),
    ('Downloaded cpython', 0.55),
    ('Using CPython', 0.75),
    ('Creating virtual environment', 0.85),
    ('+ pip==', 0.98),
)


class _StageProgress:
    """Monotonic progress inside one install stage, plus a readable status line."""

    def __init__(self, callback, span, milestones=()):
        self.callback = callback
        self.low, self.high = span
        self.milestones = milestones
        self.total = 0
        self.done = 0
        self.last = float(span[0])

    def __call__(self, line: str) -> None:
        if self.callback is None:
            return
        fraction = None
        resolved = _RESOLVED_RE.search(line)
        if resolved:
            self.total = max(1, int(resolved.group(1)))
        if _SUMMARY_RE.match(line):
            fraction = 1.0
        elif line.startswith('Downloaded ') and not line[11:12].isdigit():
            self.done += 1
        if fraction is None and self.total:
            fraction = min(1.0, self.done / max(1, self.total))
        for marker, value in self.milestones:
            if marker in line:
                fraction = max(fraction or 0.0, value)
        percent = None
        if fraction is not None:
            percent = max(self.last, self.low + (self.high - self.low) * fraction)
            self.last = percent
        self.callback({'percent': percent, 'text': line})


class InstallError(RuntimeError):
    pass


class InstallCancelled(InstallError):
    pass


def install_source(payload, destination):
    from tools.package_release import is_ignored, load_gitignore_patterns
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    patterns = load_gitignore_patterns(destination)
    with zipfile.ZipFile(payload) as archive:
        members = []
        for info in archive.infolist():
            name = info.filename.replace('\\', '/')
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or ':' in name
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError('安装包包含不安全路径：' + name)
            if info.is_dir():
                continue
            if is_ignored(name, patterns) and name != 'Open QQ Dot.exe':
                raise ValueError('安装包含运行数据或凭据，已拒绝：' + name)
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError('安装路径不能经过指向目录外的链接')
            members.append((info, target))
        # Validate every entry before writing anything. Never replace friend/account state.
        backup = destination / 'installation-backup'
        for info, target in members:
            content = archive.read(info)
            if target.is_file() and target.read_bytes() != content:
                saved = backup / target.relative_to(destination)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.install-tmp')
            temporary.write_bytes(content)
            temporary.replace(target)
    return destination


def runtime_ready(destination: Path) -> bool:
    target = Path(destination) / "qq-runtime"
    return (target / "index.js").is_file() and (target / "node.exe").is_file()


def runtime_public_file(relative) -> bool:
    """Only runtime code/assets, never the running account's state or config."""
    path = PurePosixPath(str(relative).replace('\\', '/'))
    parts = tuple(part.lower() for part in path.parts)
    private = {'state', 'qq-data', 'cache', 'config', 'logs', 'credentials', 'sessions', 'user_data', 'userdata'}
    if not parts or path.is_absolute() or '..' in parts or any(part in private for part in parts[:-1]):
        return False
    name = parts[-1]
    if (name.startswith('.env') or name in {'auth.json', 'token.json', 'tokens.json', 'credentials.json'}
            or name.endswith(('.log', '.pid', '.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3',
                              '.sqlite-wal', '.sqlite-shm', '.sqlite3-wal', '.sqlite3-shm'))):
        return False
    if len(parts) == 1:
        return name in {'index.js', 'node.exe', 'package.json', 'config.json', 'wrapper.node'} or name.endswith('.dll')
    return parts[0] in {'napcat', 'win64'}


def _runtime_source(resources: Path) -> Path | None:
    """A runtime directory shipped next to the installer (source-tree deployment)."""
    resources = Path(resources)
    for candidate in (resources / "qq-runtime", resources / "runtime",
                      resources.parent / "qq-runtime", resources.parent / "runtime"):
        if (candidate / "index.js").is_file() and (candidate / "node.exe").is_file():
            return candidate
    return None


def _safe_zip_members(archive: zipfile.ZipFile, destination: Path):
    members = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or ":" in name or stat.S_ISLNK(info.external_attr >> 16):
            raise InstallError("运行时包包含不安全路径：" + name)
        if info.is_dir():
            continue
        members.append((info, destination.joinpath(*path.parts)))
    return members


def install_runtime(resources: Path, destination: Path, emit=print, *, force: bool = False) -> bool:
    """Copy the bundled QQ runtime to ``qq-runtime``; keep existing runtime unless forced."""
    resources, destination = Path(resources), Path(destination)
    target = destination / "qq-runtime"
    if runtime_ready(destination) and not force:
        emit("QQ 运行时已存在，保留现有版本。")
        return False
    archive = resources / RUNTIME_ARCHIVE
    source = _runtime_source(resources)
    if not archive.is_file() and source is None:
        emit("安装包未包含 QQ 运行时；程序可以启动，但首次连接 QQ 需要手动准备运行时。")
        return False
    if archive.is_file() and (resources / RUNTIME_SHA).is_file():
        expected = (resources / RUNTIME_SHA).read_text("utf-8").split()[0]
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise InstallError("QQ 运行时包校验失败，请重新获取安装器。")
    staging = destination / ".qq-runtime-install"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        if archive.is_file():
            with zipfile.ZipFile(archive) as package:
                count = 0
                for info, path in _safe_zip_members(package, staging):
                    if not runtime_public_file(path.relative_to(staging)):
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(package.read(info))
                    count += 1
                    if count % 200 == 0:
                        emit(f"正在展开 QQ 运行时（{count} 个文件）……")
        elif source is not None:
            for path in source.rglob('*'):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                    continue
                relative = path.relative_to(source)
                if runtime_public_file(relative):
                    output = staging / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, output)
        if not ((staging / "index.js").is_file() and (staging / "node.exe").is_file()):
            raise InstallError("QQ 运行时不完整（缺少 index.js 或 node.exe）")
        if target.exists():
            # A previous runtime is code only; never a user-data directory.
            if getattr(target, "is_junction", lambda: False)() or target.is_symlink():
                os.rmdir(target)  # remove the link, never its target
            else:
                shutil.rmtree(target, ignore_errors=True)
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    emit("QQ 运行时已安装到 qq-runtime。")
    return True


def default_destination() -> Path:
    executable = Path(getattr(sys, "executable", Path(__file__).resolve().parent))
    portable = executable.parent / "OpenQQDot"
    if (portable / "friends.json").is_file():
        return portable
    for existing in (executable.parent, executable.parent / 'Open-QQ-Dot',
                     executable.parent.parent / 'Open-QQ-Dot'):
        if (existing / 'friends.json').is_file() and (existing / 'launcher.py').is_file():
            return existing
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs"
    return base / "OpenQQDot"


def _disk_free(path: Path) -> int:
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return 0


def app_running() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenMutexW(0x00100000, False, "Local\\OpenQQDotGui")
        if handle:
            kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False



def _stop_installation_processes(destination: Path) -> list[int]:
    """Stop the app/bridge/runtime processes that keep DLLs under destination loaded."""
    destination = Path(destination).resolve()
    prefix = str(destination).casefold()
    stopped: list[int] = []
    try:
        import psutil
        for process in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                if process.pid == os.getpid():
                    continue
                exe = str(process.info.get("exe") or "").casefold()
                command = " ".join(process.info.get("cmdline") or []).casefold()
                if not (exe.startswith(prefix) or prefix in command):
                    continue
                process.terminate()
                try:
                    process.wait(timeout=4)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
                stopped.append(process.pid)
            except Exception:
                continue
    except ImportError:
        script = ('$d=$env:QQ_UNINSTALL_DIR; '
                  'Get-CimInstance Win32_Process | '
                  'Where-Object { $_.ExecutablePath -like "$d*" -or $_.CommandLine -like "*$d*" } | '
                  'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }')
        env = dict(os.environ, QQ_UNINSTALL_DIR=str(destination))
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           env=env, capture_output=True, timeout=30)
        except Exception:
            pass
    return stopped


def _remove_tree(path: Path) -> None:
    """Windows releases file handles a little after process exit; retry briefly."""
    last_error = None
    for attempt in range(10):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except (OSError, PermissionError) as exc:
            last_error = exc
            time.sleep(0.4 + attempt * 0.2)
    raise InstallError(f"文件仍被占用，无法删除：{path}（{last_error}）。请关闭程序后重试。")


PRESERVED_ON_UNINSTALL = ("data", "friends", "workspaces", "exports", "capsules")


def uninstall(destination: Path, emit=print, *, keep_data: bool = True) -> dict:
    """Remove installed program files; keep private state by default.

    Refuses to touch a directory that does not carry this installer's marker.
    """
    destination = Path(destination).resolve()
    marker = read_marker(destination)
    if not marker or marker.get("app") != APP_NAME:
        raise InstallError("目标目录不是 Open QQ Dot 安装目录，未执行卸载。")
    stopped = _stop_installation_processes(destination)
    if stopped:
        emit("已先停止安装目录内仍运行的进程：" + "、".join(str(pid) for pid in stopped))
    removed, kept = [], []
    for item in destination.iterdir():
        name = item.name
        if keep_data and name in PRESERVED_ON_UNINSTALL:
            kept.append(name)
            continue
        _remove_tree(item)
        removed.append(name)
    for pattern in (".shortcut-*.lnk",):
        for shortcut in destination.glob(pattern):
            shortcut.unlink(missing_ok=True)
    emit("已卸载程序文件；保留：" + ("、".join(kept) if kept else "无"))
    return {"removed": removed, "kept": kept, "destination": str(destination)}

def preflight(destination: Path, resources: Path) -> dict:
    """Read-only checks shown before the first write."""
    destination, resources = Path(destination), Path(resources)
    errors, warnings = [], []
    if not str(destination).strip():
        errors.append("安装目录不能为空")
    if destination.anchor and destination.resolve() == Path(destination.anchor):
        errors.append("不能直接安装到磁盘根目录")
    try:
        resolved = destination.resolve()
        if any(part.lower() in ("windows", "system32", "program files", "program files (x86)")
               for part in resolved.parts):
            warnings.append("系统目录可能需要管理员权限，建议安装到用户目录")
    except OSError:
        pass
    parent = destination if destination.exists() else destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".openqqdot-write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"安装目录不可写：{exc}")
    payload = resources / "application.zip"
    checksum = resources / "application.sha256"
    if not payload.is_file():
        errors.append("安装包 application.zip 缺失")
    if not checksum.is_file():
        errors.append("安装包校验文件 application.sha256 缺失")
    if not (resources / "uv.exe").is_file():
        errors.append("缺少 uv.exe，无法准备 Python 环境")
    if not ((resources / RUNTIME_ARCHIVE).is_file() or runtime_ready(destination) or _runtime_source(resources)):
        warnings.append("安装包未包含 QQ 运行时；安装后可以打开程序，但首次连接 QQ 需要手动准备运行时")
    free = _disk_free(parent)
    if free and free < MIN_FREE_BYTES:
        errors.append(f"磁盘剩余空间不足（至少需要 {MIN_FREE_BYTES // (1024 ** 3)}GB，当前 {free / (1024 ** 3):.1f}GB）")
    if app_running():
        warnings.append("Open QQ Dot 正在运行，更新前建议先退出，避免文件占用")
    existing = destination / "friends.json"
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "free_gb": round(free / (1024 ** 3), 1) if free else 0,
            "update": existing.is_file(), "destination": str(destination)}


def _version() -> str:
    try:
        from version import VERSION
        return VERSION
    except ImportError:
        pass
    try:
        text = (Path(__file__).resolve().parent / "tools" / "launcher.version").read_text("utf-8")
        import re
        match = re.search(r"FileVersion'\s*,\s*'([^']+)'", text)
        return match.group(1) if match else "1.0.0"
    except OSError:
        return "1.0.0"


def _marker_path(destination: Path) -> Path:
    return Path(destination) / MARKER


def read_marker(destination: Path) -> dict:
    try:
        return json.loads(_marker_path(destination).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def write_marker(destination: Path, payload_sha: str, python_version: str = PYTHON_FALLBACK) -> Path:
    marker = {"app": APP_NAME, "version": _version(), "payload_sha256": payload_sha,
              "installed_at": time.time(), "python": python_version}
    path = _marker_path(destination)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _tail_log(log: Path, lines: int = 25) -> str:
    try:
        rows = log.read_text('utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return '\n'.join(rows[-lines:]).strip() or '（日志为空）'


def _kill_tree(process) -> None:
    """uv spawns helpers; killing only the parent leaves them holding the venv."""
    if os.name == 'nt':
        with contextlib.suppress(Exception):
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return
    with contextlib.suppress(Exception):
        process.kill()


def _run_logged(args, cwd: Path, env: dict, log: Path, emit, timeout=1800, cancel_event=None,
                on_progress=None) -> int:
    """Run a command and stream its output.

    uv writes progress bars with carriage returns and no newline, so iterating
    ``for line in process.stdout`` shows nothing until the command ends — that is why the
    installer looked frozen. Segments ending in ``\r`` go to ``on_progress`` (status line
    only, they would flood the log); segments ending in ``\n`` are written to the log.
    """
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n$ " + " ".join(str(a) for a in args) + "\n")
        stream.flush()
        process = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        pending = ''
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk.decode('utf-8', 'replace')
                while True:
                    cr, nl = pending.find('\r'), pending.find('\n')
                    cuts = [index for index in (cr, nl) if index >= 0]
                    if not cuts:
                        break
                    cut = min(cuts)
                    segment, pending = pending[:cut].strip(), pending[cut + 1:]
                    if not segment:
                        continue
                    if on_progress is not None:
                        on_progress(segment)
                    if cut != cr:
                        stream.write(segment + '\n')
                        stream.flush()
                        emit(segment)
                if cancel_event is not None and cancel_event.is_set():
                    _kill_tree(process)
                    emit("已取消安装。")
                    return -2
                if time.monotonic() > deadline:
                    _kill_tree(process)
                    emit("命令超时，已停止。")
                    return -1
            if pending.strip():
                if on_progress is not None:
                    on_progress(pending.strip())
                stream.write(pending.strip() + '\n')
                stream.flush()
                emit(pending.strip())
            return process.wait(timeout=60)
        finally:
            with contextlib.suppress(Exception):
                process.stdout.close()


def register_file_types(destination: Path, emit=print) -> list[str]:
    """Register .oqtc/.oqdbackup double-click handlers and an Open With entry."""
    if os.name != "nt":
        return []
    destination = Path(destination)
    pythonw = destination / ".venv" / "Scripts" / "pythonw.exe"
    launcher = destination / "launcher.py"
    if not pythonw.is_file() or not launcher.is_file():
        return []
    command = f'"{pythonw}" -X utf8 "{launcher}" "%1"'
    icon = destination / "assets" / "app-icon.ico"
    import winreg
    created = []
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\OpenQQDot.exe\shell\open\command", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\OpenQQDot.exe", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "FriendlyAppName", 0, winreg.REG_SZ, "Open QQ Dot")
            if icon.is_file():
                winreg.SetValueEx(key, "DefaultIcon", 0, winreg.REG_SZ, str(icon))
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\OpenQQDot.exe\SupportedTypes", 0, winreg.KEY_SET_VALUE) as key:
            for suffix in (".oqtc", ".oqdbackup", ".json", ".jsonl", ".zip", ".md"):
                winreg.SetValueEx(key, suffix, 0, winreg.REG_SZ, "")
        for suffix, prog_id, label in ((".oqtc", "OpenQQDot.Capsule", "Open QQ Dot 任务胶囊"),
                                       (".oqdbackup", "OpenQQDot.Backup", "Open QQ Dot 数据备份")):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}\shell\open\command", 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}", 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, label)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{suffix}", 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, prog_id)
            created.append(suffix)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\.oqtc\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "OpenQQDot.Capsule", 0, winreg.REG_NONE, b"")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\.oqdbackup\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "OpenQQDot.Backup", 0, winreg.REG_NONE, b"")
    except OSError as exc:
        emit(f"文件关联设置失败（不影响使用）：{exc}")
        return created
    emit("已关联任务胶囊与备份文件，可双击打开。")
    return created


def create_shortcuts(destination: Path, emit=print) -> list[Path]:
    """Desktop and Start Menu shortcuts; failures are warnings, not install failures."""
    if os.name != "nt":
        return []
    app_launcher = Path(destination) / "Open QQ Dot.exe"
    pythonw = Path(destination) / ".venv" / "Scripts" / "pythonw.exe"
    launcher = Path(destination) / "launcher.py"
    icon = Path(destination) / "assets" / "app-icon.ico"
    if app_launcher.is_file():
        # The GUI bootstrap copies the real Python interpreter and starts it with
        # CREATE_NO_WINDOW. It avoids uv's console pythonw trampoline entirely.
        target_path, arguments = app_launcher, ""
    elif pythonw.is_file() and launcher.is_file():
        target_path, arguments = pythonw, f'"{launcher}"'
    else:
        return []
    created = []
    for folder in ("Desktop", "Programs"):
        target = Path(destination) / f".shortcut-{folder}.lnk"
        escaped = {key: str(value).replace("'", "''") for key, value in
                   {"target": target_path, "workdir": destination}.items()}
        icon_line = (f"$s.IconLocation = '{str(icon).replace(chr(39), chr(39) * 2)}'\n" if icon.is_file() else "")
        args_line = f"$s.Arguments = '{arguments.replace(chr(39), chr(39) * 2)}'\n"
        script = ("$ErrorActionPreference='Stop'\n"
                  "$shell = New-Object -ComObject WScript.Shell\n"
                  f"$folder = [Environment]::GetFolderPath('{folder}')\n"
                  f"$path = Join-Path $folder '{APP_NAME}.lnk'\n"
                  "$s = $shell.CreateShortcut($path)\n"
                  f"$s.TargetPath = '{escaped['target']}'\n"
                  + args_line +
                  f"$s.WorkingDirectory = '{escaped['workdir']}'\n"
                  + icon_line +
                  "$s.Description = 'Open QQ Dot 桌面 AI 伙伴'\n"
                  "$s.Save()\n"
                  "Write-Output $path\n")
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and result.stdout.strip():
                created.append(Path(result.stdout.strip().splitlines()[-1]))
        except Exception as exc:
            emit(f"快捷方式创建失败（{folder}）：{exc}")
    if created:
        emit("已创建桌面与开始菜单快捷方式。")
    return created


def set_autostart(destination: Path, enabled: bool) -> bool:
    if os.name != "nt":
        return False
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_launcher = Path(destination) / "Open QQ Dot.exe"
    pythonw = Path(destination) / ".venv" / "Scripts" / "pythonw.exe"
    launcher = Path(destination) / "launcher.py"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled and app_launcher.is_file():
            winreg.SetValueEx(key, "OpenQQDot", 0, winreg.REG_SZ, f'"{app_launcher}"')
            return True
        if enabled and pythonw.is_file() and launcher.is_file():
            winreg.SetValueEx(key, "OpenQQDot", 0, winreg.REG_SZ, f'"{pythonw}" "{launcher}"')
            return True
        try:
            winreg.DeleteValue(key, "OpenQQDot")
        except FileNotFoundError:
            pass
    return False


def launch_app(destination: Path) -> subprocess.Popen:
    destination = Path(destination).resolve()
    app_launcher = destination / "Open QQ Dot.exe"
    if app_launcher.is_file():
        return subprocess.Popen([str(app_launcher)], cwd=str(destination), env=child_environment(destination),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pythonw = destination / ".venv" / "Scripts" / "pythonw.exe"
    python = destination / ".venv" / "Scripts" / "python.exe"
    executable = pythonw if pythonw.is_file() else python
    if not executable.is_file():
        raise InstallError("Python 环境尚未准备好，无法启动")
    env = child_environment(destination)
    return subprocess.Popen([str(executable), "-X", "utf8", str(destination / "launcher.py")],
                            cwd=str(destination), env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _python_reports(path: str, version: str) -> bool:
    """True when ``path`` runs and reports exactly ``version`` (for example '3.12')."""
    try:
        result = subprocess.run([path, '-c', 'import sys,sqlite3,ctypes,ssl;assert sys.maxsize > 2**32;print("%d.%d" % sys.version_info[:2])'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and (result.stdout or '').strip() == version


def detect_host_python(uv: Path, env: dict, emit=print) -> str:
    """Absolute path of a healthy system Python 3.11–3.13, or '' when uv must download one.

    ``py -0p`` also lists interpreters that uv installed for *another* installation
    (``...\\.runtime\\cpython-...``, registered under the Astral launcher tag by a previous
    setup). Adopting one of those builds a venv that dies as soon as that other install is
    moved or removed, so they are skipped, as are temp-dir leftovers.

    Returning a path (not a version) matters: ``uv venv --python 3.12`` would download a
    managed CPython even when a system one is present.
    """
    rows = []
    try:
        listing = subprocess.run(['py', '-0p'], capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=20,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout or ''
    except (OSError, subprocess.SubprocessError):
        listing = ''
    for line in listing.splitlines():
        match = re.match(r'\s*-V:(\S+)\s+\*?\s*(.+?)\s*$', line)
        if not match:
            continue
        tag, path = match.group(1), match.group(2).strip()
        if 'astral/' in tag.lower() or not os.path.isfile(path):
            continue
        rows.append(path)
    for name in ('python3', 'python'):
        found = shutil.which(name)
        if found:
            rows.append(found)
    temp_root = (os.environ.get('TEMP') or '').casefold()
    seen = set()
    for path in rows:
        key = os.path.normcase(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        if '\\.runtime\\' in key or (temp_root and key.startswith(temp_root)):
            continue
        for version in PYTHON_CANDIDATES:
            if _python_reports(path, version):
                emit(f'检测到本机 Python {version}：{path}')
                return path
    emit(f'本机没有可用的 Python 3.11–3.13，将由 uv 下载 Python {PYTHON_FALLBACK}。')
    return ''


def verify_install(destination: Path, python: Path | None = None) -> tuple[bool, str]:
    destination = Path(destination)
    python = python or destination / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return False, "Python 环境缺失"
    try:
        result = subprocess.run([str(python), "-X", "utf8", "-c",
                                 f"import {REQUIRED_IMPORTS}; print('ok')"],
                                cwd=str(destination), env=child_environment(destination), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "依赖导入失败")[-1200:]
    return True, ""


PYPI_MIRRORS = (
    ('清华 PyPI 镜像', 'https://pypi.tuna.tsinghua.edu.cn/simple'),
    ('阿里云 PyPI 镜像', 'https://mirrors.aliyun.com/pypi/simple'),
    ('PyPI 官方源', 'https://pypi.org/simple'),
)


def _install_requirements(uv, python: Path, requirements: Path, destination: Path, env: dict,
                          log: Path, emit, cancel_event=None, extra_args=(), on_progress=None) -> int:
    """Install from a domestic mirror first, then fall back to the next source."""
    last_code = -1
    for label, index in PYPI_MIRRORS:
        mirror_env = dict(env, UV_DEFAULT_INDEX=index, UV_INDEX_URL=index, PIP_INDEX_URL=index,
                          UV_HTTP_TIMEOUT='120')
        emit(f'使用{label}安装 Python 依赖……')
        last_code = _run_logged([str(uv), 'pip', 'install', '--python', str(python), *extra_args,
                                 '-r', str(requirements)],
                                destination, mirror_env, log, emit, cancel_event=cancel_event,
                                on_progress=on_progress)
        if last_code == 0:
            return 0
        if last_code == -2:
            return -2
        emit(f'{label}不可用，切换下一个软件源……')
    return last_code


def run_install(resources, destination, emit, launch=True, options=None, on_step=None,
                cancel_event=None, on_progress=None):
    """Install or repair an installation; emits human-readable progress lines."""
    resources, destination = Path(resources), Path(destination).resolve()
    options = dict(options or {})

    def progress(span, milestones=()):
        return _StageProgress(on_progress, span, milestones)

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise InstallCancelled("安装已取消")

    payload = resources / 'application.zip'
    checksum = resources / 'application.sha256'
    if not payload.is_file() or not checksum.is_file():
        raise InstallError("安装器缺少 application.zip 或校验文件")
    expected = checksum.read_text('utf-8').split()[0]
    actual = hashlib.sha256(payload.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError('安装包校验失败，请重新获取安装器。')

    def step(index, label):
        if on_step:
            on_step(index, label)
        emit(label)

    step(0, '正在校验安装包……')
    check_cancel()
    check = preflight(destination, resources)
    if not check["ok"]:
        raise InstallError("；".join(check["errors"]))
    for warning in check["warnings"]:
        emit("提示：" + warning)
    destination.mkdir(parents=True, exist_ok=True)
    check_cancel()
    helper = destination / '.bootstrap'
    helper.mkdir(exist_ok=True)
    uv = helper / 'uv.exe'
    if not uv.is_file() or uv.stat().st_size != (resources / 'uv.exe').stat().st_size:
        shutil.copy2(resources / 'uv.exe', uv)

    emit('正在复制程序文件（保留 friends/、data/ 与登录状态）……')
    install_source(payload, destination)
    check_cancel()
    step(1, '程序文件已更新；准备 QQ 运行时……')
    install_runtime(resources, destination, emit)
    check_cancel()
    step(2, '正在安装或更新界面与基础依赖……')

    base_env = dict(child_environment(destination), UV_PYTHON_INSTALL_DIR=str(destination / '.runtime'),
                    UV_HTTP_TIMEOUT='120',
                    PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    log = destination / 'installation.log'
    python = destination / '.venv' / 'Scripts' / 'python.exe'
    host_python = ''
    if python.is_file():
        emit('复用已有 Python 环境，只检查并补齐缺失依赖。')
        python_version = read_marker(destination).get('python') or str(python)
    else:
        host_python = detect_host_python(uv, base_env, emit)
        python_version = host_python or PYTHON_FALLBACK
    if not python.is_file():
        if not host_python:
            fetched = False
            for label, mirror in PYTHON_MIRRORS:
                env = dict(base_env)
                if mirror:
                    env['UV_PYTHON_INSTALL_MIRROR'] = mirror
                emit(f'下载 Python {PYTHON_FALLBACK}（{label}）……')
                code = _run_logged([str(uv), 'python', 'install', '--no-bin', '--no-registry', PYTHON_FALLBACK], destination, env,
                                   log, emit, cancel_event=cancel_event,
                                   on_progress=progress(PROGRESS_RUNENV, _RUNENV_MILESTONES))
                if code == -2:
                    raise InstallCancelled("安装已取消")
                if code == 0:
                    fetched = True
                    break
                emit(f'{label}下载失败（退出码 {code}），换下一个源')
            if not fetched:
                raise InstallError('Python 解释器下载失败：\n' + _tail_log(log))
        created = False
        for label, index in PYPI_MIRRORS:
            env = dict(base_env, UV_DEFAULT_INDEX=index, UV_INDEX_URL=index, PIP_INDEX_URL=index)
            if host_python:
                env['UV_PYTHON_DOWNLOADS'] = 'never'  # a system Python is present; never fetch one
            emit(f'创建独立 Python 环境（{label}）……')
            code = _run_logged([str(uv), 'venv', '--python', python_version, '--clear', '--seed',
                                str(destination / '.venv')],
                               destination, env, log, emit, cancel_event=cancel_event,
                               on_progress=progress(PROGRESS_RUNENV, _RUNENV_MILESTONES))
            if code == -2:
                raise InstallCancelled("安装已取消")
            if code == 0 and python.is_file():
                created = True
                break
            emit(f'{label}创建环境失败（退出码 {code}），换下一个源')
        if not created:
            raise InstallError('Python 环境创建失败：\n' + _tail_log(log))
    requirements = destination / 'requirements-ui-core.txt'
    if not requirements.is_file():
        requirements = resources / 'requirements-ui-core.txt'
    qfluent = destination / 'requirements-ui-qfluent.txt'
    if not qfluent.is_file():
        qfluent = resources / 'requirements-ui-qfluent.txt'
    if not requirements.is_file():
        # Older payloads only carry the full file; keep repair installs working.
        requirements = destination / 'requirements-ui.txt'
        if not requirements.is_file():
            requirements = resources / 'requirements-ui.txt'
        qfluent = None
    code = _install_requirements(uv, python, requirements, destination, base_env, log, emit,
                                 cancel_event, on_progress=progress(PROGRESS_CORE))
    if code == -2:
        raise InstallCancelled("安装已取消")
    if code != 0:
        emit('所有软件源都失败，最后再试一次官方 PyPI……')
        code = _run_logged([str(uv), 'pip', 'install', '--python', str(python), '--refresh',
                            '-r', str(requirements)], destination,
                           dict(base_env, UV_DEFAULT_INDEX='https://pypi.org/simple',
                                UV_INDEX_URL='https://pypi.org/simple', PIP_INDEX_URL='https://pypi.org/simple'),
                           log, emit, cancel_event=cancel_event,
                           on_progress=progress(PROGRESS_CORE))
    if code == -2:
        raise InstallCancelled("安装已取消")
    if code != 0:
        raise InstallError('依赖安装失败，详细日志：' + str(log))
    if qfluent is not None and qfluent.is_file():
        # qfluentwidgets declares PySide6 (meta), which would pull WebEngine.
        # Its base dependencies are already in requirements-ui-core.txt.
        code = _install_requirements(uv, python, qfluent, destination, base_env, log, emit, cancel_event,
                                     extra_args=('--no-deps',), on_progress=progress(PROGRESS_QF))
        if code == -2:
            raise InstallCancelled("安装已取消")
        if code != 0:
            raise InstallError('界面组件安装失败，详细日志：' + str(log))
    check_cancel()
    if options.get('runtimes', True):
        # The local vector/rerank runtime (PyTorch) is an installer option, checked by
        # default. It goes through local_runtimes, which uses the domestic wheel mirrors —
        # installing it from plain PyPI is what made this step crawl before.
        try:
            import local_runtimes
            emit('安装本地向量运行环境（PyTorch CPU，约 260 MB）……')

            def runtime_progress(percent, text):
                if on_progress is None:
                    return
                low, high = PROGRESS_VERIFY
                on_progress({'percent': None if percent is None
                             else low + (high - low) * max(0.0, min(100.0, percent)) / 100.0,
                             'text': str(text)})

            outcome = local_runtimes.install(
                'embedding', destination, emit=runtime_progress, cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise InstallCancelled("安装已取消")
            if outcome.get('installed'):
                emit('本地向量运行环境已安装（CPU）。')
            else:
                emit('提示：' + str(outcome.get('message') or '本地向量运行环境未安装')
                     + '；稍后可在「智能体与工具 → 模型来源」里补装。')
        except InstallCancelled:
            raise
        except Exception as exc:
            emit(f'提示：本地向量运行环境未安装（{exc}）；稍后可在程序里补装。')
    check_cancel()
    ok, reason = verify_install(destination, python)
    if not ok:
        emit('依赖校验未通过，尝试修复一次……')
        _run_logged([str(uv), 'pip', 'install', '--python', str(python), '--refresh',
                     '-r', str(requirements)], destination, base_env, log, emit,
                    cancel_event=cancel_event, on_progress=progress(PROGRESS_VERIFY, (('OK', 1.0),)))
        check_cancel()
        ok, reason = verify_install(destination, python)
    if not ok:
        raise InstallError('运行环境校验失败：' + reason)
    check_cancel()

    write_marker(destination, actual, python_version)
    if options.get('shortcut', True):
        try:
            create_shortcuts(destination, emit)
        except Exception as exc:
            emit(f"快捷方式创建失败（不影响使用）：{exc}")
    if 'autostart' in options:
        try:
            set_autostart(destination, bool(options['autostart']))
        except Exception as exc:
            emit(f"开机启动设置失败（可稍后在系统中设置）：{exc}")
    if options.get('associate'):
        try:
            register_file_types(destination, emit)
        except Exception as exc:
            emit(f"文件关联设置失败（不影响使用）：{exc}")
    step(3, '安装完成。好友数据与登录状态已保留。')
    emit('安装完成。好友数据和登录状态已保留；本地模型在程序内按需下载。')
    if launch:
        launch_app(destination)
    return destination


def _fallback_main(resources: Path, destination: Path, launch: bool) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    window = tk.Tk()
    window.title('Open QQ Dot 一键安装与启动')
    window.geometry('700x460')
    chosen = tk.StringVar(value=str(destination))
    ttk.Label(window, text='安装到所选目录，已有好友、模型和登录信息会保留。').pack(pady=16)
    line = ttk.Frame(window)
    line.pack(fill='x', padx=18)
    ttk.Entry(line, textvariable=chosen).pack(side='left', fill='x', expand=True)
    def browse():
        selected = filedialog.askdirectory()
        if selected:
            chosen.set(selected)
    ttk.Button(line, text='选择目录', command=browse).pack(side='right', padx=6)
    footer = ttk.Frame(window)
    footer.pack(side='bottom', fill='x')
    output = tk.Text(window, height=12, wrap='word', state='disabled')
    output.pack(fill='both', expand=True, padx=18, pady=12)
    events = queue.Queue()
    button = ttk.Button(footer, text='一键安装 / 更新并启动')
    button.pack(pady=(0, 14))
    def start():
        button.config(state='disabled')
        target = Path(chosen.get())
        def task():
            try:
                run_install(resources, target, lambda text: events.put(('log', text)), launch=launch)
                events.put(('done', '安装完成，程序已启动。'))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=task, daemon=True).start()
    def poll():
        while not events.empty():
            kind, text = events.get_nowait()
            output.config(state='normal')
            output.insert('end', text + '\n')
            output.see('end')
            output.config(state='disabled')
            if kind in ('done', 'error'):
                button.config(state='normal')
                if kind == 'error':
                    messagebox.showerror('安装未完成', text)
        window.after(100, poll)
    button.config(command=start)
    poll()
    window.mainloop()
    return 0


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Open QQ Dot 一键部署")
    parser.add_argument("--destination", default="")
    parser.add_argument("--silent", action="store_true", help="不显示界面，直接安装")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--no-runtimes", action="store_true", help="跳过可选的本地向量运行环境，之后可在程序内补装")
    parser.add_argument("--preview", action="store_true", help="只打开安装器界面，不写入")
    parser.add_argument("--uninstall", action="store_true", help="卸载程序文件，默认保留私人数据")
    parser.add_argument("--remove-data", action="store_true", help="卸载时同时删除 data/friends/workspaces")
    args = parser.parse_args(argv)
    resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    if args.uninstall:
        if not args.destination:
            print("卸载需要 --destination 指定安装目录")
            return 1
        try:
            uninstall(Path(args.destination), print, keep_data=not args.remove_data)
            return 0
        except Exception as exc:
            print("卸载失败：" + str(exc))
            return 1
    destination = Path(args.destination) if args.destination else default_destination()
    if args.preview or (not args.silent and sys.platform == 'win32'):
        try:
            import installer_ui
            return installer_ui.run(resources, preview=args.preview, destination=destination)
        except Exception as exc:
            print(f"图形安装器不可用，改用简易界面：{exc}", file=sys.stderr)
    if args.silent:
        check = preflight(destination, resources)
        if not check["ok"]:
            print("；".join(check["errors"]))
            return 1
        try:
            run_install(resources, destination, print, launch=not args.no_launch,
                        options={"shortcut": not args.no_shortcut, "runtimes": not args.no_runtimes})
            return 0
        except Exception as exc:
            print("安装失败：" + str(exc))
            return 1
    return _fallback_main(resources, destination, not args.no_launch)


if __name__ == '__main__':
    raise SystemExit(main())
