"""One timeline for every reversible action in the desktop app.

The app already has several independent rollback mechanisms (task workspace
undo, goal-stage undo, migration import undo, persona/memory snapshots).  This
module indexes them together so the user can:

* see what changed and when, per friend;
* preview exactly what a revert would do before doing it;
* revert one entry, or a chain newest-first;
* redo a task's file revert (the moved-aside copy is kept);
* prune old reverted entries without touching live data.

Reverting never hard-deletes user content: task files go to ``undone/``,
migrations to ``_trash/`` and capsules to ``_trash/``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = 1


def _db_path(home: Path) -> Path:
    return Path(home) / "data" / "revert" / "index.sqlite3"


def _connect(home: Path) -> sqlite3.Connection:
    path = _db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'applied',
            payload TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS operations_owner_time ON operations(owner, created_at DESC);
    ''')
    return db


def _row(value: sqlite3.Row | dict) -> dict:
    item = dict(value)
    try:
        item["payload"] = json.loads(item.get("payload") or "{}")
    except ValueError:
        item["payload"] = {}
    return item


def record(home: Path, kind: str, owner: str, target: str, *, title: str = "", summary: str = "",
           payload: dict | None = None, operation_id: str = "") -> str:
    """Register (or refresh) a reversible operation; idempotent per owner/kind/target."""
    if kind not in ("task", "goal", "migration", "capsule", "memory"):
        raise ValueError("未知的回溯类型")
    owner, target = str(owner or ""), str(target or "")
    identifier = operation_id or f"{owner}:{kind}:{target}"
    db = _connect(home)
    try:
        with db:
            db.execute('''INSERT INTO operations(operation_id,kind,owner,target,title,summary,created_at,status,payload)
                          VALUES(?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(operation_id) DO UPDATE SET
                            title=excluded.title, summary=excluded.summary, status=excluded.status,
                            payload=excluded.payload''',
                       (identifier, kind, owner, target, str(title)[:300], str(summary)[:4000], time.time(),
                        "applied", json.dumps(payload or {}, ensure_ascii=False)))
        return identifier
    finally:
        db.close()


def mark(home: Path, operation_id: str, status: str) -> None:
    db = _connect(home)
    try:
        with db:
            db.execute("UPDATE operations SET status=? WHERE operation_id=?", (status, operation_id))
    finally:
        db.close()


def timeline(home: Path, owner: str = "", limit: int = 200) -> list[dict]:
    db = _connect(home)
    try:
        if owner:
            rows = db.execute("SELECT * FROM operations WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                              (str(owner), max(1, min(int(limit), 1000)))).fetchall()
        else:
            rows = db.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?",
                              (max(1, min(int(limit), 1000)),)).fetchall()
        items = [_row(row) for row in rows]
    finally:
        db.close()
    for item in items:
        item["can_redo"] = item["kind"] == "task" and item["status"] == "reverted"
    return items


def get(home: Path, operation_id: str) -> dict:
    db = _connect(home)
    try:
        row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            raise ValueError("找不到这条回溯记录")
        return _row(row)
    finally:
        db.close()


def preview(home: Path, operation_id: str) -> dict:
    item = get(home, operation_id)
    kind, owner, target = item["kind"], item["owner"], item["target"]
    if item["status"] == "reverted":
        return {"kind": kind, "available": kind == "task", "reverted": True,
                "text": "已撤销；任务文件可以用「重做」恢复。" if kind == "task" else "已撤销"}
    if kind == "task":
        import task_undo
        info = task_undo.summary(Path(home), target)
        text = info.get("reason") or (f"删除新建文件 {len(info.get('created', []))} 个，还原修改 "
                                      f"{len(info.get('modified', []))} 个、删除 {len(info.get('deleted', []))} 个")
        return {"kind": kind, "available": bool(info.get("available")), "reverted": False,
                "text": text, **{key: info.get(key, []) for key in ("created", "modified", "deleted")}}
    if kind == "goal":
        from task_catalog import undo_preview
        return {"kind": kind, **undo_preview(Path(home), owner, "goal", target)}
    if kind == "migration":
        import platform_migration
        rows = [row for row in platform_migration.list_imports(Path(home), owner)
                if row.get("import_id") == target]
        row = rows[0] if rows else None
        if not row:
            return {"kind": kind, "available": False, "text": "迁移记录不存在"}
        return {"kind": kind, "available": True, "text": f"移除迁移项目「{row.get('project_title', '')}」及其会话；"
                                                          f"导入包移到回收目录，不删除聊天记录。",
                "conversations": row.get("stats", {}).get("conversations", 0)}
    if kind == "capsule":
        import task_capsule
        installed = [row for row in task_capsule.list_capsules(Path(home))
                     if f"capsule:{row['capsule_id']}:{row['version']}" == target]
        if not installed:
            return {"kind": kind, "available": False, "text": "胶囊未安装或已移除"}
        return {"kind": kind, "available": True, "text": f"卸载任务胶囊「{installed[0].get('title', '')}」"
                                                          f"（文件移入回收目录；已创建的工作流不自动删除）。"}
    if kind == "memory":
        import sqlite3 as _sqlite
        path = Path(home) / "friends" / owner / "history.sqlite3"
        try:
            db = _sqlite.connect(path.as_uri() + "?mode=ro", uri=True)
            row = db.execute("SELECT reason,created_at FROM memory_versions ORDER BY seq DESC LIMIT 1").fetchone()
            db.close()
            return {"kind": kind, "available": bool(row),
                    "text": f"恢复到记忆版本：{row[0]}（{row[1]}）" if row else "没有记忆版本"}
        except Exception:
            return {"kind": kind, "available": False, "text": "当前好友没有可恢复的记忆版本"}
    return {"kind": kind, "available": False, "text": "没有可用的回溯方式"}


def snapshot_memory(home: Path, owner: str, reason: str = "手动回溯点") -> str:
    """Create a memory version and register it on the timeline."""
    import memory_management
    from store import FriendStore
    store = FriendStore(Path(home) / "friends" / str(owner))
    try:
        memory_management.snapshot(store, reason)
    finally:
        store.close()
    return record(home, "memory", str(owner), "latest", title="长期记忆快照",
                  summary=f"保存记忆版本：{reason}")


def revert(home: Path, operation_id: str) -> dict:
    item = get(home, operation_id)
    kind, owner, target = item["kind"], item["owner"], item["target"]
    if item["status"] == "reverted":
        raise ValueError("这条记录已经撤销过")
    home = Path(home)
    if kind == "task":
        import task_undo
        report = task_undo.undo(home, target)
        result = {"message": f"已撤销任务文件改动：删除 {len(report['removed'])} 个、还原 {len(report['restored'])} 个",
                  **report}
    elif kind == "goal":
        from task_catalog import undo
        result = undo(home, owner, "goal", target)
    elif kind == "migration":
        import platform_migration
        result = {"message": "已撤销迁移", **platform_migration.undo_import(home, owner, target)}
    elif kind == "capsule":
        import task_capsule
        _, capsule_id, version = target.split(":", 2)
        result = {"message": "已卸载任务胶囊", **task_capsule.remove_capsule(home, capsule_id, int(version or 0))}
    elif kind == "memory":
        import memory_management
        from store import FriendStore
        store = FriendStore(home / "friends" / owner)
        try:
            report = memory_management.restore(store)
        finally:
            store.close()
        result = {"message": "记忆已恢复到上一版本", "report": report}
    else:
        raise ValueError("未知的回溯类型")
    mark(home, operation_id, "reverted")
    return result


def redo(home: Path, operation_id: str) -> dict:
    item = get(home, operation_id)
    if item["kind"] != "task":
        raise ValueError("只有任务文件撤销支持重做")
    import task_undo
    report = task_undo.redo(Path(home), item["target"])
    mark(home, operation_id, "applied")
    return {"message": f"已重做：恢复 {len(report['restored'])} 个文件，再次移出 {len(report['removed'])} 个",
            **report}


def revert_many(home: Path, operation_ids: list[str]) -> dict:
    """Revert newest-first; stops at the first refusal and reports what happened."""
    done, failed = [], []
    for identifier in operation_ids:
        try:
            revert(home, identifier)
            done.append(identifier)
        except Exception as exc:
            failed.append({"operation_id": identifier, "error": str(exc)})
            break
    return {"reverted": done, "failed": failed}


def prune(home: Path, *, keep_days: int = 60) -> dict:
    """Forget reverted index rows older than keep_days; files stay where they are."""
    cutoff = time.time() - max(1, int(keep_days)) * 86400
    db = _connect(home)
    try:
        with db:
            cursor = db.execute("DELETE FROM operations WHERE status='reverted' AND created_at<?", (cutoff,))
        return {"removed_records": cursor.rowcount, "cutoff": cutoff}
    finally:
        db.close()
