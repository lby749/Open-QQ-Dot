from pathlib import Path
import pytest
from tool_registry import validate_path_in_bounds


def test_full_access_cannot_read_pairing_secrets(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    for name in ('house_web.local.json', 'friends.json', 'api-key.bin', '.env'):
        target = root / name
        target.write_text('x')
        with pytest.raises(PermissionError):
            validate_path_in_bounds(target, root, {'full_computer_access': True})
    ssh = root / '.ssh' / 'id_rsa'
    ssh.parent.mkdir()
    ssh.write_text('x')
    with pytest.raises(PermissionError):
        validate_path_in_bounds(ssh, root, {'full_computer_access': True})


def test_normal_workspace_file_still_allowed(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    target = root / 'app.py'
    target.write_text('print(1)')
    assert validate_path_in_bounds(target, root, {'full_computer_access': True}) == target.resolve()


def test_command_guard_blocks_secret_paths():
    import secret_guard
    assert secret_guard.command_touches_secret('type house_web.local.json')
    assert secret_guard.command_touches_secret('Get-Content .ssh/id_rsa')
    assert not secret_guard.command_touches_secret('pytest -q')


def test_chat_config_switch_defaults_off():
    import config_guard
    assert config_guard.chat_config_allowed({}) is False
    assert config_guard.chat_config_allowed({'chat_config_edits': True}) is True
    assert config_guard.command_touches_config('curl /api/friend/1/model')
    assert not config_guard.command_touches_config('pytest -q')
