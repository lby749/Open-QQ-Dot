import unittest
from pathlib import Path
import dsh_backend


class TestDshBackend(unittest.TestCase):
    def test_invalid_approval_mode_cannot_expand_permissions(self):
        from unittest.mock import patch
        with patch.object(dsh_backend, 'cli_path', return_value='trusted-dsh.cmd'), \
             patch.object(dsh_backend, 'desktop_patch_path', return_value=None), \
             patch('workspace_tools.enabled', return_value={'enabled': True, 'commands': True,
                                                           'full_computer_access': True}), \
             patch.object(dsh_backend, 'run_process') as native:
            with self.assertRaises(ValueError):
                dsh_backend.generate('execute once', Path.cwd(), approval_mode='unknown-mode')
            native.assert_not_called()

    def test_uncertain_result_cannot_trigger_local_model_fallback(self):
        from unittest.mock import patch
        import main
        profile = {'model_api': {'provider': 'dsh', 'local_fallback': True}}
        with patch.object(dsh_backend, 'generate', side_effect=InterruptedError('执行结果未确认')), \
             patch.object(main, '_local_fallback') as fallback:
            with self.assertRaises(InterruptedError):
                main.codex_text('execute once', Path.cwd(), ephemeral=True, profile=profile)
            fallback.assert_not_called()

    def test_native_uncertain_result_never_replays_through_api(self):
        import subprocess
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_errors import execute_with_retry

        cases = [
            subprocess.TimeoutExpired('dsh', 180),
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=1, stdout='', stderr='tool execution failed'),
        ]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            key = folder/'api-key'
            key.write_text('local-test-key', encoding='utf-8')
            for result in cases:
                with self.subTest(result=result), \
                     patch.object(dsh_backend, 'cli_path', return_value='trusted-dsh.cmd'), \
                     patch.object(dsh_backend, 'desktop_patch_path', return_value=None), \
                     patch.object(dsh_backend, 'load_credentials_env', return_value={}), \
                     patch('workspace_tools.enabled', return_value={'enabled': True, 'commands': True,
                                                                   'full_computer_access': True}), \
                     patch('workspace_tools.workspace', return_value=folder), \
                     patch('api_backend.key_path', return_value=key), \
                     patch('api_backend.generate') as fallback, \
                     patch.object(dsh_backend, 'run_process') as native:
                    if isinstance(result, Exception):
                        native.side_effect = result
                    else:
                        native.return_value = result
                    with self.assertRaises((InterruptedError, RuntimeError)):
                        execute_with_retry('dsh', lambda: dsh_backend.generate('execute once', folder),
                                           max_retries=1, base_delay=0)
                    self.assertEqual(native.call_count, 1)
                    fallback.assert_not_called()

    def test_missing_native_credentials_can_use_explicit_api_key(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            key = folder/'api-key'
            key.write_text('local-test-key', encoding='utf-8')
            with patch.object(dsh_backend, 'cli_path', return_value='trusted-dsh.cmd'), \
                 patch.object(dsh_backend, 'desktop_patch_path', return_value=None), \
                 patch.object(dsh_backend, 'load_credentials_env', return_value={}), \
                 patch('workspace_tools.enabled', return_value={'enabled': True, 'commands': True,
                                                               'full_computer_access': True}), \
                 patch('workspace_tools.workspace', return_value=folder), \
                 patch('api_backend.key_path', return_value=key), \
                 patch('api_backend.generate', return_value='api result') as fallback, \
                 patch.object(dsh_backend, 'run_process', return_value=SimpleNamespace(
                     returncode=1, stdout='', stderr='MISSING_CREDENTIAL')):
                text, meta = dsh_backend.generate('execute once', folder)
                self.assertEqual(text, 'api result')
                self.assertFalse(meta['native'])
                fallback.assert_called_once()

    def test_native_cli_uses_configured_project_directory(self):
        import json
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            friend = Path(folder)/'friend'; project = Path(folder)/'project'
            friend.mkdir(); project.mkdir()
            (friend/'friend.json').write_text(json.dumps({'computer_tools': {
                'enabled': True, 'commands': True, 'full_computer_access': True,
                'project_path': str(project)}}), encoding='utf-8')
            with patch.object(dsh_backend, 'cli_path', return_value='trusted-dsh.cmd'), \
                 patch.object(dsh_backend, 'desktop_patch_path', return_value=None), \
                 patch.object(dsh_backend, 'load_credentials_env', return_value={}), \
                 patch.object(dsh_backend, 'run_process', return_value=SimpleNamespace(
                     returncode=0, stdout='native result', stderr='')) as run:
                text, meta = dsh_backend.generate('write artifact in project', friend)
            self.assertEqual(run.call_args.kwargs['cwd'], str(project.resolve()))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--permission-mode')+1], 'danger-full-access')
            self.assertEqual(argv[argv.index('--approval-policy')+1], 'never')
            self.assertEqual(text, 'native result')
            self.assertTrue(meta['native'])

    def test_cli_path_discovery(self):
        cli = dsh_backend.cli_path()
        if cli is None:
            self.skipTest('DSH CLI is not installed on this platform')
        # DSH is installed on D:\dsh
        self.assertIsNotNone(cli)
        self.assertTrue(Path(cli).exists())

    def test_models_fetch(self):
        models = dsh_backend.fetch_available_models()
        self.assertIsInstance(models, list)
        self.assertIn('deepseek-chat', models)

    def test_credentials_loading(self):
        env = dsh_backend.load_credentials_env()
        self.assertIsInstance(env, dict)

    def test_multi_line_prompt_reaches_the_cli_whole(self):
        """dsh.cmd runs under cmd.exe, which cut an argv prompt at its first line break; it now goes via stdin."""
        import sys
        import tempfile
        from unittest.mock import patch
        if sys.platform != 'win32':
            self.skipTest('cmd.exe argument handling')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            fake = folder / 'dsh.cmd'  # answers with the task it received, like `dsh headless -`
            fake.write_text(f'@chcp 65001 >nul\r\n@"{sys.executable}" -X utf8 -c "import sys;print(sys.stdin.read() if sys.argv[-1] == \'-\' '
                            f'else sys.argv[-1])" %*\r\n', encoding='utf-8')
            prompt = '第一行：任务\n第二行：规则 "引号" 100% & a|b\n第三行：输出格式'
            with patch.object(dsh_backend, 'cli_path', return_value=str(fake)), \
                    patch.object(dsh_backend, 'desktop_patch_path', return_value=None), \
                    patch.object(dsh_backend, 'load_credentials_env', return_value={}), \
                    patch('workspace_tools.enabled', return_value={'enabled': True, 'commands': True,
                                                                   'full_computer_access': True}):
                text, meta = dsh_backend.generate(prompt, folder, {})
        self.assertEqual(text, prompt)
        self.assertTrue(meta['native'])


if __name__ == '__main__':
    unittest.main()
