# In-chat group assistant (`group_bot.py`)

`group_bot.py` adds an assistant that people summon inside a QQ group, the way Grok works on X, ChatGPT in group chats, or Meta AI in group DMs. It runs on this machine.

It is an addition. The per-friend companion in `main.py` (private chats, persona, memory, proactive rounds, tasks) is unchanged. `Bridge.receive` sends group events to `group_bot` and handles private events exactly as before.

## What it does

| Function | How members use it |
|---|---|
| Summon anywhere in the conversation | `@Grok …`, or the bot's name at the start (`grok，…`), or reply to one of its answers to keep going |
| Answer from the conversation | Reads the quoted message and the recent group context (the last 20 messages by default) |
| Fact-check | `@Grok 这是真的吗？` while replying to a message gives a verdict (真 / 假 / 部分属实 / 无法确认) with reasons |
| Live web search with sources | Questions about current facts get searched, and the answer cites `[1]` with a `来源：` list of the results it used |
| Image understanding | Images in the triggering or quoted message go to a vision-capable backend. Otherwise it says it can't see them |
| Summaries | `@Grok 总结一下` / `tl;dr` / `catch me up` reads up to 150 recent messages |
| Deep search | `@Grok 深度搜索 …` / `deep research …` allows up to 8 search rounds and asks for a thorough, cited answer |
| Per-group settings | The group's owner or admins: `@Grok 设置 长度 简短/适中/详细`, `@Grok 设置 风格 <description>`. An empty value resets it |

## Backends (all on this machine)

| `backend` | What runs | Search | Images |
|---|---|---|---|
| `local` (default) | Any OpenAI-compatible server on loopback: Ollama `http://127.0.0.1:11434/v1`, llama.cpp `:8080/v1`, LM Studio `:1234/v1`, or the house's bundled llama-server (`"local": {"house_runtime": true}`) | `web_search` / `fetch_url` tool calls (needs a `search` provider) | With `"local": {"vision": true}` and a vision model (e.g. `qwen2.5vl`) |
| `codex` / `claude` / `custom` / … | The installed agent CLI in a per-group folder, with computer tools off | The CLI's own live search | Passed as an image file |

`fallback` names a second backend to try when the first fails or isn't running.

### Search providers (`search.provider`)

| Provider | Notes |
|---|---|
| `searxng` | Self-hosted SearXNG, so search is local too. Set `base_url`, and enable `json` under `search.formats` in its settings |
| `brave` / `tavily` | Your own API key (`api_key`) |
| `agent` | Delegates the search to an installed CLI that has sanctioned live search (`agent: "codex"`) and returns its result list |
| `off` | No search. The bot says it can't check current facts instead of guessing |

Search engines' HTML pages are not scraped; they answer bots with human-verification challenges. `fetch_url` reads only public http(s) pages: private, loopback and link-local hosts are refused, including after redirects.

## Configure (`house.local.json`)

```json
"group_bot": {
  "enabled": true,
  "name": "Grok",
  "aliases": ["grok"],
  "groups": {"123456789": {"enabled": true, "style": "optional default style for this group"}},
  "backend": "local",
  "fallback": "codex",
  "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b", "vision": false},
  "search": {"provider": "searxng", "base_url": "http://127.0.0.1:8888"},
  "context_messages": 20,
  "group_rate_per_minute": 6,
  "user_rate_per_minute": 3
}
```

Check that it can run here (config, local model server and model, fallback CLI, search):

```bash
python group_bot.py --check
```

The bridge logs the same check once, at the first group event, so a missing model server shows up in the log.

Try it without QQ:

```bash
python group_bot.py "这是真的吗？" --quote "长城是唯一能在月球上用肉眼看到的人造建筑"
python group_bot.py "Python 最新稳定版是多少？" --backend codex --search agent
python group_bot.py "图里的问题答案是多少？" --backend codex --image receipt.png
```

## Safety and privacy

- **Opt-in groups:** in a group that isn't enabled, nothing is stored or answered.
- **Untrusted data:** group messages, quoted messages, names, search results and web pages go into the prompt as data inside an envelope. They can't change the rules, approve anything or reveal the prompt.
- **Separate group memory:** group memory (`data/group_bot.sqlite3`) is kept apart from every friend's private chats, memories and files.
- **Bounded storage:** at most 400 messages per group, and nothing older than 3 days.
- **One answer per trigger:** a redelivered event is never answered twice.
- **Limits and audit:** answers are rate-limited per group and per member. Each outcome (`sent`, `rate_limited`, `failed`, `send_failed`) is stored with its backend, latency and search count.
- **Settings permissions:** only the QQ group owner or admins can change settings; members get a refusal.

## Verification status (2026-10-23)

| Piece | Evidence |
|---|---|
| Parsing, triggers, store, worker, rate limits, fallback, settings, summaries, `Bridge.receive` routing | test-real (`tests/test_group_bot.py`; a real HTTP model server on loopback, fake NapCat) |
| Local tool loop (`web_search` → cited `来源：`), vision payload, agent image hand-off | test-real (scripted model server, real PNG via `get_image`) |
| SearXNG client, agent-search parsing, `fetch_url` refusals | test-real |
| Codex backend: fact-check, live search with a source link | **live** (11–14 s) |
| `agent` search provider through Codex | **live** (returned dated python.org / discuss.python.org results) |
| Image understanding through Codex (`--image`) | **live** (read a price problem from a picture and answered it correctly, 8.7 s) |
| Local model backend against a real model | **not run**: no local runtime on the dev machine (install Ollama and `qwen3:8b` for a 12 GB GPU) |
| A real QQ group through NapCat | **not run** |
