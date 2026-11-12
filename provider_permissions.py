"""Two independent permission layers.

computer_tools: what the friend may touch on this computer (workspace/full disk/commands).
provider_approvals: how each agent backend asks for approval inside that permission envelope.
"""
from __future__ import annotations

DEFAULTS = {
    "codex": "request-review",
    "claude": "manual",
    "antigravity": "default",
    "dsh": "workspace-write",
    "api": "never",
    "custom": "never",
    "webchat_deepseek": "never",
    "webchat_chatgpt": "never",
    "webchat_gemini": "never",
}

ALIASES = {
    "on-request": "on-request", "on_request": "on-request", "ask": "on-request",
    "request-review": "request-review", "manual": "manual",
    "full-access": "full-access", "bypass": "bypass", "turbo": "turbo",
    "never": "never", "auto": "auto", "accept-edits": "accept-edits",
    "acceptEdits": "accept-edits", "plan": "plan", "default": "default",
    "workspace-write": "workspace-write", "read-only": "read-only",
    "danger-full-access": "danger-full-access", "external-native": "external-native",
}


def normalize(value, default="never"):
    if not isinstance(value, str):
        return default
    return ALIASES.get(value.strip(), value.strip() or default)


def approval_for(profile, provider, default=None):
    """Provider-specific approval, with the old global field as a compatibility fallback."""
    api = (profile or {}).get("model_api") or {}
    table = api.get("provider_approvals") if isinstance(api.get("provider_approvals"), dict) else {}
    if provider in table:
        return normalize(table[provider], default or DEFAULTS.get(provider, "never"))
    legacy = ((profile or {}).get("computer_tools") or {}).get("approval_mode")
    if legacy:
        return normalize(legacy, default or DEFAULTS.get(provider, "never"))
    return default or DEFAULTS.get(provider, "never")


def with_provider_approval(profile, provider):
    """A shallow copy whose computer_tools carries only this call's approval mode."""
    if not isinstance(profile, dict):
        return profile
    value = dict(profile)
    tools = dict(value.get("computer_tools") or {})
    tools["approval_mode"] = approval_for(profile, provider)
    value["computer_tools"] = tools
    return value
