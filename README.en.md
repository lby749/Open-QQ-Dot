# Open QQ Dot

[Long-Term Goals Guide](docs/long_term_goals_guide.md) · [BETA.3 Validation](docs/BETA3_VALIDATION.md) · [Feature Roadmap](docs/FEATURE_ROADMAP.md)

<img src="assets/app-icon.png" width="96" alt="Open QQ Dot">

A local Windows personal AI assistant integrated with QQ, retaining the native desktop GUI, mobile web interface, independent vector memory, and proactive engagement, enhanced with multi-model endpoints and per-friend terminal permissions.

**A locally hosted personal companion that interfaces with multiple LLM backends and mimics customized conversational styles.**

- Overview & Features: [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)
- Contributor Guide: [AGENTS.md](AGENTS.md)
- Validation & Known Limits: [docs/BETA3_VALIDATION.md](docs/BETA3_VALIDATION.md)

All task files, workspaces, and pipelines reside on your local machine. Models connect via your chosen CLI accounts or custom APIs, with no cloud infrastructure required. Tasks cease when the host machine powers off.

---

## 1. Quick Start

1. Install Python 3.11 or newer on Windows.
2. Extract the codebase into a writable directory and launch `start.bat` or run:
   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File .\setup_environment.ps1
   ```
3. Installation requires zero local model weights. Keep the launcher, source scripts, and assets in the same root folder.
4. The QQ runtime is **not** downloaded by the installer. The first time you press Connect, Open QQ Dot asks for confirmation and then downloads the official NapCat Windows Node package, saving its full license and source record. See `THIRD_PARTY_NOTICES.md`.
5. Run `setup_environment.ps1` once to prepare the Python environment:
   ```powershell
   powershell -ExecutionPolicy Bypass -File setup_environment.ps1
   ```
6. Start Open QQ Dot. Open **QQ → QQ 直连 (DotLink)** and use a saved quick-login account or scan the QR shown in the window. No browser page is needed. Then bind friends and configure per-friend models and tool permissions.

If installation encounters issues, rerun `setup_environment.ps1`. The script creates a local `.venv/` and installs requirements without pulling external model weights.

---

## 2. Model Providers & Backends

In "Model Settings", select between **Codex CLI / Claude CLI / Antigravity CLI / Custom API**. Each backend manages its own parameters independently.

The API section is divided into three functional categories:
- **Model API**: Chat completions, synthesis, and reasoning. Compatible with OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages protocols.
- **Retrieval API**: Dedicated embedding and reranking endpoints, supporting remote endpoints or local lightweight models with configurable dimensions, pooling, and device targets.
- **Tool API**: Register GET/POST endpoints with JSON Schema specifications and static headers, exposed dynamically as tool calls to CLI and API agents.

Universal API keys are encrypted via Windows DPAPI and stored within the specific friend directory.

CLI users must authenticate their CLIs locally. Claude operates within a project-isolated `data/claude-home/`; Antigravity CLI can be provisioned via `tools/antigravity-cli-install.ps1`.

---

## 3. Local Inference Without Heavy Downloads

The local inference subsystem supports automatic fallback: **CUDA → MPS / Metal → CPU**, alongside strict manual device selection. See [Cross-Platform Local Inference](docs/local_inference_cross_platform.md).

By default, "Enable Installed Local Models" is disabled. Real-time chat, proactive outreach, mobile web, and memory stores remain fully operational using lightweight heuristic classification and BM25 search. See [Model Recommendations and Fallbacks](docs/model_recommendations_and_fallbacks.md) for optional local models.

---

## 4. Per-Friend Workspace & Host Permissions

Under "Model Settings → Local Workspace", manage permissions per friend:
- **Enable Local Workspace**: Off by default. Allows listing, reading, and writing files within that friend's dedicated directory under `workspaces/<QQ>/`, and launching URLs in the default browser.
- **Enable Terminal Commands**: Off by default. Authorizes execution of PowerShell commands up to a 90-second timeout.

Permissions are validated on every invocation. Granting permissions to Friend A confers zero access to Friend B.

File tools reject path traversal and external symlinks. **Command execution runs with the current Windows user privileges; the workspace directory is not a kernel sandbox.** Only enable commands for trusted contacts.

For complete tool definitions, see [Tools and Host Permissions](docs/tools_and_permissions.md).

---

## 5. Mobile House Web Dashboard

`house_web.py` and `house_web.html` provide mobile access via pairing QR codes or tokens. Remote access operates over trusted VPN or Tailscale networks with per-session bearer tokens.

---

## 6. Multi-Agent & Autonomous Tasks (Beta)

| Integrated Capability | Architectural Boundary |
|---|---|
| SQLite task queue, generations, pause/cancel, checkpoints | On system reboot, interrupted tasks pause for manual resumption; completed side-effects are immutable. |
| Multi-model DAG orchestration, concurrency limits, subtask checkpoints | End-to-end multi-CLI orchestration and dedicated Git Worktree isolation continue through iterative testing. |
| ToolRegistry with per-friend permissions | Restricted mode routes calls through sandboxed tools; elevated commands require explicit toggles. |
| Pre-execution budget checks, invocation counts, token accounting | Hard token budgets reject overages; unknown pricing structures queue for resource availability. |
| Scientific hypothesis tracking, evidence verification, stage reviews | Text summaries do not equate to formal proof; automated AST checks and Lean 4 verifiers gate conclusions. |
| Per-friend completion notifications | Notification queue buffers in memory; offline delivery does not resume after system shutdown. |

---

## 7. Quality Assurance & Release Validation

Run verification suites before packaging:

```powershell
python tools/run_release_checks.py
python tools/package_release.py --check-only
python tools/package_release.py --include-launcher --output ../Open-QQ-Dot-release.zip
```

The release packager automatically strips runtime caches, personal credentials, and database files while computing SHA-256 integrity hashes.

Documentation references:
- [Security Policy](SECURITY.md)
- [Contributing Guidelines](CONTRIBUTING.md)
- [Third-Party Notices](THIRD_PARTY_NOTICES.md)
- [Contributor Guide](AGENTS.md)

---

## 8. License

Open QQ Dot is licensed under the **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. External libraries and tools remain under their respective upstream licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## 2026-10-24 Agent mailbox reliability milestone

The existing cross-tool, cross-session Agent Mail now supports crash recovery/reconciliation, atomic wakeups and receipt leasing, durable idempotent sends and replies, bounded retry/dead-letter handling, owner notifications and MCP inbox diagnostics. See [`docs/AGENT_MAIL_RELIABILITY.md`](docs/AGENT_MAIL_RELIABILITY.md). The externally authenticated Codex/Claude and NapCat end-to-end workflows remain to be verified on Windows; this is **not** OS process isolation or an exactly-once side-effect guarantee.
