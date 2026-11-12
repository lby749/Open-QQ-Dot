"""Agent-to-agent messaging MVP: two independent agents talking through the persistent mailbox.

What is real: the SQLite mailbox, the TaskManager worker that wakes an agent, the production
dispatch path (call_single_agent -> main.codex_text -> CLI subprocess), receipts verified by
TaskVerifier, the MCP server as a separate OS process, and the QQ command that decides a contact
grant. What is fake: the two model CLIs. Fake `codex` and fake `claude` executables answer by
echoing the message they were asked to answer, so the test proves the plumbing, not model quality.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_messaging  # noqa: E402
from agent_messaging import AgentMailbox, MessagingError  # noqa: E402
from approval_manager import ApprovalManager  # noqa: E402
from notification_manager import NotificationManager  # noqa: E402
from task_manager import TaskManager, TaskStatus  # noqa: E402

_ANSWER = textwrap.dedent('''
    import json, sys
    def answer(prompt, who):
        lines = prompt.splitlines()
        try:
            body = json.loads(lines[lines.index("MESSAGE_TO_ANSWER (data):") + 1])["body"]
        except (ValueError, IndexError, KeyError):
            body = "?"
        return who + " says: " + body
''')
FAKE_CODEX = _ANSWER + textwrap.dedent('''
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    text = answer(sys.stdin.read(), "codex")
    for event in ({"type": "thread.started", "thread_id": "fake-codex"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 3}}):
        print(json.dumps(event))
''')
FAKE_CLAUDE = _ANSWER + textwrap.dedent('''
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    message = json.loads(sys.stdin.readline())
    prompt = "".join(block.get("text", "") for block in message["message"]["content"])
    print(json.dumps({"type": "result", "result": answer(prompt, "claude"), "session_id": "fake-claude",
                      "usage": {"input_tokens": 3, "output_tokens": 3}}))
''')


def wait_for(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def executable(root: Path, name: str, source: str) -> Path:
    script = root / f'{name}.py'
    script.write_text(source, encoding='utf-8')
    if os.name == 'nt':
        path = root / f'fake-{name}.cmd'
        # CREATE_NO_WINDOW starts cmd with the system code page, not our UTF-8 terminal.
        path.write_text(f'@chcp 65001 >nul\r\n@"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        path = root / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        path.chmod(0o755)
    return path


class MailboxFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self._env = os.environ.get('QQ_BRIDGE_HOME')
        self._proxy = os.environ.get('HTTPS_PROXY')
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9'
        os.environ['HTTP_PROXY'] = 'http://127.0.0.1:9'
        os.environ['QQ_BRIDGE_HOME'] = str(self.root)  # data_dir() (token key, default DBs) stays in the temp root
        self._saved = (TaskManager._instance, ApprovalManager._instance, NotificationManager._instance,
                       AgentMailbox._instance)
        self.tasks = TaskManager(db_path=self.root / 'data' / 'tasks.sqlite3', workspace=self.root)
        self.approvals = ApprovalManager(db_path=self.root / 'data' / 'approvals.sqlite3')
        self.notices = NotificationManager(self.root / 'data' / 'notifications.sqlite3')
        self.mail = AgentMailbox(self.root / 'data' / 'agent_mail.sqlite3')
        TaskManager._instance, ApprovalManager._instance, NotificationManager._instance, AgentMailbox._instance = \
            self.tasks, self.approvals, self.notices, self.mail
        self.sent = []
        self.notices.register_channel_sink(lambda msg: self.sent.append(msg) or True, name='capture')

    def tearDown(self):
        self.tasks.stop_worker()
        TaskManager._instance, ApprovalManager._instance, NotificationManager._instance, AgentMailbox._instance = self._saved
        if self._env is None:
            os.environ.pop('QQ_BRIDGE_HOME', None)
        else:
            os.environ['QQ_BRIDGE_HOME'] = self._env
        if self._proxy is None:
            os.environ.pop('HTTPS_PROXY', None)
            os.environ.pop('HTTP_PROXY', None)
        else:
            os.environ['HTTPS_PROXY'] = self._proxy
            os.environ['HTTP_PROXY'] = self._proxy
        self._tmp.cleanup()


class TwoAgentConversationTests(MailboxFixture):
    def test_codex_and_claude_agents_exchange_requests_and_replies_through_woken_tasks(self):
        friend = self.root / 'friends' / '10001'
        friend.mkdir(parents=True)
        codex, claude = executable(self.root, 'codex', FAKE_CODEX), executable(self.root, 'claude', FAKE_CLAUDE)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '10001', 'model_api': {
            'provider': 'codex', 'codex_path': str(codex), 'claude_path': str(claude)}}), encoding='utf-8')
        builder = self.mail.register_agent('10001', 'shop', 'builder', 'codex', capabilities=['code'], wake=True)
        reviewer = self.mail.register_agent('10001', 'shop', 'reviewer', 'claude', capabilities=['review'], wake=True)
        self.assertEqual([a['agent_id'] for a in self.mail.list_agents(builder, capability='review')], [reviewer])
        self.assertTrue(self.tasks.start_worker())

        # Builder -> reviewer: the reviewer is idle, so a TaskManager task wakes it on its inbox.
        ask = self.mail.send(builder, reviewer, 'Please review: def add(a, b): return a - b', subject='add()')
        answer = self.mail.wait_for_reply(ask.message_id, timeout=30)
        self.assertIsNotNone(answer, self.tasks.list_tasks(owner_id='10001'))
        self.assertEqual((answer.from_agent, answer.kind, answer.thread_id), (reviewer, 'response', ask.thread_id))
        self.assertIn('claude says: Please review', answer.body)
        self.assertEqual(self.mail.receipt(ask.message_id, reviewer), 'acked')

        # Reviewer -> builder in the same thread: the conversation goes both ways, no supervisor relaying.
        fix = self.mail.send(reviewer, builder, 'Bug: add subtracts. Please fix.', thread_id=ask.thread_id)
        fixed = self.mail.wait_for_reply(fix.message_id, timeout=30)
        self.assertIsNotNone(fixed)
        self.assertIn('codex says: Bug: add subtracts', fixed.body)
        self.assertEqual([m.from_agent for m in self.mail.thread(ask.thread_id, builder)],
                         [builder, reviewer, reviewer, builder])

        # Each wake-up task completed only after TaskVerifier found the receipts in the mailbox.
        def _get_done():
            completed = [t for t in self.tasks.list_tasks(owner_id='10001')
                         if t.category == 'agent_message' and t.status == TaskStatus.COMPLETED]
            return completed[:2] if len(completed) >= 2 else None
        done = wait_for(_get_done)
        self.assertEqual(len(done), 2)
        self.assertTrue(all(t.verification_info.get('verifier_type') == 'agent_mail_receipts' for t in done))
        self.assertFalse([m for m in self.sent if m.title.startswith('Task completed: Inbox')])  # no notice spam

    def test_six_requests_are_all_responded_to_across_bounded_wake_batches(self):
        """Regression: inbox wake handled five and stranded the sixth forever."""
        friend = self.root / 'friends' / '10001'
        friend.mkdir(parents=True)
        claude = executable(self.root, 'claude', FAKE_CLAUDE)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '10001', 'model_api': {
            'provider': 'claude', 'claude_path': str(claude)}}), encoding='utf-8')
        sender = self.mail.register_agent('10001', 'shop', 'sender', 'codex', wake=False)
        worker = self.mail.register_agent('10001', 'shop', 'worker', 'claude', wake=True)
        requests = [self.mail.send(sender, worker, f'question-{i}') for i in range(6)]
        self.assertTrue(self.tasks.start_worker())
        def all_done():
            return all(self.mail.receipt(m.message_id, worker) == 'acked'
                       and self.mail.replies_to(m.message_id) for m in requests)
        self.assertTrue(wait_for(all_done, timeout=50),
                        {m.message_id: self.mail.receipt(m.message_id, worker) for m in requests})
        self.assertFalse(self.mail.has_pending(worker))
        def completed_count():
            return len([t for t in self.tasks.list_tasks(owner_id='10001')
                        if t.category == 'agent_message' and t.status == TaskStatus.COMPLETED])
        self.assertTrue(wait_for(lambda: completed_count() >= 2, timeout=10),
                        self.tasks.list_tasks(owner_id='10001'))

    def test_messages_survive_a_restart_and_unacked_leases_are_redelivered(self):
        a = self.mail.register_agent('10001', 'shop', 'a', 'codex')
        b = self.mail.register_agent('10001', 'shop', 'b', 'claude')
        sent = self.mail.send(a, b, 'hello after restart')
        restarted = AgentMailbox(self.mail.db_path)  # a new process would open the same file
        first = restarted.fetch_inbox(b, lease_seconds=5)
        self.assertEqual([m.message_id for m in first], [sent.message_id])
        self.assertEqual(restarted.fetch_inbox(b), [])  # leased, not delivered twice concurrently
        with restarted._db() as db:  # the lease runs out without an ack (the agent crashed)
            db.execute('UPDATE recipients SET lease_until = 0 WHERE message_id = ?', (sent.message_id,))
        again = AgentMailbox(self.mail.db_path).fetch_inbox(b)
        self.assertEqual([m.message_id for m in again], [sent.message_id])
        self.assertTrue(self.mail.acknowledge(b, sent.message_id))
        self.assertEqual(self.mail.fetch_inbox(b), [])


class BoundaryAndLimitTests(MailboxFixture):
    def test_cross_owner_messages_need_a_grant_the_receiving_owner_approves_from_qq(self):
        import main
        alice = self.mail.register_agent('10001', 'shop', 'alice', 'codex')
        bob = self.mail.register_agent('10002', 'lab', 'bob', 'claude')
        self.assertEqual(self.mail.list_agents(alice), [])  # discovery does not cross owners
        with self.assertRaises(MessagingError):
            self.mail.send(alice, bob, 'hi')
        grant = self.mail.request_contact(alice, bob, 'need a data review')
        self.assertTrue(any(m.title == f'Agent contact request ({grant})' and m.recipient_id == '10002' for m in self.sent))
        self.assertIn('当前没有发给你的待确认通信请求', main.handle_approval_command(f'批准 {grant}', '10001')[0])  # not alice's to decide
        self.assertIn('已允许', main.handle_approval_command(f'批准 {grant}', '10002')[0])
        self.assertTrue(self.mail.send(alice, bob, 'hi').message_id)
        self.assertEqual([a['agent_id'] for a in self.mail.list_agents(bob)], [alice])

    def test_loops_self_messages_and_unsafe_artifacts_are_refused(self):
        a = self.mail.register_agent('10001', 'shop', 'a', 'codex')
        b = self.mail.register_agent('10001', 'shop', 'b', 'claude')
        with self.assertRaises(MessagingError):
            self.mail.send(a, a, 'me')
        with self.assertRaises(MessagingError):
            self.mail.send(a, b, 'see file', artifacts=['../../secrets.txt'])
        message = self.mail.send(a, b, 'ping', kind='note')
        speaker = b
        with self.assertRaises(MessagingError) as loop:
            for _ in range(agent_messaging.MAX_HOPS + 2):
                message = self.mail.reply(speaker, message.message_id, 'pong', kind='note')
                speaker = a if speaker == b else b
        self.assertIn('loop', str(loop.exception))


class McpServerTests(MailboxFixture):
    def _server(self, token):
        env = dict(os.environ, AGENT_MAIL_TOKEN=token, QQ_BRIDGE_HOME=str(self.root))
        return subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'agent_mailbox_mcp.py')], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8')

    def test_live_session_tools_with_server_bound_identity(self):
        a = self.mail.register_agent('10001', 'shop', 'a', 'codex')
        b = self.mail.register_agent('10001', 'shop', 'b', 'claude')
        proc = self._server(agent_messaging.issue_agent_token(a))
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'send_message', 'arguments': {'to': [b], 'body': 'from a live session'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'send_message', 'arguments': {'to': [a], 'body': 'to myself'}}},
        ]
        out, err = proc.communicate('\n'.join(json.dumps(r) for r in requests) + '\n', timeout=60)
        replies = {r['id']: r for r in map(json.loads, out.splitlines())}
        self.assertEqual(replies[1]['result']['serverInfo']['name'], 'agent_mailbox', err)
        self.assertIn('fetch_inbox', [t['name'] for t in replies[2]['result']['tools']])
        self.assertFalse(replies[3]['result']['isError'])
        self.assertTrue(replies[4]['result']['isError'])  # refusals come back as tool errors
        inbox = self.mail.fetch_inbox(b)
        self.assertEqual([(m.from_agent, m.body) for m in inbox], [(a, 'from a live session')])

    def test_sessions_get_the_mailbox_only_when_the_friend_opted_in(self):
        import claude_backend
        friend = self.root / 'friends' / '10001'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '10001'}), encoding='utf-8')
        self.assertNotIn('agent_mailbox', json.loads(claude_backend._mcp_config(friend))['mcpServers'])
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '10001', 'agent_mail': {
            'enabled': True, 'project': 'shop', 'name': 'reviewer', 'capabilities': ['review']}}), encoding='utf-8')
        entry = json.loads(claude_backend._mcp_config(friend))['mcpServers']['agent_mailbox']
        self.assertEqual(agent_messaging.verify_agent_token(entry['env']['AGENT_MAIL_TOKEN']), '10001/shop/reviewer')
        self.assertEqual(self.mail.get_agent('10001/shop/reviewer').capabilities, ['review'])

    def test_server_refuses_to_start_without_a_valid_token(self):
        a = self.mail.register_agent('10001', 'shop', 'a', 'codex')
        forged = a + '|' + '0' * 64
        proc = self._server(forged)
        _, err = proc.communicate('', timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Invalid agent mailbox token', err)


# Supervisor mode: the fake codex is the master (plans a 3-step chain, then synthesises); the fake
# claude is the worker and reports whether peer findings reached its prompt.
SUPERVISOR_CODEX = textwrap.dedent('''
    import json, sys
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    prompt = sys.stdin.read()
    if "need_collaboration" in prompt:
        steps = [{"id": f"task_{i}", "title": f"step {i}", "agent": "claude", "task_type": "analysis",
                  "depends_on": [f"task_{i - 1}"] if i > 1 else [], "subtask_prompt": f"do step {i}"}
                 for i in (1, 2, 3)]
        text = "```json\\n" + json.dumps({"need_collaboration": True, "analysis": "chain", "subtasks": steps}) + "\\n```"
    else:
        text = "FINAL: all three steps combined"
    for event in ({"type": "thread.started", "thread_id": "fake-master"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 3}}):
        print(json.dumps(event))
''')
SUPERVISOR_CLAUDE = textwrap.dedent('''
    import json, re, sys
    sys.stdin.reconfigure(encoding="utf-8"); sys.stdout.reconfigure(encoding="utf-8")
    message = json.loads(sys.stdin.readline())
    prompt = "".join(block.get("text", "") for block in message["message"]["content"])
    step = re.search(r"Subtask Identifier: (\\S+)", prompt).group(1)
    text = f"claude finished {step}; peers_seen={'PEER_FINDINGS' in prompt}"
    print(json.dumps({"type": "result", "result": text, "session_id": "fake-worker",
                      "usage": {"input_tokens": 3, "output_tokens": 3}}))
''')


class SupervisorModeTests(MailboxFixture):
    def test_orchestrated_task_is_recorded_as_one_supervisor_thread_with_peer_findings(self):
        import multi_agent_orchestrator
        friend = self.root / 'friends' / '10001'
        friend.mkdir(parents=True)
        codex = executable(self.root, 'codex', SUPERVISOR_CODEX)
        claude = executable(self.root, 'claude', SUPERVISOR_CLAUDE)
        profile = {'qq_id': '10001', 'model_api': {'provider': 'codex', 'active_providers': ['codex', 'claude'],
                                                   'codex_path': str(codex), 'claude_path': str(claude)}}
        (friend / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
        outputs = {}

        def run(task, log):
            text, _ = multi_agent_orchestrator.orchestrate(task.description, friend, profile=profile)
            outputs[task.task_id] = text
            return {'output': text}

        self.tasks.register_custom_executor('general', run)
        tid = self.tasks.create_task(title='Chain', description='three dependent steps', category='general',
                                     owner_id='10001', max_retries=0)
        self.assertTrue(self.tasks.start_worker())
        self.assertTrue(wait_for(lambda: tid in outputs, timeout=90), self.tasks.get_task(tid))

        thread_id = self.tasks.get_task(tid).checkpoint.get('collaboration_thread')
        self.assertTrue(thread_id)
        self.assertIn(thread_id, outputs[tid])  # the trace points the owner at the audit thread
        self.assertIn('FINAL: all three steps combined', outputs[tid])
        supervisor, worker = f'10001/{tid}/supervisor', f'10001/{tid}/claude'
        messages = self.mail.thread(thread_id, supervisor)
        self.assertEqual([(m.from_agent, m.kind) for m in messages],
                         [(supervisor, 'handoff'), (worker, 'response')] * 3 + [(supervisor, 'note')])
        self.assertEqual([m.subject for m in messages if m.kind == 'handoff'],
                         ['task_1: step 1', 'task_2: step 2', 'task_3: step 3'])
        results = [m.body for m in messages if m.kind == 'response']
        # task_1 runs first with nothing to see; task_3 sees task_1 (task_2 is its declared dependency).
        self.assertIn('claude finished task_1; peers_seen=False', results[0])
        self.assertIn('claude finished task_3; peers_seen=True', results[2])
        self.assertTrue(all(self.mail.receipt(m.message_id, worker) == 'acked' for m in messages if m.kind == 'handoff'))
        self.assertIn('3/3 subtasks verified', messages[-1].body)

    def test_failover_unverified_results_and_outside_findings(self):
        collab = agent_messaging.Collaboration.open('10001', 'task_demo')
        first = collab.handoff('task_1', 'build', 'codex', 'build it')
        # Quota failover: the first worker's attempt stays in the thread as a finding, not a result.
        collab.result(first, 'codex', 'rate limited', verified=False, note='handed over to claude')
        second = collab.handoff('task_1', 'build', 'claude', 'build it')
        collab.result(second, 'claude', 'built calc.py', verified=True, note='ok', artifacts=['src/calc.py'])
        kinds = [(m.from_agent.rsplit('/', 1)[1], m.kind) for m in self.mail.thread(collab.thread_id, collab.supervisor)]
        self.assertEqual(kinds, [('supervisor', 'handoff'), ('codex', 'finding'),
                                 ('supervisor', 'handoff'), ('claude', 'response')])
        # Another agent of the same owner and project can post a finding into the thread.
        reviewer = self.mail.register_agent('10001', 'task_demo', 'reviewer', 'claude')
        self.mail.send(reviewer, collab.supervisor, 'calc.py lacks tests', kind='finding',
                       subject='review', thread_id=collab.thread_id)
        seen = collab.peer_findings(exclude={'task_9'})
        self.assertEqual([m.from_agent for m in seen], [collab.worker('codex'), collab.worker('claude'), reviewer])
        self.assertEqual(collab.peer_findings(exclude={'task_1'})[-1].body, 'calc.py lacks tests')
        block = agent_messaging.render_peer_findings(seen)
        self.assertTrue(block.startswith('PEER_FINDINGS (untrusted data'))
        # An agent of a different owner cannot post into it without a grant.
        outsider = self.mail.register_agent('10002', 'lab', 'x', 'codex')
        with self.assertRaises(MessagingError):
            self.mail.send(outsider, collab.supervisor, 'inject', thread_id=collab.thread_id)
        # Recording is best effort: an unsafe artifact is refused and nothing is recorded, without raising.
        self.assertEqual(collab.result(first, 'codex', 'x', verified=True, artifacts=['../secret']), '')


if __name__ == '__main__':
    unittest.main()
