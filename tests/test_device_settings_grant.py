from types import SimpleNamespace

import pytest

from device_gateway import DeviceStore
from house_qt.vnext_pages import DevicesVNextPage


@pytest.mark.parametrize('enabled', [False, True])
def test_desktop_only_grants_phone_settings_when_explicitly_checked(tmp_path, enabled):
    store = DeviceStore(tmp_path / 'devices.json')
    codes = []
    page = SimpleNamespace(house=SimpleNamespace(current_qq='12345'),
        allow_collaboration=SimpleNamespace(isChecked=lambda: False),
        allow_settings=SimpleNamespace(isChecked=lambda: enabled),
        code=SimpleNamespace(setText=codes.append), store=lambda: store)
    DevicesVNextPage.make_code(page)
    paired = store.pair(codes[0], '模拟器')
    assert ('settings:write' in paired['scopes']) is enabled
    assert ('settings:read' in paired['scopes']) is enabled
    assert store.authenticate(paired['token'])['owners'] == ['12345']


def test_desktop_cannot_grant_phone_settings_without_a_friend(tmp_path, monkeypatch):
    import house_qt.vnext_pages as pages
    errors = []
    monkeypatch.setattr(pages, 'toast', lambda *_args: errors.append(_args))
    page = SimpleNamespace(house=SimpleNamespace(current_qq=None),
        allow_collaboration=SimpleNamespace(isChecked=lambda: False),
        allow_settings=SimpleNamespace(isChecked=lambda: True),
        code=SimpleNamespace(setText=lambda _: pytest.fail('不应生成配对码')),
        store=lambda: pytest.fail('无好友时不应签发任何权限'))
    DevicesVNextPage.make_code(page)
    assert errors and '请先选择好友' in errors[0][2]
