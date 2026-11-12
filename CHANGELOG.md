# Changelog

All notable changes to this project will be documented in this file.
This project adheres to [Semantic Versioning 2.0.0](https://semver.org/).

---

## [Unreleased] - Round 13 Self-Audit & Security Hardening (2026-10-22)

### Fixed
- **VULN-01 (research_tracker.py)**: Eliminated fallback to `VERIFIED` on unsupported proof types; now returns `INCONCLUSIVE` with explicit missing-verifier warning.
- **VULN-02 (research_tracker.py)**: Blocked direct `update_finding_status` privilege escalation to `VERIFIED`; formal conclusions must traverse through `verify_finding`.
- **VULN-03 (research_tracker.py)**: Added `owner_id` authorization checks on `update_finding_status` to stop cross-tenant status modification.
- **VULN-04 (research_tracker.py)**: Completely removed silent status promotion backdoor in `record_stage_progress`.
- **VULN-05 (research_tracker.py)**: Added owner enforcement to `add_hypothesis` and `record_counterexample` to prevent cross-tenant hypothesis poisoning.
- **VULN-06 (research_tracker.py)**: Bound `schedule_next_stage` and `list_evidence` to tenant identity to prevent unauthorized scheduling and evidence snooping.

---

## [2.0.0-beta.12] - 2026-10-22

### Added
- Native `CodexAppServerClient` integrating official `codex app-server --listen stdio://` JSON-RPC 2.0 async protocol (`codex_app_server.py`).
- Runtime steering (`turn/steer`) and immediate turn interruption (`turn/interrupt`).
- Multi-user personal subscription terms acknowledgment workflow in `ApprovalManager`.
- Automated real-world end-to-end verification suite (`tests/test_e2e_codex_app_server_full_flow.py`).

### Fixed
- Counterexample detection rejecting invalid numeric propositions (e.g., rejecting claims that all evens > 10 when given [2, 4, 6]).
- AST semantic contract binding in `_check_symbolic_proposition_binding`, rejecting trivial identity checks (`assert 1 == 1`).
- Prevented research project hijacking caused by `ON CONFLICT` updates on `owner_id`.

---

## [2.0.0-beta] - 2026-10-21

### Added
- Multi-model DAG task orchestrator (`multi_agent_orchestrator.py`) supporting Planner, Dispatcher, and Verifier cycles.
- Persistent 10-state task state machine with crash recovery in `task_manager.py`.
- Tool sandboxing and path traversal guards in `tool_registry.py`.
- Historical troubleshooting and pitfall memory store in `agent_memory.py`.
- Token hard budget and execution heartbeat tracking.
- Per-friend resource-level RBAC authorization in `house_web.py`.
- Automated release packaging script with credential scanning in `tools/package_release.py`.

---

## [1.0.0] - Baseline Release

- Desktop native Qt / Fluent interface for QQ friend management.
- Integration with NapCatQQ via OneBot 11 WebSocket protocol.
- Style template extraction and persona imitation.
- Local vector and keyword memory stores.
- Mobile Web dashboard (`house_web.py`).
