import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent

import tests.test_house_qt as fixture_module
from house_qt.common import SectionSwitcher


@pytest.fixture
def house():
    fixture_module.HouseQtTests.setUpClass()
    if Path('C:/Windows/Fonts/msyh.ttc').is_file():
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    fixture = fixture_module.HouseQtTests(methodName='runTest')
    fixture.setUp()
    try:
        yield fixture.window, fixture.app
    finally:
        fixture.doCleanups()


def test_sidebar_children_are_collapsed_and_live_in_the_scroll_area(house):
    window, app = house
    panel = window.navigationInterface.panel
    assert len([item for item in panel.items.values() if not item.parentRouteKey]) == len(window.WORKSPACE_NAMES)
    for page, route in window._sidebar_routes.items():
        child = panel.widget(route)
        assert child.treeParent is not None and not child.treeParent.isExpanded
        assert child.isHidden()
    for key in ('home', 'friends', 'agents', 'tasks', 'capsules', 'collab', 'qq', 'insights'):
        assert panel.scrollWidget.isAncestorOf(window.navigationInterface.widget(window.workspaces[key].objectName()))
    window.resize(1050, 700)
    window.show()
    for item in panel.items.values():
        if not item.parentRouteKey:
            item.widget.setExpanded(True)
    app.processEvents()
    area = panel.scrollArea
    assert area.verticalScrollBar().maximum() > 0
    event = QWheelEvent(QPointF(16, 16), QPointF(area.viewport().mapToGlobal(QPoint(16, 16))),
        QPoint(), QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    app.sendEvent(area.viewport(), event)
    deadline = time.monotonic()+1
    while time.monotonic() < deadline and area.verticalScrollBar().value() == 0:
        app.processEvents()
        time.sleep(.01)
    assert area.verticalScrollBar().value() > 0


def test_single_page_has_no_switcher_and_repeated_switches_keep_content(house):
    window, app = house
    assert window.workspaces['home'].switcher_scroll.isHidden()
    assert window.workspaces['capsules'].switcher_scroll.isHidden()
    for workspace in window.workspaces.values():
        assert isinstance(workspace.seg, SectionSwitcher)
        assert workspace.seg.height() == 44
        for _ in range(2):
            for index in range(workspace.tabs.count()):
                workspace.seg.setCurrentItem(f'{workspace.objectName()}_{index}')
                app.processEvents()
                assert workspace.tabs.currentIndex() == index
                assert workspace.tabs.currentWidget() is not None
    assert isinstance(window.pages['modelPage'].model_tabs, SectionSwitcher)
    assert isinstance(window.pages['profilePage'].seg, SectionSwitcher)


def test_pending_friend_status_does_not_update_a_closing_window(house, monkeypatch):
    window, _ = house
    pending = []
    monkeypatch.setattr('house_qt.window.run_async', lambda job, done, error: pending.append(done))
    window.refresh_friend_mode()
    applied = []
    monkeypatch.setattr(window.pages['statusPage'], 'apply', applied.append)
    window.exiting = True
    pending[0]({'qq': window.current_qq})
    window.refresh_friend_mode()
    assert not applied and len(pending) == 1


def test_header_and_section_controls_fit_without_clipping_at_supported_widths(house):
    window, app = house
    window.friend_title.setText('当前好友')
    window.bridge_label.setText('个人 QQ 桥接：消息桥接运行中（进程 12345）')
    for width in (1050, 1460, 1180, 1050):
        window.resize(width, 760)
        window.show()
        app.processEvents()
        assert window.width() == width
        for control in (window.friend_box, window.bridge_label, window.connect_btn):
            text = control.currentText() if control is window.friend_box else control.text()
            padding = 36 if control is window.friend_box else 0
            assert control.width() >= control.fontMetrics().horizontalAdvance(text) + padding
            assert control.mapTo(window, QPoint()).x() + control.width() <= window.width()
        for key in ('qq', 'tasks', 'tutorial', 'agents'):
            workspace = window.workspaces[key]
            window.switchTo(workspace)
            app.processEvents()
            area = workspace.switcher_scroll
            assert area.viewport().height() >= workspace.seg.height()
            assert workspace.seg.y() >= 0
            assert workspace.seg.y() + workspace.seg.height() <= area.viewport().height()
            assert workspace.tabs.y() >= area.y() + area.height()


def test_workflow_category_changes_fields_and_does_not_keep_old_code_command(house):
    window, _ = house
    page = window.pages['workflowPage']
    page.steps = [{'title': '转换类型', 'description': '检查跨类型验收', 'category': 'code',
                   'spec': {'test_command': 'python -c "1"', 'target_files': ['first.py']}}]
    page.fill_stages()
    assert not page.test.isHidden() and page.keys.isHidden()
    page.category.setCurrentIndex(page.category.findData('data'))
    assert page.test.isHidden() and page.test.text() == '' and not page.keys.isHidden()
    page.keys.setText('ok')
    page.sync_stage()
    assert page.steps[0]['spec'] == {'expected_keys': ['ok']}
    page.category.setCurrentIndex(page.category.findData('browser'))
    assert not page.url.isHidden() and not page.actions.isHidden() and page.keys.text() == ''


def test_development_area_keeps_pairing_but_moves_unverified_call_action(house, monkeypatch):
    window, _ = house
    from house_qt.development import FEATURES
    assert window.workspaces['qq'].tabs.indexOf(window.pages['devicesPage']) >= 0
    assert window.workspaces['development'].tabs.indexOf(window.pages['healthPreviewPage']) >= 0
    assert any(status == '未完成' and target == 'voice' for _, status, _, _, target in FEATURES)
    assert window.pages['devicesPage'].invite_status.isHidden()
    created = []
    monkeypatch.setattr('voice_invites.create', lambda _root, owner: created.append(owner) or {'id': 'test'})
    monkeypatch.setattr('house_qt.vnext_pages.toast', lambda *_args: None)
    window.pages['developmentPage'].open_feature('voice')
    assert created == ['12345']
    assert '已在本机创建' in window.pages['developmentPage'].voice_status.text()
    assert '未确认' in window.pages['developmentPage'].voice_status.text()


def test_agenda_summary_matches_editor_defaults_and_saved_overrides(house):
    import json
    window, app = house
    profile = json.loads(window.profile_path.read_text('utf-8'))
    profile['proactive'] = {'enabled': False}
    window.profile_path.write_text(json.dumps(profile), 'utf-8')
    status, editor = window.pages['statusPage'], window.pages['proactivePage']
    status.show_agenda()
    editor.load()
    text = status.agenda_detail_text.text()
    assert f"{editor.numbers['rounds_min'].text()}–{editor.numbers['rounds_max'].text()}" in text
    assert f'{editor.window_start.text()}–{editor.window_end.text()}' in text
    assert '已关闭' in status.agenda_status_text.text()
    profile['proactive'].update(rounds_min=2, rounds_max=4, window_start='13:00', window_end='20:00')
    window.profile_path.write_text(json.dumps(profile), 'utf-8')
    status.show_agenda()
    assert '2–4' in status.agenda_detail_text.text() and '13:00–20:00' in status.agenda_detail_text.text()
