import json
from pathlib import Path

import pytest

import platform_migration as migration


def chatgpt_export():
    return [{
        "id": "conv-1", "title": "旅行计划", "create_time": 1700000000,
        "current_node": "n3",
        "mapping": {
            "n1": {"id": "n1", "parent": None, "message": {"id": "m1", "author": {"role": "user"},
                                                             "content": {"content_type": "text", "parts": ["帮我规划京都三天"]},
                                                             "create_time": 1700000000}},
            "n2": {"id": "n2", "parent": "n1", "message": {"id": "m2", "author": {"role": "assistant"},
                                                             "content": {"content_type": "text", "parts": ["第一天去清水寺。"]},
                                                             "create_time": 1700000010}},
            "n3": {"id": "n3", "parent": "n2", "message": {"id": "m3", "author": {"role": "user"},
                                                             "content": {"content_type": "text", "parts": ["第二天呢"]},
                                                             "create_time": 1700000020}},
        },
    }, {"id": "conv-2", "title": "空对话", "mapping": {}, "current_node": None}]


def test_inspect_chatgpt_export(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(chatgpt_export(), ensure_ascii=False), encoding="utf-8")
    report = migration.inspect_report(path)
    assert report["platform"] == "chatgpt"
    assert report["conversations"] == 1
    assert report["messages"] == 3
    assert report["titles"] == ["旅行计划"]


def test_import_creates_project_sessions_and_transcripts(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(chatgpt_export(), ensure_ascii=False), encoding="utf-8")
    manifest = migration.run_import(tmp_path, "12345", path, project_title="京都之行")
    assert not manifest.get("duplicate")
    assert manifest["stats"]["messages"] == 3
    assert len(manifest["projects"]) == 1
    workspace = manifest["projects"][0]
    assert Path(workspace["path"]).is_dir() and (Path(workspace["path"]) / "PROJECT.md").is_file()
    bundle = tmp_path / "data" / "migrations" / manifest["import_id"]
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "conversations").is_dir()
    stored = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert stored["schema"] == migration.SCHEMA
    assert all(item["session_id"] for item in stored["conversations"])
    import session_manager
    previous = session_manager.ROOT
    session_manager.ROOT = tmp_path
    try:
        sessions = session_manager.list_sessions("12345", workspace["workspace_id"])
        loaded = session_manager.load_session("12345", sessions[0]["id"], workspace["workspace_id"])
        assert loaded["messages"] and loaded["messages"][0]["role"] in ("user", "assistant", "system")
    finally:
        session_manager.ROOT = previous


def test_import_is_idempotent_unless_forced(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(chatgpt_export(), ensure_ascii=False), encoding="utf-8")
    first = migration.run_import(tmp_path, "12345", path)
    again = migration.run_import(tmp_path, "12345", path)
    assert again.get("duplicate") is True and again["import_id"] == first["import_id"]
    forced = migration.run_import(tmp_path, "12345", path, force=True)
    assert not forced.get("duplicate") and forced["import_id"] != first["import_id"]


def test_import_keeps_separate_projects_and_uses_bulk_sessions(tmp_path):
    payload = [{"id": "a", "title": "A1", "project": "项目甲", "create_time": 1700000000,
                "current_node": "n1", "mapping": {"n1": {"parent": None, "message": {
                    "id": "m1", "author": {"role": "user"}, "content": {"parts": ["甲"]}}}}},
               {"id": "b", "title": "B1", "project": "项目乙", "create_time": 1700000100,
                "current_node": "n2", "mapping": {"n2": {"parent": None, "message": {
                    "id": "m2", "author": {"role": "user"}, "content": {"parts": ["乙"]}}}}}]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest = migration.run_import(tmp_path, "12345", path)
    assert len(manifest["projects"]) == 2
    assert manifest["stats"]["projects"] == 2
    titles = {item["project_title"] for item in manifest["conversations"]}
    assert titles == {"项目甲", "项目乙"}
    import session_manager
    previous = session_manager.ROOT
    session_manager.ROOT = tmp_path
    try:
        for project in manifest["projects"]:
            assert session_manager.list_sessions("12345", project["workspace_id"])
    finally:
        session_manager.ROOT = previous


