import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import house_status



class TextStub:
    text = ""

    def get(self, *args):
        return self.text

    def configure(self, **kwargs):
        pass

    def delete(self, *args):
        self.text = ""

    def insert(self, position, text):
        self.text = text

    def yview_moveto(self, position):
        self.position = position


class ActivityTests(unittest.TestCase):
    def test_compact_history_groups_repeats_and_preserves_raw_file_and_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'activity.log'
            lines = [f'[2026-11-10T20:00:{i:02d}-05:00] 同一会话错误' for i in range(60)]
            lines.append('[2026-11-11T05:03:00-05:00] database is locked')
            raw = '\n'.join(lines)
            path.write_text(raw, 'utf-8')
            text = house_status.recent_activity(Path(folder), compact=True)
            self.assertEqual(text.count('同一会话错误'), 1)
            self.assertIn('连续出现 60 次', text)
            self.assertIn('2026-11-10T20:00:00-05:00', text)
            self.assertIn('2026-11-10T20:00:59-05:00', text)
            self.assertIn('数据库暂被占用', text)
            self.assertEqual(path.read_text('utf-8'), raw)
            self.assertEqual(house_status.recent_activity(Path(folder), limit=100).count('同一会话错误'), 60)

    def test_full_qq_number_separates_friends_with_same_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            friends = root / "friends"
            for qq in ("123451681", "555000001"):
                (friends / qq).mkdir(parents=True)
            with patch.object(main, "FRIENDS_DIR", friends), contextlib.redirect_stdout(io.StringIO()):
                bridge = main.Bridge()
                bridge.log("friend A received message", "123451681")
                bridge.log("friend B proactiveSend", "555000001")
                bridge.log("NapCat medium")
            for qq, expected, excluded in (("123451681", "friend A", "friend B"), ("555000001", "friend B", "friend A")):
                text = house_status.recent_activity(friends / qq)
                self.assertIn(expected, text)
                self.assertNotIn(excluded, text)
                self.assertNotIn("NapCat", text)
            self.assertEqual(house_status.recent_activity(friends / "99999"), "这位好友暂无小动态")
