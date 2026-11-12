import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import network


class NetworkTests(unittest.TestCase):
    def test_only_tailscale_and_loopback_addresses_are_accepted(self):
        for host in ('100.64.0.1', '100.127.255.254', '127.0.0.1', 'localhost'):
            self.assertTrue(network.accepted(host), host)
        for host in ('100.63.255.255', '100.128.0.1', '10.0.0.2', '192.168.1.1', 'example.com', ''):
            self.assertFalse(network.accepted(host), host)

    def test_mcp_urls_reject_public_lan_and_invalid_ports(self):
        self.assertTrue(network.usable_url('http://100.64.0.2:8080/mcp'))
        self.assertTrue(network.usable_url('http://127.0.0.1/mcp'))
        for url in ('https://100.64.0.2:8080/mcp', 'http://100.64.0.2:99999/mcp',
                    'http://192.168.1.1/mcp', 'http://10.0.0.2/mcp', 'http://example.com/mcp', ''):
            self.assertFalse(network.usable_url(url), url)

    def test_no_guessed_address_when_tailscale_is_absent(self):
        with patch.object(network, 'addresses', return_value=[]):
            self.assertIsNone(network.address())
        with patch.object(network, 'addresses', return_value=['100.64.0.1']):
            self.assertEqual('100.64.0.1', network.address())

    def test_only_the_tailscale_adapter_is_used(self):
        interfaces = {'Tailscale': [SimpleNamespace(address='100.64.0.1'), SimpleNamespace(address='fe80::1')],
                      'Ethernet': [SimpleNamespace(address='100.64.0.9')],
                      'EasyTier': [SimpleNamespace(address='10.0.0.1')]}
        with patch('psutil.net_if_addrs', return_value=interfaces):
            self.assertEqual(['100.64.0.1'], network.addresses())

    def test_public_url_is_the_funnel_for_the_house_port(self):
        site = 'lby.tailff4e90.ts.net:443'
        on = {'Web': {site: {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8790'}}}}, 'AllowFunnel': {site: True}}
        cases = [(on, 'https://lby.tailff4e90.ts.net/'), ({}, None),
                 ({**on, 'AllowFunnel': {site: False}}, None),  # tailnet-only `tailscale serve`
                 ({**on, 'Web': {site: {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}}}, None)]
        for config, expected in cases:
            done = SimpleNamespace(stdout=json.dumps(config).encode())
            with patch('subprocess.run', return_value=done):
                self.assertEqual(expected, network.public_url(8790))
        with patch('subprocess.run', side_effect=OSError):
            self.assertIsNone(network.public_url(8790))


if __name__ == '__main__':
    unittest.main()
