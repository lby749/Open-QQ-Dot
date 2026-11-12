import sqlite3
import zipfile

import pytest

import app_backup


def make_home(tmp_path):
    (tmp_path / "friends" / "12345").mkdir(parents=True)
    (tmp_path / "friends" / "12345" / "friend.json").write_text('{"qq":"12345"}', "utf-8")
    (tmp_path / "data").mkdir()
    with sqlite3.connect(tmp_path / "data" / "tasks.sqlite3") as db:
        db.execute("CREATE TABLE tasks (id TEXT)")
        db.execute("INSERT INTO tasks VALUES ('a')")
    (tmp_path / "capsules").mkdir()
    (tmp_path / "capsules" / "hello.oqtc").write_bytes(b"capsule")
    (tmp_path / "dotlink" / "qq-data").mkdir(parents=True)
    (tmp_path / "dotlink" / "qq-data" / "secret.db").write_bytes(b"login-only")
    (tmp_path / "dotlink" / "local.json").write_text('{"token":"x"}', "utf-8")
    (tmp_path / "dotlink" / "state").mkdir()
    (tmp_path / "dotlink" / "state" / "secret.json").write_text("x", "utf-8")
    (tmp_path / "friends.json").write_text('{"friends":{}}', "utf-8")
    return tmp_path


def test_backup_roundtrip_excludes_login_database(tmp_path):
    home = make_home(tmp_path / "app")
    target = tmp_path / "save.oqdbackup"
    report = app_backup.create_backup(home, target)
    assert report["files"] >= 4
    with zipfile.ZipFile(target) as package:
        names = set(package.namelist())
    assert "friends/12345/friend.json" in names
    assert "capsules/hello.oqtc" in names
    assert "dotlink/local.json" in names
    assert not any("secret" in name for name in names)
    info = app_backup.inspect_backup(target)
    assert info["schema"] == app_backup.SCHEMA

    (home / "friends" / "12345" / "friend.json").write_text("changed", "utf-8")
    restored = app_backup.restore_backup(home, target)
    assert restored["files"] >= 4
    assert "12345" in (home / "friends" / "12345" / "friend.json").read_text("utf-8")
    assert (home / "_before_restore").is_dir()


def test_backup_rejects_traversal(tmp_path):
    target = tmp_path / "bad.oqdbackup"
    with zipfile.ZipFile(target, "w") as package:
        package.writestr("manifest.json", '{"schema":"openqqdot.backup/1","files":[]}')
        package.writestr("../escape.txt", b"bad")
    with pytest.raises(app_backup.BackupError, match="不安全路径"):
        app_backup.inspect_backup(target)
