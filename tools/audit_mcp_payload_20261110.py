"""Offline synthetic payload audit. No model, QQ, or external API calls."""
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def size(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def main():
    output = Path(sys.argv[1])
    archive = ROOT.parent / "OpenQQDots-原代码.zip"
    mismatches = []
    with zipfile.ZipFile(archive) as zipped:
        files = [item for item in zipped.infolist() if not item.is_dir()]
        for item in files:
            relative = Path(*Path(item.filename).parts[1:])
            path = ROOT / relative
            if not path.is_file() or path.read_bytes() != zipped.read(item):
                mismatches.append(str(relative))
    result = {"measurement": "synthetic_characters_and_bytes_not_billable_tokens",
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "baseline_files_checked": len(files), "baseline_mismatches": mismatches,
              "tools": [], "group": []}
    with tempfile.TemporaryDirectory(prefix="openqq_mcp_audit_") as temporary:
        os.environ["QQ_BRIDGE_HOME"] = temporary
        import agent_group
        import api_backend
        import agent_mailbox_mcp
        directory = Path(temporary) / "friends" / "synthetic"
        directory.mkdir(parents=True)
        for kind, computer in [("chat", {"enabled": False}),
                               ("chat", {"enabled": True, "mode": "read_only"}),
                               ("work", {"enabled": True, "commands": True}),
                               ("study", {"enabled": True})]:
            for count in (0, 10, 50, 100):
                os.environ["QQ_TASK_KIND"] = kind
                os.environ["QQ_STUDY_TASK"] = "1" if kind == "study" else "0"
                configured = [{"enabled": True, "name": f"api_synthetic_{i}",
                               "description": "合成测试工具：查询指定项目的状态和摘要，不连接真实服务。",
                               "url": "https://example.invalid/status", "method": "POST",
                               "inputSchema": {"type": "object", "properties": {
                                   "query": {"type": "string", "maxLength": 120}},
                                   "required": ["query"], "additionalProperties": False}}
                              for i in range(count)]
                (directory / "friend.json").write_text(json.dumps({
                    "qq_id": "synthetic", "computer_tools": computer,
                    "function_apis": configured}), encoding="utf-8")
                tools = api_backend._tools(directory)
                names = [tool["name"] for tool in tools]
                result["tools"].append({"kind": kind, "computer": computer,
                    "synthetic_api_count": count, "tool_count": len(tools),
                    "duplicate_names": sorted({name for name in names if names.count(name) > 1}),
                    **size(tools), "names": names if not count else []})
        result["mailbox_catalog"] = {"tool_count": len(agent_mailbox_mcp.TOOLS),
                                     **size(agent_mailbox_mcp.TOOLS)}
        # Exercise only our local stdio server, never any model backend.
        os.environ["QQ_TASK_KIND"] = "work"
        os.environ["QQ_STUDY_TASK"] = "0"
        (directory / "friend.json").write_text('{"computer_tools":{"enabled":false}}', encoding="utf-8")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "offline-audit", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "api_not_granted", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        command = [sys.executable, "-X", "utf8", "-u", str(ROOT / "topic_library.py"), str(directory)]
        normal = subprocess.run(command, input="\n".join(json.dumps(x) for x in requests)+"\n",
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        result["local_mcp_smoke"] = {"exit_code": normal.returncode,
                                      "responses": [json.loads(x) for x in normal.stdout.splitlines()]}
        malformed = subprocess.run(command, input="{not-json}\n"+json.dumps(requests[-1])+"\n",
                                   capture_output=True, text=True, encoding="utf-8", timeout=15)
        result["local_mcp_malformed_json"] = {"exit_code": malformed.returncode,
            "ping_survived": '"id": 4' in malformed.stdout,
            "json_decode_error": "JSONDecodeError" in malformed.stderr}
        room = {"members": [], "messages": [], "memory": []}
        for i in range(40):
            body = f"SYNTHETIC_{i:03d}:" + "这是用于衡量上下文重复的合成内容。" * 5
            room["messages"].append({"name": "成员甲", "role": "agent", "content": body})
            room["memory"].append({"name": "成员甲", "content": body})
        memory = agent_group.shared_memory(None, None, room, "合成好友记忆")
        member = {"id": "worker", "name": "成员乙", "role": "审查结论"}
        full = agent_group.prompt_for(room, member, memory=memory)
        no_memory = agent_group.prompt_for(room, member)
        duplicated = sum(full.count(f"SYNTHETIC_{i:03d}:") > 1 for i in range(40))
        result["group_prompt"] = {"messages": 40, "duplicated_message_markers": duplicated,
                                  "current": size(full), "without_memory_block": size(no_memory)}
        for count in (1, 3, 6, 12):
            members = [dict(member, id=str(i), name=f"成员{i}") for i in range(count)]
            room["members"] = members
            # Frozen history comparison, not a simulation of generated replies growing the room.
            prompts = [agent_group.host_prompt(room, members[0], "完成合成任务", memory=memory)]
            prompts += [agent_group.prompt_for(room, m, memory=memory) for m in members[1:]]
            prompts += [agent_group.summary_prompt(room, members[0], "完成合成任务", memory=memory)]
            result["group"].append({"members": count, "calls_with_host_summary": len(prompts),
                                    "frozen_history_input": size("".join(prompts))})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "tools"}, ensure_ascii=False))
    print("Tool rows:", [(x["kind"], x["synthetic_api_count"], x["tool_count"], x["chars"])
                         for x in result["tools"]])


if __name__ == "__main__":
    main()
