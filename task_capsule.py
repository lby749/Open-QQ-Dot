"""Task capsules: a portable, replayable AI workflow package.

A capsule (``.oqtc``; ordinary ``.zip`` is also accepted) contains everything needed to reuse a learned
workflow as a tool:

* ``workflow.json`` - the versioned definition accepted by
  :func:`task_workflows.clean_definition` (stages, agents, budgets, acceptance).
* ``manifest.json`` - identity, model requirements, feature/permission needs,
  per-file SHA-256 and a payload digest.
* ``skills/`` - the textual skill instructions the workflow depends on.
* ``samples/samples.json`` - deterministic test samples with expected keys or
  text, usable without a model for structural checks and with a model for
  acceptance.
* ``demo/demo.json`` - the recorded automation demonstration (browser steps or
  a verified run trace).
* ``README.md`` - human-readable import notes.

Importing a capsule validates every member, then registers it as a tool that
creates a normal durable goal through the existing ``WorkflowStore``.  It never
executes code from the package and never widens the friend's permissions.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
import json
import re
import time
import uuid
import zipfile
from version import VERSION
from functools import wraps
from threading import RLock
from pathlib import Path, PurePosixPath

from task_spec import _safe_relative

SCHEMA = "openqqdot.capsule/1"
SUFFIX = ".oqtc"
MAX_MEMBERS = 200
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SKILLS = 30
MAX_SAMPLES = 20
ALLOWED_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml", ".png", ".jpg", ".jpeg", ".svg"}
TEXT_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml", ".svg"}
_index_lock = RLock()


def _index_mutation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _index_lock:
            return function(*args, **kwargs)
    return locked


def _visible(row, owner):
    return owner is None or not row.get('owner') or str(owner) in row.get('owners', [row['owner']])
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
)
FEATURE_BY_CATEGORY = {"code": "file_write", "file": "file_write", "browser": "browser",
                       "research": "network", "video": "video", "data": "commands"}


class CapsuleError(ValueError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash(value) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _payload_digest(files: dict[str, str]) -> str:
    return _hash(sorted(files.items()))


def _safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise CapsuleError("胶囊包含无效文件名")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.parts[0] in ("", "."):
        raise CapsuleError("胶囊包含越界路径：" + name)
    if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise CapsuleError("胶囊包含不允许的文件类型：" + name)
    return path.as_posix()


def _scan_text(name: str, text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise CapsuleError(f"文件 {name} 中疑似包含密钥或令牌，已拒绝打包/导入")


def _derived_requirements(definition: dict) -> dict:
    steps = definition.get("steps") or []
    providers = sorted({str(step.get("backend")) for step in steps if step.get("backend")})
    features = sorted({FEATURE_BY_CATEGORY.get(step.get("category"), "general") for step in steps})
    if any((step.get("spec") or {}).get("test_command") for step in steps):
        features = sorted(set(features) | {"commands"})
    if any((step.get("spec") or {}).get("browser_steps") for step in steps) or "browser" in features:
        features = sorted(set(features) | {"browser"})
    return {
        "models": {"providers": providers or ["current"],
                   "min_token_budget": definition.get("token_budget", 100000),
                   "max_calls": definition.get("max_calls", 20)},
        "features": features,
        "permissions": {"writes_workspace": "file_write" in features,
                        "runs_commands": "commands" in features,
                        "uses_browser": "browser" in features},
    }


def _auto_samples(definition: dict) -> list[dict]:
    samples = []
    for step in definition.get("steps") or []:
        keys = (step.get("spec") or {}).get("expected_keys") or []
        sample = {"name": step.get("title", "stage"), "input": {"stage": step.get("title", "")},
                  "expect": {"keys": list(keys)} if keys else {"completed": True}}
        samples.append(sample)
    return samples[:MAX_SAMPLES] or [{"name": "smoke", "input": {}, "expect": {"completed": True}}]


def _clean_samples(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SAMPLES:
        raise CapsuleError(f"测试样例应为不超过 {MAX_SAMPLES} 条的列表")
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise CapsuleError("测试样例格式无效")
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 200:
            raise CapsuleError("测试样例缺少名称")
        sample_input = item.get("input") or {}
        expect = item.get("expect") or {}
        if not isinstance(sample_input, dict) or not isinstance(expect, dict):
            raise CapsuleError("测试样例的 input/expect 应为对象")
        if set(sample_input) - {"stage", "text", "query", "url", "file", "variables"}:
            raise CapsuleError("测试样例包含不支持的输入键")
        if set(expect) - {"keys", "contains", "equals", "completed"}:
            raise CapsuleError("测试样例包含不支持的验收键")
        keys = expect.get("keys") or []
        if not isinstance(keys, list) or any(not isinstance(key, str) or len(key) > 120 for key in keys):
            raise CapsuleError("测试样例的 keys 应为文字列表")
        contains = expect.get("contains")
        if contains is not None and (not isinstance(contains, str) or len(contains) > 2000):
            raise CapsuleError("测试样例的 contains 应为文字")
        equals = expect.get("equals")
        if equals is not None and not isinstance(equals, (str, int, float, bool, list, dict)):
            raise CapsuleError("测试样例的 equals 类型无效")
        clean.append({"name": name, "input": sample_input,
                      "expect": {"keys": keys, **({"contains": contains} if contains is not None else {}),
                                 **({"equals": equals} if equals is not None else {}),
                                 **({"completed": bool(expect.get("completed"))} if "completed" in expect else {})}})
    return clean


def _clean_demo(value, definition: dict) -> dict:
    if value is None:
        steps = next(((step.get("spec") or {}).get("browser_steps") for step in definition.get("steps") or []
                      if (step.get("spec") or {}).get("browser_steps")), None)
        if steps:
            return {"kind": "browser_steps", "steps": steps,
                    "note": "由浏览器示教录制，回放复用现有浏览器执行器，不调用模型。"}
        return {"kind": "stage_plan", "steps": [{"action": "stage", "title": step.get("title", "")}
                                                for step in definition.get("steps") or []],
                "note": "没有录制操作；按阶段顺序执行。"}
    if not isinstance(value, dict):
        raise CapsuleError("自动化示范应为对象")
    demo = {"kind": str(value.get("kind") or "workflow")[:40], "steps": value.get("steps") or [], "note": str(value.get("note") or "")[:2000]}
    if not isinstance(demo["steps"], list) or len(demo["steps"]) > 100:
        raise CapsuleError("自动化示范步骤过多")
    return demo


def build_manifest(*, capsule_id: str, version: int, title: str, description: str, definition: dict,
                   files: dict[str, bytes], skills: list[dict], samples: list[dict], demo: dict,
                   source: dict | None = None) -> dict:
    hashes = {name: _sha256(data) for name, data in files.items()}
    manifest = {
        "schema": SCHEMA,
        "capsule_id": capsule_id,
        "version": int(version),
        "title": title[:200],
        "description": description[:4000],
        "created_at": time.time(),
        "generator": "OpenQQDot task capsule/1",
        "source": source or {},
        "requirements": _derived_requirements(definition),
        "skills": skills,
        "samples": len(samples),
        "demo_kind": demo.get("kind", ""),
        "files": hashes,
        "payload_digest": _payload_digest(hashes),
    }
    return manifest


def _zip_write(package: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    package.writestr(info, data)


def _read_skill(path: Path, index: int) -> tuple[str, str, bytes]:
    path = Path(path)
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.is_file():
        raise CapsuleError(f"技能文件不存在：{path}")
    data = path.read_bytes()
    if len(data) > 512 * 1024:
        raise CapsuleError(f"技能文件过大：{path.name}")
    text = data.decode("utf-8", errors="replace")
    _scan_text(path.name, text)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", path.parent.name if path.name == "SKILL.md" else path.stem).strip("-") or f"skill-{index}"
    name = f"skills/{slug[:40]}/SKILL.md"
    return name, slug[:80], data


def export_capsule(home: Path, store, owner: str, workflow_id: str, output: Path, *, title: str = "",
                   description: str = "", skills=(), samples=None, demo=None,
                   version: int = 1, capsule_id: str = "") -> dict:
    """Package one learned workflow as ``output`` (a .oqtc file); returns the manifest."""
    from task_workflows import clean_definition
    record = store.get(workflow_id, owner)
    definition = clean_definition(record["definition"])
    title = (title or record["title"]).strip()[:200]
    if not title:
        raise CapsuleError("胶囊需要一个名称")
    version = int(version)
    if not 1 <= version <= 10000:
        raise CapsuleError("胶囊版本应为 1–10000")
    capsule_id = capsule_id or ("cap_" + uuid.uuid4().hex[:12])
    if not re.fullmatch(r"cap_[A-Za-z0-9_-]{4,64}", capsule_id):
        raise CapsuleError("胶囊标识无效")
    samples = _clean_samples(samples) if samples is not None else _auto_samples(definition)
    demo = _clean_demo(demo, definition)
    files: dict[str, bytes] = {}
    skill_rows = []
    for index, skill_path in enumerate(list(skills)[:MAX_SKILLS]):
        name, slug, data = _read_skill(Path(skill_path), index)
        files[name] = data
        skill_rows.append({"name": slug, "path": name, "sha256": _sha256(data)})
    workflow = {"title": title, "definition": definition, "owner_scope": "imported capsule"}
    files["workflow.json"] = json.dumps(workflow, ensure_ascii=False, indent=1).encode()
    files["samples/samples.json"] = json.dumps({"samples": samples}, ensure_ascii=False, indent=1).encode()
    files["demo/demo.json"] = json.dumps(demo, ensure_ascii=False, indent=1).encode()
    files["README.md"] = build_readme(title, description, definition, skill_rows, samples, demo).encode()
    manifest = build_manifest(capsule_id=capsule_id, version=version, title=title, description=description,
                              definition=definition, files=files, skills=skill_rows, samples=samples, demo=demo,
                              source={"workflow_id": workflow_id, "workflow_version": record["version"],
                                      "app_version": VERSION})
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
        for name in sorted(files):
            _zip_write(package, name, files[name])
    temporary.replace(output)
    return {**manifest, "path": str(output), "workflow": workflow, "demo": demo, "sample_list": samples}


def build_readme(title: str, description: str, definition: dict, skills: list[dict], samples: list[dict], demo: dict) -> str:
    lines = [f"# 任务胶囊：{title}", "", description or "由可学习工作流导出的可复用任务胶囊。", "",
             "## 阶段", ""]
    for index, step in enumerate(definition.get("steps") or []):
        agent = f"（{step['backend']}）" if step.get("backend") else ""
        lines.append(f"{index + 1}. **{step.get('title', '')}** · {step.get('category', '')}{agent}")
        lines.append(f"   {step.get('description', '')}")
    lines += ["", "## 模型与权限要求", "",
              f"- 每轮最多调用：{definition.get('max_calls', 20)}",
              f"- 每轮词元预算：{definition.get('token_budget', 100000)}",
              "- 需要当前好友已启用胶囊中列出的智能体；不会因为导入而扩大权限。", ""]
    if skills:
        lines += ["## 技能", ""] + [f"- `{item['path']}`" for item in skills] + [""]
    lines += ["## 测试样例", ""]
    for sample in samples:
        lines.append(f"- {sample['name']}：验收 {json.dumps(sample['expect'], ensure_ascii=False)}")
    lines += ["", "## 自动化示范", "", f"- 类型：{demo.get('kind', '')}", f"- 说明：{demo.get('note', '')}", "",
              "导入后可在「任务中心 → 任务胶囊」作为工具调用；原始工作流仍要经过现有审批与验收。", ""]
    return "\n".join(lines)


def _capsule_prefix(names: set[str]) -> str:
    """Return the common root folder for a zipped capsule, or '' for root layout."""
    if "manifest.json" in names:
        return ""
    candidates = {name[:-len("manifest.json")] for name in names if name.endswith("/manifest.json")}
    valid = []
    for prefix in candidates:
        required = {prefix + name for name in ("workflow.json", "samples/samples.json", "demo/demo.json")}
        if required <= names:
            valid.append(prefix)
    if len(valid) != 1:
        raise CapsuleError("胶囊缺少 manifest.json（或压缩包内含多个胶囊）")
    return valid[0]


def inspect_capsule(path: Path) -> dict:
    """Validate a capsule without installing it. Raises CapsuleError on any doubt."""
    path = Path(path)
    if not path.is_file():
        raise CapsuleError("胶囊文件不存在")
    if path.stat().st_size > MAX_TOTAL_BYTES:
        raise CapsuleError("胶囊文件过大")
    try:
        package = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise CapsuleError("不是有效的胶囊文件（zip 损坏）") from None
    with package:
        infos = package.infolist()
        if len(infos) > MAX_MEMBERS:
            raise CapsuleError("胶囊文件数量过多")
        if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise CapsuleError("胶囊解压后过大")
        for info in infos:
            if info.file_size > MAX_MEMBER_BYTES:
                raise CapsuleError("胶囊内单个文件过大")
            if info.is_dir():
                continue
            _safe_member(info.filename)
        names = {info.filename for info in infos if not info.is_dir()}
        prefix = _capsule_prefix(names)
        required_names = {prefix + name for name in
                          ("manifest.json", "workflow.json", "samples/samples.json", "demo/demo.json")}
        for required in required_names:
            if required not in names:
                raise CapsuleError(f"胶囊缺少 {required[len(prefix):]}")
        relative = {name[len(prefix):]: name for name in names if name.startswith(prefix)}
        manifest = json.loads(package.read(prefix + "manifest.json").decode("utf-8"))
        if manifest.get("schema") != SCHEMA:
            raise CapsuleError("不是受支持的任务胶囊版本")
        if not re.fullmatch(r"cap_[A-Za-z0-9_-]{4,64}", str(manifest.get("capsule_id") or "")):
            raise CapsuleError("胶囊标识无效")
        if not isinstance(manifest.get("version"), int) or not 1 <= manifest["version"] <= 10000:
            raise CapsuleError("胶囊版本无效")
        files = {name: package.read(member) for name, member in relative.items()}
        hashes = {name: _sha256(data) for name, data in files.items() if name != "manifest.json"}
        declared = manifest.get("files") or {}
        for name, digest in declared.items():
            if name not in hashes or hashes[name] != digest:
                raise CapsuleError(f"胶囊文件校验失败：{name}")
        if _payload_digest(hashes) != manifest.get("payload_digest"):
            raise CapsuleError("胶囊内容摘要不匹配（可能被修改）")
        for name in ("skills/",):
            for member in relative:
                if member.startswith(name):
                    _scan_text(member, files[member].decode("utf-8", errors="replace"))
        workflow = json.loads(files["workflow.json"].decode("utf-8"))
        if not isinstance(workflow, dict) or not isinstance(workflow.get("definition"), dict):
            raise CapsuleError("workflow.json 内容无效")
        from task_workflows import clean_definition
        definition = clean_definition(workflow["definition"])
        samples = _clean_samples(json.loads(files["samples/samples.json"].decode("utf-8")).get("samples"))
        demo = _clean_demo(json.loads(files["demo/demo.json"].decode("utf-8")), definition)
        requirements = manifest.get("requirements") or _derived_requirements(definition)
        if not isinstance(requirements, dict):
            raise CapsuleError("胶囊需求说明无效")
        return {"path": str(path), "manifest": manifest, "workflow": workflow, "definition": definition,
                "samples": samples, "demo": demo, "requirements": requirements,
                "digest": manifest["payload_digest"], "capsule_id": manifest["capsule_id"],
                "version": manifest["version"], "title": str(manifest.get("title") or workflow.get("title") or "")}


def _index_path(home: Path) -> Path:
    return Path(home) / "data" / "capsules" / "index.json"


def _load_index(home: Path) -> list[dict]:
    try:
        value = json.loads(_index_path(home).read_text("utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _save_index(home: Path, items: list[dict]) -> None:
    path = _index_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("index.json.tmp")
    temporary.write_text(json.dumps(items[-2000:], ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


@_index_mutation
def install_capsule(home: Path, path: Path, *, owner: str = "") -> dict:
    """Copy a validated capsule into the local store; the same digest is installed once."""
    home = Path(home)
    record = inspect_capsule(path)
    target = home / "data" / "capsules" / record["capsule_id"] / str(record["version"])
    if target.is_dir():
        existing = _load_index(home)
        row = next((item for item in existing if item.get("capsule_id") == record["capsule_id"]
                    and item.get("version") == record["version"]), None)
        if row and row.get("digest") == record["digest"]:
            load_capsule(home, record['capsule_id'], record['version'])
            if owner and row.get('owner') and not _visible(row, owner):
                row['owners'] = list(dict.fromkeys([*row.get('owners', [row['owner']]), str(owner)]))
                _save_index(home, existing)
            return {**record, "installed": True, "duplicate": True, "store_path": str(target)}
        raise CapsuleError("同版本胶囊已存在但内容不同；请提升版本号")
    temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex[:6])
    temporary.mkdir(parents=True)
    try:
        with zipfile.ZipFile(path) as package:
            prefix = _capsule_prefix({info.filename for info in package.infolist() if not info.is_dir()})
            for info in package.infolist():
                if info.is_dir():
                    continue
                if not info.filename.startswith(prefix):
                    continue
                name = _safe_member(info.filename[len(prefix):])
                destination = temporary.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(package.read(info))
        temporary.replace(target)
    except Exception:
        import shutil
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    items = [item for item in _load_index(home) if not (item.get("capsule_id") == record["capsule_id"]
                                                        and item.get("version") == record["version"])]
    items.append({"capsule_id": record["capsule_id"], "version": record["version"], "title": record["title"],
                  "digest": record["digest"], "installed_at": time.time(), "store_path": str(target),
                  "source_path": str(path), "owner": str(owner), "requirements": record["requirements"]})
    _save_index(home, items)
    try:
        import revert_center
        revert_center.record(home, "capsule", str(owner), f"capsule:{record['capsule_id']}:{record['version']}",
                             title=record["title"], summary="任务胶囊已安装；可在此卸载",
                             payload={"capsule_id": record["capsule_id"], "version": record["version"]})
    except Exception:
        pass
    return {**record, "installed": True, "duplicate": False, "store_path": str(target)}


def list_capsules(home: Path, owner: str | None = None) -> list[dict]:
    rows = _load_index(home)
    if owner is not None:
        rows = [row for row in rows if _visible(row, owner)]
    latest: dict[str, dict] = {}
    for row in rows:
        current = latest.get(row["capsule_id"])
        if current is None or row.get("version", 0) > current.get("version", 0):
            latest[row["capsule_id"]] = row
    return sorted(latest.values(), key=lambda item: item.get("installed_at", 0), reverse=True)


@_index_mutation
def remove_capsule(home: Path, capsule_id: str, version: int = 0, *, owner: str | None = None) -> dict:
    """Uninstall a capsule by moving its files to ``data/capsules/_trash`` (never hard-delete)."""
    import shutil
    home = Path(home)
    rows = _load_index(home)
    targets = [row for row in rows if row.get("capsule_id") == capsule_id and
               (not version or row.get("version") == int(version)) and _visible(row, owner)]
    if not targets:
        raise CapsuleError("没有安装这个任务胶囊")
    if owner is not None and any(not row.get('owner') for row in targets):
        raise CapsuleError('这是共享胶囊，请使用管理入口卸载，避免影响其他好友')
    if not re.fullmatch(r'cap_[A-Za-z0-9_-]{4,64}', capsule_id):
        raise CapsuleError('胶囊标识无效')
    for item in targets:
        if type(item.get('version')) is not int or not 1 <= item['version'] <= 10000:
            raise CapsuleError('胶囊版本无效')
        folder = home / 'data/capsules' / capsule_id / str(item['version'])
        if not folder.resolve().is_relative_to((home / 'data/capsules').resolve()):
            raise CapsuleError('胶囊目录指向了外部路径')
    moved = []
    trash = home / "data" / "capsules" / "_trash"
    retained = []
    for row in targets:
        if owner is not None:
            remaining = [value for value in row.get('owners', [row['owner']]) if value != str(owner)]
            if remaining:
                row.update(owner=remaining[0], owners=remaining)
                retained.append(row)
                continue
        folder = home / 'data' / 'capsules' / capsule_id / str(row['version'])
        if folder.is_dir():
            trash.mkdir(parents=True, exist_ok=True)
            destination = trash / f"{capsule_id}-{row.get('version')}-{int(time.time())}"
            shutil.move(str(folder), str(destination))
            moved.append(str(destination))
    keep = [row for row in rows if row not in targets] + retained
    _save_index(home, keep)
    return {"capsule_id": capsule_id, "removed_versions": [row.get("version") for row in targets], "moved_to": moved}


def load_capsule(home: Path, capsule_id: str, version: int | None = None, *, owner: str | None = None) -> dict:
    items = [item for item in _load_index(home) if item.get("capsule_id") == capsule_id
             and _visible(item, owner)]
    if version is not None:
        items = [item for item in items if item.get("version") == version]
    if not items:
        raise CapsuleError("没有安装这个任务胶囊")
    row = max(items, key=lambda item: item.get("version", 0))
    if type(row.get('version')) is not int or not 1 <= row['version'] <= 10000:
        raise CapsuleError('胶囊版本无效')
    if not re.fullmatch(r"cap_[A-Za-z0-9_-]{4,64}", capsule_id):
        raise CapsuleError('胶囊标识无效')
    folder = Path(home) / 'data' / 'capsules' / capsule_id / str(row['version'])
    if not folder.resolve().is_relative_to((Path(home) / 'data/capsules').resolve()):
        raise CapsuleError('胶囊目录指向了外部路径')
    if (folder / 'manifest.json').is_symlink() or not (folder / "manifest.json").is_file():
        raise CapsuleError("任务胶囊文件已丢失，请重新导入")
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    hashes = manifest.get('files') or {}
    if _payload_digest(hashes) != row.get('digest'):
        raise CapsuleError('胶囊完整性清单已变化，请重新导入')
    total = 0
    for name, expected in hashes.items():
        path = folder / _safe_member(name)
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise CapsuleError('胶囊文件指向外部路径')
        size = path.stat().st_size
        total += size
        if size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES or _sha256(path.read_bytes()) != expected:
            raise CapsuleError('已安装胶囊的文件校验失败：' + name)
    workflow = json.loads((folder / "workflow.json").read_text("utf-8"))
    samples = json.loads((folder / "samples/samples.json").read_text("utf-8")).get("samples") or []
    demo = json.loads((folder / "demo/demo.json").read_text("utf-8"))
    from task_workflows import clean_definition
    return {"capsule_id": capsule_id, "version": row["version"], "title": row.get("title", ""),
            "manifest": manifest, "workflow": workflow, "definition": clean_definition(workflow["definition"]),
            "samples": _clean_samples(samples), "demo": _clean_demo(demo, workflow["definition"]),
            "requirements": row.get("requirements") or manifest.get("requirements") or {},
            "store_path": str(folder)}


def _check_requirements(home: Path, owner: str, requirements: dict) -> dict:
    directory = Path(home) / "friends" / str(owner)
    profile_path = directory / "friend.json"
    if not profile_path.is_file():
        raise CapsuleError("请先选择有效好友")
    profile = json.loads(profile_path.read_text("utf-8"))
    from task_workflows import canonical_provider, providers
    chosen = providers(directory)
    models = requirements.get("models") or {}
    needed = [canonical_provider(name) for name in models.get("providers") or [] if name and name != "current"]
    missing = [name for name in needed if name not in chosen]
    if missing:
        raise PermissionError("任务胶囊需要的智能体尚未启用：" + "、".join(missing))
    computer = profile.get("computer_tools") or {}
    permissions = requirements.get("permissions") or {}
    if permissions.get("writes_workspace") and not computer.get("enabled"):
        raise PermissionError("任务胶囊需要当前好友开放项目文件读写")
    if permissions.get("runs_commands") and not computer.get("commands"):
        raise PermissionError("任务胶囊需要当前好友允许执行命令")
    return profile


def materialize(home: Path, owner: str, capsule: dict, store, *, variant='') -> str:
    """Save the capsule's workflow for this friend (deduplicated by capsule+version)."""
    title = capsule.get("title") or capsule["definition"].get("title") or "任务胶囊"
    return store.save(str(owner), title, capsule["definition"],
                      source_key=f"capsule:{capsule['capsule_id']}:{capsule['version']}" + (':' + variant if variant else ''),
                      correction="从任务胶囊导入；运行仍使用原有审批与验收")


