"""Per-provider context windows and the 90% compaction point.

A single global 1M threshold is wrong: Claude currently exposes 200k, most
OpenAI-compatible endpoints expose 128k, and only some Codex/Gemini/DeepSeek
models take a 1M prompt.  This matrix is intentionally conservative and can be
overridden per friend with ``model_api.context_tokens`` (never above the
provider's hard limit).
"""
from __future__ import annotations

from dataclasses import dataclass

COMPACT_RATIO = 0.90
DEFAULT_WINDOW = 128_000

# Provider hard limits, in tokens.
PROVIDER_WINDOWS = {
    'codex': 1_000_000,
    'claude': 1_000_000,
    'antigravity': 1_000_000,
    'dsh': 1_000_000,
    'deepseek_harness': 1_000_000,
    'custom': 1_000_000,
    'deepseek': 1_000_000,
    'api': 1_000_000,
    'webchat_chatgpt': 400_000,
    'webchat_gemini': 1_000_000,
    'webchat_deepseek': 128_000,
}


@dataclass(frozen=True)
class ContextInfo:
    provider: str
    model: str
    window: int
    compact_at: int
    source: str

    @property
    def supports_1m(self) -> bool:
        return self.window >= 1_000_000


def _provider(api: dict) -> str:
    provider = str((api or {}).get('provider') or 'codex').lower()
    if provider == 'deepseek_harness':
        return 'dsh'
    return provider


def _model(api: dict, provider: str) -> str:
    api = api or {}
    if provider in ('dsh',):
        return str((api.get('dsh_settings') or {}).get('model') or api.get('model') or '')
    if provider == 'claude':
        return str(api.get('claude_model') or '')
    if provider == 'antigravity':
        return str(api.get('antigravity_model') or '')
    if provider in ('custom', 'deepseek', 'api'):
        return str(api.get('model') or '')
    return str(api.get('codex_model') or api.get('model') or '')


def info(api: dict) -> ContextInfo:
    api = api or {}
    provider = _provider(api)
    model = _model(api, provider)
    lower = model.casefold()
    limit = PROVIDER_WINDOWS.get(provider, DEFAULT_WINDOW)
    # Model families that are known to be smaller than the provider maximum.
    if provider == 'dsh' and ('deepseek-v3' in lower or 'deepseek-chat' in lower):
        limit = 128_000
    if provider == 'antigravity' and 'flash' in lower:
        limit = 1_000_000
    explicit = api.get('context_tokens') or api.get('compact_tokens')
    source = f'{provider} 官方窗口'
    if type(explicit) is int and explicit >= 16_000:
        # An explicit value can lower the window, but cannot exceed the hard limit.
        limit = min(explicit, limit)
        source = '好友自定义'
    return ContextInfo(provider=provider, model=model or '-', window=limit,
                       compact_at=max(16_000, int(limit * COMPACT_RATIO)), source=source)


def window(api: dict) -> int:
    return info(api).window


def compact_at(api: dict) -> int:
    return info(api).compact_at


def label(api: dict) -> str:
    data = info(api)
    return f'{data.window:,}（达到 90% 即 {data.compact_at:,} 自动压缩）'
