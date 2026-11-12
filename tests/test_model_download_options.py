"""Regression tests for quick-setup model tiers and the background download UI."""
import unittest
from unittest import mock

from house_qt.quick_setup import PERFORMANCE


class ModelTierTests(unittest.TestCase):
    def test_minimal_tier_does_not_queue_auxiliary_models_besides_default_small_voice(self):
        self.assertEqual(next(iter(PERFORMANCE)), '极小')
        self.assertEqual(PERFORMANCE['极小'][1], [])
        self.assertIn('只自动准备小型 Q8 语音识别', PERFORMANCE['极小'][0])


try:
    from PySide6.QtWidgets import QApplication
    from house_qt.model_download_dialog import ModelDownloadDialog
except Exception:  # optional Qt runtime on Linux/CI
    QApplication = None
    ModelDownloadDialog = None


@unittest.skipIf(ModelDownloadDialog is None, 'PySide6 is not installed')
class DownloadDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_exposes_background_download(self):
        dialog = ModelDownloadDialog([('embedding', 'small')], start=False)
        try:
            self.assertEqual(dialog.background.text(), '后台下载')
            self.assertEqual(dialog.cancel.text(), '取消下载')
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_background_hides_without_cancelling(self):
        import house_qt.model_download_dialog as module
        dialog = ModelDownloadDialog([], start=False)
        try:
            with mock.patch.object(module, 'toast') as toast:
                dialog._background()
            self.assertTrue(dialog.backgrounding)
            self.assertFalse(dialog.cancelled.is_set())
            self.assertFalse(dialog.isVisible())
            toast.assert_called_once()
        finally:
            dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