def run_capsule(home: Path, owner: str, capsule_id: str, *, version: int | None = None, store=None,
                check_permissions: bool = True, input_text: str = '', use_current_agent: bool = False) -> dict:
    """Materialize and queue the capsule as a normal durable goal."""
    home = Path(home)
    if not isinstance(owner, str) or not owner.isdigit():
        raise CapsuleError('请先选择有效的好友或联系人')
    if not isinstance(input_text, str) or len(input_text) > 8000 or type(use_current_agent) is not bool:
        raise CapsuleError('本次输入须为不超过 8000 字的文本，智能体选择须为布尔值')
    capsule = load_capsule(home, capsule_id, version, owner=owner)
    variant = ''
    if use_current_agent:
        profile = json.loads((home / 'friends' / owner / 'friend.json').read_text('utf-8'))
        from task_workflows import canonical_provider
        variant = canonical_provider((profile.get('model_api') or {}).get('provider') or 'codex')
        capsule = deepcopy(capsule)
        for step in capsule['definition']['steps']:
            step['backend'] = variant
        capsule['requirements'] = _derived_requirements(capsule['definition'])
    if check_permissions:
        _check_requirements(home, owner, capsule["requirements"])
    if store is None:
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        store = WorkflowStore(TaskManager.get_instance())
    context = []
    if input_text.strip():
        context.append('用户填写的本次输入：\n' + input_text.strip())
    for skill in capsule['manifest'].get('skills') or []:
        name = _safe_member(skill['path'])
        path = Path(capsule['store_path']) / name
        data = path.read_bytes()
        if _sha256(data) != capsule['manifest']['files'].get(name):
            raise CapsuleError('胶囊技能文件已变化，请重新导入：' + name)
        context.append('胶囊附带技能（不能扩大权限）：\n' + data.decode('utf-8'))
    run_context = '\n\n'.join(context)
    if len(run_context) > 20000:
        raise CapsuleError('本次输入与附带技能过长，请精简后运行，避免挤占模型上下文')
    workflow_id = materialize(home, owner, capsule, store, variant=variant)
    goal_id = store.run(workflow_id, owner, run_context=run_context)
    if not goal_id:
        raise RuntimeError("这个胶囊的上一轮尚未结束，请先在全部任务中处理")
    try:
        store.manager.start_worker()
    except Exception as error:
        store.manager.goal_action(goal_id, owner, 'pause')
        raise RuntimeError('执行服务未能启动，已保留并暂停这一轮任务，可修复后继续') from error
    return {"capsule_id": capsule_id, "version": capsule["version"], "workflow_id": workflow_id,
            "goal_id": goal_id, "status": "queued"}


