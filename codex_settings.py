"""Codex model options and per-friend generation settings."""
import json
import os
import shutil
from pathlib import Path


def cli_path(custom_path: str | None = None) -> str | None:
    """Find Codex CLI executable: custom path, PATH, or common install locations."""
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_file():
            return str(p.resolve())
        if p.is_dir():
            for name in ('codex.cmd', 'codex.exe', 'codex.ps1', 'codex'):
                if (p / name).is_file():
                    return str((p / name).resolve())
    found = shutil.which('codex')
    if found:
        return found
    candidates = [
        Path(os.environ.get('APPDATA', '')) / 'npm' / 'codex.cmd',
        Path(os.environ.get('APPDATA', '')) / 'npm' / 'codex.ps1',
        Path(os.environ.get('ProgramFiles', 'C:\\Program Files')) / 'nodejs' / 'codex.cmd',
        Path('D:/codex/codex.cmd'),
        Path('D:/codex/bin/codex.cmd'),
        Path('D:/nodejs/codex.cmd'),
        Path('D:/npm/codex.cmd'),
        Path('D:/node_global/codex.cmd'),
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return None


def model_options(custom_path: str | None = None) -> dict[str, list[str]]:
    try:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        cache = home / "models_cache.json"
        if cache.exists():
            models = json.loads(cache.read_text(encoding="utf-8"))["models"]
            live_options = {}
            for model in models:
                if model.get("visibility") == "list":
                    levels = [level["effort"] for level in model.get("supported_reasoning_levels", [])]
                    if levels:
                        live_options[model["slug"]] = levels
            if live_options:
                return live_options
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {
        "gpt-6.1-sol": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-6-astra": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-6-sol": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-5.6-sol": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-5.6-terra": ["none", "low", "medium", "high", "xhigh", "max"],
        "gpt-5.6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
    }


def fetch_available_models(custom_path: str | None = None) -> dict[str, list[str]]:
    """Query the actual CLI model/list RPC; built-in defaults are not discovery evidence."""
    if not cli_path(custom_path):
        raise RuntimeError('未找到 Codex CLI，请先在模型来源页配置')
    from codex_app_server import CodexAppServerClient
    client = CodexAppServerClient(custom_codex_path=custom_path)
    try:
        return client.list_models()
    finally:
        client.close()  # this query owns its process; never close the shared generation client


from provider_context import COMPACT_RATIO, DEFAULT_WINDOW
CONTEXT_TOKENS = 1_000_000
COMPACT_TOKENS = int(CONTEXT_TOKENS * COMPACT_RATIO)
MIN_COMPACT_TOKENS = 16_000


def compact_tokens(api):
    """Compaction threshold: 90% of the *provider-compatible* context window."""
    from provider_context import compact_at
    return compact_at(api)


def context_window(api):
    from provider_context import window
    return window(api)


def context_window(api):
    from provider_context import window
    return window(api)


def generation_settings(api):
    model = api.get("codex_model", "gpt-6-luna")
    effort = api.get("codex_effort", "low")
    options = model_options()
    if model not in options or effort not in options[model]:
        raise ValueError("Codex 模型或思考档位无效，请在模型 API 页重新选择。")
    return model, effort, "fast" if api.get("codex_fast", False) else "default"  # 2026-10-25 user: Fast off unless chosen
