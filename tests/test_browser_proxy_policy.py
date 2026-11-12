from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from browser_automation import IsolatedBrowserSession


class BrowserProxyPolicyTests(unittest.TestCase):
    def test_fake_ip_requires_explicit_local_system_proxy_and_public_hostname(self):
        with tempfile.TemporaryDirectory() as folder, patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('198.18.0.10', 443))]):
            with patch('urllib.request.getproxies', return_value={}):
                plain = IsolatedBrowserSession('plain', base_dir=Path(folder))
            with self.assertRaises(PermissionError):
                plain._validate_target_url('https://example.com')
            with patch('urllib.request.getproxies', return_value={'https': 'http://127.0.0.1:7890'}):
                proxied = IsolatedBrowserSession('proxy', base_dir=Path(folder))
            self.assertEqual(proxied._validate_target_url('https://example.com'), 'https://example.com')
            for url in ('http://198.18.0.10', 'http://127.0.0.1', 'http://169.254.169.254'):
                with self.subTest(url=url), self.assertRaises(PermissionError):
                    proxied._validate_target_url(url)

    def test_external_proxy_does_not_authorize_fake_ip_or_private_network(self):
        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.getproxies', return_value={'https': 'http://proxy.example:8080'}):
            session = IsolatedBrowserSession('owner', base_dir=Path(folder))
            self.assertIsNone(session.proxy_server)
            with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.5', 443))]):
                with self.assertRaises(PermissionError):
                    session._validate_target_url('https://example.com')
