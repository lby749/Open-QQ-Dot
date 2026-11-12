# Open QQ Dot · BETA

Turn requests from QQ into tasks you can plan, execute and inspect on your own computer. Open QQ Dot provides a desktop console, per-friend personas and memory, editable multi-agent workflows, staged long-term tasks, research records, and a paired blue Android companion.

Current version: **1.0.0-beta.4**. This is a test release with documented limitations.

<p align="center"><img src="assets/brand/project-cover.png" width="360" alt="Open QQ Dot nebula cover"/></p>

## Download and start

Use the [release page](https://github.com/lby749/Open-QQ-Dot/releases/tag/v1.0.0-beta.4). Windows users can run the Setup EXE without preinstalling Python, or fully extract the portable ZIP and run `Open QQ Dot.exe`. Keep the portable application's resources together.

Android FOSS and GMS APKs require Android 13 or later and arm64. Choose one variant; both share a package ID. The phone application requires the desktop service and pairing. Preserve the installed application when upgrading with a matching signature.

## Main capabilities

- Plan tasks, call authorized tools, inspect artifacts, and verify outcomes.
- Organize multiple agents into editable branches with model, reasoning effort, role and reporting settings.
- Retain stages, budgets, checkpoints and evidence for long-running work and research.
- Reuse verified task workflows and corrections through editable workflows and task capsules.
- Separate each friend's memory, tasks, retrieval collections, caches and permissions while sharing model weights.
- Connect CLI and API providers and expose tools and runtime context on demand through MCP.

Data is managed locally. Requests to cloud providers still transmit the content needed for the selected operation. The desktop must remain running, and local command execution is not an operating-system sandbox.

## Documentation and status

See the [Chinese introduction](PROJECT_OVERVIEW.md), [feature catalogue](docs/FEATURES.md), [release notes](docs/RELEASE_BETA4.md), [known limitations](docs/KNOWN_ISSUES.md), and [contributor agent guide](AGENTS.md). The latter describes development boundaries; runtime agent integration is documented separately in [AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md).

Verification is specific to the tested scenarios. It does not cover every model, reasoning level, device, or multi-day unattended run. Native macOS/Linux packages and complete physical-phone acceptance remain unverified.

The project is still being improved. Please share issues and suggestions through [GitHub Issues](https://github.com/lby749/Open-QQ-Dot/issues/new/choose). Do not upload credentials, QR codes, or private conversations.

Code is distributed under [GPL-3.0-or-later](LICENSE); third-party components retain their respective licences in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Thank you for your support. If this project helps you, please give it a [STAR](https://github.com/lby749/Open-QQ-Dot).
