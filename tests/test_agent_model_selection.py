from copy import deepcopy
import pytest

import agent_model_selection as selection


def test_models_are_backend_specific_and_per_call_choices_do_not_mutate_defaults(monkeypatch):
    import codex_settings
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low'], 'deep': ['high']})
    profile = {'model_api': {'provider': 'codex', 'codex_model': 'fast', 'codex_effort': 'low',
                            'dsh_settings': {'model': 'DeepSeek-V41-Flash', 'effort': 'low'}},
               'agent_adapters': {'qwen_code': {'model': 'qwen-configured'}}}
    before = deepcopy(profile)
    catalog = selection.options(profile, ['codex', 'dsh', 'qwen_code'])
    assert selection.validate(catalog, 'codex', 'deep', '') == ('deep', 'high')
    with pytest.raises(ValueError):
        selection.validate(catalog, 'codex', 'deep', 'low')
    with pytest.raises(ValueError):
        selection.validate(catalog, 'qwen_code', 'qwen-configured', 'high')
    with pytest.raises(ValueError):
        selection.validate(catalog, 'dsh', 'DeepSeek-V41-Flash', 'max')
    changed = selection.apply(profile, 'dsh', 'DeepSeek-V4-Pro', 'high')
    assert changed['model_api']['dsh_settings'] == {'model': 'DeepSeek-V4-Pro', 'effort': 'high'}
    assert profile == before


def test_external_adapter_selection_reaches_adapter_config_not_generic_api():
    profile = {'agent_adapters': {'opencode': {'model': 'go/flash', 'variant': 'low',
        'model_options': {'go/flash': ['low', 'high']}}}}
    catalog = selection.options(profile, ['opencode'])
    model, effort = selection.validate(catalog, 'opencode', 'go/flash', 'high')
    call = selection.apply(profile, 'opencode', model, effort)
    assert call['agent_adapters']['opencode']['variant'] == 'high'
    assert profile['agent_adapters']['opencode']['variant'] == 'low'


def test_summary_receives_actual_dispatch_configuration_without_credentials():
    import agent_group
    prompt = agent_group.summary_prompt({'messages': []}, {}, '审查代码', routing=[{
        'name': '审查员', 'provider': 'codex', 'model': 'deep', 'effort': 'high', 'api_key': 'synthetic-secret'}])
    assert 'deep' in prompt and 'high' in prompt and 'synthetic-secret' not in prompt


def test_summary_uses_only_current_worker_results_and_the_trusted_route():
    import agent_group
    task = 'CURRENT_TASK_MARKER'
    room = {'messages': [
        {'role':'agent', 'name':'旧成员', 'content':'OLD_RESULT_DO_NOT_REUSE'},
        {'role':'host', 'name':'主智能体', 'content':'OLD_SUMMARY_DO_NOT_REUSE'},
        {'role':'user', 'name':'用户', 'content':task},
        {'role':'host', 'name':'主智能体', 'content':'PLANNER_DISPATCH_ALREADY_IN_ROUTE'},
        {'role':'agent', 'name':'审查员', 'content':'CURRENT_WORKER_RESULT'}]}
    prompt = agent_group.summary_prompt(room, {}, task,
        routing=[{'name':'审查员', 'provider':'codex', 'model':'gpt-6-luna', 'effort':'low', 'assignment':'复核算术'}])
    assert prompt.count(task) == 1
    assert prompt.count('CURRENT_WORKER_RESULT') == 1
    assert all(text not in prompt for text in ('OLD_RESULT', 'OLD_SUMMARY', 'PLANNER_DISPATCH'))
    assert 'gpt-6-luna' in prompt and 'low' in prompt and '复核算术' in prompt


def test_mcp_model_choices_are_paginated_and_scoped_to_the_current_profile(tmp_path, monkeypatch):
    import json
    import codex_settings
    import runtime_capabilities
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low'], 'deep': ['high']})
    (tmp_path / 'friend.json').write_text(json.dumps({'model_api': {'provider': 'codex',
        'codex_model': 'fast', 'api_key': 'synthetic-secret'},
        'agent_adapters': {'qwen_code': {'model': 'other-private-model'}}}), 'utf-8')
    report = runtime_capabilities.call(tmp_path, {'section': 'model_choices', 'limit': 1})
    assert report['total'] == 2 and report['next_offset'] == 1
    assert report['items'] == [{'provider': 'codex', 'model': 'fast', 'efforts': ['low'],
        'evidence': '配置或本机缓存；未发起模型调用'}]
    assert 'synthetic-secret' not in json.dumps(report) and 'other-private-model' not in json.dumps(report)
