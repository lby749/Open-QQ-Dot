# Open QQ Dot: Prompt & Token Efficiency (2026-10-24)

This release reduces **input prompt size**, not a provider-guaranteed billing percentage.
No official tokenizer, vendor pricing API, or live cloud account was used in its benchmark.

## What changed

1. **Independent chat history budget.** Stateless API conversations no longer use half of the Codex session `compact_tokens` setting as the recent-message character budget. The new default is **24,000 characters**, independent of context-compaction size. The prior default could be as much as 128,000 characters when the compaction setting was 256,000 tokens. Each turn still retains its full latest inbound/reply round even if that alone exceeds the budget. Historical messages remain in SQLite and are eligible for incremental summaries and retrieval.
2. **Bounded prior inter-agent messages.** The *current* incoming request is always complete; up to six earlier thread messages now use bounded excerpts (max 1,000 characters per old body and 4,000 characters total for the historical snippets). The `THREAD_ID` and prior message IDs enable later retrieval from the mailbox by an authorized agent, where supported. The original mailbox entries remain untouched.
3. **Safe exact-duplicate elimination.** Subtask handoffs avoid repeating the complete mission in an identical subtask. Runtime retrieval doesn't inject substantial (80+ character) candidate text already present verbatim in the protected context. Similar but not exact material is **not** silently removed.
4. **Control in Web model settings.** New setting: **Stateless API recent-chat budget (characters)**.

## Configuration

Per-friend `friend.json`:

```json
{
  "model_api": {
    "provider": "custom",
    "recent_chat_char_budget": 24000
  }
}
```

- Default: 24,000 characters (balanced).
- Set 4,000–12,000 for short, low-cost chat; long context might become less immediately available.
- Set 48,000–128,000 for context-sensitive sessions; this uses more input tokens.
- Leave `compact_tokens` for the native Codex/Claude compaction logic. It is **not** a budget for a stateless API request.
- Existing files are not migrated or rewritten: missing/invalid recent-chat settings use the default, and deleting the field restores it.

## Offline reproducible character benchmark

Synthetic 120-round English chat, approximately 1500 characters per round:

| Path | Old-style/extended | Optimized | Reduced input characters |
|---|---:|---:|---:|
| Stateless API recent history | 127,888 | 22,860 | **82.1%** |
| Agent inbox history + current task | 49,822 | 7,256 | **85.4%** |

These are **character-count comparisons for selected portions** of the prompt using synthetic histories, *not* model token counts or whole-request cost reductions. For a short conversation or minimal Agent history, the savings are smaller or zero; output tokens, prompt cache savings, tools and extra calls change the overall bill.

## Guardrails and limits

- Current message text and user-approved system/persona rules are never truncated by this optimization.
- Most-recent conversation rounds are kept together; older details may require a summary or memory search. Check response quality on actual multilingual workloads before choosing a very small budget.
- Agent peer context includes excerpts, not a semantically perfect summary. The full history remains persistent in the mailbox; authorized clients must use supported message-retrieval tools to see it again.
- This release does **not** change model thinking effort, response-output limits, caching policy or paid subscription quotas, and does not claim external cloud savings without billing evidence.
- To measure real savings, compare the model API's reported `input_tokens`, `cached_input_tokens` and output tokens across matched workloads before and after deployment using the existing usage-accounting views.
