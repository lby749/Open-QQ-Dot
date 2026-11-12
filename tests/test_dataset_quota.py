import json
from pathlib import Path
import tempfile
import unittest

from dataset_annotation.quota_guard import QuotaGuard


class DatasetQuotaTests(unittest.TestCase):
    def state(self, root, **changes):
        path = Path(root) / 'docs/quota_state.json'
        path.parent.mkdir(exist_ok=True)
        value = {'status': 'RUNNING', 'reset_card_consumed': True}
        value.update(changes)
        path.write_text(json.dumps(value), 'utf-8')
        return path

    def limits(self, primary=94, weekly=50, reset=200):
        return {'ordinaryUsageAllowed': True, 'rateLimitsByLimitId': {'codex': {
            'primary': {'usedPercent': primary, 'windowDurationMins': 300, 'resetsAt': reset},
            'secondary': {'usedPercent': weekly, 'windowDurationMins': 10080, 'resetsAt': 1000}}}}

    def test_pause_and_verified_natural_resume_preserve_consumed_card(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.state(root)
            now = [100]
            raw = [self.limits(primary=95)]
            guard = QuotaGuard(root, reader=lambda: raw[0], clock=lambda: now[0])
            self.assertFalse(guard.allowed())
            self.assertEqual(json.loads(path.read_text())['status'], 'PAUSED_QUOTA')
            now[0] = 170
            raw[0] = self.limits(primary=20, reset=500)
            self.assertFalse(guard.allowed())  # reset time has not arrived
            now[0] = 240
            self.assertTrue(guard.allowed())
            self.assertTrue(json.loads(path.read_text())['reset_card_consumed'])

    def test_weekly_priority_missing_values_and_external_pause(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.state(root)
            guard = QuotaGuard(root, reader=lambda: self.limits(primary=95, weekly=98))
            self.assertFalse(guard.allowed())
            self.assertEqual(json.loads(path.read_text())['status'], 'PAUSED_WEEKLY')
            self.state(root)
            missing = self.limits()
            del missing['ordinaryUsageAllowed']
            self.assertFalse(QuotaGuard(root, reader=lambda: missing).allowed())
            self.state(root, status='PAUSED_BY_USER')
            self.assertFalse(QuotaGuard(root, reader=self.limits).allowed())


if __name__ == '__main__':
    unittest.main()
