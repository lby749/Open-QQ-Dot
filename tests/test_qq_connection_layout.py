"""Offline connection-page layout checks; no account, runtime, or model calls."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import PushButton

import house_settings
import house_qt.common as common
import house_qt.dotlink as dotlink_page
import house_qt.qq_connection as connection


@pytest.fixture(scope='module')
def app():
    application = QApplication.instance() or QApplication([])
    common.init_ui_thread()
    return application


@pytest.fixture
def setup_page(tmp_path, monkeypatch, app):
    monkeypatch.setattr(connection, 'ROOT', tmp_path)
    monkeypatch.setattr(dotlink_page, 'ROOT', tmp_path)
    official_connect, official_stop, personal_connect = Mock(), Mock(), Mock()
    monkeypatch.setattr(connection.qq_official, 'bots', Mock(return_value=[]))
    monkeypatch.setattr(connection.qq_official, 'connect', official_connect)
    monkeypatch.setattr(connection.qq_official, 'stop', official_stop)
    house = SimpleNamespace(current_qq='12345', active_connect_personal_qq=personal_connect)
    pages = []

    def build(saved_mode=None):
        if saved_mode:
            house_settings.save({'qq_connection_mode': saved_mode}, tmp_path)
        legacy = dotlink_page.DotLinkPage(house)
        legacy.shown = Mock()
        legacy.on_status = Mock()
        page = connection.QQConnectionPage(house, legacy)
        page.resize(1000, 500)
        page.show()
        app.processEvents()
        pages.append(page)
        return page

    yield build, tmp_path, official_connect, official_stop, personal_connect
    for page in pages:
        page.legacy.login_timer.stop()
        page.close()
        page.deleteLater()
    app.processEvents()


def assert_mode(page, mode):
    personal = mode == 'legacy'
    assert page.mode.currentRouteKey() == mode
    assert page.official_card.isHidden() == personal
    assert page.authorization_card.isHidden() == personal
    assert page.personal_hint.isHidden() != personal
    assert page.legacy_content.isHidden() != personal


def test_first_open_is_official_with_discoverable_personal_mode(setup_page):
    build, _, official_connect, official_stop, personal_connect = setup_page
    page = build()
    assert_mode(page, 'official')
    assert isinstance(page.mode, common.SectionSwitcher)
    assert page.mode.items['legacy'].text() == '个人 QQ 账号直连（NAPCAT）'
    assert page.mode.items['official'].text() == '官方 QQ 机器人'
    assert page.personal_hint.text() == '使用自己的 QQ 账号。有风险。'
    assert page.legacy.isHidden()
    page.legacy.shown.assert_not_called()
    official_connect.assert_not_called()
    official_stop.assert_not_called()
    personal_connect.assert_not_called()


def test_selected_connection_text_fits_the_actual_item_font(setup_page, app):
    build, *_ = setup_page
    page = build('legacy')
    app.processEvents()
    for item in page.mode.items.values():
        assert item.width() >= item.fontMetrics().horizontalAdvance(item.text()) + 28


def test_login_phase_is_shown_in_chinese(setup_page):
    build, *_ = setup_page
    page = build('legacy')
    page.legacy.apply({'running': True, 'accounts': [], 'login': {'phase': 'waiting_qrcode'}})
    assert '等待扫码' in page.legacy.detail.text()
    assert 'waiting_qrcode' not in page.legacy.detail.text()


def test_personal_content_has_no_nested_page_or_fixed_minimum_height(setup_page, app):
    build, *_ = setup_page
    page = build('legacy')
    assert_mode(page, 'legacy')
    assert page.legacy.widget() is None
    assert page.legacy_content is page.legacy.content
    assert page.legacy_content.parentWidget() is page.content
    assert page.legacy_content.minimumHeight() == 0
    assert page.legacy.minimumHeight() == 0
    margins = page.legacy.column.contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)
    assert not page.legacy_content.findChildren(common.Page)
    assert page.legacy.isHidden()
    page.legacy.column.activate()
    page.column.activate()
    app.processEvents()
    assert page.verticalScrollBar().maximum() > 0
    assert not page.legacy.verticalScrollBar().isVisible()


def test_switch_and_reload_show_only_selected_mode_without_connection_side_effects(setup_page):
    build, root, official_connect, official_stop, personal_connect = setup_page
    page = build()
    login_state = {'running': True, 'login': {'is_login': True, 'uin': '12345'}}
    page.legacy.state = login_state
    for _ in range(4):
        page.mode.setCurrentItem('legacy')
        page.load()
        assert_mode(page, 'legacy')
        assert house_settings.load(root)['qq_connection_mode'] == 'legacy'
        page.mode.setCurrentItem('official')
        page.load()
        assert_mode(page, 'official')
        assert house_settings.load(root)['qq_connection_mode'] == 'official'
    assert page.legacy.state is login_state
    assert page.legacy.state['login']['is_login']
    assert not page.legacy_content.findChildren(common.Page)
    official_connect.assert_not_called()
    official_stop.assert_not_called()
    personal_connect.assert_not_called()


def test_switching_panel_preserves_enabled_personal_connection(setup_page):
    build, root, official_connect, official_stop, personal_connect = setup_page
    page = build('legacy')
    assert house_settings.personal_qq_enabled(root)
    page.mode.setCurrentItem('official')
    assert house_settings.personal_qq_enabled(root)
    page.mode.setCurrentItem('legacy')
    assert house_settings.personal_qq_enabled(root)
    official_connect.assert_not_called()
    official_stop.assert_not_called()
    personal_connect.assert_not_called()


def test_multiple_bot_rows_select_independent_configuration(setup_page, monkeypatch):
    build, _, connect, stop, _ = setup_page
    rows = [{'app_id': '12345', 'name': '研究员', 'enabled': True},
            {'app_id': '67890', 'name': '审查员', 'enabled': True}]
    monkeypatch.setattr(connection.qq_official, 'bots', lambda _: rows)
    monkeypatch.setattr(connection.qq_official, 'status', lambda _, identifier: '状态 ' + identifier)
    page = build('legacy')
    page.mode.setCurrentItem('official')
    assert page.bot_table.rowCount() == 2
    page.bot_table.selectRow(1)
    assert page.identifier.text() == '67890' and page.name.text() == '审查员'
    page.disconnect_bot()
    stop.assert_called_once_with(connection.ROOT, '67890')
    connect.assert_not_called()


def test_legacy_route_selects_mode_and_keeps_all_existing_actions(setup_page):
    build, root, *_ = setup_page
    page = build()
    page.open_legacy()
    assert_mode(page, 'legacy')
    assert house_settings.load(root)['qq_connection_mode'] == 'legacy'
    labels = {button.text() for button in page.legacy_content.findChildren(PushButton)}
    assert {'快速登录', '扫码登录', '密码登录', '刷新二维码', '连接并启动桥接',
            '从 QQ 导入好友', '导出全部好友与群聊 JSON', '启动 / 修复直连', '停止直连'} <= labels
    for method in ('quick_login', 'qr_login', 'password_login', 'import_friends', 'export_all_chats'):
        assert getattr(page.legacy, method).__self__ is page.legacy
    assert page.legacy.shown.call_count == 1


def test_status_refresh_and_explicit_connect_follow_selected_mode(setup_page, monkeypatch):
    build, _, _, official_stop, personal_connect = setup_page
    page = build()
    refresh, official_connect = Mock(), Mock()
    monkeypatch.setattr(page, 'refresh_contacts', refresh)
    monkeypatch.setattr(page, 'connect_bot', official_connect)
    page.on_status()
    refresh.assert_called_once()
    page.legacy.on_status.assert_not_called()
    page.active_connect()
    official_connect.assert_called_once()
    page.open_legacy()
    refresh.reset_mock()
    page.on_status()
    page.legacy.on_status.assert_called_once()
    refresh.assert_not_called()
    page.active_connect()
    personal_connect.assert_called_once()
    official_stop.assert_not_called()


def test_personal_status_refreshes_live_login_even_while_runtime_is_running(setup_page, monkeypatch):
    build, *_ = setup_page
    legacy = build('legacy').legacy
    legacy.state = {'running': True, 'login': {'is_login': True, 'online': True}}
    refresh = Mock()
    monkeypatch.setattr(legacy, 'refresh', refresh)
    dotlink_page.DotLinkPage.on_status(legacy)
    refresh.assert_called_once_with(quiet=True)


def test_personal_port_and_cached_login_do_not_claim_online_or_connected_bridge(setup_page):
    build, *_ = setup_page
    legacy = build('legacy').legacy
    state = {'running': True, 'onebot_listening': True,
        'accounts': [{'uin': '12345', 'nickname': '甲'}, {'uin': '23456', 'nickname': '乙'}],
        'login': {'is_login': True, 'online': True, 'uin': '12345'}}
    legacy.apply(state)
    assert legacy.status.text().startswith('QQ 已在线')
    assert '桥接已连接' not in legacy.detail.text()
    legacy.accounts.setCurrentIndex(1)
    state['login']['online'] = False
    legacy.apply(state)
    assert legacy.status.text() == 'QQ 已登录，正在连接'
    assert legacy.accounts.currentData() == '23456'
    state['login']['is_login'] = False
    legacy.apply(state)
    assert legacy.status.text() == 'QQ 直连正在运行，等待登录'
