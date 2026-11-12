# Tools and Host Permissions

Tool integration is strictly limited to tools explicitly implemented within the project. External login states or uninstalled services are never presumed available.

| Tool Name | Codex CLI | Claude CLI | Antigravity CLI | Chat API |
|---|---|---|---|---|
| `search_friend_memory` / `search_topic_library` / `get_today_chat` | MCP | MCP | Programmatic Tool Loop | Function Calling |
| `phone_now` / `phone_usage_today` / `phone_screen_text` / `app_history` / `set_history_apps` | MCP | MCP | Programmatic Tool Loop | Function Calling |
| Enabled `api_*` web endpoints | MCP directory | MCP directory | Programmatic Tool Loop | Function Calling |
| Authorized `desk_*` host tools | MCP directory | MCP directory | Programmatic Tool Loop | Function Calling |
| Web Search / Page Fetching | Native Tools | `WebSearch` + `WebFetch` | `search_web` + `read_url_content` | Requires compatible custom API |
| Incoming Image Vision | Image Input | Image Input | `view_file` | Backend and model must support vision |

Each backend enforces rate limits and friend isolation boundaries. Background tasks with no tool declarations receive zero tool access. Mobile tools require successful local device pairing.

---

## Local PC & Project Permissions

- **Project Directory Binding**: Users may bind a local project directory (e.g. `D:\Projects\Demo`). CLIs treat this root as their working directory (defaults to `workspaces/<QQ>/` if blank).
- **Full Disk Access Switch**: When enabled, lifts boundary restrictions to allow file I/O across local drives; when disabled, strictly confines operations within the bound project folder.
- **Read-Only vs. Read-Write**: Read-only mode prevents file modification; read-write mode allows code editing and creation.
- **Command Execution & Approval Modes**:
  - **Codex**: Dynamically enables shell execution flags; maps to sandboxes (`dangerFullAccess`, `workspaceWrite`, `readOnly`). Supports `never` (auto) or `on-request` approval modes.
  - **Claude**: Unlocks native toolsets (Bash, Edit, Write, Read, Glob, Grep) bounded by read-only limits and `dontAsk` vs `ask` approval rules.
  - **Antigravity**: Toggles terminal command execution and file modification tools.
  - **Chat API**: Routes execution through `execute_powershell` with timeout enforcement.