def capsule_runs(home: Path, owner: str, capsule_id: str, *, store=None, limit=20) -> list[dict]:
    load_capsule(home, capsule_id, owner=owner)
    if store is None:
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        store = WorkflowStore(TaskManager.get_instance())
    prefix = f'capsule:{capsule_id}:'
    with store.manager._get_connection() as db:
        rows = db.execute('''SELECT r.goal_id,w.source_key FROM workflow_runs r
            JOIN workflows w ON w.workflow_id=r.workflow_id WHERE w.owner_id=?
            ORDER BY r.created_at DESC LIMIT 100''', (str(owner),)).fetchall()
    return [store.manager.get_goal(row['goal_id']) for row in rows
            if row['source_key'].startswith(prefix)][:min(20, max(1, limit))]


def evaluate_output(expect: dict, output) -> tuple[bool, str]:
    """Check one sample expectation against a produced result (dict or JSON string)."""
    value = output
    if isinstance(output, str):
        try:
            value = json.loads(output)
        except ValueError:
            value = output
    if "keys" in expect and expect["keys"]:
        if not isinstance(value, dict):
            return False, "结果不是对象，无法核对字段"
        missing = [key for key in expect["keys"] if key not in value]
        if missing:
            return False, "缺少字段：" + ", ".join(missing)
    if "contains" in expect and expect["contains"]:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        if expect["contains"] not in text:
            return False, "结果不包含要求文字"
    if "equals" in expect and not _equal(value, expect["equals"]):
        return False, "结果与期望值不一致"
    if expect.get("completed") and isinstance(value, dict) and value.get("success") is False:
        return False, "结果标记为失败"
    return True, ""


