"""Tests for Antigravity Codex Cockpit Server endpoints."""
import json
import urllib.error
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import cockpit_server


class CockpitServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = cockpit_server.ThreadingHTTPServer(('127.0.0.1', 0), cockpit_server.CockpitHandler)
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        for _ in range(30):
            try:
                with urllib.request.urlopen(cls.request('/api/status'), timeout=1):
                    break
            except Exception:
                pass
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    @classmethod
    def request(cls, path, data=None, headers=None, *, auth=True):
        h = dict(headers or {})
        if auth:
            h['Authorization'] = 'Bearer ' + cockpit_server.cockpit_token()
        return urllib.request.Request(f'http://127.0.0.1:{cls.port}{path}', data=data, headers=h,
                                      method='POST' if data is not None else 'GET')

    def test_ui_serves_html(self):
        req = self.request('/')
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            html = resp.read().decode("utf-8")
            self.assertIn("Open QQ Dot · 控制中心", html)
            self.assertIn('id="view-agents"', html)
            self.assertIn('No example tasks', html)

    def test_live_workspaces_are_authenticated_and_redacted(self):
        for endpoint in ('/api/workspace', '/api/insights', '/api/mailbox'):
            with self.subTest(endpoint=endpoint):
                with urllib.request.urlopen(self.request(endpoint), timeout=3) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    self.assertEqual(resp.status, 200)
                    self.assertIsInstance(data, dict)
                    self.assertNotIn('access_token', data)
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(self.request(endpoint, auth=False), timeout=2)
                self.assertEqual(err.exception.code, 401)

    def test_agent_adapter_snapshot_is_authenticated_and_read_only(self):
        req = self.request('/api/agent-adapters')
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        self.assertEqual(len(payload['adapters']), 7)
        self.assertEqual(set(x['backend'] for x in payload['adapters']),
                         {'opencode', 'cursor', 'qwen_code', 'zcode', 'kimi_code', 'acp', 'mcp'})
        self.assertNotIn('password', json.dumps(payload).lower())
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(self.request('/api/agent-adapters', auth=False), timeout=2)
        self.assertEqual(err.exception.code, 401)

    def test_status_endpoint(self):
        req = self.request('/api/status')
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("version", data)
            self.assertIn("queue_health", data)
            self.assertIn("engine", data)

    def test_queue_endpoint(self):
        req = self.request('/api/queue')
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("total_unhandled", data)
            self.assertIn("status", data)

    def test_flush_endpoint(self):
        req = self.request('/api/queue/flush', data=json.dumps({'mark_ancient_handled': False}).encode('utf-8'),
                           headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("success"))
            self.assertIn("drained_count", data)

    def test_simulation_fast_path(self):
        req = self.request('/api/simulate', data=json.dumps({'qq_id': '10001', 'body': '批准 req_123'}).encode('utf-8'),
                           headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("success"))
            self.assertTrue(data.get('fast_path'))
            self.assertEqual(data['mode'], 'preview_only')
            self.assertFalse(data['executed'])
            self.assertFalse(data['queued'])

    def test_all_sensitive_routes_reject_unauthenticated_requests(self):
        for endpoint in ('/', '/api/status', '/api/queue', '/api/mailbox', '/api/friends', '/api/logs', '/api/logs/stream'):
            with self.subTest(endpoint=endpoint), self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(self.request(endpoint, auth=False), timeout=2)
            self.assertEqual(result.exception.code, 401)
        for endpoint in ('/api/simulate', '/api/queue/flush', '/api/friends/update'):
            with self.subTest(endpoint=endpoint), self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(self.request(endpoint, data=b'{}',
                                                    headers={'Content-Type': 'application/json'}, auth=False), timeout=2)
            self.assertEqual(result.exception.code, 401)

    def test_cross_site_and_form_posts_are_rejected(self):
        for headers, expected in [({'Content-Type': 'application/json', 'Origin': 'http://evil.example'}, 403),
                                  ({'Content-Type': 'text/plain'}, 415)]:
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(self.request('/api/queue/flush', data=b'{}', headers=headers), timeout=2)
            self.assertEqual(result.exception.code, expected)

    def test_friend_ui_action_reads_and_writes_real_profile_path(self):
        import tempfile
        from unittest.mock import patch
        import house_status
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'friends' / '12345').mkdir(parents=True)
            (root / 'friends.json').write_text(json.dumps({
                'friends': {'12345': {'display_name': 'Test Friend'}}}), 'utf-8')
            profile = root / 'friends' / '12345' / 'friend.json'
            profile.write_text(json.dumps({'auto_reply': True, 'model_api': {'provider': 'codex'}}), 'utf-8')
            with patch.object(house_status, 'ROOT', root), patch.object(house_status, 'REGISTRY', root / 'friends.json'):
                with urllib.request.urlopen(self.request('/api/friends'), timeout=3) as resp:
                    result = json.load(resp)
                self.assertEqual(result['friends'][0]['qq_id'], '12345')
                self.assertTrue(result['friends'][0]['auto_reply'])
                data = json.dumps({'qq_id': '12345', 'auto_reply': False}).encode()
                with urllib.request.urlopen(self.request('/api/friends/update', data=data,
                        headers={'Content-Type': 'application/json'}), timeout=3) as resp:
                    result = json.load(resp)
                self.assertTrue(result['success'])
                self.assertFalse(json.loads(profile.read_text('utf-8'))['auto_reply'])
                bad = json.dumps({'qq_id': '../12345', 'auto_reply': True}).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(self.request('/api/friends/update', data=bad,
                        headers={'Content-Type': 'application/json'}), timeout=3)
                self.assertEqual(error.exception.code, 400)

    def test_simulator_never_executes_approval_even_when_authenticated(self):
        from unittest.mock import patch
        with patch('main.handle_approval_command', side_effect=AssertionError('MUST NOT EXECUTE')):
            with urllib.request.urlopen(self.request('/api/simulate',
                    data=json.dumps({'qq_id': 'administrator', 'body': 'approve req_1'}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}), timeout=2) as r:
                self.assertEqual(json.load(r)['executed'], False)

    def test_bulk_delete_of_unread_messages_is_disallowed(self):
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(self.request('/api/queue/flush',
                    data=json.dumps({'mark_ancient_handled': True}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}), timeout=2)
        self.assertEqual(result.exception.code, 400)
