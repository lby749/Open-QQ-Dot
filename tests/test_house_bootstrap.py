import os
import unittest
from pathlib import Path
from unittest.mock import patch

import house_bootstrap


@unittest.skipUnless(os.name == 'nt', 'Windows installer bootstrap only')
class BootstrapTests(unittest.TestCase):
    def test_first_launch_uses_existing_installer(self):
        with patch.object(Path, 'is_file', return_value=False), \
                patch.object(house_bootstrap.ctypes.windll.user32, 'MessageBoxW', return_value=6), \
                patch.object(house_bootstrap.subprocess, 'call', return_value=1) as install:
            with self.assertRaises(RuntimeError):
                house_bootstrap.main()
        script = Path(install.call_args.args[0][-1])
        self.assertEqual(script.name, 'setup_environment.ps1')
        self.assertTrue(script.is_file())


    def test_runtime_keeps_original_venv_interpreter(self):
        with patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(house_bootstrap.prepare_runtime(Path('project')),
                             Path('project/.venv/Scripts/python.exe'))

    def test_frozen_child_does_not_inherit_bundled_dll_paths(self):
        with patch.dict(os.environ, {'PATH': r'C:\bundle;C:\bundle\bin;C:\Windows\System32',
                                     '_PYI_APPLICATION_HOME_DIR': r'C:\bundle',
                                     '_MEIPASS2': r'C:\bundle'}, clear=True), \
                patch.object(house_bootstrap.sys, '_MEIPASS', r'C:\bundle', create=True), \
                patch.object(house_bootstrap.sys, 'frozen', True, create=True), \
                patch.object(house_bootstrap.ctypes.windll.kernel32, 'SetDllDirectoryW') as reset:
            env = house_bootstrap.child_environment(Path('project'))
        self.assertEqual(env['PATH'], r'C:\Windows\System32')
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR', env)
        self.assertNotIn('_MEIPASS2', env)
        self.assertEqual(env['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        reset.assert_called_once_with(None)
