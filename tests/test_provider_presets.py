import account_service
from provider_presets import PRESETS


def test_presets_are_valid_account_metadata():
    assert len(PRESETS) >= 25
    for name, (provider, model, base) in PRESETS.items():
        assert name
        assert provider in account_service.PROVIDERS
        assert isinstance(model, str)
        assert not base or base.startswith(('http://', 'https://'))
