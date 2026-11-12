"""Use an explicitly selected ordinary backend for background processing."""
from agent_model_selection import options, validate, apply


def generate(prompt, directory, *, profile, **kwargs):
    selection = profile.get('background_model') or {}
    if not selection:
        from main import codex_text
        return codex_text(prompt, directory, profile=profile, **kwargs)
    from agent_tool_adapters import AGENT_BACKENDS
    from multi_agent_orchestrator import call_single_agent
    provider = str(selection.get('provider') or '')
    model, effort = validate(options(profile, [provider]), provider,
                             selection.get('model') or '', selection.get('effort') or '')
    actual = apply(profile, provider, model, effort)
    # Tool-backed agents keep the same permission gates as their ordinary calls.
    # They reject text_only because their native tool surface cannot be disabled.
    text_only = bool(kwargs.pop('text_only', False)) and provider not in AGENT_BACKENDS
    return call_single_agent(provider, prompt, directory, actual, ephemeral=kwargs.pop('ephemeral', True),
        text_only=text_only, local_ok=False, raise_on_error=True, max_retries=0, **kwargs)
