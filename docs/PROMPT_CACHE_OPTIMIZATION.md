# Prompt Cache Optimization / 上下文缓存优化

This edition builds on **Open-QQ-Dot-Low-Latency-20261024**. It aims to improve **provider-side prompt prefix reuse**, not merely shorten prompts.

## Changes / 本次改动

1. **Stable layout for standard friend-chat prompts.** For prompts rendered with a real `【SYSTEM_RULES】 SOURCE=program TYPE=RULE` block, move unchanged rules and profile blocks ahead of retrieval, recent chat, task instructions, and other volatile data. The text stays in the **user** trust level; user-authored style and profile information are never upgraded to `system` or `developer` permissions.
2. **OpenAI-compatible Chat Completions:** put stable and dynamic text into separate user messages. Official OpenAI gets a tenant-scoped opaque `prompt_cache_key`; DeepSeek uses its automatic prefix cache without nonstandard request fields.
3. **Official OpenAI Responses:** attach an explicit cache breakpoint to the reusable user message for GPT-5.6+ family models; older models retain implicit caching. No unsupported cache options are sent to third-party gateways.
4. **Official Anthropic Messages API:** attach a five-minute ephemeral `cache_control` marker to the reusable first user text block; tool results and volatile context follow it. Third-party Anthropic-compatible endpoints get no Anthropic-specific marker by default.
5. **Stable tool ordering:** sort tool schemas by name when building API calls. Tool definitions may precede the prompt in provider cache keys, so changing their order wastes cache reuse.
6. **Correct cache accounting:** recognize OpenAI `prompt_tokens_details.cached_tokens`, Responses `input_tokens_details.cached_tokens`, DeepSeek `prompt_cache_hit_tokens`, and Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`. Anthropic input totals include read and written cache tokens, unlike normal `input_tokens` alone.
7. **Verified-only usage reporting:** use the per-friend history database. The new CLI ignores calls lacking provider usage rather than reporting them as misses.

## Configuration / 配置

In an individual friend's `friend.json`, within `model_api`, optionally set:

```json
{"prompt_cache_mode": "auto"}
```

- `auto` (default): enable the layout only for HTTPS official `api.openai.com`, `api.anthropic.com`, and `api.deepseek.com`.
- `off`: keep the legacy single-user-message prompt format.
- `on`: also split prompts for manually configured OpenAI/Anthropic-compatible gateways. **Only use after confirming their API accepts the format.** Provider-specific caching parameters still apply only to official hosts.

Codex CLI and Claude Code session history is **not** rewritten by this patch. These clients control their own sessions and caching. This change targets custom model APIs and does not bypass model context size or tenant permissions.

## Measure real API hits / 查询真实命中率

From the project folder, run:

```bash
python tools/cache_usage_report.py data/friends/FRIEND_DIRECTORY --calls 100
```

Replace the path with an actual friend directory containing `history.sqlite3`. The report prints the model's **provider-reported cached input / provider-reported total input**, not prompt character overlap or a guessed invoice discount. It does not contact external services and does not print chat text or API keys.

New usage records include `usage_source=provider` or `usage_source=estimate`. The report excludes estimated and older unverified records. Consequently, an empty report does **not** mean a 0% cache hit rate.

## Limitations / 尚未验证

- Prompt caching is provider/model/TTL dependent; a reusable prefix **does not guarantee** a hit.
- Cache-write charges (notably on Anthropic and some newer OpenAI models), cached reads and uncached inputs have different pricing. Measure **total bills**, not only hit percentage.
- Changing personas, prompt rules, tools, model, or effort can invalidate prefixes. Compacting, expiring caches, or switching model/server routing can also lower hit rates.
- Cached tokens still consume context and may count toward rate limits. Do not pad prompts unnecessarily just to trigger caching.
- The local offline tests validate payload structure and token accounting; **no live provider hit rate or invoice savings were measured**.
