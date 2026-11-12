import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api_backend
import main
from codex_settings import generation_settings


class ModelApiTests(unittest.TestCase):
    def test_codex_settings_reach_server(self):
        settings = {'model_api': {'provider': 'codex', 'codex_model': 'test-model',
                                 'codex_effort': 'high', 'codex_fast': False}}
        with patch('codex_settings.model_options', return_value={'test-model': ['high']}), \
                patch.object(main.SERVER, 'generate', return_value=('reply', 'thread')) as generate:
            result = main.codex_text('prompt', Path('.'), 'old-thread', profile=settings)
        self.assertEqual(result, ('reply', 'thread'))
        self.assertEqual(generate.call_args.args[-3:], ('test-model', 'high', 'default'))

    def test_deepseek_dispatch_does_not_use_codex(self):
        with patch.object(main, 'deepseek_generate', return_value='reply') as generate, \
                patch.object(main.SERVER, 'generate') as codex:
            result = main.codex_text('prompt', Path('.'), 'saved-thread', profile={
                'model_api': {'provider': 'deepseek', 'model': api_backend.MODELS[1]}})
        self.assertEqual(result, ('reply', None))
        self.assertEqual(generate.call_args.args[2]['model'], api_backend.MODELS[1])
        codex.assert_not_called()

    def test_invalid_effort_rejected(self):
        with patch('codex_settings.model_options', return_value={'test-model': ['low']}):
            with self.assertRaises(ValueError):
                generation_settings({'codex_model': 'test-model', 'codex_effort': 'invalid'})

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI credential store')
    def test_key_is_encrypted_and_friend_specific(self):
        build_dir = Path(__file__).resolve().parents[1] / 'build'
        build_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_dir) as folder:
            first = Path(folder) / 'first'
            second = Path(folder) / 'second'
            first.mkdir()
            second.mkdir()
            api_backend.save_key(first, 'test-secret-value')
            self.assertNotIn(b'test-secret-value', api_backend.key_path(first).read_bytes())
            self.assertEqual(api_backend.load_key(first), 'test-secret-value')
            with self.assertRaises(RuntimeError):
                api_backend.load_key(second)


if __name__ == '__main__':
    unittest.main()
