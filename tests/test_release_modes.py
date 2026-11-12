import json
import math
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import api_backend
import function_apis
import house_settings
import hybrid_search
import retrieval_api


class Endpoint(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.calls.append((self.path, None, self.headers.get('Authorization')))
        self.answer({'ok': True})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append((self.path, body, self.headers.get('Authorization')))
        if self.path.endswith('/embeddings'):
            self.answer({'data': [{'index': i, 'embedding': [3.0, 4.0]} for i in range(len(body['input']))]})
        else:
            self.answer({'results': [{'index': i, 'relevance_score': 0.8 - i * 0.1}
                                     for i in range(len(body['documents']))]})

    def answer(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)


class ReleaseModes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.directory)})
        self.environment.start()
        self.profile = {'model_api': {}, 'retrieval_api': {}}
        self.write()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def write(self):
        (self.directory / 'friend.json').write_text(json.dumps(self.profile), encoding='utf-8')

    def test_model_free_mode_does_not_require_weights(self):
        from message_classifier import get_classifier
        from program_fallbacks import RuleClassifier
        self.assertFalse(house_settings.local_models_enabled())
        self.assertIsInstance(get_classifier(self.directory), RuleClassifier)
        detail = get_classifier(self.directory).classify_with_review('这道题怎么解？')
        self.assertEqual(detail['labels']['intent'], 'inquire')
        self.assertTrue(detail['needs_review'])
        self.assertFalse((self.directory / 'build').exists())

    def test_keyword_recall_survives_both_models_being_absent(self):
        from persona_store import Unavailable
        items = [('one', '午饭可以吃面条', ['午饭']), ('two', 'badminton', ['badminton'])]
        with patch('retrieval_api.embed', side_effect=Unavailable('disabled')), \
             patch('retrieval_api.rerank', side_effect=Unavailable('disabled')):
            hits = hybrid_search.gated_recall(['午饭吃什么'], items, lambda _: {}, 'test', 3)
            self.assertEqual(hits, [0])
            self.assertEqual(hybrid_search.gated_recall(['天气如何'], items, lambda _: {}, 'test', 3), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI credential store')
    def test_generic_keys_are_independent_and_read_legacy(self):
        legacy = self.directory / 'deepseek-key.bin'
        legacy.write_bytes(api_backend._protect(b'synthetic-legacy'))
        self.assertEqual(api_backend.load_key(self.directory), 'synthetic-legacy')
        api_backend.save_key(self.directory, 'synthetic-chat')
        api_backend.save_key(self.directory, 'synthetic-search', 'embedding')
        self.assertEqual(api_backend.load_key(self.directory), 'synthetic-chat')
        self.assertEqual(api_backend.load_key(self.directory, 'embedding'), 'synthetic-search')
        self.assertNotIn(b'synthetic-chat', (self.directory / 'api-key.bin').read_bytes())
        with self.assertRaises(ValueError):
            api_backend.key_path(self.directory, '../outside')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI credential store')
    def test_http_apis_tools_and_vector_cache(self):
        Endpoint.calls = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            self.profile['retrieval_api'] = {
                'embedding': {'enabled': True, 'base_url': base + '/v1', 'model': 'synthetic-vector', 'dimension': 2},
                'rerank': {'enabled': True, 'base_url': base + '/v1', 'model': 'synthetic-rerank'}}
            self.profile['function_apis'] = [{'name': 'api_echo', 'description': 'Synthetic echo',
                'enabled': True, 'url': base + '/echo', 'method': 'GET',
                'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'maxLength': 8}},
                                'required': ['query'], 'additionalProperties': False}}]
            self.write()
            api_backend.save_key(self.directory, 'synthetic-http', 'embedding')
            vectors = retrieval_api.embed(['one', 'two'], self.directory)
            self.assertEqual(len(vectors), 2)
            self.assertTrue(math.isclose(vectors[0][0], 0.6))
            self.assertEqual(retrieval_api.rerank('q', ['a', 'b'], self.directory), [0.8, 0.7000000000000001])
            self.assertIn('api_echo', {item['name'] for item in api_backend._tools(self.directory)})
            self.assertIn('ok', function_apis.call(self.directory, 'api_echo', {'query': 'hello'})['data'])
            count = len(Endpoint.calls)
            with self.assertRaises(Exception):
                function_apis.call(self.directory, 'api_echo', {'query': 'this is too long'})
            self.assertEqual(len(Endpoint.calls), count)
            other = self.directory / 'other'
            other.mkdir()
            with self.assertRaises(ValueError):
                function_apis.call(other, 'api_echo', {'query': 'hello'})
            cache = self.directory / 'vectors.sqlite3'
            hybrid_search.cached_vectors(cache, {'x': 'text'}, self.directory)
            count = len(Endpoint.calls)
            hybrid_search.cached_vectors(cache, {'x': 'text'}, self.directory)
            self.assertEqual(len(Endpoint.calls), count)
            self.profile['retrieval_api']['embedding']['model'] = 'synthetic-vector-new'
            self.write()
            hybrid_search.cached_vectors(cache, {'x': 'text'}, self.directory)
            self.assertEqual(len(Endpoint.calls), count + 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @unittest.skipUnless(os.name == 'nt', 'PowerShell command test is Windows-specific')
    def test_local_computer_permissions_are_per_friend_and_revocable(self):
        import workspace_tools
        self.assertEqual(workspace_tools.tools(self.directory), [])
        self.profile['computer_tools'] = {'enabled': True, 'commands': False}
        self.write()
        names = {tool['name'] for tool in workspace_tools.tools(self.directory)}
        self.assertIn('desk_write_file', names)
        self.assertNotIn('desk_run_command', names)
        workspace_tools.call(self.directory, 'desk_write_file', {'path': 'result.txt', 'text': 'synthetic result'})
        self.assertEqual(workspace_tools.call(self.directory, 'desk_read_file', {'path': 'result.txt'})['text'], 'synthetic result')
        for path in ('../outside.txt', str(self.directory / 'outside.txt')):
            with self.assertRaises(ValueError):
                workspace_tools.call(self.directory, 'desk_write_file', {'path': path, 'text': 'blocked'})
        with self.assertRaises(ValueError):
            workspace_tools.call(self.directory, 'desk_write_file', {'path': 'result.txt', 'text': 'blocked'})
        other = self.directory / 'other-friend'
        other.mkdir()
        with self.assertRaises(ValueError):
            workspace_tools.call(other, 'desk_read_file', {'path': 'result.txt'})
        self.profile['computer_tools'] = {'enabled': False, 'commands': True}
        self.write()
        with self.assertRaises(ValueError):
            workspace_tools.call(self.directory, 'desk_read_file', {'path': 'result.txt'})
        self.profile['computer_tools'] = {'enabled': True, 'commands': True}
        self.write()
        result = workspace_tools.call(self.directory, 'desk_run_command', {'command': "Write-Output 'OPEN_QQ_DOT_OK'"})
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('OPEN_QQ_DOT_OK', result['output'])


if __name__ == '__main__':
    unittest.main()
