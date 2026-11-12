import json

import pytest

import chat_transcript as transcript
import qq_history_export as export


def segments(*parts):
    return [{"type": kind, "data": data} for kind, data in parts]


def row(identifier, stamp, sender, message, seq=None, nickname="昵称"):
    return {"message_id": identifier, "message_seq": seq or int(identifier), "time": stamp,
            "sender": {"user_id": int(sender), "nickname": nickname}, "user_id": int(sender),
            "message_type": "private", "message": message}


class FakeClient:
    self_id = "10001"

    def __init__(self, pages, friends=None, groups=None):
        self.pages = pages
        self.friends = friends or []
        self.groups = groups or []
        self.calls = []

    def call(self, action, params):
        self.calls.append((action, dict(params)))
        if action == "get_login_info":
            return {"user_id": int(self.self_id)}
        if action == "get_friend_list":
            return self.friends
        if action == "get_group_list":
            return self.groups
        if action in ("get_friend_msg_history", "get_group_msg_history"):
            boundary = params.get("message_seq") or 10 ** 12
            return {"messages": [m for m in self.pages if boundary is None or m["message_seq"] < boundary]}
        raise AssertionError(action)

    def client(self):
        return export.HistoryClient(self.call, self.self_id)


def test_transcript_roundtrip_and_sharegpt(tmp_path):
    document = transcript.make_transcript(
        platform="qq", kind="friend", identifier="12345", title="测试会话", self_id="10001",
        messages=[transcript.message(identifier="1", timestamp=1700000000, sender_id="12345",
                                     sender_name="小明", role="user", text="你好"),
                  transcript.message(identifier="2", timestamp=1700000060, sender_id="10001",
                                     sender_name="我", role="assistant", text="在的"),
                  transcript.message(identifier="3", timestamp=1700000120, sender_id="12345",
                                     sender_name="小明", role="user", text="帮我看看")])
    assert transcript.digest(document) == transcript.digest(json.loads(json.dumps(document)))
    assert document["stats"]["message_count"] == 3
    path = transcript.write_json(tmp_path / "chat.json", document)
    loaded = transcript.load_transcript(path)
    assert [m["text"] for m in loaded["messages"]] == ["你好", "在的", "帮我看看"]
    pairs = transcript.to_sharegpt(loaded)
    assert [p["from"] for p in pairs["conversations"]] == ["user", "assistant", "user"]
    jsonl = transcript.write_jsonl(tmp_path / "chat.jsonl", loaded)
    assert transcript.load_transcript(jsonl)["source"]["id"] == "12345"
    assert "**小明**" in transcript.to_markdown(loaded)


def test_rejects_unknown_schema_but_normalizes_message_lists():
    with pytest.raises(ValueError):
        transcript.validate({"schema": "something/else", "messages": "not-a-list"})
    normalized = transcript.validate({"messages": [{"content": "hello", "role": "user"}]})
    assert normalized["schema"] == transcript.SCHEMA
    assert normalized["messages"][0]["text"] == "hello"


def test_parse_qq_txt_export(tmp_path):
    text = """消息记录（与 小明 的聊天记录）
2024-05-01 12:00:00 小明(123456)
你好呀
今天有空吗

2024-05-01 12:01:10 我(10001)
[图片]
在的
"""
    path = tmp_path / "聊天记录.txt"
    path.write_text(text, encoding="utf-8")
    document = export.parse_local_export(path, title="与小明")
    assert [m["sender_name"] for m in document["messages"]] == ["小明", "我"]
    assert document["messages"][0]["text"] == "你好呀\n今天有空吗"
    assert document["messages"][1]["type"] == "image"
    assert document["messages"][0]["timestamp"] is not None


def test_parse_local_export_rejects_mht(tmp_path):
    path = tmp_path / "chat.mht"
    path.write_text("MIME", encoding="utf-8")
    with pytest.raises(ValueError, match="MHT"):
        export.parse_local_export(path)


def test_onebot_pagination_deduplicates_and_sorts(tmp_path):
    pages = [row("3", 300, "12345", segments(("text", {"text": "第三句"}))),
             row("2", 200, "10001", segments(("text", {"text": "第二句"}))),
             row("1", 100, "12345", segments(("text", {"text": "第一句"})))]
    client = FakeClient(pages)
    document = export.export_conversation(client.client(), friend="12345", output=tmp_path / "f.json", count=2)
    assert document["messages"] == 3
    saved = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert [m["text"] for m in saved["messages"]] == ["第一句", "第二句", "第三句"]
    assert saved["messages"][1]["role"] == "assistant"
    assert saved["source"]["platform"] == "qq"


def test_onebot_media_segments_and_group_export(tmp_path):
    message = segments(("text", {"text": "看图"}), ("image", {"url": "https://example.invalid/a.png", "file": "a.png"}))
    pages = [row("9", 500, "12345", message)]
    client = FakeClient(pages, groups=[{"group_id": 777, "group_name": "测试群"}])
    report = export.export_all(client.client(), tmp_path, include_groups=True)
    assert report["files"] and report["files"][0]["messages"] == 1
    saved = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "openqqdot.qq-export/1"
    group_file = tmp_path / "测试群（777）.json"
    assert group_file.is_file()
    document = json.loads(group_file.read_text(encoding="utf-8"))
    assert document["messages"][0]["media"][0]["kind"] == "image"


def test_cli_parse_writes_requested_format(tmp_path, capsys):
    source = tmp_path / "chat.txt"
    source.write_text("2024-05-01 12:00:00 小明(123456)\n你好\n", encoding="utf-8")
    target = tmp_path / "chat.md"
    assert export.main(["parse", str(source), "--output", str(target), "--form", "markdown"]) == 0
    assert "**小明**" in target.read_text(encoding="utf-8")
