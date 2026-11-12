import json
from pathlib import Path

import pytest

import revert_center
import task_undo
from task_manager import TaskManager
from task_workflows import WorkflowStore


def test_task_undo_redo_roundtrip(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "existing.txt").write_text("before", "utf-8")
    task_undo.begin(tmp_path, "t1", workspace)
    (workspace / "created.txt").write_text("new", "utf-8")
    (workspace / "existing.txt").write_text("after", "utf-8")
    task_undo.finish(tmp_path, "t1")
    report = task_undo.undo(tmp_path, "t1")
    assert report["removed"] == ["created.txt"] and report["restored"] == ["existing.txt"]
    assert not (workspace / "created.txt").exists()
    assert (workspace / "existing.txt").read_text("utf-8") == "before"
    redo = task_undo.redo(tmp_path, "t1")
    assert redo["restored"] == ["created.txt", "existing.txt"]
    assert (workspace / "created.txt").read_text("utf-8") == "new"
    assert (workspace / "existing.txt").read_text("utf-8") == "after"
    # undo again after redo is still possible
    assert task_undo.undo(tmp_path, "t1")["restored"] == ["existing.txt"]


def test_task_redo_refuses_when_user_edited_after_undo(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_undo.begin(tmp_path, "t2", workspace)
    (workspace / "a.txt").write_text("task", "utf-8")
    task_undo.finish(tmp_path, "t2")
    task_undo.undo(tmp_path, "t2")
    (workspace / "a.txt").write_text("user edit", "utf-8")
    report = task_undo.redo(tmp_path, "t2")
    assert any("又被修改" in item for item in report["refused"])
    assert (workspace / "a.txt").read_text("utf-8") == "user edit"


def test_revert_center_task_preview_revert_redo(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_undo.begin(tmp_path, "t3", workspace)
    (workspace / "out.txt").write_text("x", "utf-8")
    task_undo.finish(tmp_path, "t3")
    identifier = revert_center.record(tmp_path, "task", "12345", "t3", title="写文件")
    preview = revert_center.preview(tmp_path, identifier)
    assert preview["available"] and preview["created"] == ["out.txt"]
    revert_center.revert(tmp_path, identifier)
    assert not (workspace / "out.txt").exists()
    assert revert_center.timeline(tmp_path, "12345")[0]["status"] == "reverted"
    redo = revert_center.redo(tmp_path, identifier)
    assert (workspace / "out.txt").exists() and "已重做" in redo["message"]
    assert revert_center.timeline(tmp_path, "12345")[0]["status"] == "applied"


def test_revert_center_migration_and_capsule(tmp_path, monkeypatch):
    import platform_migration
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps([{"id": "c1", "title": "对话", "create_time": 1700000000,
                                   "current_node": "n1", "mapping": {"n1": {
                                       "parent": None, "message": {"id": "m1", "author": {"role": "user"},
                                                                     "content": {"parts": ["你好"]}}}}}],
                                 ensure_ascii=False), encoding="utf-8")
    manifest = platform_migration.run_import(tmp_path, "12345", export)
    timeline = revert_center.timeline(tmp_path, "12345")
    migration = next(item for item in timeline if item["kind"] == "migration")
    assert revert_center.preview(tmp_path, migration["operation_id"])["available"] is True
    revert_center.revert(tmp_path, migration["operation_id"])
    assert platform_migration.list_imports(tmp_path, "12345")[0]["status"] == "reverted"

    directory = tmp_path / "friends/12345"
    (directory / "friend.json").write_text(json.dumps({"qq_id": "12345"}), "utf-8")
    manager = TaskManager(tmp_path / "data/tasks.sqlite3", tmp_path)
    store = WorkflowStore(manager)
    workflow_id = store.save("12345", "胶囊流程", {
        "scope": "项目内", "criteria": "通过验收", "max_calls": 3, "token_budget": 4000,
        "steps": [{"title": "一步", "description": "做一件事", "category": "general", "spec": {}}]})
    import task_capsule
    capsule_path = tmp_path / "cap.oqtc"
    record = task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, capsule_path)
    installed = task_capsule.install_capsule(tmp_path, capsule_path, owner="12345")
    operation = f"12345:capsule:capsule:{record['capsule_id']}:{record['version']}"
    assert revert_center.preview(tmp_path, operation)["available"] is True
    revert_center.revert(tmp_path, operation)
    assert not task_capsule.list_capsules(tmp_path)
    manager.stop_worker()


def test_revert_center_rejects_unknown_and_double_revert(tmp_path):
    with pytest.raises(ValueError, match="找不到"):
        revert_center.preview(tmp_path, "missing")
    workspace = tmp_path / "w"
    workspace.mkdir()
    task_undo.begin(tmp_path, "t4", workspace)
    (workspace / "x.txt").write_text("1", "utf-8")
    task_undo.finish(tmp_path, "t4")
    identifier = revert_center.record(tmp_path, "task", "1", "t4")
    revert_center.revert(tmp_path, identifier)
    with pytest.raises(ValueError, match="已经撤销"):
        revert_center.revert(tmp_path, identifier)
