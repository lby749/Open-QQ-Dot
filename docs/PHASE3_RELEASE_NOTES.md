# Phase 3 — Cross-Agent Reliability Development (2026-10-24)

## Baseline

`Open-QQ-Dot-Unified-ZH-20261024.zip`, the shared Chinese/English codebase. All previously packaged features, UI locale switching and QQ/Agent integration modules are retained. No historical production user data is included.

## Implemented

1. `agent_messaging.py`: transactional delivery claims across independent SQLite clients; optional durable idempotency keys for sends; stable response idempotency after an interrupted acknowledgement; expired-lease recovery; automatic retry ceiling (five attempts); failed delivery visibility and explicit retry; passive replies/findings/notes preserved for live sessions; fail notices for the receiving owner without exposing the message text.
2. `task_manager.py`: atomic per-agent wake task enqueue (`BEGIN IMMEDIATE`) and automatic reconciliation every ~15s while the worker runs, so lost notifications and recoverable crashed leases can restart.
3. `agent_mailbox_mcp.py`: adds `idempotency_key` on `send_message` and MCP `get_mailbox_status`, `list_failed_messages`, `retry_failed_message` with explicit confirmation. All tools are bound to the authenticated MCP server identity, not an agent_id passed in tool input.
4. `tests/test_agent_mail_resilience.py`: concurrency, restart/idempotency, MCP-to-MCP protocol, automatic recovery, receipt exhaustion, failure notice, per-agent scoping, schema migration and passive message safety tests.
5. `docs/AGENT_MAIL_RELIABILITY.md` and bilingual README updates document operations, safeguards and caveats.

## Test evidence

The following two groups were independently executed successfully (no repeated test modules):

```
python -X utf8 -m pytest -q tests/test_agent_mail_resilience.py tests/test_agent_messaging.py
# 25 passed

python -X utf8 -m pytest -q tests/test_task_loop_closure.py tests/test_group_bot.py tests/test_literal_integrity.py tests/test_zh_edition.py tests/test_queue_governor.py tests/test_cockpit_server.py tests/test_agent_dispatch_real_path.py
# 74 passed, 12 subtests passed
```

A merged invocation of the same modules also completed with the earlier intermediate code state (98 passed, 12 subtests); the one additional passive-message regression was subsequently tested in the 25-test group. Linux CI environment has no authenticated NapCat/Claude/Codex connection; `tests/test_agent_mail_resilience.py` runs real independent MCP subprocesses, SQLite and TaskManager but uses a fake model CLI for one worker recovery test.

## Limitations and important next steps

- At-least-once messaging is **not exactly-once tool execution**; a process may crash after a third-party side effect. Tool- and business-level idempotency/approval remains mandatory.
- Codex/Claude provider sessions and QQ routing need Windows on-device full end-to-end acceptance with authorized user credentials; this release makes no claim that remote model collaboration was tested.
- Ephemeral inbox workers can answer bounded messages, but native proactive multi-tool peer coordination and process-level execution isolation remain incomplete.
- Same-user local untrusted code can still access the host's local credential files. OS process/file sandbox boundaries are not provided by this patch.
- Only partial regression coverage was run. Desktop PySide6 rendering, Windows tray integration, video rendering, ecommerce and research pipelines were not externally validated during this iteration.
