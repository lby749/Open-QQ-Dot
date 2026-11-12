"""Route dragged/double-clicked files to the page that can handle them.

Supported today:
  *.oqtc / *.zip         task capsule -> install (.oqtc, ZIP-compatible)
  *.oqdbackup            full data backup -> restore
  *.json / *.jsonl / *.md / *.txt / *.zip
                         migration export -> inspect
  directories            migration export -> inspect

Only local paths are accepted; the router never opens a URL.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from house_status import ROOT

OPEN_DIR = Path(ROOT) / "data" / "open_requests"
CAPSULE_SUFFIXES = {".oqtc", ".zip"}
BACKUP_SUFFIX = ".oqdbackup"
MIGRATION_SUFFIXES = {".json", ".jsonl", ".ndjson", ".md", ".markdown", ".txt", ".zip"}


def classify(path: Path) -> str:
    path = Path(path)
    if path.is_dir():
        return "migration"
    suffix = path.suffix.lower()
    if suffix in CAPSULE_SUFFIXES:
        if suffix == ".zip":
            try:
                import zipfile
                with zipfile.ZipFile(path) as package:
                    names = package.namelist()
                if any(name == "manifest.json" or name.endswith("/manifest.json") for name in names):
                    return "capsule"
            except (OSError, zipfile.BadZipFile):
                pass
        else:
            return "capsule"
    if suffix == BACKUP_SUFFIX:
        return "backup"
    if suffix in MIGRATION_SUFFIXES:
        return "migration"
    return ""


def _unique(paths):
    result, seen = [], set()
    for item in paths or []:
        try:
            resolved = Path(item).expanduser().resolve()
        except OSError:
            resolved = Path(item).expanduser()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def queue_open(home: Path, paths) -> list[Path]:
    """Second instance: leave a small request file the running window polls."""
    accepted = _unique(paths)
    if not accepted:
        return []
    directory = Path(home) / "data" / "open_requests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"paths": [str(item) for item in accepted]}, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(target)
    return accepted


def drain_open(home: Path) -> list[Path]:
    """First instance: collect queued paths (oldest first) and delete the requests."""
    directory = Path(home) / "data" / "open_requests"
    if not directory.is_dir():
        return []
    paths = []
    for request in sorted(directory.glob("*.json")):
        try:
            value = json.loads(request.read_text("utf-8"))
            paths.extend(value.get("paths") or [])
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            request.unlink(missing_ok=True)
    return _unique(paths)


def route(window, paths) -> int:
    """Send each path to its page; returns how many files were accepted."""
    accepted = _unique(paths)
    if not accepted:
        return 0
    capsules = [item for item in accepted if classify(item) == "capsule"]
    backups = [item for item in accepted if classify(item) == "backup"]
    migrations = [item for item in accepted if classify(item) == "migration"]
    unknown = [item for item in accepted if not classify(item)]
    handled = 0

    if capsules:
        page = window.pages.get("capsulePage")
        if page is not None:
            window.switchTo(page)
            page.show()
            page.install_paths(capsules)
            handled += len(capsules)

    if backups:
        restore = getattr(window, "restore_backup", None)
        if callable(restore):
            for item in backups:
                restore(item)
            handled += len(backups)
        else:
            from .common import toast
            toast(window, "warning", "当前版本还没有启用备份恢复。", "文件")

    if migrations:
        page = window.pages.get("migrationPage")
        if page is not None:
            window.switchTo(page)
            page.show()
            page.inspect_paths(migrations)
            handled += len(migrations)

    if unknown:
        from .common import toast
        names = "、".join(item.name for item in unknown[:3])
        toast(window, "warning", f"暂不支持这种文件：{names}", "拖放")
    return handled
