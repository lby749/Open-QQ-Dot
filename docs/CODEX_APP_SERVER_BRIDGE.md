# Codex App Server Task Bridge (2026-10-23)

This functionality is integrated into the official `TaskManager._default_orchestrated_executor()`, rather than existing solely as an isolated test module. It requires a local installation of the official Codex CLI with an active, Terms-of-Service compliant login state to execute authentic cloud tasks.

## Enablement

Standard QQ messaging continues to use the existing model router; do not replace the global chat backend to enable code tasks.

App Server is used exclusively when code/file tasks explicitly specify `initial_checkpoint={"backend":"codex_app_server",...}`. Example (for local test environments bound to authenticated users):

```python
manager.create_task(
    title="Test module improvement",
    description="Please repair foo.py in the authorized test project and run unit tests",
    category="code",
    owner_id="<authenticated QQ user id>",
    initial_checkpoint={
        "backend": "codex_app_server",
        "target_files": ["foo.py"],
        "test_command": "python -m pytest -q tests/test_foo.py",
    },
)
```

`friend.json` fields `computer_tools.enabled` and `computer_tools.commands` must be explicitly enabled in advance; `computer_tools.project_path` points to the authorized test workspace for the user. **User identity must be verified and injected by the QQ/Web server; agent self-declared identities must never be trusted.**

App Server threads use `workspace-write` (or task-specified `read-only`) sandboxing. Requesting `danger-full-access` via task JSON is strictly forbidden. Subprocesses lacking external privileges must fail authentically rather than using mocked success.

## Production Execution Sequence

TaskManager claims task -> Execution permission/budget checks -> Thread creation (`thread/start`) -> Thread ID checkpoint persistence -> Turn execution (`turn/start`) -> Progress and approval events -> File diff snapshot -> TaskVerifier validation -> Task state + experience archival.

- Only records genuinely modified file paths; tasks returning `COMPLETED` without target artifacts or test evidence will not produce fabricated verification success.
- Cancellation and pauses are delivered to the specific task via `turn/interrupt`, preventing disruption of shared App Server processes across other users.
- Calculates current turn usage from incremental `thread/tokenUsage/updated` values, avoiding duplicate accumulation of historical thread consumption.
- Command approvals require authentic user decisions; unapproved commands default to rejection.
- User identity, task ID, and session Thread cannot be cross-transferred; unverified historical threads cannot bypass authentication on resume.

## Verification Boundaries

In environments lacking active Codex CLI installations or live user login states, verified testing covers:

1. TaskManager standard worker operating through injected protocol clients, verifying production entrypoints, permissions, verification, and failure handling.
2. Authentic local JSON-RPC subprocess execution verifying handshakes, callbacks, event streams, and message results (this does not represent live OpenAI model calls).

Full verification on Windows 11 with test QQ credentials requires: Codex CLI startup and authentication, live `thread/start` and `turn/start`, actual test repository modifications, interruptibility, authentic NapCat round-trips, and quota accounting. Simulated JSON-RPC processes must not be reported as live cloud integrations.

## Current Retained Constraints

- Although Thread IDs are persisted in task checkpoints, **cross-process restart session recovery with strong authentication is not yet implemented**; retrying historical turns requires caution, isolated worktrees, and idempotent side-effect controls.
- App Server is shared across worker threads within a single host process; if an untrusted agent can execute arbitrary Python code under host privileges, it could bypass in-process logical constraints. Production should deploy restricted subprocess sandboxes, filesystem ACLs, and egress policies.
- Full end-to-end Windows execution across local models, GUI, and NapCat requires independent acceptance.
