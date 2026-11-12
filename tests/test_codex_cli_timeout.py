import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

import main


class CodexCliTimeoutTests(unittest.TestCase):
    def test_success_preserves_output_and_closes_stdin(self):
        result = main.run_codex_cli(
            [sys.executable, '-c', 'import sys; print(sys.stdin.read()); print("diagnostic", file=sys.stderr)'],
            '测试输入',
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '测试输入')
        self.assertEqual(result.stderr.strip(), 'diagnostic')

    def test_default_waits_five_minutes(self):
        with patch.object(main.subprocess, 'Popen') as popen:
            popen.return_value.communicate.return_value = ('output', '')
            main.run_codex_cli(['codex.cmd'], 'prompt')
        popen.return_value.communicate.assert_called_once_with(input='prompt', timeout=300)

    def test_timeout_kills_descendant_and_does_not_expose_private_output(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'child.pid'
            script = (
                'import subprocess,sys,time; from pathlib import Path; '
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                'Path(sys.argv[1]).write_text(str(child.pid)); '
                'print("PRIVATE_OUTPUT",flush=True); time.sleep(60)'
            )
            with self.assertRaises(TimeoutError) as error:
                main.run_codex_cli([sys.executable, '-c', script, str(marker)], 'PRIVATE_PROMPT', timeout=2)
            self.assertNotIn('PRIVATE', str(error.exception))
            self.assertNotIn('Command', str(error.exception))
            self.assertTrue('超过 2 秒' in str(error.exception) or '2 seconds' in str(error.exception))
            self.assertTrue(marker.exists())
            try:
                child = psutil.Process(int(marker.read_text()))
            except psutil.NoSuchProcess:
                return
            _, alive = psutil.wait_procs([child], timeout=5)
            self.assertEqual(alive, [])


if __name__ == '__main__':
    unittest.main()
