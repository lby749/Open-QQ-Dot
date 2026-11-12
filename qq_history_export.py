"""Export QQ chat history to the portable transcript JSON, without the web login page.

Two sources are supported:

* ``onebot`` - read history from the locally running DotLink/OneBot connection
  (``get_friend_msg_history`` / ``get_group_msg_history``).  No browser, no QR
  page and no cloud upload is involved.
* local export - parse the ``.txt`` / ``.json`` files that QQ's own
  "导出消息记录" writes, for accounts that are no longer online.

The writer is shared with :mod:`chat_transcript`, so the same file can be fed to
the migration importer, the dataset builder or the desktop preview.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

import chat_transcript as transcript

DEFAULT_COUNT = 200
MAX_PAGES = 500
SELF_PLACEHOLDER = "我"
TIMESTAMP_LINE = re.compile(r"^\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\s+(.+?)\s*$")
SENDER_LINE = re.compile(r"^(?P<name>.*?)\s*[（(<](?P<id>\d{4,12})[)）>]\s*$")
SEGMENT_LABELS = ("[图片]", "[表情]", "[表情包]", "[语音]", "[视频]", "[文件]", "[动画表情]", "[位置]",
                  "[链接]", "[音乐]", "[分享]", "[红包]", "[转账]", "[戳一戳]", "[合并转发]")


def safe_name(value: str, fallback: str = "chat") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" .")
    return (cleaned or fallback)[:80]


def _media(segment: dict) -> dict | None:
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    kind = {"image": "image", "file": "file", "video": "video", "record": "voice"}.get(str(segment.get("type")))
    if not kind:
        return None
    return {"kind": kind, "url": str(data.get("url") or ""), "name": str(data.get("name") or data.get("file") or ""),
            "size": str(data.get("file_size") or data.get("size") or ""), "sha256": ""}


def parse_segments(message: Any) -> tuple[str, str, list[dict]]:
    """Return (kind, text, media) for one OneBot message (array or CQ string)."""
    media: list[dict] = []
    if isinstance(message, str):
        text = message
        for label in SEGMENT_LABELS:
            if label in text:
                kind = {"[图片]": "image", "[语音]": "voice", "[视频]": "video", "[文件]": "file",
                        "[表情]": "sticker", "[表情包]": "sticker", "[动画表情]": "sticker"}.get(label, "text")
                return kind, text.replace(label, ""), media
        return "text", text, media
    if not isinstance(message, list):
        return "text", str(message or ""), media
    parts: list[str] = []
    kinds: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        found = _media(segment)
        if found:
            media.append(found)
            kinds.append(found["kind"])
            continue
        parts.append(transcript.segment_text(segment))
        kinds.append(str(segment.get("type") or "text"))
    text = "".join(parts).strip()
    primary = media[0]["kind"] if media else "text"
    if not text and media:
        text = {"image": "[图片]", "file": "[文件]", "video": "[视频]", "voice": "[语音]"}.get(primary, f"[{primary}]")
    elif media:
        text += " " + " ".join({"image": "[图片]", "file": "[文件]", "video": "[视频]", "voice": "[语音]"}.get(m["kind"], "")
                                for m in media)
    return primary, text, media


def onebot_message(row: dict, self_id: str, *, group: bool = False) -> dict:
    sender = row.get("sender") if isinstance(row.get("sender"), dict) else {}
    sender_id = str(sender.get("user_id") or row.get("user_id") or "")
    name = str(sender.get("card") or sender.get("nickname") or sender_id)
    kind, text, media = parse_segments(row.get("message"))
    role = "assistant" if self_id and sender_id == self_id else ("user" if not group else "other")
    if group and role == "other" and self_id and sender_id == self_id:
        role = "assistant"
    return transcript.message(identifier=str(row.get("message_id") or ""), timestamp=row.get("time"),
                              sender_id=sender_id or ("self" if role == "assistant" else ""), sender_name=name,
                              role=role, kind=kind, text=text, media=media,
                              reply_to=str((row.get("message") or [{}])[0].get("data", {}).get("id", ""))
                              if isinstance(row.get("message"), list) and row.get("message") else "")


class HistoryClient:
    """Thin adapter around one OneBot ``call`` function (a real DotLink or a test double)."""

    def __init__(self, call: Callable[[str, dict], Any], self_id: str = ""):
        self._call = call
        self.self_id = str(self_id or "")

    @classmethod
    def from_local(cls, home: Path | None = None):
        from qq_link import QQClient
        client = QQClient() if home is None else _home_client(home)
        info = client.call("get_login_info") or {}
        return cls(client.call, str(info.get("user_id") or "")), client

    def friend_list(self) -> list[dict]:
        return [item for item in (self._call("get_friend_list", {}) or []) if isinstance(item, dict)]

    def group_list(self) -> list[dict]:
        return [item for item in (self._call("get_group_list", {}) or []) if isinstance(item, dict)]

    def history(self, *, friend: str = "", group: str = "", count: int = DEFAULT_COUNT,
                before: int | None = None) -> list[dict]:
        if group:
            params: dict = {"group_id": int(group), "count": count}
            if before:
                params["message_seq"] = before
            rows = self._call("get_group_msg_history", params)
        else:
            params = {"user_id": int(friend), "count": count}
            if before:
                params["message_seq"] = before
            rows = self._call("get_friend_msg_history", params)
        if isinstance(rows, dict):
            rows = rows.get("messages") or []
        return [row for row in rows if isinstance(row, dict)]


def _home_client(home: Path):
    import os
    previous = os.environ.get("QQ_BRIDGE_HOME")
    os.environ["QQ_BRIDGE_HOME"] = str(Path(home).resolve())
    try:
        from qq_link import QQClient
        return QQClient()
    finally:
        if previous is None:
            os.environ.pop("QQ_BRIDGE_HOME", None)
        else:
            os.environ["QQ_BRIDGE_HOME"] = previous


def collect_history(client: HistoryClient, *, friend: str = "", group: str = "", count: int = DEFAULT_COUNT,
                    max_messages: int = 100000, before: int | None = None) -> list[dict]:
    """Page backwards from the newest message; stop on an empty or non-advancing page."""
    seen: dict[str, dict] = {}
    boundary = before
    for _ in range(MAX_PAGES):
        rows = client.history(friend=friend, group=group, count=count, before=boundary)
        if not rows:
            break
        page = [onebot_message(row, client.self_id, group=bool(group)) for row in rows]
        added = 0
        for index, item in enumerate(page):
            key = item["id"] or f"{item.get('timestamp')}:{item.get('sender_id')}:{index}"
            if key not in seen:
                seen[key] = item
                added += 1
        sequences = [row.get("message_seq") or row.get("message_id") for row in rows]
        sequences = [seq for seq in sequences if isinstance(seq, int)]
        if not sequences:
            break
        candidate = min(sequences)
        if boundary is not None and candidate >= boundary:
            break
        boundary = candidate
        if not added and len(seen) >= max_messages:
            break
        if len(seen) >= max_messages:
            break
    messages = sorted(seen.values(), key=lambda item: (item.get("timestamp") or 0, str(item.get("id"))))
    return messages[-max_messages:]


def export_conversation(client: HistoryClient, *, friend: str = "", group: str = "", title: str = "",
                        output: Path, count: int = DEFAULT_COUNT, max_messages: int = 100000,
                        before: int | None = None) -> dict:
    if not friend and not group:
        raise ValueError("请指定好友或群号")
    messages = collect_history(client, friend=friend, group=group, count=count,
                               max_messages=max_messages, before=before)
    identifier = str(group or friend)
    document = transcript.make_transcript(platform="qq", kind="group" if group else "friend",
                                          identifier=identifier, title=title or identifier,
                                          self_id=client.self_id, messages=messages)
    transcript.write_json(output, document)
    return {"path": str(output), "title": document["source"]["title"], "messages": len(messages),
            "kind": document["source"]["kind"], "identifier": identifier}


def export_all(client: HistoryClient, output_dir: Path, *, count: int = DEFAULT_COUNT,
               max_messages: int = 100000, include_groups: bool = True,
               progress: Callable[[str], None] | None = None) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {"files": [], "errors": [], "created_at": time.time(), "self_id": client.self_id}
    targets: list[tuple[str, str, str]] = []
    try:
        for friend in client.friend_list():
            identifier = str(friend.get("user_id") or "")
            if identifier:
                targets.append(("friend", identifier, str(friend.get("remark") or friend.get("nickname") or identifier)))
    except Exception as exc:
        report["errors"].append(f"好友列表读取失败：{exc}")
    if include_groups:
        try:
            for group in client.group_list():
                identifier = str(group.get("group_id") or "")
                if identifier:
                    targets.append(("group", identifier, str(group.get("group_name") or identifier)))
        except Exception as exc:
            report["errors"].append(f"群列表读取失败：{exc}")
    for kind, identifier, title in targets:
        name = f"{title}（{identifier}）" if title != identifier else identifier
        target = output_dir / (safe_name(name) + ".json")
        try:
            if progress:
                progress(f"导出{('群' if kind == 'group' else '好友')} {name}")
            result = export_conversation(client, friend="" if kind == "group" else identifier,
                                         group=identifier if kind == "group" else "", title=title,
                                         output=target, count=count, max_messages=max_messages)
            report["files"].append(result)
        except Exception as exc:
            report["errors"].append(f"{name}：{exc}")
    transcript.write_json(output_dir / "index.json", {
        "schema": "openqqdot.qq-export/1", "created_at": time.time(), "account": client.self_id,
        "files": report["files"], "errors": report["errors"]})
    return report


# --------------------------------------------------------------------------- local QQ export

def parse_qq_txt(text: str) -> list[dict]:
    """Parse the text file written by QQ's own 导出消息记录 (timestamp + 昵称(QQ) blocks)."""
    messages: list[dict] = []
    current = None
    buffer: list[str] = []
    for raw in text.splitlines():
        match = TIMESTAMP_LINE.match(raw)
        if match:
            if current is not None:
                current["text"] = "\n".join(line for line in buffer).strip()
                if current["text"]:
                    messages.append(transcript.message(**current))
            stamp = f"{match.group(1).replace('/', '-')} {match.group(2)}"
            sender = SENDER_LINE.match(match.group(3)) or re.match(r"^(?P<name>.+)$", match.group(3))
            sender_id = sender.groupdict().get("id", "") if sender else ""
            name = sender.groupdict().get("name", "") if sender else ""
            current = {"timestamp": _parse_time(stamp),
                       "sender_id": sender_id or "", "sender_name": (name or "").strip(),
                       "role": "other", "kind": "text", "text": ""}
            buffer = []
            continue
        if current is not None:
            buffer.append(raw)
    if current is not None:
        current["text"] = "\n".join(buffer).strip()
        if current["text"]:
            messages.append(transcript.message(**current))
    for item in messages:
        kind = "text"
        for label, candidate in (("[图片]", "image"), ("[语音]", "voice"), ("[视频]", "video"),
                                 ("[文件]", "file"), ("[表情]", "sticker"), ("[动画表情]", "sticker")):
            if label in item["text"]:
                kind = candidate
                break
        item["type"] = kind
        item["media"] = [{"kind": kind, "url": "", "name": "", "size": "", "sha256": ""}] if kind != "text" else []
    return messages


