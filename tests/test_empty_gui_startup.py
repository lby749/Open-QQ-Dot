import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from house_qt import window
    from house_qt.common import init_ui_thread
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, 'PySide6 is not installed')
class EmptyGuiStartupTests(unittest.TestCase):
    def test_new_install_without_friends_opens(self):
        app = QApplication.instance() or QApplication([])
        init_ui_thread()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(window, 'ROOT', root), \
                    patch.object(window, 'REGISTRY', root / 'friends.json'):
                panel = window.HouseWindow()
                self.assertIsNone(panel.current_qq)
                self.assertEqual(panel.windowTitle(), window.TITLE)
                self.assertIs(panel.stackedWidget.currentWidget(), panel.workspaces['home'])
                self.assertIs(panel.workspaces['home'].tabs.currentWidget(), panel.pages['workbenchPage'])
                # The library emits triggerByUser(bool), not the target QWidget.
                for target, route in panel._sidebar_routes.items():
                    panel.navigationInterface.widget(route).clicked.emit(True)
                    app.processEvents()
                    workspace = panel._workspaces_for_page[target]
                    self.assertIs(panel.stackedWidget.currentWidget(), workspace)
                    self.assertIs(workspace.tabs.currentWidget(), target)
                panel.tray.hide()
                panel.bridge_pool.shutdown(wait=False)
                panel.deleteLater()
                app.sendPostedEvents()
