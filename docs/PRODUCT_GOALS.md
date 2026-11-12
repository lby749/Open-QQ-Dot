# Open QQ Dot Product Goals, Five Core Scenarios, and Architectural Constraints (`PRODUCT_GOALS.md`)

**Updated**: 2026-10-22  
**Document Scope**: Long-term product mission, five core business scenario definitions, and non-negotiable engineering boundaries.

---

## 1. Product Positioning

Open QQ Dot is an **intelligent personal agent platform founded on conversational QQ messaging, supporting multi-user isolation, persistent memory, proactive execution, multi-agent collaboration, and cross-device automation**.

- It is not merely a chat bot, nor is it a superficial wrapper around an external CLI (such as Codex or Claude Code).
- Operating locally on the user's personal hardware, it continuously tracks user long-term goals and orchestrates diverse models, specialized subagents, MCP/Skills plugins, and local software tools (FFmpeg, browser automation, formal theorem provers, development toolchains) to independently execute complex tasks and deliver objectively validated results.

---

## 2. Five Core Product Scenarios

### Scenario A: Intelligent QQ Messaging (Foundational Scenario, Never Diminished)
- **Core Capabilities**: Direct and group message connectivity (NapCat OneBot 11), per-friend isolated personas and permissions, natural multi-bubble conversational flow, local sticker intelligent retrieval, multimodal image comprehension, proactive care and continuation, task completion notifications.
- **Three-Tier Lightweight Dispatch**:
  1. **Casual Chat**: Direct routing to lightweight conversational generation; invoking the full multi-agent orchestrator or creating heavy background tasks on everyday messages is strictly prohibited;
  2. **Immediate Status & Memory Correction**: In-chat queries ("how is the task progressing?"), and personal fact updates ("no, I moved to Shanghai" automatically sets outdated speculative memories to `is_valid=0` and writes high-confidence explicit memories);
  3. **Seamless Task Handoff**: Identifying long-running demands and smoothly transferring them to background asynchronous `TaskManager` execution, immediately releasing the interactive chat thread.

### Scenario B: Autonomous Software Development & Maintenance
- **Core Workflow**: Requirement analysis -> project inspection -> retrieval of cross-agent historical failure avoidance memories -> tool/MCP matching -> execution plan derivation -> invoking Codex / Claude Code to modify code -> running automated tests -> review and build -> delivering verifiable diffs and persisting verified experience.
- **Integrity Requirement**: Tasks failing tests or lacking objective verification evidence must never be archived as `SUCCESS_VERIFIED` / `HIGH_VERIFIED` in the cross-agent knowledge store.

### Scenario C: Video Pipeline Automation
- **Core Workflow**: Topic selection -> storyboard script generation -> voice synthesis (Edge-TTS / FFmpeg) -> SRT subtitle generation -> authentic FFmpeg audio/video rendering with soft subtitles -> `ffprobe` metadata and frame decoding quality inspection.
- **Integrity Requirement**: Missing FFmpeg must raise explicit errors; generating fabricated bytes pretending to be MP4 files is strictly forbidden; quality control requires genuine container parsing and frame decoding validation.

### Scenario D: E-Commerce Automation (Buyer and Seller Dual-Role)
- **Buyer Capabilities**: Cross-platform item discovery and price-performance scoring, order tracking and logistics updates.
- **Seller Capabilities**: Customer service reply draft generation, shipment fulfillment record creation.
- **Security & Authorization Boundaries**:
  - Order data is strictly isolated by `owner_id`; reading or updating orders belonging to other users or tampering with order ownership is prohibited;
  - Read queries (`ecommerce_query`) and write operations (`ecommerce_dispatch`, `ecommerce_payment`) are strictly segregated by permission tiers in `ToolRegistry`;
  - High-impact operations with external side effects (payments, dispatch) strictly require explicit idempotency keys (`idempotency_key`) and `ApprovalManager` human authorization; production mode must never return `PAID_SUCCESS` when no genuine payment gateway is configured.

### Scenario E: Autonomous Scientific Research
- **Core Workflow**: Long-term research topic maintenance -> authentic academic literature retrieval (arXiv / OpenAlex) -> local vector search and reranking -> generating daily digests with authentic citations and candidate innovation hypotheses -> multi-stage sandboxed execution and formal verification (SymPy / Lean 4).
- **Integrity Requirement**:
  - Failed literature queries must honestly report `retrieval_failed`; fabricating synthetic citations into the literature store is prohibited;
  - Strict distinction between finite numerical experiments (`EXPERIMENTALLY_SUPPORTED`), symbolic algebraic simplification, and universal formal proofs (`FORMAL_PROOF`); masquerading single instance assignments or irrelevant identities as universal mathematical proofs is strictly blocked.

---

## 3. Key Architectural Boundaries (Non-Negotiable)

1. **Multi-User Architecture and Strict Isolation**:
   - Multi-QQ user support is maintained across all layers; memories, tasks, workspace files, e-commerce records, crash logs, and browser cookies are strongly partitioned by `owner_id`.
   - When multiple users share personal subscription backends (such as personal Codex or Claude accounts), administrator risk confirmations and user first-use acknowledgments must be recorded (explicitly noting that risk confirmation does not equal vendor licensing).
   - Unauthenticated client claims passing `"admin"` or `"system"` string literals to bypass tenant permission checks are strictly blocked via server-side cryptographic/HMAC identity contexts.
2. **Local Embedding + Local Reranker Priority with Honest Degradation**:
   - Primary retrieval relies on local `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3` (or explicitly user-configured APIs) combined with SQLite FTS5 / BM25.
   - MD5 hash vectors masquerading as dense embeddings are forbidden; when local models are unavailable, return empty dense vectors and explicitly mark `degraded_lexical_fts5_bm25` degraded backend.
   - No mandatory resident local LLMs are introduced beyond documented configuration.
3. **Incremental Evolution and Component Reuse**:
   - Reuses `TaskManager`, `MultiAgentOrchestrator`, `AgentMemoryStore`, `ApprovalManager`, `ToolRegistry`, and `ResearchTracker` as single unified sources of truth, rather than inventing redundant scheduling or permission subsystems.
