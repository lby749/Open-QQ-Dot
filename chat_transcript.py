"""Portable, platform-neutral chat transcript format.

One canonical JSON document is produced by every exporter (QQ/DotLink, ChatGPT,
Claude, Gemini, generic JSONL ...).  Migration, dataset building and the
desktop preview all read this shape, so a conversation exported from another
platform can be imported without a second converter.

The format is deliberately additive: unknown keys are kept but never trusted,
and every value that is rendered or sent to a model is plain text.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "openqqdot.transcript/1"
MAX_TEXT = 20000  # a single message longer than this is truncated for display/model use
ROLES = ("user", "assistant", "system", "other")
KINDS = ("text", "image", "file", "video", "voice", "sticker", "location", "forward",
         "json", "xml", "system", "unknown")
TIMESTAMP_RE = re.compile(r"^\s*(\d{4}-\d{1,2}-\d{1,2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\s+(.+?)\s*$")
SENDER_RE = re.compile(r"^(?P<name>.+?)\s*[（(](?P<id>\d{4,12})[)）]\s*$")


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "").strip()
    return value[:limit]


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            pass
        text = value.replace("Z", "+00:00")
        try:
            from datetime import datetime
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def participant(identifier: str, name: str = "", role: str = "other") -> dict:
    return {"id": _text(identifier, 64), "name": _text(name or identifier, 200), "role": role}


def message(*, identifier: str = "", timestamp: Any = None, sender_id: str = "", sender_name: str = "",
            role: str = "other", kind: str = "text", text: str = "", media: list | None = None,
            reply_to: str = "", raw: dict | None = None) -> dict:
    """Build one canonical message; ids are optional because many exports have none."""
    role = role if role in ROLES else "other"
    kind = kind if kind in KINDS else "unknown"
    stamp = _timestamp(timestamp)
    item = {
        "id": _text(identifier, 128),
        "timestamp": stamp,
        "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else "",
        "sender_id": _text(sender_id, 64),
        "sender_name": _text(sender_name, 200),
        "role": role,
        "type": kind,
        "text": _text(text),
        "media": [m for m in (media or []) if isinstance(m, dict)][:20],
        "reply_to": _text(reply_to, 128),
    }
    if raw:
        # Bounded provenance only: never carry tokens, headers or full provider envelopes.
        item["raw"] = {str(k)[:64]: _text(v, 2000) for k, v in list(raw.items())[:30]
                       if isinstance(v, (str, int, float, bool))}
    return item


def make_transcript(*, platform: str, kind: str, identifier: str, title: str = "", self_id: str = "",
                    participants: Iterable[dict] = (), messages: Iterable[dict] = (),
                    source_path: str = "", extra: dict | None = None) -> dict:
    messages = [m for m in messages if isinstance(m, dict)]
    messages.sort(key=lambda m: (m.get("timestamp") or 0, str(m.get("id") or "")))
    people = {}
    for person in [*participants, *[participant(m.get("sender_id", ""), m.get("sender_name", ""), m.get("role"))
                                    for m in messages]]:
        if person.get("id") or person.get("name"):
            key = person.get("id") or person.get("name")
            people.setdefault(key, person)
    stamps = [m["timestamp"] for m in messages if m.get("timestamp")]
    document = {
        "schema": SCHEMA,
        "source": {
            "platform": _text(platform, 64) or "unknown",
            "kind": _text(kind, 32) or "conversation",
            "id": _text(identifier, 128),
            "title": _text(title or identifier, 300),
            "self_id": _text(self_id, 64),
            "path": _text(source_path, 500),
        },
        "participants": list(people.values()),
        "messages": messages,
        "stats": {
            "message_count": len(messages),
            "first_timestamp": min(stamps) if stamps else None,
            "last_timestamp": max(stamps) if stamps else None,
        },
    }
    if extra:
        document.update({str(k): v for k, v in extra.items() if k not in document})
    document["integrity"] = {"message_digest": digest(document)}
    return document


def digest(document: dict) -> str:
    clone = {k: v for k, v in document.items() if k != "integrity"}
    payload = json.dumps(clone, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate(document: Any) -> dict:
    """Return the document if it is a usable transcript, else raise ValueError."""
    if not isinstance(document, dict):
        raise ValueError("聊天记录应为 JSON 对象")
    if document.get("schema") != SCHEMA:
        legacy = document.get("messages")
        if isinstance(legacy, list):
            # Tolerate a raw OneBot / generic message list: normalize instead of rejecting a user's export.
            return normalize_messages(document, platform="generic", kind="conversation")
        raise ValueError("不是受支持的聊天记录格式（schema 不匹配）")
    messages = document.get("messages")
    if not isinstance(messages, list):
        raise ValueError("聊天记录缺少 messages 列表")
    if len(messages) > 200000:
        raise ValueError("聊天记录消息过多（上限 20 万条）")
    clean = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        clean.append(message(identifier=item.get("id", ""), timestamp=item.get("timestamp"),
                             sender_id=item.get("sender_id", ""), sender_name=item.get("sender_name", ""),
                             role=item.get("role", "other"), kind=item.get("type", "text"),
                             text=item.get("text", ""), media=item.get("media") or [],
                             reply_to=item.get("reply_to", ""), raw=item.get("raw")))
    source = document.get("source") if isinstance(document.get("source"), dict) else {}
    result = make_transcript(platform=source.get("platform", "unknown"), kind=source.get("kind", "conversation"),
                             identifier=source.get("id", ""), title=source.get("title", ""),
                             self_id=source.get("self_id", ""), messages=clean,
                             source_path=source.get("path", ""))
    return result


def normalize_messages(values: Any, *, platform: str, kind: str, identifier: str = "", title: str = "",
                       self_id: str = "") -> dict:
    """Best-effort conversion of generic message dictionaries into the canonical schema."""
    if isinstance(values, dict):
        values = values.get("messages") or values.get("data") or []
    if not isinstance(values, list):
        raise ValueError("消息列表无效")
    result = []
    for index, item in enumerate(values[:200000]):
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content") or item.get("message") or ""
        if isinstance(text, list):
            text = " ".join(segment_text(part) for part in text)
        sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
        result.append(message(identifier=item.get("id") or item.get("message_id") or f"m{index}",
                              timestamp=item.get("timestamp") or item.get("time") or item.get("created_at"),
                              sender_id=item.get("sender_id") or sender.get("id") or item.get("user_id", ""),
                              sender_name=item.get("sender_name") or sender.get("name") or item.get("nickname", ""),
                              role=item.get("role", "other"),
                              kind=item.get("type") if item.get("type") in KINDS else "text",
                              text=text, media=item.get("media") or [],
                              reply_to=item.get("reply_to") or ""))
    return make_transcript(platform=platform, kind=kind, identifier=identifier, title=title,
                           self_id=self_id, messages=result)


def segment_text(segment: Any) -> str:
    if isinstance(segment, str):
        return segment
    if not isinstance(segment, dict):
        return ""
    kind = str(segment.get("type") or "")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    if kind == "text":
        return _text(data.get("text"))
    labels = {"image": "[图片]", "face": "[表情]", "record": "[语音]", "video": "[视频]",
              "file": f"[文件] {data.get('name') or data.get('file') or ''}".strip(),
              "json": "[卡片消息]", "xml": "[卡片消息]", "forward": "[合并转发]", "at": f"@{data.get('qq') or ''} ",
              "reply": "", "mface": "[表情包]", "poke": "[戳一戳]"}
    return labels.get(kind, f"[{kind}]" if kind else "")


# --------------------------------------------------------------------------- writers

def write_json(path: Path, document: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)
    return path


def write_jsonl(path: Path, document: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"type": "meta", "schema": SCHEMA, "source": document.get("source", {})},
                                ensure_ascii=False) + "\n")
        for item in document.get("messages", []):
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    return path


def to_sharegpt(document: dict, *, self_id: str = "") -> dict:
    """Conversation pairs for fine-tuning: consecutive same-role turns are merged."""
    owner = str(self_id or (document.get("source") or {}).get("self_id") or "")
    turns: list[dict] = []
    for item in document.get("messages", []):
        text = item.get("text") or ""
        if not text:
            continue
        role = item.get("role")
        if role == "other":
            role = "assistant" if owner and str(item.get("sender_id")) != owner else "user"
        if role not in ("user", "assistant", "system"):
            continue
        if turns and turns[-1]["from"] == role:
            turns[-1]["value"] += "\n" + text
        else:
            turns.append({"from": role, "value": text})
    return {"conversations": turns, "source": document.get("source", {})}


def to_markdown(document: dict) -> str:
    source = document.get("source") or {}
    lines = [f"# {source.get('title') or source.get('id') or '聊天记录'}", ""]
    meta = [f"- 平台：{source.get('platform', '')}", f"- 类型：{source.get('kind', '')}",
            f"- 会话：{source.get('id', '')}", f"- 消息数：{document.get('stats', {}).get('message_count', 0)}"]
    lines.extend(meta)
    lines.append("")
    for item in document.get("messages", []):
        stamp = item.get("time_text") or ""
        name = item.get("sender_name") or item.get("sender_id") or item.get("role")
        prefix = f"**{name}**" + (f" · {stamp}" if stamp else "")
        lines.append(f"{prefix}  ")
        lines.append(str(item.get("text") or f"[{item.get('type')}]").replace("\n", "  \n"))
        lines.append("")
    return "\n".join(lines)


def load_transcript(path: Path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        items = []
        meta = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("type") == "meta":
                meta = value
            elif isinstance(value, dict):
                items.append(value)
        source = meta.get("source") or {}
        return normalize_messages(items, platform=source.get("platform", "jsonl"),
                                  kind=source.get("kind", "conversation"),
                                  identifier=source.get("id", path.stem), title=source.get("title", path.stem))
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"无法读取聊天记录 JSON：{exc}") from None
    return validate(value)


def summarize(document: dict) -> dict:
    stats = document.get("stats") or {}
    source = document.get("source") or {}
    return {"title": source.get("title", ""), "platform": source.get("platform", ""),
            "kind": source.get("kind", ""), "identifier": source.get("id", ""),
            "messages": stats.get("message_count", len(document.get("messages", []))),
            "first": stats.get("first_timestamp"), "last": stats.get("last_timestamp")}
