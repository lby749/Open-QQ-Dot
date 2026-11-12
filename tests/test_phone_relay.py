import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import house_web
import phone_client
import phone_relay
from tests import test_house_web


class PhoneRelayTests(unittest.TestCase):
    call = test_house_web.HouseWebTests.call
    profile = test_house_web.HouseWebTests.profile

    def setUp(self):
        test_house_web.HouseWebTests.setUp(self)
        for item in (patch.object(house_web, 'PORT', self.server.server_port),
                     patch.object(phone_relay, 'WAIT_SECONDS', 5),
                     patch.object(phone_relay, 'POLL_SECONDS', .1)):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(phone_relay.cancel, '12345')
        self.addCleanup(phone_relay.cancel, '67890')
        self.device = 'phone-device-token-0123456789'
        status, _ = self.call('/api/phone/register', {'qq': '12345', 'transport': 'relay', 'token': self.device})
        self.assertEqual(status, 200)

    def phone(self, action, body=None, device=None, qr=None, extra=None):
        request = Request(f'http://127.0.0.1:{self.server.server_port}/api/phone/{action}',
                          data=json.dumps(body or {}).encode(),
                          headers={'Authorization': 'Bearer ' + (qr or self.secret),
                                   'X-Phone-Token': device or self.device,
                                   'Content-Type': 'application/json', **(extra or {})})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_round_trip_uses_model_client_and_only_current_friend(self):
        profile = self.profile()
        self.assertTrue(phone_client.configured(profile))
        self.assertEqual(profile['phone_mcp_url'], '')
        self.assertNotIn('phone_transport', json.loads((self.other_directory / 'friend.json').read_text('utf-8')))
        output = []
        worker = threading.Thread(target=lambda: output.append(phone_client.client_for(profile).call('phone_now')))
        worker.start()
        try:
            with phone_relay._changed:
                self.assertTrue(phone_relay._changed.wait_for(lambda: '12345' in phone_relay._pending, timeout=3))
            status, body = self.phone('poll')
            self.assertEqual(status, 200)
            task = body['task']
            self.assertEqual(task['name'], 'phone_now')
            self.assertEqual(self.phone('poll', qr=self.other_secret)[0], 403)
            self.assertEqual(self.phone('result', {'id': task['id'], 'result': {'content': []}}, device='wrong')[0], 403)
            result = {'content': [{'type': 'text', 'text': 'UNTRUSTED\nphone is locked'}]}
            self.assertEqual(self.phone('result', {'id': task['id'], 'result': result})[0], 200)
        finally:
            worker.join(3)
        self.assertEqual(output, ['UNTRUSTED\nphone is locked'])
        self.assertFalse(phone_relay._pending)
        self.assertEqual(self.phone('result', {'id': task['id'], 'result': result})[0], 404)

    def test_status_and_prompt_follow_the_phones_last_poll(self):
        # 2026-10-12 user: "paired" did not say whether the phone was connected, and the model was never told of the tools
        import os
        import time
        import main
        self.assertEqual(self.phone('poll', qr=self.other_secret)[0], 403)
        self.assertIsNone(phone_client.last_seen(self.directory))  # a rejected poll does not count
        phone = self.call('/api/friend/12345')[1]['phone']
        self.assertEqual((phone['connected'], phone['online']), (True, False))
        self.assertIn('还没连上来过', phone['note'])
        self.assertEqual(main.phone_line(self.profile(), self.directory), '')
        self.assertEqual(self.phone('poll'), (200, {'task': {}}))
        phone = self.call('/api/friend/12345')[1]['phone']
        self.assertEqual((phone['online'], phone['note']), (True, '手机在线；模型可按需查询。'))
        self.assertIn('PHONE_TOOLS(ONLINE): phone_now', main.phone_line(self.profile(), self.directory))
        old = time.time() - phone_client.ONLINE_SECONDS - 120
        os.utime(self.directory / phone_client.SEEN_FILE, (old, old))
        self.assertIn('手机离线：最后连接', self.call('/api/friend/12345')[1]['phone']['note'])
        self.assertEqual(main.phone_line(self.profile(), self.directory), '')

    def test_query_timeout_is_removed_and_never_redelivered(self):
        with patch.object(phone_relay, 'WAIT_SECONDS', .05):
            with self.assertRaises(phone_client.PhoneUnavailable):
                phone_client.client_for(self.profile()).call('phone_now')
        self.assertEqual(self.phone('poll'), (200, {'task': {}}))
        self.assertFalse(phone_relay._pending)

    def test_repair_revokes_old_device_and_pause_rejects_queries(self):
        old = self.device
        new = old + '-new'
        self.assertEqual(self.call('/api/phone/register', {'transport': 'relay', 'token': new})[0], 200)
        self.assertEqual(self.phone('poll', device=old)[0], 403)
        self.assertEqual(self.phone('poll', device=new)[0], 200)
        profile = self.profile()
        profile['bridge_enabled'] = False
        (self.directory / 'friend.json').write_text(json.dumps(profile))
        self.assertEqual(self.phone('poll', device=new)[0], 403)
        self.assertEqual(self.phone('query', {'name': 'phone_now', 'arguments': {}}, device=new)[0], 403)

    def test_funnel_forwarded_requests_cannot_query(self):
        # tailscale funnel reaches the loopback listener; only the phone's poll/result may come through it.
        for header in ('X-Forwarded-For', 'X-Forwarded-Proto', 'Tailscale-Funnel-Request'):
            extra = {header: '?1'}
            self.assertEqual(self.phone('query', {'name': 'phone_now', 'arguments': {}}, extra=extra)[0], 403)
        self.assertEqual(self.phone('poll', extra={'X-Forwarded-For': '1.2.3.4'}), (200, {'task': {}}))

    def test_unknown_control_tool_and_unmatched_results_are_rejected(self):
        session = self.profile()
        session['phone_pc_url'] = 'http://example.com/'
        with self.assertRaises(phone_client.PhoneUnavailable):
            phone_client.client_for(session)
        self.assertEqual(self.phone('query', {'name': 'tap', 'arguments': {}})[0], 400)
        self.assertEqual(self.phone('result', {'id': 'unknown', 'result': {'content': []}})[0], 404)
        self.assertEqual(self.phone('result', {'id': 'unknown', 'result': {}})[0], 400)
        self.assertEqual(self.phone('result', {'id': 'unknown', 'result': {'content': [None]}})[0], 400)
        self.assertEqual(self.call('/api/phone/register', {'qq': '67890', 'transport': 'relay', 'token': self.device})[0], 403)

    def test_delivered_task_is_not_sent_twice_and_repair_cancels_waiter(self):
        errors = []
        def ask():
            try:
                phone_relay.query('12345', self.device, 'set_history_apps', {'packages': []})
            except TimeoutError as exc:
                errors.append(str(exc))
        worker = threading.Thread(target=ask)
        worker.start()
        try:
            task = self.phone('poll')[1]['task']
            self.assertTrue(task)
            with self.assertRaises(ValueError):
                phone_relay.query('12345', self.device, 'phone_now', {})
            self.assertEqual(self.phone('poll')[1], {'task': {}})
            self.assertEqual(self.call('/api/phone/register', {'transport': 'relay', 'token': self.device + '-new'})[0], 200)
        finally:
            worker.join(3)
        self.assertEqual(len(errors), 1)
        self.assertFalse(phone_relay._pending)


if __name__ == '__main__':
    unittest.main()
