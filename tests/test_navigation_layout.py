import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QWheelEvent

import tests.test_house_qt as fixture_module
from house_qt.common import SectionSwitcher
from tests.test_voice_console import microphone


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


def test_original_settings_is_below_friends_and_unverified_accounts_is_last(house):
    window,app=house
    window.resize(1250,850);window.show();app.processEvents()
    panel=window.navigationInterface.panel
    friends=panel.widget(window.workspaces['friends'].objectName())
    settings=panel.widget(window.workspaces['settings'].objectName())
    assert panel.scrollWidget.isAncestorOf(settings)
    assert settings.mapTo(panel.scrollWidget,QPoint()).y()>friends.mapTo(panel.scrollWidget,QPoint()).y()
    assert settings.mapTo(panel.scrollWidget,QPoint()).y()<panel.widget(window.workspaces['connection'].objectName()).mapTo(panel.scrollWidget,QPoint()).y()
    development=panel.widget(window.workspaces['development'].objectName())
    tutorial=panel.widget(window.workspaces['tutorial'].objectName())
    assert development.mapTo(panel,QPoint()).y()>tutorial.mapTo(panel,QPoint()).y()
    assert window.workspaces['development'].tabs.widget(window.workspaces['development'].tabs.count()-1) is window.pages['accountsPreviewPage']
    agents=window.workspaces['agents'];agents.tabs.setCurrentIndex(1)
    window.switchTo(agents);app.processEvents()
    assert agents.tabs.currentWidget() is window.pages['modelPage']


def test_prepared_template_is_visible_and_switching_friend_drops_old_state(house,monkeypatch):
    import json,persona_template
    from PySide6.QtWidgets import QLabel
    from house_qt.quick_setup import build_prompts
    window,app=house
    monkeypatch.setattr(persona_template,'list_templates',lambda:[])
    profile=json.loads(window.profile_path.read_text('utf-8'))
    _,_,profile['prompt_presets']=build_prompts('冷静理性','好朋友','不透露','中性','chat','关闭主动')
    window.profile_path.write_text(json.dumps(profile),'utf-8')
    page=window.pages['profilePage'].template_page;page.refresh();app.processEvents()
    text=' '.join(w.text() for row in page.rows for w in ([row] if isinstance(row,QLabel) else row.findChildren(QLabel)))
    assert '冷静理性' in text and '12 条' in text and '尚无模板' not in text
    profile.pop('prompt_presets');window.profile_path.write_text(json.dumps(profile),'utf-8')
    page.refresh();app.processEvents()
    text=' '.join(w.text() for row in page.rows for w in ([row] if isinstance(row,QLabel) else row.findChildren(QLabel)))
    assert '冷静理性' not in text


def test_compact_console_keeps_visible_buttons_inside_one_row(house):
    window,app=house;console=window.pages['workbenchPage']
    window.switchTo(console)
    for width in (1050,1460,1180,1050):
        window.resize(width,800);window.show();app.processEvents();console.fit_input_controls();app.processEvents()
        controls=[console.btn_attach,console.voice_btn,console.provider_combo,console.model_btn,
                  console.access_badge,console.options_btn,console.send_btn]
        visible=[control for control in controls if control.isVisible()]
        for control in visible:
            rect=control.geometry()
            assert console.input_card.rect().contains(rect), (width,control.text(),rect,console.input_card.rect())
        for left,right in zip(visible,visible[1:]):assert not left.geometry().intersects(right.geometry())
        assert max(c.geometry().bottom() for c in visible)-min(c.y() for c in visible)<=38
        assert console.input_card.height()<=135
    profile={'computer_tools':{'enabled':False},'model_api':{'provider':'codex'}}
    console.update_access_badge(profile)
    assert console.access_badge.text()=='仅聊天'


def test_persona_editor_refreshes_external_changes_but_preserves_unsaved_draft(house, monkeypatch):
    import json
    import house_qt.pages_library as library
    window, app = house
    page = window.pages['profilePage'].template_page
    page.load()
    profile = json.loads(window.profile_path.read_text('utf-8'))
    profile['persona'] = '快速配置后更新的常驻指令'
    window.profile_path.write_text(json.dumps(profile), 'utf-8')
    page.shown()
    assert page.persona.toPlainText() == profile['persona']
    page.persona.insertPlainText('，未保存草稿')
    draft = page.persona.toPlainText()
    page.shown()
    assert page.persona.toPlainText() == draft
    messages = []
    monkeypatch.setattr(library, 'toast', lambda *args: messages.append(args))
    profile['persona'] = '另一个编辑入口刚保存的内容'
    window.profile_path.write_text(json.dumps(profile), 'utf-8')
    page.save_persona()
    assert json.loads(window.profile_path.read_text('utf-8'))['persona'] == profile['persona']
    assert messages and '其他' in str(messages[-1])


