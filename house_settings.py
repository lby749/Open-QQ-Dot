"""Project-local options and the Python executable used by house child processes."""
import json
import os
import sys
import tempfile
from pathlib import Path


def root():
    return Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))


def replace_file(source, target, attempts=40):
    """os.replace that waits out a Windows sharing violation. Replacing a file another thread or process is reading
    fails with WinError 5 (1244 of 3000 replaces in a measured read/replace race), which surfaced as a random
    "Save failed" on the settings pages; readers hold the file only for milliseconds."""
    import time
    for attempt in range(attempts):
        try:
            return os.replace(source, target)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.005 * (attempt + 1), 0.1))


def code_root():
    """Where the program's own scripts live (MCP tool servers are started from here).

    Normally the same folder as root(), but QQ_BRIDGE_HOME may point a test or a second house at a
    separate data home that has no copy of the code."""
    return Path(__file__).resolve().parent


def data_dir():
    """The one shared runtime data directory (tasks, approvals, notifications, memory).

    Every process (bridge, panel, MCP tool servers) must resolve the same path, so this never
    depends on the current working directory."""
    path = root() / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(project_root=None):
    path = Path(project_root or root()) / 'house.local.json'
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def personal_qq_enabled(project_root=None):
    settings = load(project_root)
    return settings.get('qq_personal_enabled', settings.get('qq_connection_mode') == 'legacy') is True


def save(value, project_root=None):
    directory = Path(project_root or root())
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    Path(stream.name).replace(directory / 'house.local.json')


def local_models_enabled(project_root=None):
    return load(project_root).get('local_models_enabled') is True


_LOCAL_MODEL_LISTENERS = []


def subscribe_local_models(callback):
    """Register a UI refresher; called after the shared switch changes."""
    if callback not in _LOCAL_MODEL_LISTENERS:
        _LOCAL_MODEL_LISTENERS.append(callback)


def set_local_models_enabled(enabled, project_root=None):
    """Single write path so every page can mirror the real saved state."""
    root_path = project_root if project_root is not None else root()
    value = load(root_path)
    value['local_models_enabled'] = bool(enabled)
    save(value, root_path)
    for callback in list(_LOCAL_MODEL_LISTENERS):
        try:
            callback(bool(enabled))
        except Exception:
            pass
    return bool(enabled)


def python_executable(project_root=None):
    if os.name!='nt':
        directory=Path(project_root or root())
        return next((str(directory/name/'bin'/exe) for name in ('.venv','.venv-posix')
                     for exe in ('python3','python') if (directory/name/'bin'/exe).is_file()),sys.executable)
    scripts = Path(project_root or root()) / '.venv' / 'Scripts'
    return next((str(scripts / name) for name in ('python.exe',)
                 if (scripts / name).is_file()), sys.executable)
