"""Real subprocess/HTTP + same persistent MCP mailbox, without cloud-model claims."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_messaging
from agent_interop_client import AgentInteropClient, InteropClientError
from agent_interop_contract import make_handoff
from agent_messaging import AgentMailbox, MessagingError


class UniversalInteropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.original_env = os.environ.get('QQ_BRIDGE_HOME')
        os.environ['QQ_BRIDGE_HOME'] = str(self.home)
        self.db = self.home / 'data' / 'agent_mail.sqlite3'
        self.mail = AgentMailbox(self.db)
        self.a = self.mail.register_agent('alice', 'projectA', 'planner', 'custom-model',
                                          capabilities=['research', 'planning'])
        self.b = self.mail.register_agent('alice', 'projectA', 'reviewer', 'ide-extension',
                                          capabilities=['code-review'])
        self.c = self.mail.register_agent('bob', 'projectB', 'scientist', 'local-agent',
                                          capabilities=['research'])
        self.proc = None
        self.start_gateway()

    def tearDown(self):
        self.stop_gateway()
        if self.original_env is None:
            os.environ.pop('QQ_BRIDGE_HOME', None)
        else:
            os.environ['QQ_BRIDGE_HOME'] = self.original_env
        self.tmp.cleanup()

    def start_gateway(self):
        env = dict(os.environ, PYTHONUNBUFFERED='1')
        self.proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'agent_interop_gateway.py'),
                                      '--port', '0', '--db', str(self.db)],
                                     env=env, text=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.proc.stdout.readline().strip()
        assert line.startswith('AGENT_INTEROP_READY='), line + (self.proc.stderr.read() if self.proc.poll() is not None else '')
        self.url = 'http://' + line.split('=', 1)[1]
        self.alice = AgentInteropClient(self.url, agent_messaging.issue_agent_token(self.a))
        self.reviewer = AgentInteropClient(self.url, agent_messaging.issue_agent_token(self.b))
        self.bob = AgentInteropClient(self.url, agent_messaging.issue_agent_token(self.c))

    def stop_gateway(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate(timeout=5)
            self.proc = None

    def raw(self, path, *, token=None, origin=None, host=None, body=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        if origin is not None:
            headers['Origin'] = origin
        if host is not None:
            headers['Host'] = host
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(body).encode('utf-8')
        req = Request(self.url + path, headers=headers, data=body,
                      method='POST' if body is not None else 'GET')
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as res:
                return res.status, json.load(res)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_clients_discover_presence_handoff_and_reply(self):
        names = {t['name'] for t in self.alice.tools()}
        self.assertTrue({'send_message', 'send_handoff', 'open_session', 'heartbeat_session'} <= names)
        agents = self.alice.list_agents()
        self.assertEqual([a['agent_id'] for a in agents], [self.b])
        self.assertFalse(agents[0]['online'])
        active = self.reviewer.call('open_session', client_name='Cursor_Ext', transport='sdk', ttl_seconds=90)
        agents = self.alice.list_agents(capability='review')
        self.assertTrue(agents[0]['online'])
        self.assertEqual(agents[0]['transports'], ['sdk'])
        self.assertEqual(self.reviewer.call('heartbeat_session', session_id=active['session_id'])['session_id'],
                         active['session_id'])
        with self.assertRaises(InteropClientError):
            self.alice.call('heartbeat_session', session_id=active['session_id'])
        accepted = self.alice.handoff([self.b], 'fix-app-301', 'Review the function', ['Run unit tests'],
                                      context='Project owner approved exchanging this task, not executing code.',
                                      artifacts=['src/core.py'], limits={'max_tokens': 1000})
        inbox = self.reviewer.inbox()
        self.assertEqual(len(inbox), 1)
        job = json.loads(inbox[0]['body'])
        self.assertEqual(job['schema'], 'openqqdot.agent-handoff.v1')
        self.assertEqual(job['status'], 'unverified_request')
        self.assertEqual(inbox[0]['message_id'], accepted['message_id'])
        response = self.reviewer.reply(inbox[0]['message_id'], 'Reviewed, tests not run')
        self.assertEqual(response['thread_id'], accepted['thread_id'])
        self.assertEqual([x['body'] for x in self.alice.inbox()], ['Reviewed, tests not run'])
        self.assertTrue(self.reviewer.call('close_session', session_id=active['session_id'])['closed'])
        self.assertFalse(self.alice.list_agents()[0]['online'])

    def test_cross_owner_boundary_persists_even_with_http_token(self):
        self.assertFalse(self.b in {x['agent_id'] for x in self.bob.list_agents()})
        with self.assertRaises(InteropClientError):
            self.alice.send([self.c], 'private cross-tenant task')
        # Administrative fixture: simulate an already APPROVED grant. In production,
        # decide_contact requires TrustedIdentityContext from the receiving owner.
        with self.mail._db() as db:
            db.execute("""INSERT INTO contact_grants
                (grant_id, from_owner, from_project, to_owner, to_project, status,
                 requested_by, created_at) VALUES ('test_grant','alice','projectA','bob','projectB',
                 'approved',?,?)""", (self.a, time.time()))
        self.assertIn(self.c, {x['agent_id'] for x in self.alice.list_agents()})
        message = self.alice.send([self.c], 'authorized cross-project message')
        self.assertEqual(self.bob.inbox()[0]['message_id'], message['message_id'])
        # A different recipient must not gain access to arbitrary other threads.
        with self.assertRaises(InteropClientError):
            self.reviewer.call('reply_message', message_id=message['message_id'], body='not mine')
        self.assertEqual(self.reviewer.call('get_thread', thread_id=message['thread_id'])['untrusted_messages'], [])

    def test_gateway_identity_origin_host_and_legacy_token_refused(self):
        self.assertEqual(self.raw('/v1/tools')[0], 401)
        self.assertEqual(self.raw('/v1/tools', token='nonsense')[0], 401)
        token = agent_messaging.issue_agent_token(self.a)
        self.assertEqual(self.raw('/v1/tools', token=token, origin='https://evil.test')[0], 403)
        self.assertEqual(self.raw('/v1/tools', token=token, host='attacker.example')[0], 403)
        v1 = self.a + '|' + hmac.new(agent_messaging._token_key(), self.a.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(agent_messaging.verify_agent_token(v1), self.a)
        self.assertEqual(self.raw('/v1/tools', token=v1)[0], 401)
        self.assertEqual(self.raw('/v1/tools', token=token)[0], 200)
        self.assertEqual(self.raw('/v1/call', token=token, body={'name':'send_message','arguments': {
            'to':[self.b], 'body': 'from untrusted argument', 'from_agent':self.c}})[0], 400)
        self.assertEqual(self.reviewer.inbox(peek=True), [])
        self.assertEqual(self.raw('/v1/call', token=token, body={'name':'unknown'})[0], 400)

    def test_session_expiry_and_handoff_validation(self):
        active = self.alice.call('open_session', client_name='custom', transport='http', ttl_seconds=15)
        with self.mail._db() as db:
            db.execute('UPDATE agent_sessions SET expires_at=0 WHERE session_id=?', (active['session_id'],))
        self.assertFalse(self.mail.active_sessions(self.a))
        with self.assertRaises(InteropClientError):
            self.alice.call('heartbeat_session', session_id=active['session_id'])
        with self.assertRaises(MessagingError):
            make_handoff('unsafe/../id', 'test', ['done'])
        with self.assertRaises(MessagingError):
            make_handoff('safe', 'test', ['done'], artifacts=['../../secret'])
        with self.assertRaises(MessagingError):
            make_handoff('safe', 'test', ['done'], limits={'max_tokens': True})

    def test_http_agent_and_real_mcp_subprocess_exchange_messages(self):
        sent = self.alice.handoff([self.b], 'mcp-http-bridge-1', 'Review a design',
                                  ['Return a verifiable review'])
        token = agent_messaging.issue_agent_token(self.b)
        env = dict(os.environ, AGENT_MAIL_TOKEN=token)
        def mcp_call(name, args):
            proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'agent_mailbox_mcp.py')],
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            reqs = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                    {'jsonrpc':'2.0','id':2,'method':'tools/call',
                     'params':{'name':name,'arguments':args}}]
            out, err = proc.communicate(''.join(json.dumps(req) + '\n' for req in reqs), timeout=12)
            responses = [json.loads(line) for line in out.splitlines()]
            self.assertEqual(responses[0]['result']['serverInfo']['name'], 'agent_mailbox', err)
            self.assertFalse(responses[1]['result']['isError'], err)
            return json.loads(responses[1]['result']['content'][0]['text'])
        inbox = mcp_call('fetch_inbox', {})['untrusted_messages']
        self.assertEqual([m['message_id'] for m in inbox], [sent['message_id']])
        answered = mcp_call('reply_message', {'message_id':sent['message_id'],
                                               'body':'MCP process reviewed the HTTP handoff'})
        self.assertEqual(answered['thread_id'], sent['thread_id'])
        received = self.alice.inbox()
        self.assertEqual(received[0]['body'], 'MCP process reviewed the HTTP handoff')
        self.assertEqual(self.mail.receipt(sent['message_id'], self.b), 'acked')

    def test_long_poll_receives_asynchronous_message(self):
        results = []
        t = threading.Thread(target=lambda: results.extend(self.reviewer.wait(timeout_seconds=5)))
        t.start()
        time.sleep(0.4)
        sent = self.alice.send([self.b], 'asynchronous provider-neutral event')
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual([m['message_id'] for m in results], [sent['message_id']])
        self.assertEqual(self.mail.receipt(sent['message_id'], self.b), 'delivered')

    def test_short_lived_token_expiry_and_corrupt_tokens(self):
        from unittest.mock import patch
        with patch.object(agent_messaging.time, 'time', return_value=100_000):
            token = agent_messaging.issue_agent_token(self.a, ttl_seconds=20)
        with patch.object(agent_messaging.time, 'time', return_value=100_021):
            with self.assertRaises(PermissionError):
                agent_messaging.verify_agent_token(token)
        for malformed in ['v2.nonsense.nonsense', 'v2.....', 'v2.%%%%.123']:
            with self.assertRaises(PermissionError):
                agent_messaging.verify_agent_token(malformed)

    def test_real_independent_client_processes_survive_gateway_restart(self):
        sender_token = agent_messaging.issue_agent_token(self.a)
        reviewer_token = agent_messaging.issue_agent_token(self.b)
        script = """import os,json\nfrom agent_interop_client import AgentInteropClient\nc=AgentInteropClient(os.environ['BRIDGE_URL'],os.environ['BRIDGE_TOKEN'])\nmode=os.environ['MODE']\nif mode=='send': print(json.dumps(c.send([os.environ['TARGET']], 'independent CLI request')))\nelif mode=='respond':\n inbox=c.inbox(); print(json.dumps(c.reply(inbox[0]['message_id'], 'independent IDE response')))\nelse: print(json.dumps(c.inbox()))\n"""
        def run(mode, token, target=''):
            env=dict(os.environ, BRIDGE_URL=self.url, BRIDGE_TOKEN=token,
                     MODE=mode, TARGET=target, PYTHONPATH=str(ROOT))
            return json.loads(subprocess.check_output([sys.executable, '-c', script],
                                                      text=True, env=env, timeout=10))
        sent = run('send', sender_token, self.b)
        self.stop_gateway()
        self.start_gateway()   # same DB and token key, but a NEW gateway process
        run('respond', reviewer_token)
        answer = run('read', sender_token)
        self.assertEqual(answer[0]['body'], 'independent IDE response')
        self.assertEqual(answer[0]['thread_id'], sent['thread_id'])


if __name__ == '__main__':
    unittest.main()
