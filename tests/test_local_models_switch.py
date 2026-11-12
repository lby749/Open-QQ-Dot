from pathlib import Path
import house_settings


def test_set_local_models_enabled_writes_and_notifies(tmp_path):
    seen = []
    house_settings.subscribe_local_models(seen.append)
    house_settings.set_local_models_enabled(True, tmp_path)
    assert house_settings.local_models_enabled(tmp_path) is True
    assert seen[-1] is True
    house_settings.set_local_models_enabled(False, tmp_path)
    assert house_settings.local_models_enabled(tmp_path) is False
    assert seen[-1] is False
