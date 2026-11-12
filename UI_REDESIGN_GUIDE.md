# Open QQ Dot — Unified Workspace UI

## What changed

The desktop Qt panel and authenticated browser cockpit now use the same six primary workspaces:

| Workspace | Browser | Desktop Qt |
|---|---|---|
| Home | Real task, approval, agent and bridge snapshots | Operational overview and original friends overview |
| Tasks | Searchable tasks, statuses, redacted detail drawer | Task overview, existing development workbench, proactive/long-term plans |
| Agent Network | Registered agents, live sessions, mailbox status and collaboration metadata | Read-only collaboration overview, link to browser workspace |
| QQ & Conversations | Friends, group health, auto-reply switches | Existing QQ connection/status and chat/audit editors |
| Insights | Model usage, provider-reported prompt cache, mail failures and current logs | Read-only Token and cache overview |
| Settings | Safe queue maintenance and intent preview; settings guide | Full existing model, persona and memory editors in tabs |

Nothing in the existing native Codex, Claude, MCP, task, retrieval, token optimization, queue or prompt-cache code is deliberately replaced.

## Running the browser workspace

Run `python cockpit_server.py`, then visit `http://127.0.0.1:8790`.
The cockpit remains bound to loopback and requires HTTP Basic credentials: **username `admin`**, password stored locally in `<data_dir>/.cockpit_admin_token`. You may instead set `OPENQQ_COCKPIT_TOKEN` (at least 32 characters) before startup. Never paste the token into a shareable URL or commit the token file.

The UI does not auto-approve requests. Approvals must still happen through a real authorized session. Queue maintenance will not silently mark unread messages as handled. Message bodies, prompts, API keys and task output are not served by dashboard endpoints.

## Where did the old functions go?

The legacy editors are preserved in the desktop secondary tabs. The original `HouseWindow.show_friend(..., page=...)` and `HouseWindow.switchTo(...)` route old page names into their corresponding workspace tab, preserving deep links and memory unsaved-change protection.

## Important limitations

- A static HTML snapshot can be opened for layout inspection, but its API calls will fail outside the authenticated local server; this is not a program failure.
- The desktop Qt visual layout has not been rendered on a real Windows/PySide6 machine in this build environment. Test at 1050×700, 1440×900 and display scaling 125%/150%.
- Browser panels intentionally do **not** expose raw private QQ messages, authenticated inbox bodies or unverified model latency metrics.
- Editing provider credentials and trust-sensitive permissions remains on the original audited desktop pages, not new unverified browser endpoints.
- Existing TaskManager ownership/approval rules remain unchanged.
- Cache hit ratios are calculated from provider-reported usage rows; unavailable usage is displayed as unknown.

## Validation

Run `python -m pytest -q tests/test_ui_redesign.py tests/test_cockpit_server.py` for UI snapshots, local browser API authorization, truthfulness, and friend-toggle contract tests. Additional task, mail, group and prompt-cache tests are supplied in the repository.
