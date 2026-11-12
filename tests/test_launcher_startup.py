import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher


@unittest.skipUnless(os.name == 'nt', 'DotLink launcher tests are Windows-specific')
class LauncherStartupTests(unittest.TestCase):
    def test_successful_dotlink_start_returns_empty_message(self):
        with patch("dotlink.ensure_started", return_value=""):
            self.assertEqual(launcher.start_dotlink(Path(__file__).resolve().parents[1]), "")

    def test_dotlink_error_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("dotlink.ensure_started", side_effect=RuntimeError("testfailed")):
                self.assertIn("testfailed", launcher.start_dotlink(Path(folder)))

    def test_missing_runtime_is_reported(self):
        with patch("dotlink.ensure_started", return_value="QQ 直连运行时不完整"):
            self.assertIn("运行时不完整", launcher.start_dotlink(Path(".")))


if __name__ == "__main__":
    unittest.main()
