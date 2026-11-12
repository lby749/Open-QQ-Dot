from copy import deepcopy
from pathlib import Path
import pytest

import background_generation


def test_background_dsh_model_and_effort_are_real_overrides_on_a_copy(monkeypatch):
    import multi_agent_orchestrator
    profile = {'model_api': {'provider': 'codex', 'codex_model': 'chat-model'},
               'background_model': {'provider': 'dsh', 'model': 'DeepSeek-V41-Flash', 'effort': 'low'}}
    before = deepcopy(profile)
    called = []
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', lambda *args, **kw: (called.append((args, kw)) or ('{}', None)))
    assert background_generation.generate('整理', Path('.'), profile=profile, ephemeral=True, text_only=True) == ('{}', None)
    args, kw = called[0]
    assert args[0] == 'dsh' and args[3]['model_api']['dsh_settings'] == {'model': 'DeepSeek-V41-Flash', 'effort': 'low'}
    assert kw['text_only'] and kw['max_retries'] == 0 and profile == before


def test_background_tool_model_reaches_native_adapter_without_granting_permissions(monkeypatch):
    import multi_agent_orchestrator
    profile = {'agent_adapters': {'zcode': {'model': 'configured-tool-model'}},
               'computer_tools': {'enabled': False},
               'background_model': {'provider': 'zcode', 'model': 'configured-tool-model', 'effort': ''}}
    called = []
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', lambda *args, **kw: (called.append((args, kw)) or ('{}', None)))
    background_generation.generate('整理', Path('.'), profile=profile, text_only=True)
    args, kw = called[0]
    assert args[3]['agent_adapters']['zcode']['model'] == 'configured-tool-model'
    assert args[3]['computer_tools']['enabled'] is False and kw['text_only'] is False
    profile['background_model']['effort'] = 'made-up-strength'
    with pytest.raises(ValueError):
        background_generation.generate('整理', Path('.'), profile=profile)
    assert len(called) == 1


def test_empty_background_selection_uses_the_existing_chat_path(monkeypatch):
    import main
    called = []
    monkeypatch.setattr(main, 'codex_text', lambda *args, **kw: (called.append((args, kw)) or ('{}', None)))
    background_generation.generate('整理', Path('.'), profile={}, text_only=True)
    assert called[0][1]['text_only'] is True
