"""Undo journal for the files a task writes in its workspace.

Before a task's first run, begin() records every file in the operation directory and copies it into
data/task_undo/<task_id>/before/ (skipped when the workspace is too large; then only newly created
files can be undone). After each run, finish() keeps backups only for files the task changed and
records their resulting hashes. undo() moves the task's created files and changed versions into
data/task_undo/<task_id>/undone/ and puts the backed-up originals back; it never deletes anything,
and refuses files that were edited again after the task (their hash no longer matches).

Effects outside the workspace (QQ messages, web forms, remote pushes) are not undoable and are not
claimed to be.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.mypy_cache', '.pytest_cache', '.tox',
             'browser_sessions'}  # the friend's persistent browser profile (logins): never a task's output
# App state the tools keep in a workspace: undoing a task must not delete the friend's retrieval index.
SKIP_FILES = ('retrieval_engine.sqlite3',)
MAX_BACKUP_BYTES = 300 * 1024 * 1024
MAX_BACKUP_FILES = 20000


def journal_dir(root: Path, task_id: str) -> Path:
    if not task_id or any(ch in task_id for ch in '/\\:') or '..' in task_id:
        raise ValueError('无效的任务编号')
    return Path(root) / 'data' / 'task_undo' / task_id


def _files(directory: Path, root: Path) -> dict[str, list]:
    """Files under directory, except dependency/VCS folders and the app's own data/ (task database, this journal)."""
    found = {}
    excluded = (Path(root) / 'data').resolve()
    if not directory.is_dir():
        return found
    stack = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS and entry.resolve() != excluded:
                        stack.append(entry)
                elif entry.is_file() and not entry.name.startswith(SKIP_FILES):
                    stat = entry.stat()
                    found[entry.relative_to(directory).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                continue
    return found


def _sha256(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _load(journal: Path) -> dict | None:
    try:
        return json.loads((journal / 'journal.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None


def _save(journal: Path, value: dict) -> None:
    temporary = journal / 'journal.json.tmp'
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding='utf-8')
    temporary.replace(journal / 'journal.json')


def begin(root: Path, task_id: str, directory: Path) -> dict:
    """Record the workspace before the task's first run; later runs keep that original baseline."""
    journal = journal_dir(root, task_id)
    existing = _load(journal)
    if existing is not None:
        return existing
    directory = Path(directory).resolve()
    before = _files(directory, root)
    total = sum(size for size, _ in before.values())
    mode = 'full' if total <= MAX_BACKUP_BYTES and len(before) <= MAX_BACKUP_FILES else 'created_only'
    journal.mkdir(parents=True, exist_ok=True)
    if mode == 'full':
        for relative in before:
            target = journal / 'before' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(directory / relative, target)
            except OSError:
                before[relative].append('unreadable')
    value = {'task_id': task_id, 'directory': str(directory), 'mode': mode, 'started_at': time.time(),
             'before': before, 'changes': {}, 'undone': False}
    _save(journal, value)
    return value


def finish(root: Path, task_id: str, prune: bool = False) -> dict | None:
    """Work out what the task changed. With prune (the task reached a final state), drop the backups of
    files it left alone; a later retry then can only undo what still has a backup."""
    journal = journal_dir(root, task_id)
    value = _load(journal)
    if value is None or value.get('undone'):
        return value
    directory = Path(value['directory'])
    now = _files(directory, root)
    before = value['before']
    changes = {}
    for relative, stat in now.items():
        if relative not in before:
            changes[relative] = {'kind': 'created', 'sha256': _sha256(directory / relative)}
        elif stat[:2] != before[relative][:2]:
            changes[relative] = {'kind': 'modified', 'sha256': _sha256(directory / relative)}
    for relative in before:
        if relative not in now:
            changes[relative] = {'kind': 'deleted'}
    value['changes'] = changes
    value['finished_at'] = time.time()
    if prune and value['mode'] == 'full':
        for relative in list(before):
            if relative not in changes:
                (journal / 'before' / relative).unlink(missing_ok=True)
    _save(journal, value)
    return value


def summary(root: Path, task_id: str) -> dict:
    """What undo would do, for display: counts, file names and whether it is still possible."""
    value = _load(journal_dir(root, task_id))
    if value is None:
        return {'available': False, 'reason': '该任务没有撤销记录（较早的任务或未改动工作区）'}
    if value.get('undone'):
        return {'available': False, 'undone': True, 'reason': '已撤销', 'report': value.get('undo_report', {})}
    changes = value.get('changes') or {}
    if not changes:
        return {'available': False, 'reason': '任务没有改动工作区文件', 'directory': value['directory']}
    kinds = {kind: sorted(k for k, v in changes.items() if v['kind'] == kind)
             for kind in ('created', 'modified', 'deleted')}
    limited = value['mode'] != 'full'
    return {'available': True, 'directory': value['directory'], 'limited': limited, **kinds,
            'reason': '工作区过大，只能撤销新建的文件' if limited else ''}


def undo(root: Path, task_id: str) -> dict:
    """Reverse the task's file changes; returns what was restored, moved aside or refused."""
    journal = journal_dir(root, task_id)
    value = _load(journal)
    if value is None:
        raise RuntimeError('该任务没有撤销记录')
    if value.get('undone'):
        raise RuntimeError('该任务已经撤销过')
    directory = Path(value['directory'])
    stamp = time.strftime('%Y%m%d_%H%M%S')
    aside = journal / 'undone' / stamp
    report = {'removed': [], 'restored': [], 'refused': [], 'kept_aside': str(aside)}
    full = value['mode'] == 'full'
    for relative, change in sorted((value.get('changes') or {}).items()):
        current = directory / relative
        backup = journal / 'before' / relative
        kind = change['kind']
        if kind in ('created', 'modified'):
            if not current.is_file():
                report['refused'].append(f'{relative}（已不存在）')
                continue
            if _sha256(current) != change['sha256']:
                report['refused'].append(f'{relative}（任务之后又被修改过）')
                continue
            if kind == 'modified' and (not full or not backup.is_file()):
                report['refused'].append(f'{relative}（没有改动前的备份）')
                continue
            target = aside / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(target))
            if kind == 'created':
                report['removed'].append(relative)
            else:
                shutil.copy2(backup, current)
                report['restored'].append(relative)
        elif kind == 'deleted':
            if current.exists():
                report['refused'].append(f'{relative}（同名文件已重新出现）')
            elif not full or not backup.is_file():
                report['refused'].append(f'{relative}（没有删除前的备份）')
            else:
                current.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, current)
                report['restored'].append(relative)
    value['undone'] = True
    value['undone_at'] = time.time()
    value['undo_report'] = report
    value.setdefault('history', []).append({'action': 'undo', 'at': value['undone_at'], 'kept_aside': str(aside)})
    _save(journal, value)
    return report


def redo(root: Path, task_id: str) -> dict:
    """Re-apply a task's changes from the ``undone`` copy kept by :func:`undo`.

    Only files still matching the state undo produced are touched; a file edited
    after the undo is refused instead of overwritten.  The redo report is kept in
    the journal so the operation can be undone again later.
    """
    journal = journal_dir(root, task_id)
    value = _load(journal)
    if value is None:
        raise RuntimeError('该任务没有撤销记录')
    if not value.get('undone'):
        raise RuntimeError('该任务当前没有处于已撤销状态')
    directory = Path(value['directory'])
    aside = Path((value.get('undo_report') or {}).get('kept_aside') or '')
    if not aside.is_dir():
        raise RuntimeError('撤销时的文件副本已不在，无法恢复')
    stamp = time.strftime('%Y%m%d_%H%M%S')
    deleted_aside = journal / 'redone_deleted' / stamp
    report = {'restored': [], 'removed': [], 'refused': []}
    for relative, change in sorted((value.get('changes') or {}).items()):
        current, saved = directory / relative, aside / relative
        backup = journal / 'before' / relative
        kind = change['kind']
        if kind in ('created', 'modified'):
            if not saved.is_file():
                report['refused'].append(f'{relative}（撤销副本已不存在）')
                continue
            if current.is_file():
                expected = _sha256(backup) if kind == 'modified' and backup.is_file() else None
                if expected is None or _sha256(current) != expected:
                    report['refused'].append(f'{relative}（撤销后又被修改过）')
                    continue
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(saved), str(current))
            report['restored'].append(relative)
        elif kind == 'deleted':
            if not current.is_file() or not backup.is_file() or _sha256(current) != _sha256(backup):
                report['refused'].append(f'{relative}（内容已变化）')
                continue
            target = deleted_aside / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(target))
            report['removed'].append(relative)
    value['undone'] = False
    value['redone_at'] = time.time()
    value['redo_report'] = report
    value.setdefault('history', []).append({'action': 'redo', 'at': value['redone_at']})
    _save(journal, value)
    return report
