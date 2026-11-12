import unittest

import phone_client


def fake_phone(listed, calls):
    """A PhoneClient whose MCP calls are answered in memory: tools/list gives `listed`, tools/call is recorded."""
    client = phone_client.PhoneClient('http://127.0.0.1:8080/mcp', 'token-token-token-token')

    def call(method, params, notify=False):
        if method == 'tools/list':
            return {'tools': [{'name': name} for name in listed]}
        if method == 'tools/call':
            calls.append(params['name'])
            return {'content': [{'type': 'text', 'text': 'ok'}]}
        return {}

    client._call = call
    return client


class PhoneToolNameTests(unittest.TestCase):
    def test_short_name_is_sent_as_the_phone_prefixed_name(self):
        calls = []
        self.assertEqual(fake_phone(['android_phone_now', 'android_get_screen_state'], calls).call('phone_now'), 'ok')
        self.assertEqual(calls, ['android_phone_now'])

    def test_device_name_in_the_prefix_is_matched_too(self):
        calls = []
        fake_phone(['android_pixel7_phone_now'], calls).call('phone_now')
        self.assertEqual(calls, ['android_pixel7_phone_now'])

    def test_missing_tool_is_reported_without_a_call(self):
        calls = []
        with self.assertRaises(phone_client.PhoneToolError):
            fake_phone(['android_click_node'], calls).call('phone_now')
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
