import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
import usage_accounting as usage
from store import FriendStore


def test_unowned_recent_log_uses_bounded_quota_tail_without_full_conversation_parse(tmp_path, monkeypatch):
    from unittest.mock import Mock
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    friend = tmp_path / 'friend'
    friend.mkdir()
    path = sessions / 'other.jsonl'
    rate = {'type': 'event_msg', 'timestamp': datetime.now(timezone.utc).isoformat(),
            'payload': {'type': 'token_count', 'rate_limits': {'primary': {
                'window_minutes': 300, 'used_percent': 25, 'resets_at': time.time() + 3600}}}}
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'other', 'cwd': str(tmp_path / 'other')}}) +
                    '\n' + json.dumps({'type': 'response_item', 'payload': 'x' * 2_000_000}) + '\n' +
                    json.dumps(rate) + '\n', encoding='utf-8')
    reader = SimpleNamespace(HOME=tmp_path, TOKEN_FIELDS=('input_tokens', 'output_tokens', 'cached_input_tokens'),
        read_log=Mock(side_effect=AssertionError('Must not parse another conversation')),
        window_points=lambda events, minutes: [{'used': 25, 'reset': time.time() + 3600}])
    monkeypatch.setattr(usage, 'plugin', lambda: reader)
    for _ in range(2):
        data = usage.summary(friend)
        assert data['sessions'] == 0 and data['tokens']['input_tokens'] == 0
        assert data['account_remaining'] == 75
    reader.read_log.assert_not_called()


class UsageAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / 'friend'
        self.store = FriendStore(self.directory)
        self.addCleanup(lambda: self.store.close())
        try:
            reader = usage.plugin()
        except Exception as exc:
            self.skipTest(f'Codex usage plugin unavailable: {exc}')
        self.reader = SimpleNamespace(HOME=self.home, TOKEN_FIELDS=reader.TOKEN_FIELDS,
                                      read_log=reader.read_log, billing_summary=reader.billing_summary,
                                      window_points=reader.window_points)
        self.patch = patch.object(usage, 'plugin', return_value=self.reader)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def log(self, identifier='one', directory=None):
        folder = self.home / 'sessions'
        folder.mkdir(exist_ok=True)
        path = folder / (identifier + '.jsonl')
        tokens = dict(input_tokens=200, cached_input_tokens=50, output_tokens=20, total_tokens=220)
        events = [dict(type='session_meta', payload=dict(id=identifier, cwd=str(directory or self.directory))),
                  dict(type='turn_context', payload=dict(model='gpt-6.1-sol', service_tier='default')),
                  dict(type='event_msg', timestamp=datetime.now(timezone.utc).isoformat(),
                       payload=dict(type='token_count', info=dict(total_token_usage=tokens, last_token_usage=tokens),
                                    rate_limits=dict(primary=dict(window_minutes=300, used_percent=25, resets_at=time.time()+3600))))]
        path.write_text('\n'.join(json.dumps(event) for event in events), encoding='utf-8')
        return path

    def test_main_history_and_ephemeral_calls_are_combined_without_cache_double_count(self):
        self.log()
        usage.record(self.directory, 'gpt-6.1-sol', 'fast', dict(input_tokens=100, cached_input_tokens=20, output_tokens=10))
        data = usage.summary(self.directory)
        self.assertEqual(data['tokens']['input_tokens'], 300)
        self.assertEqual(data['tokens']['cached_input_tokens'], 70)
        self.assertEqual(data['tokens']['output_tokens'], 30)
        self.assertEqual(data['sessions'], 1)
        self.assertEqual(data['temporary_calls'], 1)
        self.assertIsNotNone(data['total_usd'])
        self.assertEqual(data['account_remaining'], 75)

    def test_friend_isolation_but_account_quota_is_shared(self):
        other = self.home / 'other'
        other.mkdir()
        self.log(directory=other)
        data = usage.summary(self.directory)
        self.assertEqual(data['tokens']['input_tokens'], 0)
        self.assertEqual(data['account_remaining'], 75)

    def test_archived_copy_is_not_counted_twice(self):
        path = self.log()
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        shutil.copy2(path, archive / path.name)
        self.assertEqual(usage.summary(self.directory)['tokens']['input_tokens'], 200)

    def test_unknown_and_failed_calls_never_look_like_complete_zero_cost(self):
        usage.record(self.directory, 'unknown-model', 'default', dict(input_tokens=100, output_tokens=20))
        usage.record(self.directory, 'gpt-6.1-sol', 'default', None)
        data = usage.summary(self.directory)
        self.assertEqual(data['unknown_records'], 2)
        self.assertIsNone(data['total_usd'])
        self.assertIn('无法完整计价', usage.display(data))

    def test_cli_records_usage_before_content_validation_failure(self):
        result = SimpleNamespace(returncode=1, stderr='failed', stdout=json.dumps(
            dict(type='turn.completed', usage=dict(input_tokens=90, cached_input_tokens=10, output_tokens=5))))
        with patch.object(main, 'run_codex_cli', return_value=result):
            with self.assertRaises(RuntimeError):
                main.codex_text('test', self.directory, ephemeral=True, text_only=True)
        self.assertEqual(usage.summary(self.directory)['tokens']['input_tokens'], 90)

    def test_timeout_records_missing_usage_and_keeps_failure(self):
        with patch.object(main, 'run_codex_cli', side_effect=TimeoutError('timeout')):
            with self.assertRaises(TimeoutError):
                main.codex_text('test', self.directory, ephemeral=True, text_only=True)
        self.assertEqual(usage.summary(self.directory)['unknown_records'], 1)

    def test_record_survives_reopen_and_summary_does_not_create_calls(self):
        usage.record(self.directory, 'gpt-6.1-sol', 'default', dict(input_tokens=42, output_tokens=3))
        self.store.close()
        self.store = FriendStore(self.directory)
        for _ in range(2):
            data = usage.summary(self.directory)
            self.assertEqual(data['temporary_calls'], 1)
            self.assertEqual(data['tokens']['input_tokens'], 42)


if __name__ == '__main__':
    unittest.main()


def test_legacy_provider_input_is_normalized_once_without_rewriting_history(tmp_path):
    import sqlite3
    from ui_dashboard_data import usage_snapshot
    directory = tmp_path / 'friends' / '12345'
    directory.mkdir(parents=True)
    raw = dict(input_tokens=2, cached_input_tokens=15000, cache_write_input_tokens=5000, output_tokens=20)
    canonical = usage.normalize_usage(raw, 'claude')
    assert canonical['input_tokens'] == 20002
    assert usage.normalize_usage(canonical, 'claude') == canonical
    assert usage.normalize_usage(raw, 'api') == raw
    usage.record(directory, 'legacy-provider', 'claude', raw)
    with sqlite3.connect(directory / 'history.sqlite3') as db:
        # Also keep a genuinely old row with no schema marker.
        db.execute('INSERT INTO usage_calls(model,tier,usage) VALUES(?,?,?)',
                   ('legacy-provider', 'claude', json.dumps(raw)))
    before = (directory / 'history.sqlite3').read_bytes()
    with patch.object(usage, 'plugin', side_effect=ValueError('not installed')):
        totals = usage.summary(directory)['tokens']
    snapshot = usage_snapshot(tmp_path / 'data', '12345')
    assert totals['input_tokens'] == snapshot['input_tokens'] == 40004
    assert totals['cached_input_tokens'] == snapshot['cached_input_tokens'] == 30000
    assert totals['cached_input_tokens'] / totals['input_tokens'] < 1
    assert (directory / 'history.sqlite3').read_bytes() == before
