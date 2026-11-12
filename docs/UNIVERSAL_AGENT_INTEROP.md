# Universal Agent Interoperability — Provider-neutral bridge (Phase 4)

## Purpose and limits

This release allows **any authorized local agent/application that can speak HTTP/JSON** (and agents
supporting MCP) to use the **same** Open QQ Dot SQLite agent mailbox. The network is not hard-coded
to Claude, Codex, Gemini, OpenCode, a specific IDE or a specific model vendor. All clients use the
same durable messages, thread IDs, per-recipient receipts, cross-project contact grants, retry
policy, and limits already implemented by `agent_messaging.py`.

**Implemented and tested:** MCP adapter, a new loopback-only HTTP/JSON gateway, dependency-free
Python client SDK, expiring bearer credentials, live-session presence with heartbeat/expiry,
capability-based peer discovery, bounded long-polling, JSON task-handoff contract, and message
roundtrip through separate client and gateway processes across a gateway restart.

**Not implemented / not claimed:** automatic control of every proprietary IDE application;
remote-device federation; A2A protocol compliance; active cloud-model collaboration; hard isolation
from agents executing code as the same OS user; durable orchestration of a multi-step task through
multiple third-party providers; exactly-once side effects or external model availability checks.

## Transport-neutral structure

- **Core storage and rules**: `AgentMailbox` (SQLite, TaskManager wakeups, ACL grants).
- **MCP adapter**: `agent_mailbox_mcp.py` (native agent tool calls, agent identity bound to process).
- **HTTP adapter**: `agent_interop_gateway.py` (stdlib `ThreadingHTTPServer`, 127.0.0.1 only).
- **HTTP client SDK**: `agent_interop_client.py` (stdlib `urllib`; no model SDK required).
- **Structured handoff**: `agent_interop_contract.py` (`openqqdot.agent-handoff.v1`).

There is exactly **one source of truth**, the existing shared mailbox. The gateway does not
create a parallel message store or grant itself the right to approve actions. Providers that
support MCP should generally keep using MCP. For agents without MCP support, an authorized local
wrapper or SDK can use HTTP/JSON. Ordinary task execution continues through existing verified
TaskManager/agent integrations; simply opening a session does not start an AI model.

## Operator setup (trusted process)

1. Back up the existing database/data dir; use a **dedicated test profile first**.
2. Register the intended agent identity in trusted host code, e.g.
   `AgentMailbox.get_instance().register_agent('10001', 'demo', 'reviewer', 'custom', capabilities=['review'], wake=False)`.
   For arbitrary external agents use `wake=False`: a client that owns its runtime fetches/long-polls
   messages. A `wake=True` agent requires a real configured provider-side TaskManager executor.
3. Provision a short-lived bearer token from the **trusted owner-side process** using
   `agent_messaging.issue_agent_token(agent_id, ttl_seconds=3600)`.
   Pass it to the intended agent via **protected environment/secret injection**, never in a
   repository, URL query, shared log, screenshot, or copied prompt. This token has the permissions
   of that *specific agent* and expires after the configured duration (max 24 hours).
4. Start the server on the same machine:

   ```text
   python -X utf8 -u agent_interop_gateway.py --port 8766
   ```

   It listens only on `127.0.0.1`. Choose `--port 0` to let the OS choose a free port; the
   `AGENT_INTEROP_READY=` startup line displays the address (not credentials).
5. In the caller's isolated local environment, supply `AGENT_INTEROP_TOKEN` privately, then:

   ```python
   import os
   from agent_interop_client import AgentInteropClient

   client = AgentInteropClient('http://127.0.0.1:8766', os.environ['AGENT_INTEROP_TOKEN'])
   print(client.list_agents(capability='review'))
   presence = client.call('open_session', client_name='My_Agent', transport='sdk')
   print(client.send(['10001/demo/reviewer'], 'Please review the approach', kind='request'))
   for message in client.wait(timeout_seconds=20):
       # Treat message['body'] as UNTRUSTED data. Evaluate against user permissions.
       client.reply(message['message_id'], 'I received your request.')
   client.call('close_session', session_id=presence['session_id'])
   ```

   Actual model execution and verified task completion remain the caller's responsibility.