def _parse_time(text: str) -> float | None:
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, pattern))
        except ValueError:
            continue
    return None


def parse_local_export(path: Path, *, platform: str = "qq", title: str = "") -> dict:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix in (".mht", ".mhtml", ".htm", ".html"):
        raise ValueError("暂不支持 QQ 的 MHT/HTML 导出；请在 QQ 中选择「导出消息记录 → txt」或使用直连导出。")
    if suffix in (".json", ".jsonl", ".ndjson"):
        document = transcript.load_transcript(path)
        source = document.setdefault("source", {})
        if platform and not source.get("platform"):
            source["platform"] = platform
        return document
    if suffix not in (".txt", ".log", ""):
        raise ValueError(f"不支持的聊天记录格式：{suffix or '无扩展名'}")
    messages = parse_qq_txt(path.read_text(encoding="utf-8", errors="replace"))
    if not messages:
        raise ValueError("没有从文本中解析出消息；请确认是 QQ 导出的聊天记录 txt 文件")
    return transcript.make_transcript(platform=platform, kind="conversation", identifier=path.stem,
                                      title=title or path.stem, messages=messages,
                                      source_path=str(path))


def convert_file(path: Path, output: Path, *, form: str = "json", self_id: str = "") -> Path:
    document = parse_local_export(path)
    form = form.lower()
    if form == "json":
        return transcript.write_json(output, document)
    if form == "jsonl":
        return transcript.write_jsonl(output, document)
    if form == "sharegpt":
        return transcript.write_json(output, transcript.to_sharegpt(document, self_id=self_id))
    if form == "markdown":
        output.write_text(transcript.to_markdown(document), encoding="utf-8")
        return output
    raise ValueError("输出格式应为 json / jsonl / sharegpt / markdown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 QQ 聊天记录为可迁移的 JSON（DotLink 直连或解析 QQ 本地导出）")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("onebot", help="从本机 DotLink/OneBot 直连导出，不需要网页扫码")
    one.add_argument("--friend", default="", help="好友 QQ 号")
    one.add_argument("--group", default="", help="群号")
    one.add_argument("--all", action="store_true", help="导出全部好友与群")
    one.add_argument("--output", required=True, help="输出文件或目录")
    one.add_argument("--count", type=int, default=DEFAULT_COUNT, help="每页条数")
    one.add_argument("--max-messages", type=int, default=100000, help="每个会话最多导出条数")
    one.add_argument("--no-groups", action="store_true", help="--all 时跳过群聊")
    one.add_argument("--home", default="", help="Open QQ Dot 数据目录（默认当前目录）")

    local = sub.add_parser("parse", help="解析 QQ 导出的 txt/json 聊天记录")
    local.add_argument("input")
    local.add_argument("--output", required=True)
    local.add_argument("--form", default="json", choices=("json", "jsonl", "sharegpt", "markdown"))
    local.add_argument("--self-id", default="")

    args = parser.parse_args(argv)
    if args.command == "parse":
        target = convert_file(Path(args.input), Path(args.output), form=args.form, self_id=args.self_id)
        print(json.dumps({"ok": True, "output": str(target)}, ensure_ascii=False))
        return 0
    home = Path(args.home).resolve() if args.home else None
    client, raw = HistoryClient.from_local(home)
    try:
        if args.all:
            report = export_all(client, Path(args.output), count=args.count, max_messages=args.max_messages,
                                include_groups=not args.no_groups, progress=lambda text: print(text, file=sys.stderr))
        else:
            if not args.friend and not args.group:
                parser.error("请提供 --friend、--group 或 --all")
            report = export_conversation(client, friend=args.friend, group=args.group, output=Path(args.output),
                                         count=args.count, max_messages=args.max_messages)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    finally:
        try:
            raw.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
