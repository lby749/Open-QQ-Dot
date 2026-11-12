import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workspace_tools


class CliNativeWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest('PySide6 is not installed')
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.profile = self.directory / 'friend.json'
        self.profile.write_text(json.dumps({'model_api': {}}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def set_computer(self, **kwargs):
        data = json.loads(self.profile.read_text('utf-8'))
        data['computer_tools'] = kwargs
        self.profile.write_text(json.dumps(data), encoding='utf-8')

    def test_custom_project_folder_binding(self):
        custom_proj = self.directory / 'my_custom_project'
        custom_proj.mkdir()
        self.set_computer(enabled=True, project_path=str(custom_proj))
        self.assertEqual(workspace_tools.workspace(self.directory), custom_proj.resolve())

    def test_read_only_mode_blocks_write(self):
        self.set_computer(enabled=True, commands=False, mode='read_only')
        # write should be blocked
        with self.assertRaises(ValueError) as ctx:
            workspace_tools.call(self.directory, 'desk_write_file', {'path': 'test.txt', 'text': 'hello'})
        self.assertIn('read-only mode', str(ctx.exception))

    def test_whole_computer_access_switch(self):
        outside_file = self.directory.parent / 'outside_test.txt'
        outside_file.write_text('outside content', encoding='utf-8')
        try:
            # Without full access -> blocked
            self.set_computer(enabled=True, full_computer_access=False)
            with self.assertRaises(ValueError):
                workspace_tools.call(self.directory, 'desk_read_file', {'path': str(outside_file)})

            # With full access -> allowed!
            self.set_computer(enabled=True, full_computer_access=True)
            res = workspace_tools.call(self.directory, 'desk_read_file', {'path': str(outside_file)})
            self.assertEqual(res['text'], 'outside content')
        finally:
            if outside_file.exists():
                outside_file.unlink()

    def test_ui_persists_all_new_options(self):
        from house_qt.pages_friend import ModelPage
        from PySide6.QtWidgets import QMainWindow
        with patch('house_qt.pages_friend.ROOT', self.directory):
            house = QMainWindow()
            house.friend_dir = self.directory
            house.profile_path = self.profile
            house.current_qq = '123456'
            house.records = {}
            page = ModelPage(house)
            custom_dir = str(self.directory / 'my_ide_project')
            page.computer_enabled.setChecked(True)
            page.computer_commands.setChecked(True)
            page.computer_project.setText(custom_dir)
            page.computer_full_access.setChecked(True)
            page.computer_mode.setCurrentIndex(1)  # read_only
            page.computer_approval.setCurrentIndex(1)  # on-request
            page.save_computer_tools()

            saved = json.loads(self.profile.read_text('utf-8'))['computer_tools']
            self.assertTrue(saved['enabled'])
            self.assertTrue(saved['commands'])
            self.assertEqual(saved['project_path'], custom_dir)
            self.assertTrue(saved['full_computer_access'])
            self.assertEqual(saved['mode'], 'read_only')
            self.assertEqual(saved['approval_mode'], 'on-request')


if __name__ == '__main__':
    unittest.main()
