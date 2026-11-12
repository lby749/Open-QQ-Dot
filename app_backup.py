"""Portable backup/restore for Open QQ Dot data.

The archive is a plain zip with a manifest at ``manifest.json``.  It contains
friends, data (tasks/revert/migration indexes), capsules and DotLink settings,
but deliberately excludes the QQ login database (``dotlink/qq-data``) and logs.
SQLite files are copied through sqlite3's backup API so a running app does not
produce a half-written database.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "openqqdot.backup/1"
SUFFIX = ".oqdbackup"
ROOTS = ("friends", "data", "capsules", "dotlink")
FILES = ("friends.json", "house.local.json", "settings.json", "dotlink_vendor_defaults.json")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "qq-data", "state", "open_requests", "logs", "cache"}
EXCLUDED_SUFFIXES = {".log", ".pid"}
SQLITE_SUFFIXES = {".sqlite3", ".db"}


class BackupError(RuntimeError):
    pass


def _iter_files(home: Path):
    for name in FILES:
        path = home / name
        if path.is_file():
            yield path, name
    for root_name in ROOTS:
        root = home / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(home)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            yield path, relative.as_posix()


def _safe_member(name: str) -> PurePosixPath:
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ":" in name:
        raise BackupError("备份包含不安全路径：" + name)
    return path


def create_backup(home: Path, target: Path) -> dict:
    home, target = Path(home), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex[:6])
    files = []
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
            for path, name in _iter_files(home):
                if path.suffix.lower() in SQLITE_SUFFIXES:
                    copy = target.with_name(".sqlite-" + uuid.uuid4().hex + path.suffix)
                    source = output = None
                    try:
                        source = sqlite3.connect(str(path), timeout=5)
                        output = sqlite3.connect(str(copy))
                        source.backup(output)
                        output.close()
                        output = None
                        source.close()
                        source = None
                        package.write(copy, name)
                    finally:
                        if output is not None:
                            output.close()
                        if source is not None:
                            source.close()
                        copy.unlink(missing_ok=True)
                else:
                    package.write(path, name)
                files.append({"path": name, "size": path.stat().st_size})
            manifest = {"schema": SCHEMA, "created_at": time.time(), "files": files,
                        "note": "不含 QQ 登录数据库；恢复后如未登录，请在 QQ 直连页重新扫码。"}
            package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"schema": SCHEMA, "target": str(target), "files": len(files),
            "bytes": target.stat().st_size, "created_at": time.time()}


def inspect_backup(path: Path) -> dict:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as package:
            manifest = json.loads(package.read("manifest.json").decode("utf-8"))
            if manifest.get("schema") != SCHEMA:
                raise BackupError("不是 Open QQ Dot 备份文件")
            for info in package.infolist():
                _safe_member(info.filename)
            return {"schema": manifest["schema"], "files": len(manifest.get("files") or []),
                    "created_at": manifest.get("created_at"), "path": str(path),
                    "names": [item.get("path") for item in (manifest.get("files") or [])][:20]}
    except zipfile.BadZipFile as exc:
        raise BackupError("备份文件已损坏") from exc
    except KeyError as exc:
        raise BackupError("备份缺少 manifest.json") from exc


def restore_backup(home: Path, path: Path) -> dict:
    home, path = Path(home), Path(path)
    info = inspect_backup(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    before = home / "_before_restore" / stamp
    staging = home / (".restore-" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as package:
            for info_member in package.infolist():
                relative = _safe_member(info_member.filename)
                if relative.name == "manifest.json":
                    continue
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.read(info_member))
        # Keep the old data instead of deleting it; users can remove it later.
        for name in ROOTS:
            source = home / name
            if source.exists():
                before.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, before / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("qq-data", "*.log", "*.pid"))
        for name in FILES:
            source = home / name
            if source.is_file():
                before.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, before / name)
        for item in staging.iterdir():
            destination = home / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"schema": SCHEMA, "files": info["files"], "path": str(path),
            "previous": str(before) if before.exists() else "", "restored_at": time.time()}
