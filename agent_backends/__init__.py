"""Per-agent entry points.

Each external agent owns a small backend module here (the same layout as
app_server.py / claude_backend.py / antigravity_backend.py).  The shared
transport remains in agent_tool_adapters until each adapter gets its own
protocol implementation; permission and approval decisions are made here.
"""


def generate(name, prompt, directory, profile, **kwargs):
    module = {
        "opencode": "agent_backends.opencode_backend",
        "cursor": "agent_backends.cursor_backend",
        "qwen_code": "agent_backends.qwen_backend",
        "zcode": "agent_backends.zcode_backend",
        "kimi_code": "agent_backends.kimi_backend",
        "acp": "agent_backends.acp_backend",
    }.get(str(name))
    if not module:
        raise ValueError(f"unknown agent backend: {name}")
    return __import__(module, fromlist=["generate"]).generate(
        prompt, directory, profile, **kwargs)