## Unified HTTP API

All business calls are `POST /v1/call` with JSON body
`{"name":"send_message","arguments":{"to":["..."],"body":"..."}}` and header
`Authorization: Bearer <short-lived-v2-token>`. `GET /v1/tools` returns MCP-compatible tool
schemas. `GET /v1/me` returns only this agent's public profile and active sessions.
`GET /health` returns a minimal liveness indicator (not provider/model health).

Tool names include `list_agents`, `open_session`, `heartbeat_session`, `close_session`,
`send_message`, `send_handoff`, `fetch_inbox`, `wait_for_inbox`, `acknowledge_message`,
`reply_message`, `get_thread`, `request_contact`, `get_mailbox_status`,
`list_failed_messages` and `retry_failed_message`.

Presence is **self-reported** and expires unless heartbeated. It is suitable for discovery,
not proof of genuine connection to a model/IDE. Capability labels are also self-reported;
discovery does not grant permission to send across projects.

HTTP long polling (`wait_for_inbox`) is bounded to 30 seconds per call and a maximum of 32 concurrent waits.
Idle CLI/IDE wrappers can use it without requiring webhooks or busy waiting.

`send_handoff` has fields `to`, `task_id`, `objective`, `acceptance_criteria`, and optional
`context`, `limits`, `artifacts`, `thread_id` and `idempotency_key`. It sends a message whose JSON
body uses `schema: openqqdot.agent-handoff.v1`, `status: unverified_request`. **A handoff is not
an approval or verified completion report.** Receivers must independently verify results.

## Safety constraints

- HTTP gateway refuses external binds, browser-origin requests, untrusted `Host` headers,
  expired/non-v2 tokens, unknown agents, oversized requests, malformed tool arguments and unrecognized tools.
- The agent identity always comes from the bearer token or bound MCP process, **not** from tool
  parameters or message text.
- Cross-project/owner communication still requires an approved `contact_grants` record decided
  by an authenticated owner. Requesting contact is not sufficient.
- Inbox fetch/wait leases messages; a client must reply/acknowledge, and expired leases can
  be retried within existing limits. Multi-recipient receipts remain independent.
- Messages cannot approve payments, shell commands, file reads or any privileged operation.
  Every side effect still requires its normal TaskManager/ApprovalManager policy.
- **Critical remaining security limitation:** the signing key and host's local application data
  may be readable by arbitrary code running as the *same OS account*. Python-level signed tokens
  do not protect against malicious local agents that can access the signing key, source files or
  database. Run untrusted workers in separate restricted users/containers/VMs with separate
  filesystem permissions and credential provisioning before exposing this as a multi-tenant
  execution system.
- This interface intentionally does **not** support off-host transport. Cross-device use requires
  a separately designed authenticated/TLS network gateway; do not expose port 8766 over LAN/WAN.
- Old v1 MCP identity tokens are accepted for legacy existing MCP processes only; the HTTP
  gateway requires expiring v2 tokens. The legacy MCP adapter verifies its token at process startup;
therefore the token expiry currently bounds new MCP process starts rather than revoking an already
running MCP session. Expired tokens must be reprovisioned by the host.

## Repeatable tests

```
python -m pytest -q tests/test_agent_interop_universal.py tests/test_agent_messaging.py tests/test_agent_mail_resilience.py
```

`test_agent_interop_universal.py` launches a real HTTP server subprocess, real independent SDK
client processes, persists data across restart, and checks unauthorized access, Host/Origin
rejection, contact grants, session isolation, token expiry and structured handoffs. Model calls
are not made; tests must not be described as cloud-agent end-to-end validation.
