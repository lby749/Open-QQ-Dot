# Open QQ Dot System Architecture & Data Flow (`ARCHITECTURE.md`)

**Updated**: 2026-10-22 (Round 14)
**Target Release**: Open QQ Dot v2.8 (P0 Root-Cause Governance & Production Integration)

---

## 0. Round 14: verified status and the loops that now close

Read this section first. The original layout below is still accurate, but its "Integrated" labels were not verified. This table is.

Evidence key:
- **test-real**: driven through production code with real processes and databases; only the model CLI or the QQ transport is faked.
- **test-mock**: covered only with patched functions.
- **live**: run against real Codex/Claude/QQ. Round 15 (2026-10-23) ran the first live checks with Codex CLI 0.162.1 (see `tools/e2e_live.py`); Claude CLI is not installed on the dev machine and no real QQ session was run.

| Component | Status | Evidence |
|---|---|---|
| Shared data dir (`house_settings.data_dir()`) | All stores resolve `QQ_BRIDGE_HOME/data`, never the cwd | test-real |
| TaskManager single worker | One worker per task DB via an OS file lock (bridge and panel can't both run the queue) | test-real |
| QQ task creation (`main.make_reply` + `task_spec.py`) | Bilingual intents (`lexicon.py`). The checkpoint carries test command, target files, URL and order id from the owner's message | test-real |
| Agent dispatch (`call_single_agent` → `main.codex_text`) | One router for chat and workers. Task failures raise instead of returning error text | test-real (fake CLI) |
| Approvals (`ApprovalManager`) | Waits poll the shared DB (decisions from any process). Owner notified. `ApprovalPending` parks the task. Approved "once" requests are consumed exactly once | test-real (2nd OS process) |
| QQ decisions (`main.handle_approval_command`) | `批准`/`拒绝`/`approve`/`reject <apr_…/task_…/grant_…>`, `待审批`. Channel identity for the authenticated sender; owner-only, never admin | test-real |
| Codex approvals in tasks (`app_server._answer_server_request`) | Routed to the owner instead of failing the turn. Owner wait does not consume the turn deadline | test-real (fake transport) |
| TaskVerifier (code) | Junk paths ignored; project tests detected and re-run; changes without an objective check go to owner review | test-real |
| Result delivery | Completed (with the result), failed, needs-review notices. The bridge flushes pending notices every 30 s | test-real (capture sink) |
| Agent mailbox (`agent_messaging.py`, `agent_mailbox_mcp.py`) | See §0.2 | test-real (fake codex + fake claude) |
| QQ code task end to end (M1) | Real Codex fixes a failing test through the permission-checked desk tools; TaskVerifier re-runs the tests independently; owner notified | **live** (3/3 runs) |
| Agent mailbox, two agents | Builder → reviewer request answered by a woken real Codex agent; receipt acked | **live** |
| Supervisor mode (Mode B, `agent_messaging.Collaboration`) | `orchestrate` records each task as one mailbox thread: handoff per subtask, worker reply (`response` if verified, else `finding`), failover findings, peer findings injected as untrusted context, synthesis note; thread id in the checkpoint | test-real (fake codex + fake claude) |
| Group assistant (`group_bot.py`, `web_search.py`) | Summoned in opt-in QQ groups; context, fact-check, live search with sources, images, summaries, admin settings; local model or agent CLI backends. See `docs/GROUP_BOT.md` | test-real; Codex backend and agent search **live** |
| Cross-agent experience (`DynamicContextBuilder`) | Still written but never read in production | open |
| Ecommerce / video storyboard / research hypotheses | Simulated or templated output | open |
| GUI modules (`house_qt/`, `*_gui.py`, `house_status.py`, `house_web.py` text) | Translation damage not yet repaired | open |

### 0.1 The QQ task loop

```text
QQ "任务: 修复解析；测试: pytest -q"
  -> make_reply: lexicon intent -> task_spec checkpoint -> TaskManager.create_task -> reply "Queued … you'll get a QQ message"
  -> worker (single, file-locked) -> executor -> agent via call_single_agent / Codex app-server
       |- tool or Codex approval -> ApprovalManager request -> owner notified over QQ
       |     -> in-line wait (cross-process DB poll) or ApprovalPending -> task WAITING_APPROVAL (worker freed)
       |     -> owner replies 「批准 apr_…」 (QQ) or decides in the panel (any process)
       |     -> on_approval_decided -> task PENDING -> re-run consumes the approved request
  -> TaskVerifier: target files / re-run tests / else owner review
  -> COMPLETED (notice with result) | FAILED (notice with error) | WAITING_APPROVAL (notice: 「批准/拒绝 task_…」)
  -> bridge flush_notifications() delivers notices published by any process
```

### 0.2 Agent-to-agent messaging (MVP)

Design and prior art are in `docs/AGENT_COMMS_RESEARCH.md`.

- **Identity:** `owner/project/name` (stable), with provider, capabilities and a description. Registered by code, or by a friend's `friend.json`:

  ```json
  "agent_mail": {"enabled": true, "project": "shop", "name": "builder", "capabilities": ["code"], "wake": true}
  ```
- **Delivery:**
  - Per-recipient receipts: pending → delivered (leased) → acked. A lapsed lease is redelivered.
  - Messages persist across restarts in `data/agent_mail.sqlite3`.
- **Mode A (direct):**
  - Live sessions get the `agent_mailbox` MCP server: `list_agents`, `send_message`, `fetch_inbox`, `acknowledge_message`, `reply_message`, `get_thread`, `request_contact`.
  - Identity is bound by a signed token at server start.
  - An idle agent with `wake: true` is woken by an `agent_message` task that answers its inbox. TaskVerifier checks the receipts.
- **Boundaries:**
  - Same owner and project: allowed.
  - Anything else needs a contact grant that the receiving owner approves (QQ `批准 grant_…`).
  - Discovery never crosses a boundary without a grant.
- **Limits:** 8 hops, 60 messages per thread, 30 sends per minute per agent, 32 KB bodies, 7-day TTL.
- **Security:** bodies are untrusted data. They are rendered in an envelope and can never approve anything.
- **Mode B (supervisor):** inside a task, `orchestrate` opens one thread per task with supervisor `owner/<task_id>/supervisor` and workers `owner/<task_id>/<provider>`. Other agents of the same owner and project (or across a grant) can post findings into it; the next worker sees them as untrusted `PEER_FINDINGS`.
- **Not yet built:**
  - No search over messages.
  - No advisory file leases.
  - No A2A gateway.

---

## 1. Overall System Layering

```text
+---------------------------------------------------------------------------------------------------+
|                                 Ingress & User Interaction Layer                                  |
|  [NapCat OneBot 11 WS] ---> main.py (Bridge.receive / make_reply)                                 |
|  [PySide6 Desktop GUI] ---> gui.pyw / house_qt/ (window.py, agent_console_widget.py, pages_*.py)  |
|  [Remote Mobile Web]   ---> house_web.py / house_web.html (Bearer Token + Tailscale/Loopback)     |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
|                              Three-Tier Intent Dispatch & Chat Core                               |
|  Layer 1: Casual Chat & Emotional Bonding ---> main.py:make_reply -> Stickers/Memory -> Direct LLM |
|  Layer 2: Lightweight Queries & Fact Updates ---> Task Status / Fact Invalidation / Topic Search  |
|  Layer 3: Complex Asynchronous Task Dispatch ---> TaskManager.create_task (video/ecom/research/...) |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
|                        Persistent Task Scheduling & Multi-Agent Orchestration                     |
|  - task_manager.py: SQLite persistent queue, run_id leasing, DAG cycle checks, restart recovery   |
|  - task_runtime.py: Execution context, workspace isolation, subprocess PID + BirthTime tracking   |
|  - multi_agent_orchestrator.py: Planner -> Executor -> Verifier -> Coordinator feedback loop      |
|  - DynamicContextBuilder: 6-layer dynamic context assembly with token budget enforcement           |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
         +----------------------------------------+----------------------------------------+
         v                                        v                                        v
+----------------------------------+  +------------------------------------+  +----------------------------------+
| Specialized Agents & Backends    |  | Controlled Tools & Pipelines       |  | Local Search, Memory & Security  |
| - model_dispatcher.py            |  | - tool_registry.py (ToolRegistry)  |  | - local_retrieval_engine.py      |
| - codex_app_server.py (JSON-RPC) |  |   * system_info / read_file        |  |   * FTS5 + BGE-M3 + Reranker     |
| - app_server.py (Codex Chat)     |  |   * write_file (WORKSPACE_WRITE)   |  |   * Honest degraded reporting    |
| - claude_backend.py (Claude CLI) |  |   * run_command (SENSITIVE_EXEC)   |  | - embedding_provider.py (BGE-M3) |
| - antigravity_backend.py (agy)   |  |   * browser_navigate / interact    |  | - reranker.py (bge-reranker)     |
| - api_backend.py / dsh_backend.py|  |   * video_produce (FFmpeg QC)      |  | - agent_memory.py (4-tier store) |
|                                  |  |   * ecommerce_query (SAFE_READ)    |  | - approval_manager.py (ACL / RBAC)|
|                                  |  |   * ecommerce_dispatch / payment   |  | - research_tracker.py (Prover)   |
|                                  |  |   * academic_research_digest       |  | - agenda.py / notification_mgr   |
+----------------------------------+  +------------------------------------+  +----------------------------------+
```

---

## 2. Core Call Flows

### 1. Ingress Message Handling & Model Routing
1. **Ingress**: `napcat_ws.py:NapCat` connects to the local NapCat OneBot 11 WebSocket (strictly validating loopback origins), delivering events to `main.py:Bridge.receive(event)`.
2. **Multi-User Isolation**: Extracts `user_id` (QQ number) to load isolated storage (`store.py` SQLite database) and individual configurations from `friends/<qq_id>/friend.json`.
3. **Three-Tier Intent Dispatch (`main.py:make_reply`)**:
   - **Branch 1 (Task Status Inquiries)**: Keywords matching task status queries query `TaskManager.list_tasks(owner_id=owner_qq_id)` directly, returning live status without waking heavy orchestrators.
   - **Branch 2 (User Fact Correction)**: Prefixes matching personal corrections ("no, I...", "correction:...") invoke `AgentMemoryStore.store_memory(..., source=USER_EXPLICIT, confidence=1.0)` and automatically set outdated contradictory memories to `is_valid=0`.
   - **Branch 3 (Asynchronous Complex Tasks)**: Explicit `/task` commands or recognized long-running goals (coding, video rendering, e-commerce price checks, research literature synthesis) route to `TaskManager.create_task` for background queueing, returning task IDs immediately.
   - **Branch 4 (Lightweight Chat)**: Assembles persona prompts, candidate stickers (`sticker_library.py`), valid memories (`is_valid=1`), and topical context (`topic_research.py`) to generate 1–4 conversational reply bubbles.

### 2. TaskManager Execution, Verification, and Recovery
1. **Queue Leasing & Concurrency Control**: `TaskManager._worker_loop` polls pending tasks satisfying DAG dependencies (`_fetch_next_executable_task` with DFS cycle detection). `_process_single_task` locks tasks atomically using `BEGIN IMMEDIATE`, updating `run_id`, `worker_pid`, and `worker_birth` (process creation timestamp preventing PID reuse bugs).
2. **Execution Dispatch (`_default_orchestrated_executor`)**:
   - `category == 'video'`: Invokes `VideoAutomationPipeline.run_full_pipeline` (storyboard -> TTS -> SRT -> FFmpeg rendering -> `ffprobe`/decoding QC);
   - `category == 'ecommerce'`: Invokes `EcommerceAutomationPipeline` (price evaluation, tenant-checked order tracking, customer service drafting);
   - `category == 'browser'`: Invokes `get_browser_session(task.owner_id)` with isolated storage under `data/browser_sessions/<owner_id>/`;
   - `category == 'research'`: Invokes `ResearchTracker.run_autonomous_stage_cycle` for hypothesis generation, sandboxed experiment execution, and proof validation;
   - `category == 'code'` / `'general'`: Invokes `multi_agent_orchestrator.orchestrate`.
3. **Objective Verification Gate**: Completed runs enter `VERIFYING`, evaluated by `TaskVerifier.verify`. Only when `verification_info.get("success") is True` with genuine artifacts or test results does `update_task_status` record experience as `SUCCESS_VERIFIED` / `HIGH_VERIFIED`. Unverified completions degrade to `EXECUTION_FINISHED` / `LOW_UNVERIFIED`.
4. **Crash Recovery**: Host restarts trigger `_recover_interrupted_tasks` to inspect `(worker_pid, worker_birth)`. Dead tasks transition to `PAUSED` while retaining checkpoints to prevent accidental non-idempotent replays.

### 3. Model Dispatcher, Agent Backends, and Authorization
1. **Model Selection (`model_dispatcher.py`)**: Selects `codex` (`codex_app_server.py` JSON-RPC or CLI), `claude` (`claude_backend.py` with isolated `data/claude-home/`), `antigravity` (`antigravity_backend.py`), or custom OpenAI/Anthropic APIs (`api_backend.py`) based on task category and friend configuration.
2. **Tool Permissions and Approvals (`tool_registry.py` + `approval_manager.py`)**:
   - Tools declare explicit `ToolPermissionLevel` (`SAFE_READ`, `WORKSPACE_WRITE`, `NETWORK_FETCH`, `SENSITIVE_EXEC`). Read and write operations are strictly partitioned.
   - Filesystem I/O enforces directory boundary checks (`validate_path_in_bounds`), resolving symlinks to prevent directory traversal.
   - Sensitive operations (shell execution, financial payments, order fulfillment, subscription sharing) require `ApprovalManager` approval tickets bound to `owner_id`. Unauthenticated claims of `"admin"` or `"system"` throw `PermissionError`.

### 4. Local Hybrid Retrieval & Reranking Architecture
1. **One scheduler (`retrieval_core.py`, 2026-10-26)**: every module that retrieves goes through it.
   - Model choice: the five embedding and four reranker presets (`small_models.PRESETS`); a friend's `retrieval_api` setting or the house override (`house.local.json` → `retrieval_models`) first, then by task, language, amount of text and free GPU memory. Only installed local models are chosen automatically.
   - Caches: query vectors and rerank pairs in memory, document vectors on disk keyed by model signature.
   - Shared hygiene: small-talk lines are not searched, rerank only a fused shortlist, relevance floors, dynamic top-K, near-duplicate removal (MMR), identifier check for task dedupe; `rank_texts()` runs the whole pipeline for plain collections.
2. **Callers**:
   - Conversational modules: `hybrid_search.py` (BM25 + dense + RRF, reranked through the scheduler; gated recall for long-term memory and the topic library; one vector space per model in `cached_vectors`).
   - Agents: `local_retrieval_engine.py` (experiences, tool discovery, task dedupe, failure diagnosis, task history; stored vectors carry their model signature and are re-embedded when it changes), `agent_memory.py` (fix memory, version history) and `memory_hub.py` (cross-agent recall with kinds, provenance and permission rules).
   - A QQ reply retrieves memory, topics and template examples side by side after one shared query-embedding batch (`prompt_context.retrieved_blocks`).
3. **Honest Degradation**: When local models or retrieval APIs are unavailable, `compute_embedding` returns empty vectors `[]` (never fabricating synthetic hash vectors) and explicitly reports `degraded_lexical_fts5_bm25` in `get_backend_report()`; each search falls back to BM25 with a shared-word requirement.
4. **Measurement**: `tools/retrieval_benchmark.py` (synthetic corpus in `tools/retrieval_bench_data.py`, results in `docs/retrieval_bench/`); report: `docs/检索与记忆统一优化_20261026.md`.

---

## 3. Production Component Mapping

| Module File | Primary Responsibility | Ingress Point | Status |
| :--- | :--- | :--- | :--- |
| `main.py` | NapCat WS bridge, three-tier intent dispatch, chat prompt assembly, proactive care loop | Application main / `make_reply` | Integrated |
| `task_manager.py` | SQLite task queue, DAG dependencies, budget controls, checkpoint recovery | `main.py`, `house_web.py`, `agenda.py` | Integrated |
| `multi_agent_orchestrator.py` | Planner/Executor/Verifier/Coordinator orchestration, dynamic context injection | `TaskManager._default_orchestrated_executor` | Integrated |
| `local_retrieval_engine.py` | Hybrid search, cross-agent experience store, capability discovery, research digest | `main.py`, `task_manager.py`, `multi_agent_orchestrator.py` | Integrated |
| `tool_registry.py` | Shared tool registration, schema validation, path boundary containment | `multi_agent_orchestrator.py`, `workspace_tools.py` | Integrated |
| `approval_manager.py` | Sensitive operation approval tickets, user-scoped approvals, subscription risk logs | `tool_registry.py`, `codex_app_server.py`, `house_web.py` | Integrated |
| `research_tracker.py` | Long-term research projects, hypothesis tracking, sandboxed experiments, Lean 4 verification | `task_manager.py`, `house_web.py` | Integrated |
| `browser_automation.py` | User-isolated browser sessions, DOM interaction validation, CAPTCHA takeover | `tool_registry.py`, `task_manager.py` | Integrated |
| `video_ecommerce_pipeline.py` | FFmpeg video synthesis and stream QC, tenant-isolated e-commerce pipelines | `tool_registry.py`, `task_manager.py` | Integrated |
| `house_qt/` & `house_web.py` | PySide6 desktop administration GUI and authenticated remote web interface | `gui.pyw`, `main.py` | Integrated |

---

## 4. Codex App Server TaskManager Integration

- `TaskManager._default_orchestrated_executor` routes to `_execute_codex_app_server` when code/file tasks specify `checkpoint.backend=codex_app_server`.
- Call chain: TaskManager -> Execution identity/command privileges -> App Server JSON-RPC `initialize` -> `thread/start` -> `turn/start` -> notifications & approvals -> local workspace diff snapshot -> TaskVerifier -> persistent task/experience archival.
- `trusted_admin_policy` reads server-side admin ACLs from environment variables or `house_web.local.json`; `friend.json.is_admin` is not an authorization basis.
- Browser automation in TaskManager enforces genuine Chromium/Playwright execution; network or dependency failures must report authentic errors without placeholder screenshots.
- Full setup instructions can be found in `docs/CODEX_APP_SERVER_BRIDGE.md`.
