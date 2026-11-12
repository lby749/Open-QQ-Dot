# Low-latency dispatch (2026-10-24)

This patch reduces avoidable **local scheduling overhead**. It does not accelerate cloud-model inference, token generation, QQ network latency, or remote tool execution.

## Production changes

- `TaskManager` uses an in-process `threading.Event` to wake its worker when a new task, inbox wakeup, resumed task, completed dependency or newly available worker capacity appears. The existing 0.5 s timeout remains so **other OS processes** writing to the same SQLite DB are still noticed.
- Adds the composite SQLite index `(status, priority DESC, created_at ASC)` used by task scheduling.
- `AgentMailbox` uses a `Condition`/generation counter to notify same-process HTTP/MCP long-poll and send-and-wait consumers after a message is committed. The original 250 ms bounded poll remains as the cross-process safety fallback. The generation is snapshotted before each DB fetch to avoid lost signals.
- The QQ bridge invokes maintenance at most every 2 seconds instead of once after each message batch. This prioritizes message handling during bursts at the cost of up to roughly 2 s additional scheduling delay for some background-only checks.
- Optional metadata-only reply timings: set environment variable `QQDOT_LATENCY_TRACE=1` on the QQ bridge to log `generation` and `first_delivery` elapsed seconds. No prompt, text, or message body is written by this trace.

## Reproducible synthetic local benchmarks

Run `PYTHONPATH=. python tools/benchmark_response_latency.py` from the project root. The script uses disposable SQLite databases, mock task work (not a cloud model), and local threads. Run it on both the **token-optimized baseline** and this version under the same environment for a comparison.

October 24 2026 local Linux container, 18 sequential task submissions and 14 mailbox long-polls in each version:

| Path | Before median | After median | Before mean | After mean |
|---|---:|---:|---:|---:|
| New inbox task creation -> start of worker execution | 463.3 ms | 2.7 ms | 465.6 ms | 2.8 ms |
| Message committed -> in-process long-poll delivery | 220.8 ms | 4.5 ms | 221.1 ms | 4.7 ms |

Sample-specific figures, **not** an end-to-end QQ response speedup claim. The same-process notification avoids polling; a separate Agent process still depends on DB polling and will have different results. The task benchmark uses a test executor and does not include model reasoning, task verification or result notifications. CPU load, I/O load, SQLite contention and scheduling can change the results.

## Tests and limitations

`tests/test_low_latency_dispatch.py` verifies same-process wakeups, a second mailbox instance writing to the same DB, reply waits, resumed tasks and task worker startup/dispatch. Additional existing task, QQ and interop tests should be run in isolated groups because the repository's aggregate suite may hang in this Linux environment.

**Still to validate on Windows:** NapCat real incoming-to-first-reply latency distributions, real BGE embedding/rerank latency, long-running mixed QQ/Agent load, real Codex/Claude model latency and request cost. Use trace logs plus model-provider usage data rather than interpreting this microbenchmark as a complete response-time measurement.
