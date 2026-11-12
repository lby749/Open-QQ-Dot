# Cross-Agent Communication: Prior Art and Design Decisions

Researched 2026-10-22. This feeds Phase 3b, the agent communication MVP. Sources are listed at the end.
Claims here come from each project's docs and README. None of these projects were run locally.

## 1. What exists in Open QQ Dot today (verified in code)

| Piece | Reusable for messaging? |
|---|---|
| `TaskManager` (SQLite queue, leasing, DAG, PID/birth recovery, steering) | Yes, to **wake** an idle agent and give it budgets and restart recovery |
| `NotificationManager` (persistent, sink-push, retry on reconnect) | Pattern only. It pushes to user sinks and has no pull, ack, correlation or per-recipient state |
| `TrustedIdentityContext` / `trusted_admin_policy` | Yes, for server-side sender identity |
| `multi_agent_orchestrator.orchestrate` | Supervisor mode only. It relays text into prompts, with no agent-to-agent channel |
| `codex_app_server.CodexAppServerClient` (`turn/steer`, `turn/interrupt`, approvals) | Yes. `turn/steer` can **push** a message into a live Codex turn |
| Local stdio MCP servers (`topic_library`, `phone_tools`) injected into Codex and Claude | Yes, as the delivery surface for mailbox tools |
| `validate_path_in_bounds`, ApprovalManager | Yes, for artifact references and for gating side effects |

Missing: an agent registry, mailboxes, threads, acks, cross-project grants and loop limits.

## 2. Prior art

### MCP Agent Mail (Dicklesworthstone/mcp_agent_mail; Rust successor mcp_agent_mail_rust)
"Gmail for coding agents" over MCP. The Python version is unmaintained; the Rust rewrite is current.
- **Data model:** `projects`, `agents` (name, program, model, task_description, last_active, contact_policy), `messages` (thread_id, subject, body_md, importance, ack_required), **`message_recipients` (per-recipient `read_ts` / `ack_ts`)**, `file_reservations`, `agent_links` (cross-project approvals), and an FTS5 index over messages.
- **Tools:** `register_agent`, `send_message`, `fetch_inbox(unread_only, since_ts, ...)`, `acknowledge_message`, `reply_message` (inherits `thread_id`), `search_messages`, `summarize_thread`, `request_contact`/`respond_contact`, `file_reservation_paths`/`release_file_reservations`, plus macros such as `macro_start_session`.
- **Cross-project:** *discovery does not authorise messaging*. Cross-project messages need an approved `AgentLink`.
- **Advisory file leases:** a TTL lease on path patterns. Conflicts are reported, not blocked, and an optional pre-commit guard enforces exclusive leases.
- Delivery is pull-only (agents poll `fetch_inbox`). A Git archive provides human audit; `am doctor` checks for stale locks and orphaned recipients.

### agent-loom (osteele/agent-mail): a mixed-harness mail bus
- A per-project durable **spool**. The sender writes to the spool directly when no daemon is running, so delivery never depends on a server.
- **Receipt states:** spooled, pending, held, pushed, push-unreachable, read, refused, expired.
- **Push where the harness supports it, pull everywhere else:**
  - **Claude Code:** pushed through a *channels* plugin.
  - **Codex:** pull-only, plus reminder hooks that inject **only unread counts** (never senders or bodies) on turn events.
- **Identity:** stable session IDs are the durable identity, and friendly names are aliases. The sender is stamped only when provable.
- **Security rule:** *inbound mail is untrusted data. It cannot approve permissions or override a session's rules.* The daemon is localhost-only.
- **Pitfall:** marking a broadcast read hides it for everyone. Per-recipient state avoids this.

### A2A (Agent2Agent protocol, spec v1.0)
- **AgentCard:** identity, skills, capabilities (`streaming`, `pushNotifications`), and `securitySchemes`.
- **Task lifecycle:** `SUBMITTED → WORKING → (INPUT_REQUIRED | AUTH_REQUIRED) → COMPLETED | FAILED | CANCELED | REJECTED`.
- **`contextId`** (the conversation) vs **`taskId`** (one unit of work). Messages are for communication; results are **Artifacts**.
- Methods: `SendMessage` (with `returnImmediately`), `GetTask`, `CancelTask`, `SubscribeToTask`, and push-config CRUD (webhooks).
- The spec warns that streamed messages are not a reliable delivery channel; state must be fetched from the task.

### Letta (stateful agents)
- Built-in tools: `send_message_to_agent_async` (returns a delivery receipt), `send_message_to_agent_and_wait_for_reply`, and `send_message_to_agents_matching_all_tags` (supervisor broadcast by tag).
- **Shared memory blocks** attach to several agents, each read-only, read-write or private.

