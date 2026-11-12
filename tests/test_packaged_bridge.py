import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QQ_BRIDGE_HOME', str(Path(__file__).resolve().parents[1]))
import packaged_bridge


class PackagedBridgeTests(unittest.TestCase):
    def test_bootstrap_parent_exit_recovers_its_running_bridge_child(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'bridge.err.log').write_text('', 'utf-8')
            started = False
            child = SimpleNamespace(pid=321, is_running=lambda: True,
                cmdline=lambda: ['python.exe', str(home / 'launcher.py'), '--bridge-run'],
                exe=lambda: 'C:/Python312/python.exe')
            def launch(*args, **kwargs):
                nonlocal started
                started = True
                return SimpleNamespace(pid=123, poll=lambda: 0)
            with patch.object(packaged_bridge, 'ROOT', home), \
                    patch.object(packaged_bridge, 'PID_FILE', home / 'bridge.pid'), \
                    patch.object(packaged_bridge, 'ERROR_FILE', home / 'bridge.err.log'), \
                    patch.object(packaged_bridge.psutil, 'Process', side_effect=packaged_bridge.psutil.NoSuchProcess(123)), \
                    patch.object(packaged_bridge.psutil, 'process_iter', side_effect=lambda *args: [child] if started else []), \
                    patch.object(packaged_bridge.subprocess, 'Popen', side_effect=launch), \
                    patch.object(packaged_bridge.time, 'sleep'):
                self.assertIn('321', packaged_bridge.start())
                self.assertEqual((home / 'bridge.pid').read_text(), '321')

    def test_missing_pid_recovers_bridge_from_another_python_entrypoint(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            process = SimpleNamespace(pid=321, is_running=lambda: True,
                cmdline=lambda: [str(home / '.venv/Scripts/python.exe'), str(home / 'launcher.py'), '--bridge-run'],
                exe=lambda: 'C:/Python313/python.exe')
            with patch.object(packaged_bridge, 'ROOT', home), \
                    patch.object(packaged_bridge, 'PID_FILE', home / 'bridge.pid'), \
                    patch.object(packaged_bridge.psutil, 'process_iter', return_value=[process]):
                self.assertIs(packaged_bridge.bridge_process(), process)
                self.assertEqual((home / 'bridge.pid').read_text(), '321')

    def test_another_project_is_not_our_bridge(self):
        process = SimpleNamespace(is_running=lambda: True,
            cmdline=lambda: ['python.exe', 'D:/another-copy/launcher.py', '--bridge-run'],
            exe=lambda: 'C:/Python313/python.exe')
        self.assertFalse(packaged_bridge._is_local_bridge(process))

    def test_plain_python_process_is_not_a_bridge(self):
        process = SimpleNamespace(is_running=lambda: True, cmdline=lambda: ['python.exe'])
        self.assertFalse(packaged_bridge._is_local_bridge(process))

    def test_legacy_python_entrypoint_is_recognized_for_the_same_project(self):
        home = packaged_bridge.ROOT.resolve()
        process = SimpleNamespace(is_running=lambda: True,
            cmdline=lambda: ['python.exe', '-X', 'utf8', str(home / 'main.py'), 'run'])
        self.assertTrue(packaged_bridge._is_local_bridge(process))
        process.cmdline = lambda: ['python.exe', 'D:/another-copy/main.py', 'run']
        self.assertFalse(packaged_bridge._is_local_bridge(process))

    def test_source_status_uses_the_existing_packaged_bridge(self):
        import house_status
        with patch.dict(os.environ, {'QQ_BRIDGE_LAUNCHER': ''}), \
                patch.object(packaged_bridge, 'bridge_process', return_value=object()), \
                patch.object(packaged_bridge, 'invoke', return_value=(0, 'Running, PID 321')) as invoke, \
                patch.object(house_status.subprocess, 'run') as powershell:
            self.assertEqual(house_status.invoke_bridge('status'), (0, 'Running, PID 321'))
        invoke.assert_called_once_with('status')
        powershell.assert_not_called()
