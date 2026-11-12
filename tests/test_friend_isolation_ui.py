"""2026-10-26 user: "这个好像没有按好友隔离" — a friend's page must read only that friend's rows.

The desktop collaboration network and the operational counters used to read the whole database
while sitting on a friend-scoped page, so one friend saw another friend's agents, mail, tasks and
approvals. These checks use real SQLite files and the real read-only snapshot code.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import ui_dashboard_data

try:
    from PySide6.QtWidgets import QApplication, QWidget
    import house_qt.collaboration as collaboration_module
except ImportError:  # PySide6 is required by the current desktop UI
    QApplication = None
    QWidget = object

ONE, TWO = '11111', '22222'


def _mail_db(base: Path):
    db = sqlite3.connect(base / 'agent_mail.sqlite3')
    db.executescript("""
        CREATE TABLE agents (agent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, project_id TEXT NOT NULL,
            name TEXT NOT NULL, provider TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '', wake INTEGER NOT NULL DEFAULT 0,
            registered_at REAL NOT NULL, last_active_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE messages (message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL, reply_to TEXT NOT NULL DEFAULT '', from_agent TEXT NOT NULL,
            kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
            artifacts TEXT NOT NULL DEFAULT '[]', hop_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, expires_at REAL NOT NULL, idempotency_key TEXT NOT NULL DEFAULT '');
        CREATE TABLE recipients (message_id TEXT NOT NULL, agent_id TEXT NOT NULL, state TEXT NOT NULL,
            lease_until REAL NOT NULL DEFAULT 0, delivered_at REAL NOT NULL DEFAULT 0,
            acked_at REAL NOT NULL DEFAULT 0, delivery_count INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '', PRIMARY KEY (message_id, agent_id));
        CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            connector TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]',
            started_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL);
    """)
    now = time.time()
    for agent, owner, project, provider in (
            ('review-a/codex', ONE, 'task_a', 'codex'),
            ('review-b/claude', TWO, 'task_b', 'claude')):
        db.execute('INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (agent, owner, project, agent + ' 名称', provider, '[]', '', 0, now, now))
    for message, thread, sender, body in (
            ('msg-a', 'thread-a', 'review-a/codex', '好友一的协作内容'),
            ('msg-b', 'thread-b', 'review-b/claude', '好友二的协作内容')):
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (message, thread, 'corr-' + message, '', sender, 'note', sender + ' 汇报', body,
                    '[]', 0, now, now + 600, ''))
        db.execute('INSERT INTO recipients VALUES (?,?,?,?,?,?,?,?)',
                   (message, sender, 'pending', 0, 0, 0, 0, ''))
    db.commit()
    db.close()


def _tasks_db(base: Path):
    db = sqlite3.connect(base / 'tasks.sqlite3')
    db.executescript("""
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            category TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 50,
            depends_on TEXT NOT NULL DEFAULT '[]', checkpoint TEXT NOT NULL DEFAULT '{}',
            artifacts TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', verification_info TEXT NOT NULL DEFAULT '{}',
            max_retries INTEGER NOT NULL DEFAULT 2, retry_count INTEGER NOT NULL DEFAULT 0,
            max_duration_seconds INTEGER NOT NULL DEFAULT 1800, token_budget INTEGER NOT NULL DEFAULT 0,
            cost_budget REAL NOT NULL DEFAULT 0.0, owner_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
    """)
    for task_id, owner in (('task_a', ONE), ('task_b', TWO)):
        db.execute("INSERT INTO tasks (task_id, title, description, category, status, owner_id,"
                   " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                   (task_id, '任务 ' + task_id, '说明', 'research', 'RUNNING', owner, '2026-10-26', '2026-10-26'))
    db.commit()
    db.close()


def _approvals_db(base: Path):
    db = sqlite3.connect(base / 'approvals.sqlite3')
    db.executescript("""
        CREATE TABLE approval_requests (req_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            category TEXT NOT NULL, description TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL, decision_reason TEXT NOT NULL DEFAULT '',
            allowed_scope TEXT NOT NULL DEFAULT 'once', owner_id TEXT NOT NULL DEFAULT '',
            timeout_seconds INTEGER NOT NULL DEFAULT 300, created_at TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '');
    """)
    for req_id, owner in (('req_a', ONE), ('req_b', TWO)):
        db.execute("INSERT INTO approval_requests (req_id, task_id, category, description, status,"
                   " owner_id, created_at) VALUES (?,?,?,?,?,?,?)",
                   (req_id, 'task_' + req_id[-1], 'cmd', '命令', 'pending', owner, '2026-10-26'))
    db.commit()
    db.close()


@unittest.skipIf(QApplication is None, 'PySide6 not installed')
class FriendIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        _mail_db(self.base)
        _tasks_db(self.base)
        _approvals_db(self.base)
        self.addCleanup(self.temp.cleanup)

    def test_snapshot_without_owner_still_sees_every_friend(self):
        report = ui_dashboard_data.system_snapshot(self.base)
        self.assertEqual(report['agents'], 2)
        self.assertEqual(sum(report['tasks'].values()), 2)
        self.assertEqual(sum(report['mail'].values()), 2)

    def test_snapshot_with_owner_returns_only_that_friends_rows(self):
        report = ui_dashboard_data.system_snapshot(self.base, ONE)
        self.assertEqual(report['agents'], 1)
        self.assertEqual([item['agent_id'] for item in report['agent_items']], ['review-a/codex'])
        self.assertEqual(sum(report['tasks'].values()), 1)
        self.assertEqual([item['task_id'] for item in report['task_items']], ['task_a'])
        self.assertEqual(sum(report['approvals'].values()), 1)
        self.assertEqual([item['req_id'] for item in report['approval_items']], ['req_a'])
        self.assertEqual(sum(report['mail'].values()), 1)
        self.assertEqual([item['to_agent'] for item in report['mail_items']], ['review-a/codex'])

    def test_collaboration_page_follows_the_selected_friend(self):
        house = _StubHouse(ONE)
        page = collaboration_module.CollaborationPage(house, base=self.base)
        self.addCleanup(page.deleteLater)
        self.assertEqual([agent['agent_id'] for agent in page.data['agents']], ['review-a/codex'])
        self.assertEqual(page.data['summary']['agents'], 1)
        self.assertIn('当前好友 ' + ONE, page.caption.text())
        house.current_qq = TWO
        page.refresh()
        self.assertEqual([agent['agent_id'] for agent in page.data['agents']], ['review-b/claude'])
        self.assertIn('当前好友 ' + TWO, page.caption.text())

    def test_collaboration_page_shows_nothing_without_a_friend(self):
        house = _StubHouse(None)
        page = collaboration_module.CollaborationPage(house, base=self.base)
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.data['agents'], [])
        self.assertEqual(page.agent_list.count(), 0)
        self.assertIn('尚未选择好友', page.caption.text())

    def test_downloaded_models_share_one_directory_for_every_friend(self):
        """'下载的模型应该都是共用的': weights live under the project root, never under friends/<qq>/."""
        import model_downloader
        from house_settings import root

        self.assertEqual(model_downloader.project_root(), Path(root()).resolve())
        for kind, tiers in model_downloader.MODEL_CATALOG.items():
            for tier, config in tiers.items():
                target = config['target_dir']
                self.assertNotIn('friends', target, f'{kind}/{tier}: {target}')
                self.assertFalse(Path(target).is_absolute(), f'{kind}/{tier}: {target}')


class _StubHouse(QWidget):
    """The collaboration page only asks its window for the selected friend."""

    def __init__(self, qq):
        super().__init__()
        self.current_qq = qq
        self.messages = []

    def switchTo(self, _page):
        pass


if __name__ == '__main__':
    unittest.main()
