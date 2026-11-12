import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.package_release import create_clean_package, scan_for_leaks


class Packaging(unittest.TestCase):
    def test_json_quoted_secret_detected_even_in_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);test=root/'test_config.py'
            test.write_text('config = {"api_key": "'+'sk-'+'z'*32+'"}',encoding='utf-8')
            result=scan_for_leaks(root,[test])
            self.assertTrue(result)
            self.assertNotIn('z'*32,' '.join(result))

    def test_release_excludes_private_state_and_models_and_writes_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp);root=parent/'source';root.mkdir()
            (root/'main.py').write_text('print("synthetic")',encoding='utf-8')
            (root/'friends').mkdir();(root/'friends'/'private.txt').write_text('private')
            (root/'api-key.bin').write_bytes(b'private')
            (root/'weights.gguf').write_bytes(b'not a real model')
            (root/'house.local.json').write_text('{}')
            output=parent/'release.zip'
            count,_=create_clean_package(root,output)
            self.assertEqual(count,1)
            with zipfile.ZipFile(output) as z:
                self.assertEqual(z.namelist(),['Open-QQ-Dot/main.py'])
                self.assertIsNone(z.testzip())
            self.assertIn(hashlib.sha256(output.read_bytes()).hexdigest(),output.with_suffix('.zip.sha256').read_text())


if __name__=='__main__':
    unittest.main()
