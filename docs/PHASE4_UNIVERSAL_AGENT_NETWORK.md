# Phase 4: Universal, Provider-Neutral Agent Interoperability (2026-10-24)

## Scope

Extend the previous Chinese/English shared core rather than creating provider-specific Claude↔Codex code.
The same persistent, owner-isolated mailbox now has an MCP endpoint and a **local HTTP/JSON
transport** consumable by independent CLI, IDE or custom Agent wrappers. All agents share the
same registry, contacts, receipts, threads and retries.

### Code changes

- `agent_messaging.py`: persistent `agent_sessions` table, online presence, owner-bound session
  heartbeat/close, wait/lease inbox, short-lived signed `v2` credentials. Legacy `v1` MCP tokens
  remain accepted for compatibility, but HTTP requires `v2`.
- `agent_mailbox_mcp.py`: structured handoff, presence tools and `wait_for_inbox` while retaining
  legacy tool contracts. Fixed a legacy MCP initialization call referencing a nonexistent method.
- `agent_interop_contract.py`: validates and serializes `openqqdot.agent-handoff.v1` including
  objectives, acceptance criteria, resource limits and safe artifact references. Status explicitly
  remains `unverified_request` until genuine downstream verification.
- `agent_interop_gateway.py`: loopback-only `127.0.0.1` HTTP adapter. Bearer-bound identity,
  Origin/Host checks, 64 KiB request cap, tool schema validation, capped long-poll concurrency,
  no arbitrary command execution or public remote listener.
- `agent_interop_client.py`: standard-library SDK with peer discovery, messaging, long-polling,
  reply, structured handoff and tool discovery. Does not require a particular model or IDE vendor.
- `tests/test_agent_interop_universal.py`: real gateway subprocess, independent HTTP client
  subprocesses and real MCP server subprocess, persistence across gateway restart, auth refusal,
  contact grants, tenant isolation, presence, handoffs, concurrent receipt and token expiry.
- `docs/UNIVERSAL_AGENT_INTEROP.md`: usage, architecture and threat model.

### Repeated test evidence

| Group | Actual result |
| --- | --- |
| New interoperability, original Agent mailbox and resilience | 33 passed |
| Group chat + literal/translation integrity | 34 passed |
| Agent task dispatch | 4 passed |
| QQ task closure | 14 passed |
| Queue governance + cockpit | 15 passed, plus 12 subtests |
| Python source syntax | 415 files, 0 failures |

**Total of separately completed groups: 100 pytest cases passed, plus 12 additional subtests.**
A single combined run of all selected groups hung previously, so they were executed in isolated
pytest processes. This does not claim a fully passing *entire-project* regression suite.

### Real vs simulated

Real execution: SQLite DB transactions, separate local MCP and HTTP gateway processes, separate
Python HTTP clients, cross-transport handoff + response, restart persistence, actual HTTP auth
and authorization failures. **No third-party cloud models were called.** There is no verified
live Codex/Gemini/Claude/OpenCode or IDE-session automation in this phase. Self-reported transport
and presence do not prove that a vendor agent was genuinely attached.

### Known limitations

- Existing host-signed identities and the shared SQLite database are **not** a security boundary
  against nontrusted code with the same OS account/file permissions. Use a restricted OS user,
  container or VM for genuinely untrusted Agent tools; never expose the HTTP port publicly.
- No cross-device TLS federation, native A2A compliance, remote CLI installation or direct
  proprietary IDE session management.
- Sessions expire on the presence TTL; live credentials require host-side provisioning/rotation.
  Legacy MCP validates its token at startup only.
- The handoff contract is unverified intent. ApprovalManager and TaskVerifier must still authorize
  and verify any consequential actions. Idempotency of mailbox messages is not an exactly-once
  guarantee for external side effects.
- GUI/NapCat and production cloud-agent integration must be validated on the user's Windows host;
  the full project's complete test suite was not run in this phase.
