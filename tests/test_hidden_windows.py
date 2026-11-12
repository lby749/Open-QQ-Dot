"""Task-owned child processes must not open console windows on the owner's desktop (Windows)."""
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import task_runtime


class HiddenWindowTests(unittest.TestCase):
    def process(self):
        process = MagicMock()
        process.communicate.return_value = ('', '')
        process.returncode = 0
        process.poll.return_value = 0
        process.__enter__.return_value = process
        return process

    def test_run_process_hides_console_windows_by_default(self):
        with patch.object(task_runtime.subprocess, 'Popen', return_value=self.process()) as run:
            task_runtime.run_process('pytest -q', shell=True)
        self.assertEqual(run.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_an_explicit_flag_is_kept(self):
        with patch.object(task_runtime.subprocess, 'Popen', return_value=self.process()) as run:
            task_runtime.run_process(['agy'], creationflags=0x10)
        self.assertEqual(run.call_args.kwargs['creationflags'], 0x10)


if __name__ == '__main__':
    unittest.main()
