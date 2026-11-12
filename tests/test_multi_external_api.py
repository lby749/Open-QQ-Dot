"""Regression tests: named multi-provider API connections without cross-key leaks."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api_backend
from api_profiles import (PRESETS, activate, clean_connection, discover_models,
                          load_connections, new_connection, validate_url)


class MultipleConnectionsTests(unittest.TestCase):
    def test_legacy_migration_and_activation_without_plain_keys(self):
        old = {'model_api': {'provider': 'custom', 'api_base_url': 'https://api.deepseek.com',
                             'api_protocol': 'openai-completions', 'model': 'deepseek-chat',
                             'api_key_id': 'chat'}, 'user_data': {'keep': True}}
        entries, selected = load_connections(old)
        self.assertEqual(selected, 'chat')
        self.assertEqual(len(entries), 1)
        new = new_connection('测试网关', 'generic_openai')
        new['api_base_url'] = 'https://gateway.example.com/v1'
        new['model'] = 'my-model'
        entries[new['api_key_id']] = new
        changed = activate(old, new['api_key_id'], entries)
        self.assertEqual(changed['model_api']['api_key_id'], new['api_key_id'])
        self.assertEqual(changed['model_api']['model'], 'my-model')
        self.assertEqual(changed['user_data'], {'keep': True})
        self.assertNotIn('api_key', changed['api_profiles']['chat'])  # only api_key_id allowed
        self.assertEqual(old['model_api']['api_key_id'], 'chat')

    def test_presets_and_insecure_remote_http_rejected(self):
        self.assertIn('generic_openai', PRESETS)
        self.assertIn('generic_anthropic', PRESETS)
        self.assertIn('generic_responses', PRESETS)
        self.assertEqual(validate_url('http://127.0.0.1:11434/v1'), 'http://127.0.0.1:11434/v1')
        self.assertEqual(validate_url('https://gateway.example.com/v1/'), 'https://gateway.example.com/v1')
        for url in ('http://example.com/v1', 'https://u:secret@host.test/v1',
                    'https://example.org/v1?x=1', 'file:///secret', 'https://host.test/../v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_api_profile_rejects_cleartext_credentials(self):
        source = {'label': 'test', 'api_key_id': 'p1', 'api_key': 'PRIVATE',
                  'api_protocol': 'openai-completions', 'model': 'm',
                  'api_base_url': 'https://example.org/v1'}
        result = clean_connection(source, identifier='p1')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('api_key', result)

    def test_retry_cooldown_is_per_connection_not_per_friend(self):
        api_backend._retry_until.clear()
        with tempfile.TemporaryDirectory() as root:
            d = Path(root)
            first = {'api_base_url': 'https://first.example.com/v1',
                     'model': 'm', 'api_key_id': 'p1', 'api_effort': 'off'}
            second = {'api_base_url': 'https://second.example.com/v1',
                      'model': 'm', 'api_key_id': 'p2', 'api_effort': 'off'}
            class Failure(Exception): pass
            class Response(io.StringIO): pass
            def fake_open(request, timeout):
                if 'first' in request.full_url:
                    raise api_backend.urllib.error.URLError('unavailable')
                self.assertEqual(request.get_header('Authorization'), 'Bearer p2-key')
                return Response(json.dumps({'choices': [{'finish_reason': 'stop',
                                                         'message': {'content': 'OK'}}]}))
            with patch.object(api_backend.urllib.request, 'urlopen', side_effect=fake_open), \
                 patch.object(api_backend, 'load_key', side_effect=lambda directory, purpose: purpose + '-key'), \
                 patch('workspace_tools.instructions', return_value=''):
                with self.assertRaises(RuntimeError):
                    api_backend.generate('hi', d, first)
                self.assertEqual(api_backend.generate('hi', d, second), 'OK')
            api_backend._retry_until.clear()

    def test_model_discovery_uses_auth_matching_protocol(self):
        class FakeResponse(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        class Opener:
            def open(self, request, timeout):
                self.last = request
                return FakeResponse(json.dumps({'data': [{'id': 'model-x'}]}).encode())
        opener = Opener()
        with patch('api_profiles.NoCredentialRedirects.opener', return_value=opener):
            self.assertEqual(discover_models('https://api.anthropic.com', 'anthropic-messages', 'secret'), ['model-x'])
            self.assertEqual(opener.last.get_header('X-api-key'), 'secret')
            self.assertIsNone(opener.last.get_header('Authorization'))
            self.assertEqual(discover_models('https://gateway.example/v1', 'openai-completions', 'key'), ['model-x'])
            self.assertEqual(opener.last.get_header('Authorization'), 'Bearer key')

    def test_decrypted_binary_key_is_not_read_as_utf8_text_in_gui(self):
        source = Path(__file__).parents[1].joinpath('house_qt/pages_friend.py').read_text('utf-8')
        block = source.split('    def fetch_api_models(self):', 1)[1].split('    def update_efforts', 1)[0]
        self.assertIn('load_key(self.house.friend_dir, key_id)', block)
        self.assertNotIn("key_file.read_text", block)
        self.assertIn('self.api_preset', source)
        self.assertIn('self.delete_api_profile', source)

    def test_desktop_palette_matches_original_white_and_pale_blue(self):
        root = Path(__file__).parents[1]
        qt = root.joinpath('house_qt/window.py').read_text('utf-8')
        web = root.joinpath('cockpit_ui.html').read_text('utf-8')
        self.assertIn('#EFF6FF', qt)
        self.assertIn('--nav:#eff6ff', web)
        self.assertIn('--surface:#fff', web)


if __name__ == '__main__':
    unittest.main()

class MobileModelSafetyTests(unittest.TestCase):
    def test_remote_model_page_does_not_repoint_an_existing_key(self):
        import house_web
        with tempfile.TemporaryDirectory() as tmp:
            friend = Path(tmp)
            profile = {'model_api': {'provider': 'custom', 'api_key_id': 'chat',
                                     'api_protocol': 'openai-completions',
                                     'api_base_url': 'https://trusted.example/v1', 'model': 'safe-model',
                                     'api_effort': 'off'},
                       'api_profiles': {'chat': {'label': 'Trusted', 'api_key_id': 'chat',
                                                 'api_protocol': 'openai-completions',
                                                 'api_base_url': 'https://trusted.example/v1',
                                                 'model': 'safe-model', 'api_effort': 'off'}}}
            from unittest.mock import patch
            with patch.object(house_web, '_directory', return_value=friend), \
                 patch.object(house_web, 'load_profile', return_value=profile), \
                 patch('claude_backend.settings'), patch('antigravity_backend.settings'):
                with self.assertRaisesRegex(ValueError, 'Windows'):
                    house_web.post_model('123', {'provider': 'custom',
                                                  'api_protocol': 'openai-completions',
                                                  'api_base_url': 'https://attacker.example/v1',
                                                  'model': 'safe-model', 'api_effort': 'off'})

    def test_mobile_page_lists_only_nonsecret_connection_fields(self):
        import house_web
        source = Path(__file__).parents[1].joinpath('house_web.py').read_text('utf-8')
        self.assertIn("'api_profiles': [{**value, 'key_saved'", source)
        page = Path(__file__).parents[1].joinpath('house_web.html').read_text('utf-8')
        self.assertIn('apiConnection', page)
        self.assertIn('baseUrl.readOnly=true', page)
