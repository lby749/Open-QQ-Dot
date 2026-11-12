# Feature Matrix: Open QQ Dot vs. ChatGPT Dot / Work

The architectural goal of this project is to execute AI companion and assistant tasks directly on a local Windows machine. Model reasoning may route to external cloud providers; no cloud infrastructure is provisioned, and execution halts when the local machine is powered down. This comparison references public documentation for Dot and ChatGPT Work.

| Capability | Open QQ Dot Status | Prerequisites & Roadmap |
|---|---|---|
| QQ entry point with multi-friend isolation | Supported | Currently focuses on direct private messaging |
| Long-term memories, preferences, persona imitation, proactive outreach | Supported; programmatic fallbacks when local models are omitted | Rule-based classification does not substitute for full semantic parsing |
| Multi-model integration, tool calling, HTTP APIs | Multi-CLI and REST API endpoints supported | Requires operator credentials and accounts per provider |
| Local file and terminal tasks | Supported via per-friend workspace authorization | Terminal commands require explicit authorization with 90s timeout |
| Web research and opening local browser URLs | CLI native search and URL launching supported | Launching a URL does not equate to browser DOM automation |
| Multi-step automation inside authenticated browsers | Not yet unified | Requires dedicated browser automation / MCP with session lifecycle |
| Desktop screenshots, app clicks, and visual OS control | Not yet unified | Requires native accessibility / UI automation drivers and audit logs |
| Long-running task queue, pause/resume, simultaneous dialogue | Supported for specialized research / tasks | General task persistence exists; full Dot-style scheduling is evolving |
| Multi-agent parallel coordination | DAG-based subtask orchestration supported | Worker execution isolated by workspace directories |
| Execution after host PC shutdown | Not provided by design | Requires local PC and background bridge process to be running |

---

### Official References
- [Dot Computers and Apps](https://learn.chatgpt.com/docs/dots/computers-and-apps)
- [Dot Tasks and Memory](https://learn.chatgpt.com/docs/dots/tasks-and-memory)
- [ChatGPT Work](https://learn.chatgpt.com/docs/get-started-with-work)