def test_console_enter_during_chinese_composition_does_not_submit(house, monkeypatch):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QInputMethodEvent, QKeyEvent
    window, app = house
    console = window.pages['workbenchPage']
    submitted = []
    monkeypatch.setattr(console, 'send_message', lambda: submitted.append(True))
    app.sendEvent(console.input_edit, QInputMethodEvent('拼音候选', []))
    app.sendEvent(console.input_edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert not submitted
    commit = QInputMethodEvent()
    commit.setCommitString('中文')
    app.sendEvent(console.input_edit, commit)
    app.sendEvent(console.input_edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert submitted == [True]


def test_voice_input_settings_are_visible_before_the_bulk_download_table(house):
    window, app = house
    page = window.pages['modelPage']
    window.switchTo(page)
    page.switch_model_section('local')
    window.show()
    app.processEvents()
    assert page._section_cards['local'][0] is page.voice_in_card
    assert page.voice_in_tier.isVisible() and page.voice_in_download.isVisible()
    assert all(page.column.indexOf(page.voice_in_card) < page.column.indexOf(card)
               for card in page._section_cards['local'][1:])


def test_voice_button_records_on_first_click_and_stops_on_second(house, microphone, monkeypatch):
    from PySide6.QtWidgets import QDialog
    import voice_input
    import house_qt.voice_capture as capture
    window, app = house
    console = window.pages['workbenchPage']
    window.switchTo(console);window.show();app.processEvents()
    monkeypatch.setattr(voice_input, 'local_ready', lambda *args: True)
    monkeypatch.setattr(voice_input, 'transcribe_local', lambda *args: ('识别结果', ''))
    pending=[]
    monkeypatch.setattr(capture, 'run_async', lambda job,done,error,**kw:pending.append((job,done,error)))
    submitted=[];monkeypatch.setattr(console, 'send_message', lambda:submitted.append(True))
    icon = console.voice_btn.icon().cacheKey()
    console.voice_btn.click()
    assert console._voice_capture.state == 'recording' and console.voice_btn.text() == '停止录音'
    assert console.voice_btn.icon().cacheKey() != icon
    assert not any(isinstance(widget,QDialog) and widget.isVisible() for widget in app.topLevelWidgets())
    window.resize(1050,800);app.processEvents();console.fit_input_controls();app.processEvents()
    controls=[w for w in (console.btn_attach,console.voice_btn,console.provider_combo,console.model_btn,
              console.access_badge,console.options_btn,console.send_btn) if w.isVisible()]
    assert all(console.input_card.rect().contains(w.geometry()) for w in controls)
    assert all(not a.geometry().intersects(b.geometry()) for a,b in zip(controls,controls[1:]))
    console.voice_btn.click()
    assert console.voice_btn.text() == '转写中' and not console.voice_btn.isEnabled()
    job,done,_ = pending[0];done(job(None))
    assert console.voice_btn.text() == '语音' and console.voice_btn.isEnabled()
    assert '识别结果' in console.input_edit.toPlainText() and not submitted


def test_voice_recording_stops_when_switching_views_and_ignores_late_result(house, microphone, monkeypatch):
    import voice_input
    import house_qt.voice_capture as capture
    window, app = house
    console = window.pages['workbenchPage']
    window.switchTo(console);window.show();app.processEvents()
    monkeypatch.setattr(voice_input, 'local_ready', lambda *args: True)
    pending=[]
    monkeypatch.setattr(capture, 'run_async', lambda job,done,error,**kw:pending.append(done))
    console.voice_btn.click();console.voice_btn.click()
    window.switchTo(window.overview);app.processEvents()
    pending[0]('旧录音不得插入')
    assert console._voice_capture.state == 'idle' and console._voice_capture.audio is None
    assert '旧录音不得插入' not in console.input_edit.toPlainText()


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
