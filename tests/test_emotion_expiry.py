import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
from store import FriendStore



class EmotionExpiryTests(unittest.TestCase):
    def test_model_and_display_expire_at_five_hours_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = FriendStore(directory)
            store.record_emotions('happy', '好奇', 'test')
            store.set('emotion_state', json.dumps({'user': 'happy', 'self': '好奇', 'updated_at': 1000}))
            count = len(store.recent_emotions())
            for elapsed, expired in [(17999, False), (18000, True), (18001, True)]:
                with self.subTest(elapsed=elapsed), patch('time.time', return_value=1000 + elapsed):
                    expected = {'user': 'uncertain', 'self': 'calm'} if expired else {'user': 'happy', 'self': '好奇'}
                    self.assertEqual(main.current_emotions(store), expected)
            self.assertEqual(len(store.recent_emotions()), count)
            self.assertEqual(json.loads(store.get('emotion_state'))['user'], 'happy')
            store.close()


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'PySide6 is not installed')
class EmotionPageExpiryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_qt_page_shows_expired_state(self):
        from house_qt.pages_more import EmotionPage
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = FriendStore(directory)
            store.set('emotion_state', json.dumps({'user': 'happy', 'self': '好奇', 'updated_at': 1000}))
            store.close()
            page = EmotionPage(SimpleNamespace(profile_path=directory / 'friend.json', friend_dir=directory, current_qq='12345'))
            with patch('time.time', return_value=1000 + 18000):
                page.refresh()
            self.assertIn('已过期', page.current.text())
