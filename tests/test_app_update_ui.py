import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QWidget
import pytest

from house_qt import updates
import app_update
import house_settings
import time
from version import VERSION


@pytest.fixture
def page(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    calls = []
    monkeypatch.setattr(updates, 'ROOT', tmp_path)
    monkeypatch.setattr(updates, 'run_async', lambda job, done, error, **_: calls.append((job, done, error)))
    widget = updates.UpdatePage(SimpleNamespace(exiting=False))
    yield widget, calls, application
    widget.close()
    widget.deleteLater()
    application.processEvents()


def test_check_deduplicates_inflight_request_and_renders_notes_as_plain_text(page):
    widget, calls, _ = page
    widget.check_updates(automatic=False)
    widget.check_updates(automatic=False)
    assert len(calls) == 1 and not widget.check_button.isEnabled()
    calls[0][1]({'status': 'available', 'release': {'version': '1.1.0', 'notes': '<script>plain text</script>',
                                                 'url': 'https://github.com/example/application/releases/tag/v1.1.0'}})
    assert widget.check_button.isEnabled() and widget.open_button.isEnabled()
    assert widget.notes.toPlainText() == '<script>plain text</script>'
    widget.check_updates(automatic=False)
    calls[1][2](OSError('private diagnostic'))
    assert not widget.open_button.isEnabled() and not widget.notes.toPlainText()
    assert 'private diagnostic' not in widget.status.text()


def test_preference_is_global_and_does_not_overwrite_other_settings(page, tmp_path):
    widget, _, _ = page
    house_settings.save({'qq_connection_mode': 'official', 'unrelated': 'preserved'}, tmp_path)
    widget.automatic.setChecked(False)
    saved = house_settings.load(tmp_path)
    assert saved == {'qq_connection_mode': 'official', 'unrelated': 'preserved', 'app_auto_check_updates': False}


def test_failed_preference_write_restores_checkbox(page, monkeypatch):
    widget, _, _ = page
    def fail(*_):
        raise PermissionError('unwritable')
    monkeypatch.setattr(house_settings, 'save', fail)
    widget.automatic.setChecked(False)
    assert widget.automatic.isChecked()
    assert '未保存' in widget.status.text()


def test_unconfigured_source_does_not_start_background_network(page, monkeypatch):
    widget, calls, _ = page
    monkeypatch.setattr(app_update, 'repository', lambda: '')
    widget.check_updates(automatic=True)
    widget.shown()
    assert not calls and '尚未配置更新来源' in widget.status.text()


def test_feedback_opens_only_the_configured_repository_without_attaching_data(page, monkeypatch):
    widget, _, _ = page
    opened = []
    monkeypatch.setattr(updates.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    monkeypatch.setattr(app_update, 'repository', lambda: '')
    widget.shown()
    widget.open_feedback()
    assert not widget.feedback_button.isEnabled() and not opened
    monkeypatch.setattr(app_update, 'repository', lambda: 'example/application')
    widget.shown()
    widget.open_feedback()
    assert widget.feedback_button.isEnabled()
    assert opened == ['https://github.com/example/application/issues/new/choose']


def test_real_notification_is_shown_once_per_new_version(page, tmp_path, monkeypatch):
    widget, _, application = page
    house = QWidget()
    house.switchTo = lambda _: None
    widget.house = house
    monkeypatch.setattr(app_update, 'repository', lambda: 'example/application')
    app_update._save(tmp_path, {'repository': 'example/application', 'current': VERSION,
        'checked_at': time.time(), 'status': 'available',
        'release': {'version': '1.1.0', 'tag': 'v1.1.0', 'notes': '中文更新内容'}})
    widget.finished(app_update.check(tmp_path), automatic=True)
    application.processEvents()
    assert len(house.findChildren(updates.InfoBar)) == 1
    assert app_update.state(tmp_path)['notified_version'] == '1.1.0'
    widget.finished(app_update.check(tmp_path), automatic=True)
    application.processEvents()
    assert len(house.findChildren(updates.InfoBar)) == 1
    house.close()
    house.deleteLater()
    application.processEvents()
