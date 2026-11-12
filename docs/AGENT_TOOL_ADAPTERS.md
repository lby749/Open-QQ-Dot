# External coding-agent adapters (Open QQ Dot)

**Added in the 2026-10-25 integration.** One safe dispatch entrypoint serves OpenCode, Cursor, Qwen Code, ZCode, Kimi Code and other ACP clients. The existing Open QQ Dot MCP mailbox server remains supported, and a *separate* MCP stdio client supports trusted tool discovery/calls.

## Installation and availability

Run `python tools/agent_adapter_probe.py`. This only reports executables found on PATH; it does **not** confirm sign-in, licenses, available quota, or working model endpoints. All native products must be installed and authenticated by their respective authorized user.

| Backend selector | Transport | Official command/endpoint |
|---|---|---|
| `opencode` | local HTTP | start `opencode serve --hostname 127.0.0.1 --port 4096` from the authorized workspace; set `OPENCODE_SERVER_PASSWORD` for Basic authentication |
| `cursor` | CLI | `agent -p "..." --output-format json`, or `cursor-agent`; `--force` only under explicit write authorization |
| `qwen_code` | CLI | `qwen --output-format json`, prompt passed via stdin |
| `zcode` | CLI | `zcode --prompt "..." --cwd <workspace> --mode plan --json --no-color`; `yolo` only with explicit write authorization |
| `kimi_code` | ACP stdio | `kimi acp` (Kimi Code **Node.js** version, not the deprecated Python CLI) |
| `acp` | ACP stdio | trusted local executable and argv, configured by the host |
| `mcp` | MCP stdio | trusted tool server executable; initialize, list tools, call tools |

Official resources: https://docs.opencode.ai/docs/server/ , https://cursor.com/docs/cli/overview , https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/headless.md , https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp , https://agentclientprotocol.com/ , https://modelcontextprotocol.io/ , https://github.com/zai-org/ZCode . Flags/protocol versions may change; recheck installed versions before production deployment.

## TaskManager usage

For a code/file task, select `checkpoint.backend` explicitly (`opencode`, `cursor`, `qwen_code`, `zcode`, `kimi_code`, or `acp`). The task retains the **existing** owner identity, workspace, budgets, cancellation lifecycle, and independent TaskVerifier. A provider's final text is never proof of successful code modifications.

The user must have explicitly enabled all of `computer_tools.enabled`, `computer_tools.commands`, `computer_tools.full_computer_access`, and must **not** be read-only. This is intentional: native coding CLIs run under the host OS user and **are not sandboxed** by merely setting a working directory. Do not enable those permissions for untrusted QQ friends or third-party requests. Secure per-user process isolation is future work.

Optional **trusted** model profile adapter config (never accept arbitrary executable commands or target URLs from incoming QQ messages or a Web API):

```json
{
  "agent_adapters": {
    "opencode": {"url": "http://127.0.0.1:4096"},
    "acp": {"command": ["/absolute/path/to/trusted-acp-agent", "acp"], "protocol_version": 1}
  }
}
```

`opencode` always checks the server's `/path.directory` against the TaskManager workspace and refuses mismatches to avoid forwarding another user's task to the wrong project. Session IDs from shared profiles are deliberately ignored; a fresh task-local session is created. Future persistent session restoration must be **owner-scoped** and verified on reconnect.

`mcp` is **not** a substitute for an independent coding Agent. It is a tool protocol. Existing `agent_mailbox_mcp.py` exposes Open QQ Dot tools to external Agent sessions. The new stdio **client** (`mcp_list_tools`, `mcp_call_tool`) should only be used on vetted MCP servers with separately approved tool effects. MCP tool responses and cross-Agent messages are untrusted content.

## Safety and real-world testing limitations

- ACP reverse RPC permission requests, filesystem access and terminal execution are rejected rather than auto-approved. Some agents need an interactive client to finish a tool-dependent task; in that case execution fails rather than pretending success.
- Cursor CLI's noninteractive mode may write files on certain versions even without `--force`. Treat every coding CLI as potentially destructive.
- Program arguments, process environment and shared home-directory credentials can be visible to same-account programs; a task workspace directory is not an OS sandbox.
- OpenCode Basic authentication is applied only to **127.0.0.1** with redirects blocked. Avoid exposing its unauthenticated server on public interfaces.
- The HTTP console provides authenticated **read-only** adapter availability; it deliberately cannot remotely set commands, credentials or run native agent processes.
- In this build the different CLI/ACP/MCP protocols are exercised against **independent local test doubles**, not paid logged-in provider accounts. Real Windows EXE/Qt, NapCat and end-to-end cloud execution must be tested on the owner's system.
