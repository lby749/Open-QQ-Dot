import json

import pytest

import house_status


def test_delete_friend_removes_registry_and_local_folder(tmp_path):
    friend = tmp_path / "friends" / "12345678"
    friend.mkdir(parents=True)
    (friend / "friend.json").write_text('{"qq_id":"12345678"}', "utf-8")
    (tmp_path / "friends.json").write_text(json.dumps({"friends": {"12345678": {"qq_id": "12345678"}}}), "utf-8")
    message = house_status.delete_friend("12345678", root=tmp_path)
    assert "已删除" in message
    assert not friend.exists()
    assert json.loads((tmp_path / "friends.json").read_text("utf-8"))["friends"] == {}


def test_delete_friend_rejects_invalid_identifier(tmp_path):
    with pytest.raises(ValueError):
        house_status.delete_friend("../12345678", root=tmp_path)


def test_delete_friend_reports_missing_friend(tmp_path):
    (tmp_path / "friends.json").write_text('{"friends": {}}', "utf-8")
    with pytest.raises(KeyError):
        house_status.delete_friend("12345678", root=tmp_path)