def test_runtime_error_for_invalid_owner_and_missing_source(tmp_path):
    with pytest.raises(migration.MigrationError, match="好友"):
        migration.run_import(tmp_path, "abc", tmp_path / "x.json")
    with pytest.raises(migration.MigrationError, match="不存在"):
        migration.inspect_source(tmp_path / "missing.json")


def test_claude_and_sillytavern_and_markdown(tmp_path):
    claude = [{"uuid": "c1", "name": "写作", "created_at": "2024-05-01T10:00:00Z",
               "chat_messages": [{"uuid": "m1", "sender": "human", "text": "帮我改标题", "created_at": "2024-05-01T10:00:00Z"},
                                 {"uuid": "m2", "sender": "assistant", "text": "改成《春夜》。", "created_at": "2024-05-01T10:01:00Z"}]}]
    path = tmp_path / "claude-conversations.json"
    path.write_text(json.dumps(claude), encoding="utf-8")
    source = migration.inspect_source(path)
    assert source.platform == "claude" and source.message_count == 2

    tavern = tmp_path / "chat.jsonl"
    tavern.write_text('{"name":"小明","is_user":true,"mes":"你好","send_date":"2024-05-01T10:00:00Z"}\n'
                      '{"name":"助手","is_user":false,"mes":"在的","send_date":"2024-05-01T10:01:00Z"}\n',
                      encoding="utf-8")
    assert migration.inspect_source(tavern).platform == "sillytavern"

    note = tmp_path / "笔记.md"
    note.write_text("# 项目想法\n\n做一个桌面助手。\n", encoding="utf-8")
    markdown = migration.inspect_source(note)
    assert markdown.platform == "markdown" and markdown.conversations[0].messages[0]["text"].startswith("# 项目")


def test_undo_import_restores_workspace_list(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(chatgpt_export()), encoding="utf-8")
    manifest = migration.run_import(tmp_path, "12345", path)
    import session_manager
    previous = session_manager.ROOT
    session_manager.ROOT = tmp_path
    try:
        before = len(session_manager.list_workspaces("12345"))
    finally:
        session_manager.ROOT = previous
    report = migration.undo_import(tmp_path, "12345", manifest["import_id"])
    assert report["removed_workspaces"]
    assert Path(report["moved_to"]).is_dir()
    session_manager.ROOT = tmp_path
    try:
        assert len(session_manager.list_workspaces("12345")) < before
    finally:
        session_manager.ROOT = previous
    assert migration.list_imports(tmp_path, "12345")[0]["status"] == "reverted"
    with pytest.raises(migration.MigrationError, match="已经撤销"):
        migration.undo_import(tmp_path, "12345", manifest["import_id"])


def test_zip_export_is_read_without_extracting(tmp_path):
    import zipfile
    payload = json.dumps(chatgpt_export()).encode()
    target = tmp_path / "export.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("conversations.json", payload)
        archive.writestr("../../escape.txt", b"must not be extracted")
    source = migration.inspect_source(target)
    assert source.platform == "chatgpt" and source.message_count == 3
    assert not (tmp_path.parent / "escape.txt").exists()


def test_cli_import_copies_directory_only_without_chat_context(tmp_path):
    source = tmp_path / "my-cli-project"
    source.mkdir()
    (source / "README.md").write_text("project", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    manifest = migration.run_cli_import(tmp_path, "12345", source, project_title="CLI 项目")
    assert manifest["mode"] == "cli"
    assert manifest["stats"] == {"conversations": 0, "messages": 0, "projects": 1}
    copied = Path(manifest["projects"][0]["path"])
    assert (copied / "README.md").read_text(encoding="utf-8") == "project"
    assert (copied / "src" / "main.py").is_file()
    assert not (copied / "conversations").exists()
    import session_manager
    previous = session_manager.ROOT
    session_manager.ROOT = tmp_path
    try:
        sessions = session_manager.list_sessions("12345", manifest["projects"][0]["workspace_id"])
        assert sessions and sessions[0]["id"] == manifest["projects"][0]["session_id"]
        loaded = session_manager.load_session("12345", sessions[0]["id"],
                                              manifest["projects"][0]["workspace_id"])
        assert loaded["messages"] == []
    finally:
        session_manager.ROOT = previous
