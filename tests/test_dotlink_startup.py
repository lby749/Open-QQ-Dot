import json
import os
import unittest
from pathlib import Path

import dotlink


ROOT = Path(__file__).resolve().parents[1]


class DotLinkStartupTests(unittest.TestCase):
    def test_scripts_use_dotlink_and_never_open_a_web_login_page(self):
        for name in ("start_dotlink.ps1", "启动QQ直连.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8-sig")
            self.assertIn("dotlink.py", text)
            self.assertNotIn("webui?", text.lower())
            self.assertNotIn("Start-Process ('http", text)

    def test_prepare_creates_local_only_configuration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "napcat" / "runtime" / "napcat"
            runtime.mkdir(parents=True)
            (runtime.parent / "index.js").write_text(
                "const path = require('path');\nprocess.env.NAPCAT_DISABLE_PIPE = '1';\n", encoding="utf-8")
            (runtime / "napcat.mjs").write_text(
                "let e = this.context.wrapper.NodeQQNTWrapperUtil.getNTUserDataInfoConfig();\n", encoding="utf-8")
            summary = dotlink.prepare(Path(folder))
            self.assertEqual(summary["ws_url"], "ws://127.0.0.1:3001")
            webui = json.loads((Path(folder) / "dotlink/state/config/webui.json").read_text("utf-8"))
            self.assertEqual(webui["host"], "127.0.0.1")
            onebot = json.loads((Path(folder) / "dotlink/state/config/onebot11.json").read_text("utf-8"))
            self.assertTrue(onebot["network"]["websocketServers"][0]["enable"])


if __name__ == "__main__":
    unittest.main()
