# Open QQ Dot Feature Roadmap and Progress

Last Updated: October 22, 2026.

Status Definitions:
- `NOT_STARTED`: Development has not yet commenced.
- `IN_PROGRESS`: Actively under development.
- `TESTED`: Unit and adversarial tests passing.
- `INTEGRATED`: Integrated into main pipeline with end-to-end regression validation.

---

## 1. Subsystem Status and Maturity Assessment

Evaluation Criteria: Functional Completeness (15), Pipeline Integration (25), Boundary Defense (25), Safety & Recovery (20), Real-World Environment (15).

| Subsystem / Module | Status | Score (100) | Verification Evidence | Next Milestones |
|---|---|---:|---|---|
| **Long-Running Research Agent** | **INTEGRATED** | **99 / 100** | `research_tracker.py`, `task_manager.py` (Remediated 5 P0 vulnerabilities; integrated local Lean 4.35.0 compiler with theorem verification and rejection of incomplete proofs). | Integrate Mathlib4 for advanced mathematical domains. |
| **Task Persistence & Background Worker** | **INTEGRATED** | **96 / 100** | `task_manager.py` (Auto-dispatch for research tasks, stage re-queuing, dedicated background worker, process tree termination). | Long-term observation under high network jitter. |
| **Multi-Agent Orchestration** | **INTEGRATED** | **90 / 100** | `multi_agent_orchestrator.py`, `research_tracker.py` (Planner / Executor / Verifier / Coordinator loop). | Evaluation of large-scale parallel code refactoring. |
| **Proactive Event Engine** | **INTEGRATED** | **90 / 100** | `agenda.py` (Isolated per-target dispatching, deduplication of state changes, persistent disabled flags). | Ingest live GitHub / webhook events. |
| **Long-Term Goal Management** | **INTEGRATED** | **93 / 100** | `task_manager.py` (Diagnostic logging on blocks, automatic revival on revision, failure decoupling). | Link with agent pitfall memory store. |
| **Agent Memory & Isolation** | **INTEGRATED** | **92 / 100** | `agent_memory.py` (Tenant `owner_id` filtering, fallback defaults, pitfall retrieval loop). | Vector store scaling and long-context summarization. |
| **Cross-Device Sync & Notifications** | **INTEGRATED** | **89 / 100** | `notification_manager.py`, `house_web.py`, `main.py` (RBAC authorization on notifications, named QQ sink preventing re-send duplicates). | Mobile Web Push and live QQ bot notification testing. |
| **Host Tools & Permission Mapping** | **INTEGRATED** | **97 / 100** | `task_runtime.py`, `workspace_tools.py`, `research_tracker.py` (Unified execution context, PID lifecycle, path traversal guards). | Additional OS-level container isolation. |
| **Cross-Platform Local Inference** | **TESTED** | **82 / 100** | Windows CPU/CUDA and macOS MPS dispatch policy tests verified. | Live memory pressure testing on Apple Silicon. |
| **Security & Release Packaging** | **INTEGRATED** | **98 / 100** | `package_release.py` (Secret scanning, runtime artifact omission, automated packaging). | Continuous integration testing on remote GitHub runners. |
| **Dedicated Mac / iOS Client** | **NOT_STARTED** | **--** | Scheduled following core agent loop maturation. | Evaluate lightweight mobile or native client wrappers. |

---

## 2. Core Defect Remediation & Lean 4 Integration

- **Test 1 (Symbolic Proof Fallback Removal)**: PASS. Eliminated unconditional `return True, ""`; symbolic statements without verification return `WAITING_VERIFICATION`.
- **Test 2 (Formal Proof Spoofing Defense)**: PASS. Rejects attempts by basic Python/SymPy scripts to spoof `FORMAL_PROOF`.
- **Test 3 (Self-Declaration Interception)**: PASS. Intercepts scripts printing bare `claim_verified: True` and categorizes them as `UNTRUSTED`.
- **Test 4 (Unknown Problem Fallback Elimination)**: PASS. Unknown research topics return empty code and transition to `NEEDS_PLANNING`.
- **Test 5 (Numerical Evidence Downgrade)**: PASS. Stage evidence strictly retains original tiers; bounded numerical data cannot elevate to `VERIFIED`.
- **Test 6 (RBAC Command Interception)**: PASS. Unauthorized requests (`commands=False`) are blocked deterministically.
- **Test 7 (Irrelevant Telemetry Rejection)**: PASS. System CPU/memory telemetry cannot serve as proof for formal mathematical hypotheses.
- **Test 8 (Real Lean 4 Theorem Verification)**: PASS. Successfully invoked local Lean 4.35.0 compiler, validating theorem of commutativity of addition over natural numbers.
- **Test 9 (Lean 4 Counterproof Interception)**: PASS. Type-mismatched proofs and proofs with `sorry` placeholders are rejected by the compiler.

---

## 3. Future Roadmap

1. **Academic Retrieval Integration**: Provide search API access to `ResearchTracker` to cross-validate findings against external academic repositories.
2. **Lake & Mathlib4 Configuration**: Support Lake package builds for deep algebraic structures.
3. **Advanced LLM Code Generation**: Connect `NEEDS_PLANNING` states with specialized coding models via `ModelDispatcher`.
