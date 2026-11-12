import json
from unittest.mock import patch

from agent_errors import AgentErrorType, classify_error
from model_dispatcher import ModelDispatcher


def test_model_entitlement_does_not_cool_down_all_claude_models():
    error = classify_error('claude', error_payload={'error': {'type': 'rate_limit_error',
        'message': 'Fable 5.1 requires usage credits. Switch to another model, or manage usage credits.'}})
    assert error.error_type == AgentErrorType.MODEL_UNAVAILABLE
    assert not error.retryable
    dispatcher = ModelDispatcher()
    def fail():
        raise error
    try:
        dispatcher.execute_with_budget_and_gate('claude', fail)
    except type(error):
        pass
    assert dispatcher.is_available('claude')
    assert dispatcher.execute_with_budget_and_gate('claude', lambda: 'haiku works') == 'haiku works'


def test_codex_reasoning_levels_are_not_invented(tmp_path, monkeypatch):
    import codex_settings
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'models_cache.json').write_text(json.dumps({'models': [{'visibility': 'list',
        'slug': 'gpt-test', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}]}]}), 'utf-8')
    assert codex_settings.model_options() == {'gpt-test': ['low', 'high']}


def test_opencode_desktop_installation_is_detected_without_path_entry():
    import agent_tool_adapters as adapters
    with patch.object(adapters, '_opencode_cli', return_value='D:/installed/opencode-cli.exe'):
        assert next(row for row in adapters.available_adapters() if row['backend'] == 'opencode')['installed']


def test_verified_cli_quota_errors_are_not_retried_as_tool_failures():
    for provider, message in [('antigravity', 'Individual quota reached. Resets in 6h35m48s.'),
                              ('opencode', 'Free usage limit reached.')]:
        error = classify_error(provider, exc=RuntimeError(message))
        assert error.error_type == AgentErrorType.QUOTA_EXHAUSTED
        assert not error.retryable


