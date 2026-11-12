# Agent Mail Reliability — 2026-10-24

## What is implemented

This release extends the **same Chinese/English shared codebase**. It does not create a second agent communication system.

- **Passive message preservation:** Only `request`, `handoff`, and `review` messages trigger background wakeups. Responses, findings, and notes remain in a live agent's inbox; a background worker does not silently consume them.
- **Crash recovery:** Every active `TaskManager` worker reconciles the mailbox every ~15 seconds. Undelivered messages and expired receipt leases may wake a registered `wake=True` agent again. An active lease remains untouched until it expires.
- **Atomic wakeup deduplication:** `TaskManager.enqueue_agent_mail_task` checks and creates an inbox task in one SQLite `BEGIN IMMEDIATE` transaction. Two senders should not create two runnable wake tasks for the same agent.
- **Atomic receipt claims:** `fetch_inbox` leases a message within a SQLite immediate transaction. Concurrent mailbox clients cannot both claim the same pending receipt at the same time.
- **Durable send idempotency:** Optional `idempotency_key` on MCP `send_message` and the Python API. A retry with the same sender, key, recipient(s), kind and body returns the original message ID. Changing the payload with the same key is rejected.
- **Durable reply idempotency:** Normal `response` replies are automatically keyed to the request ID. A crash after writing a reply but before acknowledging the request will not create a second answer when retried. The TaskManager inbox executor checks for an existing response before invoking an AI backend again.
- **Bounded retries:** After five abandoned deliveries, a message moves to `failed`. Automatic retries stop; its receiving owner is notified without including message contents. Explicit recovery is supported.
- **Operational visibility:** The bound agent's MCP connection exposes `get_mailbox_status`, `list_failed_messages` and `retry_failed_message`. The last tool requires `confirm: true`.
- **Migration:** Existing SQLite mail databases get the new idempotency column/index in place, without discarding their historical messages or receipts.

## Native MCP tool workflow

1. `list_agents` finds authorized peers. Cross-project or cross-owner contact requires a pre-approved contact grant.
2. `send_message({"to":["owner/project/agent"],"body":"...", "idempotency_key":"stable-task-or-request-id"})` sends a persistent request. Reuse the **same** key when recovering an uncertain network/process result.
3. The receiver uses `fetch_inbox` and `reply_message`. The response is available in `get_thread` or the sender's inbox.
4. `get_mailbox_status` reports per-agent pending/delivered/acked/failed/expired counts.
5. After automatic attempts are exhausted, `list_failed_messages` lists failed receipt IDs and reasons. The recipient agent may call `retry_failed_message({"message_id":"msg_...", "confirm":true})` **only after** checking why the previous attempts failed.

## Safety, delivery semantics, and known limits

- Messaging is **at-least-once**, not globally exactly-once. Response and send idempotency reduce duplicate delivery, but cannot undo an external side effect that an AI/tool performed before crashing. External operations still require their own approval and idempotency.
- Five attempts is an automatic circuit breaker, not a guarantee that a model will eventually succeed. Retrying uses token/call budgets and may have a cost. Failed messages remain available for diagnosis.
- `get_mailbox_status` and `list_failed_messages` never return other agents' inboxes or raw message bodies. The receiving owner gets a failure notice, not the private content.
- Direct Agent-to-Agent MCP communication works between independent stdio server processes. **Real third-party model execution and external provider authentication were not exercised in the development container.** The included MCP subprocess tests use real JSON-RPC and SQLite but no remote model.
- A live QQ conversation with opt-in `agent_mail` can expose the MCP mailbox to supported Codex/Claude integrations. Automated ephemeral agent-inbox tasks still primarily perform request/response work; unrestricted autonomous multi-step tool-to-tool collaboration is **not yet validated**.
- Tokens and HMAC signing keys are only scoped by the host OS account. This release **does not** provide isolation from malicious code running as the same OS user. Use dedicated process identities/OS sandboxing for untrusted agents, not just Python checks.
- Full Windows/NapCat and real Codex/Claude end-to-end acceptance must be performed on an authorized local installation. No sign-in details or local secrets are packaged.

## Tests

```bash
python -m pytest -q tests/test_agent_mail_resilience.py tests/test_agent_messaging.py
python -m pytest -q tests/test_task_loop_closure.py tests/test_group_bot.py tests/test_literal_integrity.py tests/test_zh_edition.py tests/test_queue_governor.py tests/test_cockpit_server.py tests/test_agent_dispatch_real_path.py
python -X utf8 tools/package_release.py --check-only
```

For local Windows testing, **back up the existing databases** and run in a separate directory. The next practical acceptance test is: authorized QQ user → live agent mailbox MCP → another real Codex/Claude session → persistent reply → real task verification → QQ output.
