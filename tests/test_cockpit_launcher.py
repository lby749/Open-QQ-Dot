from email.message import Message
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import house_web
import launcher_codex


class CockpitLauncherTests(unittest.TestCase):
    def test_mobile_and_admin_workbench_do_not_share_port(self):
        self.assertNotEqual(launcher_codex.PORT, house_web.PORT)

    def test_different_service_on_port_is_not_opened_as_cockpit(self):
        error = HTTPError('http://localhost/', 401, 'Unauthorized', Message(), None)
        with patch.object(launcher_codex, 'is_port_open', return_value=True), \
                patch('urllib.request.urlopen', side_effect=error), self.assertRaisesRegex(RuntimeError, '其他服务'):
            launcher_codex.start_server_if_needed()

    def test_existing_authenticated_cockpit_is_recognized_by_service_header(self):
        headers = Message()
        headers['X-OpenQQDot-Service'] = 'cockpit'
        error = HTTPError('http://localhost/', 401, 'Unauthorized', headers, None)
        with patch.object(launcher_codex, 'is_port_open', return_value=True), \
                patch('urllib.request.urlopen', side_effect=error):
            launcher_codex.start_server_if_needed()