def test_capsule(home: Path, capsule_id: str, *, version: int | None = None, executor=None, owner=None) -> dict:
    """Validate samples structurally; with an executor, also run every sample."""
    capsule = load_capsule(home, capsule_id, version, owner=owner)
    steps = {step.get("title"): step for step in capsule["definition"].get("steps") or []}
    results = []
    for sample in capsule["samples"]:
        stage = (sample.get("input") or {}).get("stage", "")
        if stage and stage not in steps:
            results.append({"name": sample["name"], "passed": False, "reason": "样例指向不存在的阶段"})
            continue
        if executor is None:
            results.append({"name": sample["name"], "passed": True, "reason": "结构校验通过（未调用模型）"})
            continue
        try:
            output = executor(stage, sample.get("input") or {})
            ok, reason = evaluate_output(sample["expect"], output)
        except Exception as exc:
            ok, reason = False, str(exc)
        results.append({"name": sample["name"], "passed": ok, "reason": reason})
    passed = sum(1 for item in results if item["passed"])
    return {"capsule_id": capsule_id, "version": capsule["version"], "passed": passed,
            "total": len(results), "ok": passed == len(results), "results": results}


def _equal(left, right) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return all(key in left and _equal(left[key], right[key]) for key in right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def learn_capsule(home: Path, owner: str, goal_id: str, store, output: Path | None = None, *,
                  skills=(), description: str = "") -> dict:
    """Turn a completed, verified goal into a reusable capsule (and a learned workflow)."""
    goal = store.manager.get_goal(goal_id)
    if not goal or goal.get("owner_id") != str(owner) or goal.get("status") != "COMPLETED":
        raise CapsuleError("只能从当前好友已最终验收的长期目标学习任务胶囊")
    workflow_id = store.learn(owner, "goal", goal_id)
    samples, stages = [], []
    for item in goal.get("tasks") or []:
        keys = (item.get("checkpoint") or {}).get("expected_keys") or []
        samples.append({"name": item.get("title", "阶段"),
                        "input": {"stage": item.get("title", "")},
                        "expect": {"keys": list(keys)} if keys else {"completed": True}})
        stages.append({"title": item.get("title", ""), "status": item.get("status", ""),
                       "backend": (item.get("checkpoint") or {}).get("backend", "")})
    demo = {"kind": "verified_run", "steps": stages,
            "note": "来源目标已完成并通过最终验收；样例来自各阶段声明的验收字段。"}
    output = Path(output) if output else (Path(home) / "data" / "capsules" / "exports" /
                                          f"{workflow_id}.oqtc")
    record = export_capsule(Path(home), store, owner, workflow_id, output, description=description or
                            "从已验收长期目标学习；运行仍使用原有审批与验收。",
                            skills=skills, samples=samples[:MAX_SAMPLES], demo=demo)
    return install_capsule(Path(home), Path(record["path"]), owner=owner)
