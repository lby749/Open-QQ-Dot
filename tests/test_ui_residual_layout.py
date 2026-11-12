"""Offline regression checks for late history reads and narrow desktop pages."""
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QBoxLayout, QWidget

import house_qt.common as common
import house_qt.pages_more as pages_more
from house_qt.task_center import TaskCenterPage
from house_qt.collaboration import CollaborationPage


@pytest.fixture(scope='module')
def app():
    application = QApplication.instance() or QApplication([])
    common.init_ui_thread()
    return application


@pytest.fixture
def history(tmp_path, monkeypatch, app):
    calls, failures = [], []
    monkeypatch.setattr(pages_more, 'run_async', lambda read, done, failed: calls.append((read, done, failed)))
    monkeypatch.setattr(pages_more, 'toast', lambda *args: failures.append(args))
    house = SimpleNamespace(current_qq='12345', friend_dir=tmp_path / '12345')
    page = pages_more.HistoryPage(house)
    page.resize(760, 650)
    page.show()
    app.processEvents()
    yield page, house, calls, failures
    page.close()
    page.deleteLater()
    app.processEvents()


def rows(prefix='消息', count=80):
    return [{'created_at': '2026-11-11T12:00:00', 'direction': 'friend', 'body': f'{prefix}{i}'} for i in range(count)]


def test_history_clears_old_friend_and_rejects_late_success_or_error(history):
    page, house, calls, failures = history
    page.refresh()
    old = calls[-1]
    house.current_qq = '67890'
    house.friend_dir = house.friend_dir.parent / '67890'
    page.refresh()
    assert not page.text.toPlainText()
    old[1]((rows('旧好友', 1), 1))
    old[2](RuntimeError('old owner error'))
    assert not page.text.toPlainText() and not failures
    calls[-1][1]((rows('当前好友', 1), 1))
    assert '当前好友' in page.text.toPlainText()
    house.current_qq = '12345'
    house.friend_dir = house.friend_dir.parent / '12345'
    page.refresh()
    old[1]((rows('同一好友的过期快照', 1), 1))
    assert not page.text.toPlainText()


def test_history_coalesces_pending_reads_and_preserves_scroll_and_selection(history, app):
    page, _, calls, _ = history
    page.refresh()
    page.refresh()
    assert len(calls) == 1
    data = rows()
    calls[-1][1]((data, len(data)))
    app.processEvents()
    bar = page.text.verticalScrollBar()
    assert bar.maximum() > 0
    cursor = page.text.textCursor()
    cursor.setPosition(12)
    cursor.setPosition(16, QTextCursor.KeepAnchor)
    page.text.setTextCursor(cursor)
    bar.setValue(bar.maximum() // 2)
    position, selection = bar.value(), page.text.textCursor().selectedText()
    page.refresh()
    calls[-1][1]((data, len(data)))
    app.processEvents()
    assert bar.value() == position
    assert page.text.textCursor().selectedText() == selection
    assert 'showing' not in page.count.text() and 'Friend' not in page.text.toPlainText()


def test_hidden_friend_change_does_not_leave_history_read_permanently_pending(history):
    page, house, calls, _ = history
    page.refresh()
    old = calls[-1]
    house.current_qq = '67890'  # another visible page handled this friend switch
    old[1]((rows('已丢弃的旧数据', 1), 1))
    assert not page.text.toPlainText()
    house.current_qq = '12345'
    page.refresh()
    assert len(calls) == 2
    calls[-1][1]((rows('重新读取成功', 1), 1))
    assert '重新读取成功' in page.text.toPlainText()


def test_history_only_follows_new_messages_when_already_at_bottom(history, app):
    page, _, calls, _ = history
    data = rows()
    page.refresh()
    calls[-1][1]((data, len(data)))
    app.processEvents()
    bar = page.text.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    position = bar.value()
    page.refresh()
    calls[-1][1]((data + rows('新增', 1), len(data) + 1))
    app.processEvents()
    assert abs(bar.value() - position) <= 2
    bar.setValue(bar.maximum())
    page.refresh()
    calls[-1][1]((data + rows('新增', 3), len(data) + 3))
    app.processEvents()
    assert bar.maximum() - bar.value() <= 2
    page.load_older()
    calls[-1][1]((rows('更早', 20) + data, 100))
    app.processEvents()
    assert bar.value() == bar.minimum()


def test_task_center_adapts_at_narrow_width_without_nested_form_card(app):
    page = TaskCenterPage(SimpleNamespace(current_qq='12345'))
    try:
        for width, expected in ((760, QBoxLayout.TopToBottom), (1100, QBoxLayout.LeftToRight), (650, QBoxLayout.TopToBottom)):
            page.resize(width, 650)
            page.show()
            app.processEvents()
            assert page.create_row.direction() == expected
            assert page.horizontalScrollBar().maximum() == 0
            assert page.content.width() == page.viewport().width()
        assert not page.column.itemAt(0).widget().findChildren(common.Card)
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_collaboration_legend_wraps_instead_of_forcing_a_long_horizontal_line(monkeypatch, app):
    house = QWidget()
    house.current_qq = '12345'
    monkeypatch.setattr(CollaborationPage, 'refresh', lambda self: None)
    page = CollaborationPage(house)
    try:
        page.timer.stop()
        assert page.legend.wordWrap()
        assert page.legend.minimumSizeHint().width() < 400
        assert page.legend.heightForWidth(400) > page.legend.heightForWidth(1000)
    finally:
        page.timer.stop()
        page.close()
        house.close()
        page.deleteLater()
        house.deleteLater()
        app.processEvents()
