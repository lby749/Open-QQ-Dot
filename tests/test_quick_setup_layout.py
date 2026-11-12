"""The longest wizard step must not push navigation outside a laptop screen."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QScrollArea, QWidget

import house_qt.quick_setup as setup


def test_all_steps_keep_navigation_visible_and_long_content_scrolls(tmp_path, monkeypatch):
    import house_status

    monkeypatch.setattr(house_status, 'ROOT', tmp_path)
    monkeypatch.setattr(setup, 'detect_agents', lambda: [
        {'id': 'codex', 'label': 'Codex CLI', 'model': 'gpt-6-luna', 'effort': 'low', 'rank': 0}])
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    dialog = setup.QuickSetupDialog(parent, '12345')
    dialog.resize(820, 640)
    dialog.show()
    app.processEvents()
    try:
        for step in range(3):
            dialog.pages.setCurrentIndex(step)
            dialog._update_nav()
            app.processEvents()
            assert dialog.height() <= 640
            assert dialog.rect().contains(dialog.next.geometry())
            assert dialog.next.isVisible()
            assert isinstance(dialog.pages.currentWidget(), QScrollArea)
        dialog.pages.setCurrentIndex(1)
        app.processEvents()
        area = dialog.pages.currentWidget()
        assert area.verticalScrollBar().maximum() > 0
        area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
        app.processEvents()
        assert dialog.next.isVisible()
        assert dialog.preview.isVisible()
    finally:
        dialog.reject()
        parent.close()
