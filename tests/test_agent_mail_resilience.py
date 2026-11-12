"""Independent regression tests for durable cross-session agent communication.

No external AI or QQ endpoints: test SQLite transactions, recovery, TaskManager
wakeup state and MCP exposure. Production model quality is outside this suite.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_messaging
from agent_messaging import AgentMailbox, MessagingError, MAX_DELIVERY_ATTEMPTS
from task_manager import TaskManager, TaskStatus


class DurableMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_home = os.environ.get('QQ_BRIDGE_HOME')
        self.previous = (TaskManager._instance, AgentMailbox._instance)
        self._proxy = os.environ.get('HTTPS_PROXY')
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9'
        os.environ['HTTP_PROXY'] = 'http://127.0.0.1:9'
        os.environ['QQ_BRIDGE_HOME'] = str(self.root)
        self.manager = TaskManager(db_path=self.root / 'data' / 'tasks.sqlite3', workspace=self.root)
        self.mail = AgentMailbox(db_path=self.root / 'data' / 'agent_mail.sqlite3')
        TaskManager._instance, AgentMailbox._instance = self.manager, self.mail
        self.a = self.mail.register_agent('alice', 'project_a', 'author', 'codex')
        self.b = self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude')

    def tearDown(self):
        self.manager.stop_worker()
        TaskManager._instance, AgentMailbox._instance = self.previous
        if self.original_home is None:
            os.environ.pop('QQ_BRIDGE_HOME', None)
        else:
            os.environ['QQ_BRIDGE_HOME'] = self.original_home
        if self._proxy is None:
            os.environ.pop('HTTPS_PROXY', None)
            os.environ.pop('HTTP_PROXY', None)
        else:
            os.environ['HTTPS_PROXY'] = self._proxy
            os.environ['HTTP_PROXY'] = self._proxy
        self.temp.cleanup()

    def test_durable_idempotent_send_survives_new_mailbox_instance(self):
        first = self.mail.send(self.a, self.b, 'Review this', idempotency_key='request-2026-1')
        restart = AgentMailbox(self.mail.db_path)
        again = restart.send(self.a, self.b, 'Review this', idempotency_key='request-2026-1')
        self.assertEqual(first.message_id, again.message_id)
        self.assertEqual(len(self.mail.fetch_inbox(self.b)), 1)
        with self.assertRaisesRegex(MessagingError, 'different message content'):
            restart.send(self.a, self.b, 'changed contents', idempotency_key='request-2026-1')
        with self.assertRaises(MessagingError):
            restart.send(self.a, self.a, 'other', idempotency_key='request-2026-1')

    def test_concurrent_process_style_send_is_one_message(self):
        def send_from_separate_connection(i):
            return AgentMailbox(self.mail.db_path).send(self.a, self.b, 'at-most-one',
                                                        idempotency_key='one-request').message_id
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send_from_separate_connection, range(12)))
        self.assertEqual(len(set(results)), 1, results)
        with self.mail._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)

    def test_two_mailboxes_cannot_lease_same_message_concurrently(self):
        sent = self.mail.send(self.a, self.b, 'only once')
        def claim(_):
            return [m.message_id for m in AgentMailbox(self.mail.db_path).fetch_inbox(self.b)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(claim, range(2)))
        self.assertEqual(sorted([len(a), len(b)]), [0, 1])
        self.assertEqual((a or b), [sent.message_id])

    def test_response_is_idempotent_after_ack_failure_and_restart(self):
        request = self.mail.send(self.a, self.b, 'Please help')
        self.mail.fetch_inbox(self.b)
        old_ack = self.mail.acknowledge
        with mock.patch.object(self.mail, 'acknowledge', side_effect=RuntimeError('crashed before ack')):
            with self.assertRaises(RuntimeError):
                self.mail.reply(self.b, request.message_id, 'result one')
        resumed = AgentMailbox(self.mail.db_path)
        response = resumed.reply(self.b, request.message_id, 'result two (must not be sent)')
        self.assertEqual(response.body, 'result one')
        self.assertEqual(len(resumed.replies_to(request.message_id)), 1)
        self.assertEqual(resumed.receipt(request.message_id, self.b), 'acked')
        self.assertEqual(old_ack(self.b, request.message_id), False)

    def test_replay_skips_model_if_response_was_committed(self):
        request = self.mail.send(self.a, self.b, 'Question')
        self.mail.fetch_inbox(self.b, lease_seconds=5)
        self.mail.send(self.b, self.a, 'Answer persisted', kind='response', reply_to=request.message_id,
                       idempotency_key=f'reply:{request.message_id}')
        with self.mail._db() as db:
            db.execute('UPDATE recipients SET lease_until=0 WHERE message_id=? AND agent_id=?',
                       (request.message_id, self.b))
        fake_task = type('Task', (), {'checkpoint': {'agent_id': self.b}, 'owner_id': 'alice',
                                     'max_duration_seconds': 120})()
        with mock.patch('multi_agent_orchestrator.call_single_agent', side_effect=AssertionError('must not call model')):
            answer = agent_messaging.run_agent_inbox(fake_task, None, {}, lambda _: None)
        self.assertEqual(answer['handled_message_ids'], [request.message_id])
        self.assertEqual(self.mail.receipt(request.message_id, self.b), 'acked')
        self.assertEqual(len(self.mail.replies_to(request.message_id)), 1)

    def test_reconciler_restores_lost_wakeup_but_does_not_duplicate_tasks(self):
        sent = self.mail.send(self.a, self.b, 'wake me')
        # A dormant agent changes to an opt-in wake-enabled agent after the message arrived.
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude', wake=True)
        self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 1)
        self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 0)
        pending = [t for t in self.manager.list_tasks(owner_id='alice')
                   if t.category == 'agent_message' and t.status == TaskStatus.PENDING]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].checkpoint['agent_id'], self.b)
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'pending')

    def test_reconcile_expired_lease_not_live_lease(self):
        sent = self.mail.send(self.a, self.b, 'retry after crash')
        self.mail.fetch_inbox(self.b, lease_seconds=100)
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude', wake=True)
        self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 0)
        with self.mail._db() as db:
            db.execute('UPDATE recipients SET lease_until=0 WHERE message_id=? AND agent_id=?',
                       (sent.message_id, self.b))
        self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 1)

    def test_simultaneous_wakeups_across_task_manager_instances_are_atomic(self):
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude', wake=True)
        manager2 = TaskManager(db_path=self.manager.db_path, workspace=self.root)
        agent = self.mail.get_agent(self.b)
        def wake(mgr):
            return self.mail._wake(agent, task_manager=mgr)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(wake, (self.manager, manager2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len([t for t in self.manager.list_tasks(owner_id='alice')
                              if t.category == 'agent_message']), 1)

    def test_worker_recovers_mail_with_no_send_time_wakeup(self):
        """Actual TaskManager background tick notices mail nobody scheduled."""
        folder = self.root / 'friends' / 'alice'
        folder.mkdir(parents=True)
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_agent_messaging import executable, FAKE_CODEX
        codex = executable(self.root, 'codex', FAKE_CODEX)
        (folder / 'friend.json').write_text(json.dumps({'qq_id': 'alice', 'model_api': {
            'provider': 'codex', 'codex_path': str(codex)}}),
                                            encoding='utf-8')
        sent = self.mail.send(self.a, self.b, 'request with lost wake', kind='request')
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'codex', wake=True)
        self.assertTrue(self.manager.start_worker())
        for _ in range(80):
            if self.mail.receipt(sent.message_id, self.b) == 'acked':
                break
            time.sleep(0.1)
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'acked',
                         self.manager.list_tasks(owner_id='alice'))

    def test_reconciliation_does_not_consume_unread_passive_peer_results(self):
        """A periodic worker must not eat answers meant for a live Claude/Codex session."""
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude', wake=True)
        sent = self.mail.send(self.a, self.b, 'A peer finding', kind='finding')
        self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 0)
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'pending')
        self.assertTrue(self.mail.has_pending(self.b))
        self.assertFalse(self.mail.has_pending(self.b, answerable_only=True))

    def test_dead_letters_are_bounded_and_owner_scoped(self):
        sent = self.mail.send(self.a, self.b, 'failing model')
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            batch = self.mail.fetch_inbox(self.b, lease_seconds=5)
            self.assertEqual([m.message_id for m in batch], [sent.message_id])
            with self.mail._db() as db:
                db.execute('UPDATE recipients SET lease_until=0 WHERE message_id=? AND agent_id=?',
                           (sent.message_id, self.b))
        self.assertFalse(self.mail.fetch_inbox(self.b))
        self.assertFalse(self.mail.has_pending(self.b))
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'failed')
        self.assertEqual(self.mail.failed_deliveries(self.b)[0]['delivery_count'], MAX_DELIVERY_ATTEMPTS)
        outsider = self.mail.register_agent('bob', 'lab', 'other', 'codex')
        self.assertEqual(self.mail.failed_deliveries(outsider), [])
        self.assertFalse(self.mail.retry_failed(outsider, sent.message_id))
        self.assertTrue(self.mail.retry_failed(self.b, sent.message_id))
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'pending')

    def test_reconcile_marks_exhausted_leases_failed_and_notifies_receiving_owner(self):
        sent = self.mail.send(self.a, self.b, 'still failing')
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            self.mail.fetch_inbox(self.b)
            with self.mail._db() as db:
                db.execute('UPDATE recipients SET lease_until=0 WHERE message_id=? AND agent_id=?',
                           (sent.message_id, self.b))
        self.mail.register_agent('alice', 'project_a', 'reviewer', 'claude', wake=True)
        with mock.patch('notification_manager.NotificationManager.get_instance') as get_notification:
            self.assertEqual(self.mail.reconcile_wake_tasks(task_manager=self.manager), 0)
        self.assertEqual(self.mail.receipt(sent.message_id, self.b), 'failed')
        notice = get_notification.return_value.publish.call_args.kwargs
        self.assertEqual(notice['recipient_id'], 'alice')
        self.assertNotIn('still failing', notice['content'])  # no raw body in owner notification
        with mock.patch('notification_manager.NotificationManager.get_instance') as again:
            self.mail.reconcile_wake_tasks(task_manager=self.manager)
        again.return_value.publish.assert_not_called()  # one notification per transition

    def test_failed_message_mcp_requires_explicit_confirmation(self):
        from agent_mailbox_mcp import call
        message = self.mail.send(self.a, self.b, 'retry')
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            self.mail.fetch_inbox(self.b)
            with self.mail._db() as db:
                db.execute('UPDATE recipients SET lease_until=0 WHERE message_id=? AND agent_id=?',
                           (message.message_id, self.b))
        self.mail.fetch_inbox(self.b)
        self.assertEqual(call(self.mail, self.b, 'list_failed_messages', {})['failed_messages'][0]['message_id'],
                         message.message_id)
        with self.assertRaises(MessagingError):
            call(self.mail, self.b, 'retry_failed_message', {'message_id': message.message_id})
        self.assertTrue(call(self.mail, self.b, 'retry_failed_message',
                             {'message_id': message.message_id, 'confirm': True})['requeued'])

    def test_old_mail_database_migrates_in_place(self):
        old_db = self.root / 'data' / 'legacy_mail.sqlite3'
        with closing(sqlite3.connect(old_db)) as db, db:
            db.execute('''CREATE TABLE messages (message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
             correlation_id TEXT NOT NULL, reply_to TEXT NOT NULL DEFAULT '', from_agent TEXT NOT NULL,
             kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
             artifacts TEXT NOT NULL DEFAULT '[]', hop_count INTEGER NOT NULL DEFAULT 0,
             created_at REAL NOT NULL, expires_at REAL NOT NULL)''')
            db.execute("""INSERT INTO messages VALUES ('legacy', 'thread', 'legacy', '', 'alice/x/a',
                       'note', '', 'historical data', '[]', 0, 1, 99999999999)""")
        migrated = AgentMailbox(old_db)
        self.assertEqual(migrated.get_message('legacy').body, 'historical data')
        with migrated._db() as db:
            self.assertIn('idempotency_key', [r['name'] for r in db.execute('PRAGMA table_info(messages)')])

    def test_two_independent_mcp_subprocesses_exchange_real_jsonrpc(self):
        import agent_mailbox_mcp
        script = str(ROOT / 'agent_mailbox_mcp.py')
        def server(agent_id):
            env = {**os.environ, 'AGENT_MAIL_TOKEN': agent_messaging.issue_agent_token(agent_id)}
            return subprocess.Popen([sys.executable, '-u', script], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8')
        def call(proc, seq, name, args):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': seq, 'method': 'tools/call',
                                          'params': {'name': name, 'arguments': args}}) + '\n')
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            self.assertEqual(response['id'], seq)
            self.assertFalse(response['result']['isError'], response)
            return json.loads(response['result']['content'][0]['text'])
        left = server(self.a)
        right = server(self.b)
        try:
            sent = call(left, 1, 'send_message', {'to': [self.b], 'body': 'Cross-session review',
                                                 'idempotency_key': 'mcp-retry-123'})
            retried = call(left, 2, 'send_message', {'to': [self.b], 'body': 'Cross-session review',
                                                    'idempotency_key': 'mcp-retry-123'})
            self.assertEqual(sent['message_id'], retried['message_id'])
            inbox = call(right, 3, 'fetch_inbox', {})['untrusted_messages']
            self.assertEqual([m['message_id'] for m in inbox], [sent['message_id']])
            response = call(right, 4, 'reply_message', {'message_id': sent['message_id'],
                                                        'body': 'Peer reviewed'})
            self.assertEqual(response['thread_id'], sent['thread_id'])
            inbox2 = call(left, 5, 'fetch_inbox', {})['untrusted_messages']
            self.assertEqual([m['body'] for m in inbox2], ['Peer reviewed'])
            self.assertEqual(call(right, 6, 'list_failed_messages', {})['failed_messages'], [])
            self.assertEqual(call(right, 8, 'get_mailbox_status', {})['states']['acked'], 1)
            self.assertEqual(call(right, 7, 'retry_failed_message',
                                  {'message_id': sent['message_id'], 'confirm': True})['requeued'], False)
        finally:
            for proc in (left, right):
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
