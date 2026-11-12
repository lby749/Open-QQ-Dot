"""Import conversations and projects from other AI platforms.

Supported inputs (auto-detected from file name and contents):

* ChatGPT data export (``conversations.json``, also inside the official ``.zip``)
* Claude data export (``conversations.json`` with ``chat_messages``)
* DeepSeek / Open WebUI / generic conversation JSON (``messages`` or ``mapping``)
* SillyTavern ``.jsonl`` chat logs
* Markdown notes (imported as a project document)

Every import is a two-step, reviewable operation: :func:`inspect_source` only
reads and reports, :func:`run_import` writes a canonical transcript bundle,
creates real desktop projects/workspaces and sessions through
``session_manager``, and records a manifest so the whole import can be reverted.
Nothing is written to the source directory and no credentials or provider
tokens are copied.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import chat_transcript as transcript

SCHEMA = "openqqdot.migration/1"
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_CONVERSATIONS = 5000
MAX_MESSAGES = 200000
DEFAULT_SESSION_MESSAGES = 2000


class MigrationError(ValueError):
    pass


@dataclass
class Conversation:
    identifier: str
    title: str
    messages: list[dict] = field(default_factory=list)
    project: str = ""
    created_at: float | None = None


@dataclass
class Source:
    path: Path
    platform: str
    conversations: list[Conversation]
    source_hash: str
    note: str = ""

    @property
    def message_count(self) -> int:
        return sum(len(item.messages) for item in self.conversations)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_text(path: Path) -> str:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise MigrationError("文件过大（上限 256MB）")
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _load_json(path: Path):
    text = _read_text(path)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise MigrationError(f"JSON 解析失败：{exc}") from None


def _zip_json(path: Path):
    """Read conversations.json (or a small set of known names) from an export zip without extracting."""
    with zipfile.ZipFile(path) as archive:
        candidates = [name for name in archive.namelist()
                      if name.lower().endswith(".json") and
                      ("conversation" in name.lower() or "chat" in name.lower())]
        if not candidates:
            candidates = [name for name in archive.namelist() if name.lower().endswith(".json")]
        for name in candidates[:5]:
            info = archive.getinfo(name)
            if info.file_size > MAX_SOURCE_BYTES:
                continue
            payload = archive.read(name)
            try:
                value = json.loads(payload.decode("utf-8-sig", errors="replace"))
            except ValueError:
                continue
            return name, value
    raise MigrationError("压缩包中没有找到可识别的 conversations.json")


def _slug(value: str, fallback: str = "project") -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(value or "").strip(), flags=re.UNICODE).strip("-_")
    return (value or fallback)[:60]


def _timestamp(value, fallback: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        # Claude/ChatGPT exports are seconds; reject obvious milliseconds.
        return float(value) / 1000 if value > 10 ** 11 else float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            from datetime import datetime
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return fallback
    return fallback


def _role(value: str) -> str:
    value = (value or "").lower()
    return {"human": "user", "user": "user", "assistant": "assistant", "ai": "assistant",
            "model": "assistant", "system": "system", "tool": "system"}.get(value, "other")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image":
                    parts.append("[图片]")
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return _content_text(content.get("parts") or content.get("text") or content.get("content") or "")
    return ""


def _chatgpt(value: list) -> list[Conversation]:
    result = []
    for item in value[:MAX_CONVERSATIONS]:
        if not isinstance(item, dict):
            continue
        mapping = item.get("mapping") if isinstance(item.get("mapping"), dict) else {}
        nodes = []
        current = item.get("current_node")
        guard = 0
        while current and current in mapping and guard < 10000:
            node = mapping[current]
            message = node.get("message") if isinstance(node.get("message"), dict) else {}
            if message:
                author = message.get("author") if isinstance(message.get("author"), dict) else {}
                text = _content_text(message.get("content"))
                if text.strip():
                    nodes.append(transcript.message(
                        identifier=str(message.get("id") or node.get("id") or ""),
                        timestamp=message.get("create_time") or item.get("create_time"),
                        sender_id=str(author.get("name") or author.get("role") or ""),
                        sender_name=str(author.get("name") or author.get("role") or ""),
                        role=_role(author.get("role")), text=text))
            current = node.get("parent")
            guard += 1
        nodes.reverse()
        title = str(item.get("title") or "未命名对话")
        result.append(Conversation(str(item.get("id") or uuid.uuid4().hex[:12]), title, nodes,
                                   str(item.get("project") or ""), _timestamp(item.get("create_time"))))
    return result


def _claude(value: list) -> list[Conversation]:
    result = []
    for item in value[:MAX_CONVERSATIONS]:
        if not isinstance(item, dict):
            continue
        nodes = []
        for entry in (item.get("chat_messages") or [])[:MAX_MESSAGES]:
            if not isinstance(entry, dict):
                continue
            text = _content_text(entry.get("text") or entry.get("content"))
            if not text.strip():
                continue
            nodes.append(transcript.message(identifier=str(entry.get("uuid") or ""),
                                            timestamp=entry.get("created_at") or item.get("created_at"),
                                            sender_name=str(entry.get("sender") or ""),
                                            role=_role(entry.get("sender")), text=text))
        result.append(Conversation(str(item.get("uuid") or uuid.uuid4().hex[:12]),
                                   str(item.get("name") or item.get("title") or "未命名对话"), nodes,
                                   str(item.get("project") or item.get("project_uuid") or ""),
                                   _timestamp(item.get("created_at"))))
    return result


def _generic(value) -> list[Conversation]:
    if isinstance(value, dict):
        for key in ("conversations", "chats", "data", "items"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value]
    if not isinstance(value, list):
        raise MigrationError("无法识别的对话列表")
    result = []
    for index, item in enumerate(value[:MAX_CONVERSATIONS]):
        if not isinstance(item, dict):
            continue
        raw_messages = item.get("messages") or item.get("chat_messages") or []
        if isinstance(item.get("chat"), dict):
            raw_messages = item["chat"].get("messages") or raw_messages
            item = {**item["chat"], **item}
        nodes = []
        for position, entry in enumerate(raw_messages[:MAX_MESSAGES]):
            if not isinstance(entry, dict):
                continue
            text = _content_text(entry.get("content") or entry.get("text") or entry.get("message"))
            if not text.strip():
                continue
            sender = entry.get("sender") if isinstance(entry.get("sender"), dict) else {}
            nodes.append(transcript.message(identifier=str(entry.get("id") or position),
                                            timestamp=entry.get("timestamp") or entry.get("create_time")
                                            or entry.get("created_at") or item.get("create_time"),
                                            sender_id=str(sender.get("id") or entry.get("role") or ""),
                                            sender_name=str(sender.get("name") or entry.get("role") or ""),
                                            role=_role(entry.get("role") or sender.get("role")), text=text))
        if not nodes:
            continue
        result.append(Conversation(str(item.get("id") or item.get("uuid") or index),
                                   str(item.get("title") or item.get("name") or f"对话 {index + 1}"),
                                   nodes, str(item.get("project") or item.get("project_name") or ""),
                                   _timestamp(item.get("create_time") or item.get("created_at"))))
    return result


def _sillytavern(path: Path) -> list[Conversation]:
    nodes = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or "mes" not in entry:
            continue
        nodes.append(transcript.message(timestamp=entry.get("send_date"),
                                        sender_name=str(entry.get("name") or ""),
                                        role="user" if entry.get("is_user") else "assistant",
                                        text=str(entry.get("mes") or "")))
    if not nodes:
        raise MigrationError("没有从 SillyTavern 记录中解析出消息")
    return [Conversation(path.stem, path.stem, nodes)]


def inspect_source(path: Path) -> Source:
    """Read and classify an export. Never writes anything."""
    path = Path(path).resolve()
    if not path.exists():
        raise MigrationError(f"文件或目录不存在：{path}")
    if path.is_dir():
        for name in ("conversations.json", "chats.json", "chat.json"):
            candidate = path / name
            if candidate.is_file():
                path = candidate
                break
        else:
            notes = sorted(path.glob("*.md"))
            if notes:
                return _markdown_source(notes[0])
            raise MigrationError("目录中没有找到 conversations.json / chats.json / *.md")
    if path.suffix.lower() == ".zip":
        name, value = _zip_json(path)
        source_hash = _hash_file(path)
        platform = "chatgpt" if isinstance(value, list) and any(
            isinstance(item, dict) and "mapping" in item for item in value[:10]) else "generic"
        conversations = _chatgpt(value) if platform == "chatgpt" else _generic(value)
        return Source(path, platform, conversations, source_hash, f"压缩包成员：{name}")
    source_hash = _hash_file(path)
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        try:
            first = next((json.loads(line) for line in _read_text(path).splitlines() if line.strip()), None)
        except ValueError:
            first = None
        if isinstance(first, dict) and "mes" in first:
            return Source(path, "sillytavern", _sillytavern(path), source_hash)
        return Source(path, "jsonl", _generic(list(_iter_jsonl(path))), source_hash)
    if path.suffix.lower() in (".md", ".markdown", ".txt"):
        return _markdown_source(path)
    value = _load_json(path)
    platform = _detect_platform(value, path)
    conversations = {"chatgpt": _chatgpt, "claude": _claude}.get(platform, _generic)(value)
    conversations = [item for item in conversations if item.messages]
    if not conversations:
        raise MigrationError("没有解析出任何带消息的对话")
    return Source(path, platform, conversations, source_hash)


def _iter_jsonl(path: Path):
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _detect_platform(value, path: Path) -> str:
    name = path.name.lower()
    items = value if isinstance(value, list) else [value]
    sample = [item for item in items if isinstance(item, dict)][:20]
    if sample and all("mapping" in item for item in sample if item):
        return "chatgpt"
    if sample and any("chat_messages" in item for item in sample):
        return "claude"
    if "claude" in name:
        return "claude"
    if "chatgpt" in name or "openai" in name:
        return "chatgpt"
    return "generic"


def _markdown_source(path: Path) -> Source:
    text = _read_text(path)
    title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.strip().startswith("#")), path.stem)
    document = transcript.make_transcript(platform="markdown", kind="note", identifier=path.stem, title=title,
                                          source_path=str(path), messages=[
                                              transcript.message(timestamp=path.stat().st_mtime,
                                                                 sender_name="document", role="system",
                                                                 text=text[:200000])])
    return Source(path, "markdown", [Conversation(path.stem, title, document["messages"])],
                  _hash_file(path), "Markdown 文档作为项目资料导入")


# --------------------------------------------------------------------------- planning / import

def project_name(source: Source, override: str = "") -> str:
    if override.strip():
        return override.strip()
    projects = [item.project for item in source.conversations if item.project.strip()]
    if projects:
        return projects[0][:80]
    return f"{source.platform} 对话 {time.strftime('%Y-%m-%d')}"


def inspect_report(path: Path) -> dict:
    source = inspect_source(path)
    projects = sorted({(item.project or project_name(source)).strip() for item in source.conversations if item.messages})
    return {"platform": source.platform, "path": str(source.path), "source_hash": source.source_hash,
            "conversations": len(source.conversations), "messages": source.message_count,
            "project": project_name(source), "projects": projects[:30], "note": source.note,
            "titles": [item.title for item in source.conversations[:30]],
            "earliest": min((item.created_at for item in source.conversations if item.created_at), default=None),
            "latest": max((item.created_at for item in source.conversations if item.created_at), default=None)}


def _migration_root(home: Path) -> Path:
    return Path(home) / "data" / "migrations"


def _index(home: Path) -> list[dict]:
    path = _migration_root(home) / "index.json"
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _save_index(home: Path, items: list[dict]) -> None:
    path = _migration_root(home) / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("index.json.tmp")
    temporary.write_text(json.dumps(items[-1000:], ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _session_manager(home: Path):
    import session_manager
    previous = session_manager.ROOT
    session_manager.ROOT = Path(home)
    try:
        yield session_manager
    finally:
        session_manager.ROOT = previous


def run_import(home: Path, owner: str, path: Path, *, project_title: str = "", max_session_messages: int = DEFAULT_SESSION_MESSAGES,
               force: bool = False, progress=None) -> dict:
    """Import one export for ``owner``; returns the manifest (also written to disk)."""
    home, owner = Path(home), str(owner)
    if not re.fullmatch(r"\d{5,12}", owner):
        raise MigrationError("请先选择有效好友，再迁移对话")
    source = inspect_source(path)
    key = _hash_bytes(f"{source.platform}|{source.source_hash}|{owner}|{project_title}".encode())
    existing = next((item for item in _index(home) if item.get("key") == key and item.get("status") == "imported"), None)
    if existing and not force:
        return {**existing, "duplicate": True}
    default_title = project_name(source, project_title)
    import_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    folder = _migration_root(home) / import_id
    folder.mkdir(parents=True, exist_ok=False)
    conversations_dir, projects_dir = folder / "conversations", folder / "projects"
    conversations_dir.mkdir()
    projects_dir.mkdir()
    # A ChatGPT/Claude export can contain several projects. Keep them separate
    # instead of merging every conversation into one workspace.
    groups: dict[str, list[Conversation]] = {}
    for conversation in source.conversations:
        if conversation.messages:
            groups.setdefault((conversation.project or default_title).strip()[:80] or default_title, []).append(conversation)
    manifest = {"schema": SCHEMA, "import_id": import_id, "key": key, "owner": owner,
                "platform": source.platform, "source_path": str(source.path), "source_hash": source.source_hash,
                "created_at": time.time(), "project_title": default_title, "note": source.note,
                "status": "imported", "conversations": [], "projects": [], "warnings": []}
    records = []
    used_slugs = set()
    for index, conversation in enumerate(source.conversations):
        if not conversation.messages:
            continue
        document = transcript.make_transcript(platform=source.platform, kind="conversation",
                                              identifier=conversation.identifier, title=conversation.title,
                                              messages=conversation.messages, source_path=str(source.path),
                                              extra={"project": conversation.project} if conversation.project else None)
        name = f"{index + 1:04d}-{_slug(conversation.title, 'chat')}.json"
        target = conversations_dir / name
        transcript.write_json(target, document)
        record = {"id": conversation.identifier, "title": conversation.title, "file": str(target.relative_to(folder)),
                  "messages": len(conversation.messages), "sha256": _hash_file(target),
                  "session_id": "", "project": conversation.project,
                  "project_title": (conversation.project or default_title).strip()[:80] or default_title}
        manifest["conversations"].append(record)
        records.append((record, document))
        if progress:
            progress(f"写入对话 {index + 1}/{len(source.conversations)}：{conversation.title}")
    with _session_manager(home) as sessions:
        for title, conversations in groups.items():
            base_slug = _slug(title, "project")
            slug, counter = base_slug, 2
            while slug in used_slugs:
                slug, counter = f"{base_slug}-{counter}", counter + 1
            used_slugs.add(slug)
            project_dir = projects_dir / slug
            project_dir.mkdir(parents=True)
            workspace = sessions.add_workspace(owner, str(project_dir), title)
            manifest["projects"].append({"title": title, "path": str(project_dir), "workspace_id": workspace["id"]})
            group_records = [item for item in records if item[0]["project_title"] == title]
            _write_project_doc(project_dir, source, title, conversations)
            for record, document in group_records:
                session = sessions.create_session(owner, workspace["id"], record["title"])
                record["session_id"] = session["id"]
                sessions.append_messages(owner, session["id"], _session_messages(document, max_session_messages),
                                         ws_id=workspace["id"])
    if len(manifest["projects"]) > 1:
        manifest["project_title"] = "、".join(item["title"] for item in manifest["projects"])[:300]
    manifest["stats"] = {"conversations": len(manifest["conversations"]),
                         "messages": sum(item["messages"] for item in manifest["conversations"]),
                         "projects": len(manifest["projects"])}
    manifest["integrity"] = {"manifest_digest": _hash_bytes(json.dumps(
        {key: value for key, value in manifest.items() if key != "integrity"},
        ensure_ascii=False, sort_keys=True).encode())}
    _write_json(folder / "manifest.json", manifest)
    items = [item for item in _index(home) if item.get("import_id") != import_id]
    items.append(manifest)
    _save_index(home, items)
    try:
        import revert_center
        revert_center.record(home, "migration", owner, import_id, title=manifest["project_title"],
                             summary=f"从 {source.platform} 迁移 {manifest['stats']['conversations']} 个对话",
                             payload={"platform": source.platform, "projects": [item["title"] for item in manifest["projects"]]})
    except Exception:
        pass
    return manifest



def run_cli_import(home: Path, owner: str, path: Path, *, project_title: str = "", force: bool = False, progress=None) -> dict:
    """Migrate a CLI project directory without copying any chat/context.

    The directory is copied into a migration bundle, registered as a workspace,
    and an empty named session is created so the project appears in the console.
    Continuing the work is delegated to the local CLI from that directory.
    """
    home, owner = Path(home), str(owner)
    if not re.fullmatch(r"\d{5,12}", owner):
        raise MigrationError("请先选择有效好友，再迁移项目")
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        raise MigrationError("CLI 迁移需要一个已存在的项目目录")
    root = _migration_root(home).resolve()
    if source == root or root in source.parents:
        raise MigrationError("不能把迁移目录自身再迁移一次")
    title = (project_title or source.name or "CLI 项目").strip()[:80]
    key = _hash_bytes(f"cli|{source}|{owner}|{title}".encode())
    existing = next((item for item in _index(home) if item.get("key") == key and item.get("status") == "imported"), None)
    if existing and not force:
        return {**existing, "duplicate": True}
    import_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    folder = root / import_id
    folder.mkdir(parents=True, exist_ok=False)
    slug = _slug(source.name or "cli-project", "cli-project")
    project_dir = folder / "projects" / slug
    project_dir.parent.mkdir(parents=True)
    if progress:
        progress(f"复制 CLI 项目目录：{source.name}…")
    shutil.copytree(source, project_dir, symlinks=False)
    manifest = {"schema": SCHEMA, "mode": "cli", "import_id": import_id,
                "key": key,
                "owner": owner, "platform": "cli", "source_path": str(source),
                "source_hash": "", "created_at": time.time(), "project_title": title,
                "note": "只迁移项目目录，不导入对话、上下文或登录信息",
                "status": "imported", "conversations": [], "projects": [], "warnings": []}
    (project_dir / "PROJECT.md").write_text(
        f"# {title}\n\n- 来源：本地 CLI 项目 `{source}`\n- 导入时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
        "- 本目录由 Open QQ Dot CLI 迁移生成，不含原平台聊天记录或上下文。\n"
        "- 继续工作时，从该目录调用对应的本地 CLI；控制台只显示项目和会话入口。\n",
        encoding="utf-8")
    with _session_manager(home) as sessions:
        workspace = sessions.add_workspace(owner, str(project_dir), title)
        session = sessions.create_session(owner, workspace["id"], title)
        manifest["projects"].append({"title": title, "path": str(project_dir),
                                     "workspace_id": workspace["id"], "session_id": session["id"]})
    manifest["stats"] = {"conversations": 0, "messages": 0, "projects": 1}
    manifest["integrity"] = {"manifest_digest": _hash_bytes(json.dumps(
        {key: value for key, value in manifest.items() if key != "integrity"},
        ensure_ascii=False, sort_keys=True).encode())}
    _write_json(folder / "manifest.json", manifest)
    items = [item for item in _index(home) if item.get("import_id") != import_id]
    items.append(manifest)
    _save_index(home, items)
    try:
        import revert_center
        revert_center.record(home, "migration", owner, import_id, title=title,
                             summary=f"从 CLI 迁移项目目录：{title}",
                             payload={"platform": "cli", "projects": [title]})
    except Exception:
        pass
    return manifest


def _known_desktop_roots() -> list[tuple[str, Path]]:
    homes = {str(p).casefold(): p for p in (
        Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        Path.home() / ".codex",
        Path.home() / ".claude" / "projects",
        Path.home() / ".dsh" / "sessions",
        Path(os.environ.get("APPDATA", "")) / "Codex",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Codex",
    ) if str(p) and p.is_dir()}
    labels = {'sessions': 'Codex', '.codex': 'Codex', 'projects': 'Claude',
              '.claude': 'Claude', '.dsh': 'DeepSeek Harness'}
    return [(labels.get(path.name, path.name), path) for path in sorted(homes.values(), key=str)]


def discover_desktop_projects(max_files: int = 2000) -> list[dict]:
    """Find project directories referenced by Codex/Claude/DSH session logs.

    Only their known data roots are read, and only the first line of a session
    file.  Chat text is deliberately not parsed: the project directory is the
    migration unit.
    """
    projects: dict[str, dict] = {}
    seen = 0
    for app, root in _known_desktop_roots():
        for pattern in ("**/*.jsonl", "*.jsonl"):
            for path in root.glob(pattern):
                seen += 1
                if seen > max_files:
                    break
                try:
                    with path.open(encoding="utf-8", errors="replace") as stream:
                        first = stream.readline()
                    data = json.loads(first)
                    payload = data.get("payload") if isinstance(data, dict) else {}
                    cwd = (payload.get("cwd") if isinstance(payload, dict) else "") or data.get("cwd") \
                        if isinstance(data, dict) else ""
                    if not cwd:
                        continue
                    project = Path(str(cwd)).expanduser()
                    if not project.is_dir():
                        continue
                    folded = str(project.resolve()).casefold()
                    if '\\appdata\\local\\temp\\' in folded or '/tmp/' in folded:
                        continue
                    key = str(project.resolve()).casefold()
                    item = projects.setdefault(key, {"app": app, "title": project.name or str(project),
                                                      "path": str(project.resolve()), "sessions": 0})
                    item["sessions"] += 1
                except (OSError, ValueError, AttributeError, TypeError):
                    continue
            if seen > max_files:
                break
        if seen > max_files:
            break
    return sorted(projects.values(), key=lambda item: (item["app"], item["title"].casefold()))

def _session_messages(document: dict, limit: int) -> Iterable[dict]:
    messages = [item for item in document.get("messages", []) if item.get("text")]
    if limit and len(messages) > limit:
        messages = messages[-limit:]
    for item in messages:
        role = item.get("role")
        if role not in ("user", "assistant", "system"):
            role = "user"
        yield {"role": role, "content": item["text"], "timestamp": item.get("time_text") or None}


def _write_project_doc(project_dir: Path, source: Source, title: str, conversations: list[Conversation]) -> None:
    total = sum(len(item.messages) for item in conversations)
    lines = [f"# {title}", "",
             f"- 来源：{source.platform}（`{source.path.name}`）",
             f"- 导入时间：{time.strftime('%Y-%m-%d %H:%M')}",
             f"- 对话：{len(conversations)} 个，消息：{total} 条", "",
             "## 对话索引", ""]
    for item in conversations[:500]:
        lines.append(f"- {item.title}（{len(item.messages)} 条）")
    lines += ["", "## 说明", "",
              "本目录由 Open QQ Dot 迁移工具生成。原始聊天记录保存在导入包的 `conversations/` 中，"
              "桌面项目与对话只是可继续使用的副本；不会上传到任何外部服务。",
              "导入不会复制账号、令牌或源平台的登录信息。删除项目前请先在「回溯中心」确认可撤销。", ""]
    (project_dir / "PROJECT.md").write_text("\n".join(lines), encoding="utf-8")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


def list_imports(home: Path, owner: str = "") -> list[dict]:
    return [item for item in reversed(_index(home)) if not owner or item.get("owner") == str(owner)]


def undo_import(home: Path, owner: str, import_id: str) -> dict:
    """Remove the imported workspace/sessions and move the bundle to trash (never hard-delete)."""
    home = Path(home)
    items = _index(home)
    record = next((item for item in items if item.get("import_id") == import_id), None)
    if not record:
        raise MigrationError("找不到这次迁移记录")
    if record.get("owner") != str(owner):
        raise PermissionError("不能撤销其他好友的迁移")
    if record.get("status") == "reverted":
        raise MigrationError("这次迁移已经撤销")
    folder = _migration_root(home) / import_id
    report = {"removed_sessions": [], "removed_workspaces": [], "moved_to": ""}
    with _session_manager(home) as sessions:
        for project in record.get("projects", []):
            workspace_id = project.get("workspace_id")
            if workspace_id:
                for conversation in record.get("conversations", []):
                    if conversation.get("session_id"):
                        try:
                            if sessions.delete_session(owner, conversation["session_id"], workspace_id):
                                report["removed_sessions"].append(conversation["session_id"])
                        except Exception:
                            pass
                if sessions.remove_workspace(owner, workspace_id):
                    report["removed_workspaces"].append(workspace_id)
    if folder.is_dir():
        trash = _migration_root(home) / "_trash"
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f"{import_id}-{int(time.time())}"
        shutil.move(str(folder), str(target))
        report["moved_to"] = str(target)
    record["status"] = "reverted"
    record["reverted_at"] = time.time()
    _save_index(home, items)
    try:
        import revert_center
        revert_center.mark(home, f"{owner}:migration:{import_id}", "reverted")
    except Exception:
        pass
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    parser = argparse.ArgumentParser(description="把其他平台的对话与项目迁移进 Open QQ Dot")
    parser.add_argument("source")
    parser.add_argument("--friend", required=True, help="接收迁移的好友 QQ 号")
    parser.add_argument("--home", default=os.environ.get("QQ_BRIDGE_HOME", str(Path(__file__).resolve().parent)))
    parser.add_argument("--project", default="", help="自定义项目名称")
    parser.add_argument("--inspect", action="store_true", help="只检查，不写入")
    parser.add_argument("--force", action="store_true", help="重复来源也再次导入")
    args = parser.parse_args(argv)
    if args.inspect:
        print(json.dumps(inspect_report(Path(args.source)), ensure_ascii=False, indent=1))
        return 0
    manifest = run_import(Path(args.home), args.friend, Path(args.source), project_title=args.project,
                          force=args.force)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
