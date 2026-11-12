import json
from pathlib import Path

import pytest

import provider_router


def profile(root: Path, qq='123', provider='codex', **extra):
    directory = root / 'friends' / qq
    directory.mkdir(parents=True)
    api = {'provider': provider, 'model': 'model-x'}
    api.update(extra.pop('model_api', {}))
    data = {'basic_info': {'name': '测试'}, 'model_api': api, **extra}
    (directory / 'friend.json').write_text(json.dumps(data), 'utf-8')


def test_accounts_read_non_secret_fields_and_approval(tmp_path):
    profile(tmp_path, api_key_id='key-1', model_api={'provider': 'codex', 'model': 'm', 'api_key_id': 'key-1'},
            api_profiles={'key-1': {'label': 'x'}},
            computer_tools={'approval_mode': 'on-request'})
    rows = provider_router.accounts(tmp_path)
    assert rows[0].qq == '123'
    assert rows[0].provider == 'codex'
    assert rows[0].approval == 'on-request'
    assert 'key-1' in rows[0].api_key_id
    # No secret values are read by the router.
    assert not hasattr(rows[0], 'api_key')


def test_provider_specific_approval_overrides_global(tmp_path):
    profile(tmp_path, model_api={'provider': 'codex', 'model': 'm',
                                  'provider_approvals': {'codex': 'never'}},
            computer_tools={'approval_mode': 'on-request'})
    assert provider_router.accounts(tmp_path)[0].approval == 'never'


def test_route_unknown_or_missing_profile_fails(tmp_path):
    profile(tmp_path, provider='unknown-provider')
    with pytest.raises(ValueError):
        provider_router.route(tmp_path, '123')
    with pytest.raises(ValueError):
        provider_router.route(tmp_path, '999')


def test_route_agent_without_install_is_not_reported_as_ready(tmp_path, monkeypatch):
    profile(tmp_path, provider='cursor')
    monkeypatch.setattr('agent_tool_adapters.available_adapters',
                        lambda: [{'backend': 'cursor', 'installed': False, 'transport': 'headless'}])
    with pytest.raises(ValueError):
        provider_router.route(tmp_path, '123')


def test_account_binding_overrides_profile_route(tmp_path):
    import account_service
    row = account_service.add(tmp_path, {'label': 'Bound', 'provider': 'codex', 'model': 'm'})
    account_service.bind(tmp_path, '123456789', row['id'])
    selected = provider_router.route(tmp_path, '123456789')
    assert selected.provider == 'codex'
    assert selected.model == 'm'
    assert 'account_catalog binding' in selected.reason
