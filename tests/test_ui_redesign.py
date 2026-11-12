"""Non-GUI security, data truthfulness and navigation-contract tests for the UI rebuild."""
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ui_dashboard_data import system_snapshot, usage_snapshot


class UIDataSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_workspace_unknown_instead_of_fabricated_zero(self):
        r = system_snapshot(self.data)
        self.assertIsNone(r['tasks'])
        self.assertIsNone(r['mail'])
        self.assertIsNone(r['agents'])
        self.assertFalse(r['task_items'])
        self.assertFalse((self.data/'tasks.sqlite3').exists())

    def test_task_fields_only_no_result_or_secrets(self):
        with closing(sqlite3.connect(self.data/'tasks.sqlite3')) as db, db:
            db.execute('''CREATE TABLE tasks(task_id TEXT, title TEXT, category TEXT,
                status TEXT, owner_id TEXT, priority INTEGER, updated_at TEXT,
                created_at TEXT, result TEXT, description TEXT)''')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('one','Fix bug','code','RUNNING','alice',50,'2026-10-24','2026-10-24',
                 'SECRET_RESULT','SECRET_TASK_INPUT'))
        r=system_snapshot(self.data)
        self.assertEqual(r['tasks'],{'RUNNING':1})
        self.assertEqual(r['task_items'][0]['title'],'Fix bug')
        self.assertNotIn('SECRET',json.dumps(r))

    def test_redacted_agent_messages_and_live_sessions(self):
        with closing(sqlite3.connect(self.data/'agent_mail.sqlite3')) as db, db:
            db.executescript('''
             CREATE TABLE agents(agent_id TEXT, name TEXT, provider TEXT, project_id TEXT,
                 capabilities TEXT, wake INTEGER, last_active_at REAL);
             CREATE TABLE agent_sessions(agent_id TEXT, expires_at REAL);
             CREATE TABLE recipients(message_id TEXT, agent_id TEXT, state TEXT, delivery_count INTEGER);
             CREATE TABLE messages(message_id TEXT, thread_id TEXT, from_agent TEXT,
                 subject TEXT, kind TEXT, created_at REAL, body TEXT);
             ''')
            db.execute('INSERT INTO agents VALUES(?,?,?,?,?,?,?)',('alice/proj/alpha','alpha','MCP','proj','["review"]',1,0))
            db.execute('INSERT INTO agent_sessions VALUES(?,?)',('alice/proj/alpha',9999999999))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',('msg1','thread1','A','Request','request',1,'PRIVATE_CHAT_BODY'))
            db.execute('INSERT INTO recipients VALUES(?,?,?,?)',('msg1','alice/proj/alpha','pending',1))
        r=system_snapshot(self.data)
        self.assertEqual(r['sessions'],1)
        self.assertEqual(r['mail'],{'pending':1})
        self.assertEqual(r['mail_items'][0]['subject'],'Request')
        self.assertNotIn('PRIVATE_CHAT_BODY',json.dumps(r))

    def test_usage_real_rows_only_and_no_zero_miss_guess(self):
        friend=self.root/'friends'/'12345'
        friend.mkdir(parents=True)
        with closing(sqlite3.connect(friend/'history.sqlite3')) as db, db:
            db.execute('CREATE TABLE usage_calls(seq INTEGER PRIMARY KEY,model TEXT,usage TEXT)')
            db.execute('INSERT INTO usage_calls(model,usage) VALUES(?,?)',
                       ('gpt',json.dumps({'input_tokens':100,'output_tokens':20,'cached_input_tokens':70})))
            db.execute('INSERT INTO usage_calls(model,usage) VALUES(?,?)',
                       ('legacy','{}'))
        result=usage_snapshot(self.data)
        self.assertTrue(result['available'])
        self.assertEqual(result['input_tokens'],100)
        self.assertEqual(result['cached_input_tokens'],70)
        self.assertEqual(result['calls'],1)
        self.assertEqual(result['unknown'],1)

    def test_six_sections_and_no_static_fake_project_data(self):
        source=(Path(__file__).resolve().parent.parent/'cockpit_ui.html').read_text('utf-8')
        for section in ('home','tasks','agents','qq','insights','settings'):
            self.assertIn(f'id="view-{section}"',source)
            self.assertIn(f'data-nav="{section}"',source)
        self.assertIn('No example tasks',source)
        self.assertNotIn('8,942',source)
        self.assertIn('const esc=',source)

if __name__=='__main__':unittest.main()
