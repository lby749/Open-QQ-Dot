"""Actual labels, native menus, command routing and old/new notification compatibility."""
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import importlib.util

import pytest
from ui_locale import tr

if importlib.util.find_spec('PySide6') is None:
    pytest.skip('PySide6 is not installed', allow_module_level=True)


@pytest.fixture(autouse=True)
def chinese(monkeypatch):
    monkeypatch.setenv('OPENQQ_UI_LANG', 'zh-CN')


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_labels_are_chinese_and_identifiers_remain_exact():
    for source, translated in (('Copy content', '复制内容'), ('Save Changes', '保存修改'),
                               ('Intent / Topic', '任务意图 / 主题'), ('Provider', '来源')):
        assert tr(source) == translated
    for identifier in ('api_weather', 'gpt-6-luna', 'BAAI/bge-m3', 'https://example.com/v1/models', 'My English Project'):
        assert tr(identifier) == identifier
    assert tr('State transition -> WAITING_APPROVAL') == '状态变更：等待确认'


def test_explicit_english_override_is_still_supported(monkeypatch):
    monkeypatch.setenv('OPENQQ_UI_LANG', 'en')
    assert tr('Copy content') == 'Copy content'


def test_dialog_buttons_and_titles_are_translated(app):
    from unittest.mock import Mock, patch
    from house_qt import common
    dialog = Mock()
    dialog.exec.return_value = True
    with patch.object(common, 'MessageBox', return_value=dialog) as factory:
        assert common.confirm(None, 'Clear Chat', 'Save Changes')
    assert factory.call_args.args[:2] == ('清空对话', '保存修改')
    dialog.cancelButton.setText.assert_called_once_with('取消')


def test_native_qt_menu_defaults_to_chinese_without_rewriting_text(app):
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QLineEdit
    from house_qt.window import install_language
    install_language(app)
    assert QLocale().language() == QLocale.Chinese
    entry = QLineEdit('api_weather')
    entry.selectAll()
    menu = entry.createStandardContextMenu()
    try:
        labels = [action.text().replace('&', '').split('\t')[0] for action in menu.actions()]
        assert any('复制' in label for label in labels), labels
        assert not any(label == 'Copy' for label in labels)
        assert entry.text() == 'api_weather'
    finally:
        menu.deleteLater()
        entry.deleteLater()


def test_chinese_quick_commands_keep_old_action_ids_and_english_search(app):
    from PySide6.QtWidgets import QWidget, QLabel
    from house_qt.agent_console_widget import CommandSuggestionWidget
    widget = CommandSuggestionWidget(QWidget())
    try:
        widget.show_slash_commands('engine')
        assert widget.list_widget.count() >= 1
        assert any(item[-1] == 'action_switch_provider' for item in widget.items_data)
        assert any('模型引擎' in label.text() for label in widget.findChildren(QLabel))
        widget.show_slash_commands('思考')
        assert any(item[-1] == 'action_effort_toggle' for item in widget.items_data)
    finally:
        widget.deleteLater()


@pytest.mark.parametrize('old,new,status', [
    ('Task completed: ', '任务已完成：', 'completed'),
    ('Task failed: ', '任务失败：', 'failed'),
    ('Task needs your review: ', '任务需要确认：', 'review')])
def test_old_notifications_and_chinese_notifications_have_the_same_facts(old, new, status):
    from notification_chat import event_facts
    notice = lambda title: SimpleNamespace(category='task', metadata={}, title=title + '打开网页', content='')
    expected = event_facts(notice(old))
    assert event_facts(notice(new)) == expected
    assert expected['status'] == status
    assert expected['subject'] == '打开网页'
