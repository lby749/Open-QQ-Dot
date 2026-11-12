"""friend.json saves must survive a reader holding the file (Windows WinError 5 on os.replace)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import house_settings
import house_status


class ReplaceFileTests(unittest.TestCase):
    def test_sharing_violation_is_waited_out(self):
        real, calls = house_settings.os.replace, []

        def busy_twice(source, target):
            if Path(target).name != 'friend.json':  # the source-state token has its own writer
                return real(source, target)
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, 'Access is denied', None, 5)
            return real(source, target)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'friend.json'
            path.write_text('{"auto_reply": true}', encoding='utf-8')
            with patch.object(house_settings.os, 'replace', side_effect=busy_twice):
                house_status.write_profile(path, {'auto_reply': False})
            self.assertEqual(json.loads(path.read_text('utf-8')), {'auto_reply': False})
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])  # no temp file left

    def test_a_file_that_stays_locked_still_fails_without_leaving_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'friend.json'
            path.write_text('{}', encoding='utf-8')
            with patch.object(house_settings.os, 'replace', side_effect=PermissionError(13, 'denied')), \
                    patch('time.sleep'):
                with self.assertRaises(PermissionError):
                    house_status.write_profile(path, {'x': 1})
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
