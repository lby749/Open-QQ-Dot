"""Model-facing tools for installed task capsules.

The owner is always the current friend's directory; a capsule can only be
listed, inspected, tested structurally or queued for that friend.  Running one
still goes through ``WorkflowStore.run`` and therefore through the existing
agent permissions, approvals, budgets and verifiers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import task_capsule


def _home(directory: Path) -> Path:
    root = os.environ.get("QQ_BRIDGE_HOME")
    if root:
        return Path(root)
    # friends/<qq> -> the project root
    directory = Path(directory).resolve()
    return directory.parent.parent


def tools(directory=None) -> list[dict]:
    if directory is None:
        return []
    try:
        installed = task_capsule.list_capsules(_home(directory), owner=Path(directory).name)
    except Exception:
        installed = []
    if not installed:
        return []
    names = ", ".join(item.get("title") or item.get("capsule_id") for item in installed[:12])
    return [
        {"name": "capsule_list",
         "description": f"列出当前好友可用的任务胶囊（已安装的可复用 AI 工作流）。当前有：{names}。",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "capsule_inspect",
         "description": "查看一个任务胶囊的说明、阶段、模型与权限要求、测试样例和自动化示范。只读，不执行工作流。",
         "inputSchema": {"type": "object", "properties": {
             "capsule_id": {"type": "string", "maxLength": 80}}, "required": ["capsule_id"],
             "additionalProperties": False}},
        {"name": "capsule_test",
         "description": "按胶囊内置测试样例做结构校验（不调用模型），确认阶段和验收字段是否仍匹配。",
         "inputSchema": {"type": "object", "properties": {
             "capsule_id": {"type": "string", "maxLength": 80}}, "required": ["capsule_id"],
             "additionalProperties": False}},
        {"name": "capsule_run",
         "description": "把任务胶囊作为一个工具调用：按胶囊内的可学习工作流创建一轮持久化任务。仍受当前好友的智能体、项目权限、调用预算和现有验收约束。",
         "inputSchema": {"type": "object", "properties": {
             "capsule_id": {"type": "string", "maxLength": 80},
             "input_text": {"type": "string", "maxLength": 8000},
             "use_current_agent": {"type": "boolean"},
             "version": {"type": "integer", "minimum": 1}}, "required": ["capsule_id"],
             "additionalProperties": False}},
    ]


def call(directory, name, arguments, context=None) -> dict:
    home, owner = _home(directory), Path(directory).name
    if name == "capsule_list":
        rows = []
        for item in task_capsule.list_capsules(home, owner=owner):
            try:
                capsule = task_capsule.load_capsule(home, item["capsule_id"], owner=owner)
            except Exception:
                continue
            rows.append({"capsule_id": item["capsule_id"], "version": item.get("version"),
                         "title": item.get("title", ""), "stages": len(capsule["definition"].get("steps") or []),
                         "providers": (item.get("requirements") or {}).get("models", {}).get("providers", []),
                         "samples": len(capsule["samples"])})
        return {"capsules": rows, "note": "用 capsule_inspect 查看详情，capsule_run 调用。"}
    capsule_id = str((arguments or {}).get("capsule_id") or "")
    if not capsule_id:
        raise ValueError("缺少 capsule_id")
    if name == "capsule_inspect":
        capsule = task_capsule.load_capsule(home, capsule_id, (arguments or {}).get("version"), owner=owner)
        definition = capsule["definition"]
        return {"capsule_id": capsule_id, "version": capsule["version"], "title": capsule["title"],
                "stages": [{"title": step.get("title"), "category": step.get("category"),
                            "backend": step.get("backend", "")} for step in definition.get("steps") or []],
                "requirements": capsule["requirements"], "samples": capsule["samples"][:10],
                "demo": capsule["demo"]}
    if name == "capsule_test":
        return task_capsule.test_capsule(home, capsule_id, version=(arguments or {}).get("version"), owner=owner)
    if name == "capsule_run":
        try:
            return task_capsule.run_capsule(home, owner, capsule_id, version=(arguments or {}).get("version"),
                input_text=(arguments or {}).get('input_text', ''),
                use_current_agent=(arguments or {}).get('use_current_agent', False))
        except PermissionError as exc:
            raise ValueError(f"当前好友未满足胶囊要求：{exc}") from None
    raise ValueError("未知的任务胶囊工具")


def tool_annotations(name: str) -> dict:
    readonly = name in ("capsule_list", "capsule_inspect", "capsule_test")
    return {"readOnlyHint": readonly, "destructiveHint": not readonly, "openWorldHint": False}
