"""Per-agent model and thinking-effort capabilities.

Every agent backend owns its own capability description.  There is no global
"one model list for all agents": a CLI that cannot enumerate models says so
instead of inventing names.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentCapability:
    models: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    model_supported: bool = False
    effort_supported: bool = False
    note: str = ""


def capability(provider: str, config: dict | None = None) -> AgentCapability:
    module = {
        "opencode": "opencode_backend",
        "cursor": "cursor_backend",
        "qwen_code": "qwen_backend",
        "zcode": "zcode_backend",
        "kimi_code": "kimi_backend",
        "acp": "acp_backend",
    }.get(str(provider))
    if not module:
        return AgentCapability(note="未知智能体")
    backend = __import__(f"agent_backends.{module}", fromlist=["capabilities"])
    return backend.capabilities(config or {})


def list_models(provider: str, config: dict | None = None) -> tuple[list[str], str]:
    module = {
        "opencode": "opencode_backend",
        "cursor": "cursor_backend",
        "qwen_code": "qwen_backend",
        "zcode": "zcode_backend",
        "kimi_code": "kimi_backend",
        "acp": "acp_backend",
    }.get(str(provider))
    if not module:
        raise ValueError("未知智能体")
    backend = __import__(f"agent_backends.{module}", fromlist=["list_models"])
    return backend.list_models(config or {})