### AstrBot (the QQ/NapCat ecosystem peer)
- SubAgents are reached through **handoff-as-tool** (`transfer_to_<name>`), chosen from each sub-agent's public description. A background mode acks first and resumes the main agent later.
- A sub-agent **does not get the main history**; the main agent must pass the needed context, which is context minimisation.
- There is no sub-agent-to-sub-agent messaging and no persistence of sub-agent runs. Sub-agents are not a permission boundary.

### Codex app-server (official docs; affects our transport)
- Approvals: `item/commandExecution/requestApproval` and `item/fileChange/requestApproval`. The decisions are `accept`, `acceptForSession`, `decline` and `cancel`.
  - **Bug:** our client sends `"reject"` (`codex_app_server.py:259, 303`).
- Diffs: `turn/diff/updated` (aggregated unified diff) and `fileChange` items on `item/completed`.
  - **Bug:** our `item/fileChange/patchUpdated` handler (`codex_app_server.py:370`) listens for a method that doesn't exist.
- `turn/steer {threadId, input, expectedTurnId}` appends input to the in-flight turn. This is the push path into a live Codex agent.
- The wire omits `"jsonrpc":"2.0"`.

### Claude Code channels
- A channel is an MCP server that pushes events into a **running interactive** session (research preview). It requires a claude.ai login and `--channels`.
- It does not fit our backend, which runs `claude -p` per call. For Claude, wake-up goes through TaskManager instead.

## 3. Design decisions for Phase 3b

1. **Build a small native module and borrow MCP Agent Mail's data model, instead of depending on it.**
   - We need `owner_id` tenant isolation, TaskManager wake-ups, ApprovalManager and our identity contexts, and none of these fit an external server.
   - **Keep its tool names** (`send_message`, `fetch_inbox`, `acknowledge_message`, `reply_message`, `list_agents`) so agents and prompts familiar with them work unchanged.
2. **Per-recipient delivery state** (`message_recipients`: `delivered_ts`, `read_ts`, `ack_ts`, `state`) using agent-loom's receipt states, cut to `pending`, `delivered`, `read`, `acked`, `refused` and `expired`.
3. **SQLite as the spool.** The sender's write is the delivery and no daemon is needed. This matches the existing TaskManager and Notification stores, with a path rooted at the data dir, not cwd.
4. **Identities:**
   - `agent_id` is stable: `owner/project/provider[/session]`, with friendly names as aliases.
   - The sender is bound server-side from the execution context (`task_runtime.current_execution`) and never taken from tool arguments.
5. **`thread_id` (the conversation, A2A `contextId`) and `task_id` (the TaskManager unit of work) are separate fields.** Results travel as artifact references, not bodies.
6. **Cross-project and cross-owner messages need an explicit grant**, modelled on `AgentLink`: `request_contact` creates a pending grant that the owner or an admin approves. Discovery never authorises.
7. **Inbound messages are untrusted data.**
   - They are rendered into prompts inside a data envelope and cannot carry approvals.
   - Side effects still go through ApprovalManager.
   - Reminder hooks expose only unread counts.
8. **Delivery per harness:**
   - **Codex live turn:** push via `turn/steer`.
   - **Idle agent of any kind:** TaskManager enqueues an `agent_message` task that runs it with its inbox.
   - **Claude and Antigravity:** pull through MCP tools during their turn.
9. **Loop and cost control:** a `hop_count` and TTL on every message, per-thread message and token caps, and a reply-depth limit. Exceeding a limit sets the message to `refused`, with an audit entry.
10. **Supervisor mode on the same bus.** `orchestrate` sends subtasks as `handoff` messages carrying explicit, minimised context (AstrBot and Letta show this works) and subscribes to the thread. Agents may also message each other directly inside the thread (Mode A within Mode B).
11. **Later:**
    - Advisory file leases for parallel code subtasks, reusing `target_files`.
    - An AgentCard-shaped registry entry, so an A2A gateway can expose agents externally without a redesign.
    - Search over messages with the existing FTS5 stack.
    - A `doctor` check for stale leases and orphaned recipients.

## Sources
- MCP Agent Mail: https://github.com/Dicklesworthstone/mcp_agent_mail (Rust: https://github.com/Dicklesworthstone/mcp_agent_mail_rust)
- agent-loom / agent-mail: https://github.com/osteele/agent-mail
- A2A specification: https://a2a-protocol.org/latest/specification/
- Letta multi-agent: https://docs.letta.com/guides/agents/multi-agent, shared memory: https://docs.letta.com/guides/agents/shared-memory-blocks
- AstrBot SubAgent orchestration: https://docs.astrbot.app/en/use/subagent.html
- Codex app-server: https://developers.openai.com/codex/app-server (redirects to https://learn.chatgpt.com/docs/app-server)
- Claude Code channels: https://code.claude.com/docs/en/channels
