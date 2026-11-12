import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collaboration_view
from agent_messaging import AgentMailbox, Collaboration


def task_db(base: Path, task_id: str, status: str, title: str = '生成 CHANGELOG'):
    db = sqlite3.connect(base / 'tasks.sqlite3')
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, title TEXT, status TEXT, '
                       'category TEXT, updated_at TEXT)')
            db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?)', (task_id, title, status, 'file', ''))
    finally:
        db.close()


class CollaborationViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.mailbox = AgentMailbox(self.base / 'agent_mail.sqlite3')

    def collaborate(self, owner='10001', task='task_a', verified=True):
        collab = Collaboration(self.mailbox, owner, task)
        first = collab.handoff('t1', '读取 calc.py', 'antigravity', 'FIRST')
        collab.result(first, 'antigravity', '找到 add 与 div', verified=verified)
        second = collab.handoff('t2', '写 CHANGELOG', 'codex', 'SECOND')
        collab.result(second, 'codex', 'x' * 500, verified=True, artifacts=['CHANGELOG.md'])
        collab.note('Synthesis by claude: 2/2 subtasks verified; final reply 40 chars.', ['antigravity', 'codex'])
        return collab

    def test_tree_shows_task_lead_handoffs_and_results(self):
        self.collaborate()
        task_db(self.base, 'task_a', 'RUNNING')
        data = collaboration_view.snapshot(self.base)
        self.assertEqual(data['summary']['threads'], 1)
        thread = data['threads'][0]
        self.assertEqual(thread['title'], '生成 CHANGELOG')
        root = thread['tree']
        self.assertEqual((root['type'], root['label'], root['brief']), ('task', '生成 CHANGELOG', '进行'))
        [lead] = root['children']
        self.assertEqual((lead['type'], lead['label'], lead['provider']), ('agent', '编排主管 · Claude Code', 'claude'))
        handoffs = [n for n in lead['children'] if n['kind'] == 'handoff']
        self.assertEqual([n['label'] for n in handoffs], ['Antigravity', 'Codex'])
        self.assertEqual([n['children'][0]['kind_label'] for n in handoffs], ['交回结果', '交回结果'])
        summary = lead['children'][-1]
        self.assertEqual((summary['kind_label'], summary['provider']), ('汇总', 'claude'))
        self.assertIn('2/2 个子任务通过验证', summary['detail'])
        for node in handoffs:  # every message node points at its message
            self.assertEqual(thread['messages'][node['index']]['kind'], 'handoff')
        long = thread['messages'][handoffs[1]['children'][0]['index']]
        self.assertEqual(len(long['excerpt']), collaboration_view.EXCERPT + 1)  # cut, with an ellipsis
        self.assertEqual(long['artifacts'], ['CHANGELOG.md'])
        # the state under each node, two characters
        self.assertEqual(lead['brief'], '规划')
        self.assertEqual([n['brief'] for n in handoffs], ['交回', '交回'])
        self.assertEqual([n['children'][0]['brief'] for n in handoffs], ['通过', '通过'])
        self.assertEqual(summary['brief'], '2/2')

        def every(node):
            yield node
            for child in node['children']:
                yield from every(child)
        self.assertTrue(all(0 < len(node['brief']) <= 3 for node in every(root)))
        # titles stay narrow: a one-character tag, then a three-letter name
        self.assertEqual((root['short'], lead['short']), ('生成 CHA…', '主管'))
        self.assertEqual([n['tag'] + n['short'] for n in handoffs], ['派Agy', '派Cdx'])
        self.assertEqual((handoffs[0]['children'][0]['tag'], summary['tag'], summary['short']), ('回', '汇', '主管'))
        self.assertEqual(collaboration_view._fit('给商城加购物车功能'), '给商城加购…')

    def test_brief_follows_what_came_back(self):
        collab = Collaboration(self.mailbox, '10001', 'task_b')
        waiting = collab.handoff('t1', '等结果', 'codex', 'A')
        unverified = collab.handoff('t2', '没通过', 'claude', 'B')
        collab.result(unverified, 'claude', '写了一半', verified=False)
        root = collaboration_view.snapshot(self.base)['threads'][0]['tree']
        [lead] = root['children']
        briefs = {node['detail'].split(':')[0]: node['brief'] for node in lead['children']}
        self.assertEqual(briefs, {'t1': '等待', 't2': '未过'})  # t1 not picked up yet
        self.assertEqual(lead['children'][1]['children'][0]['brief'], '未过')
        self.assertEqual(root['brief'], '对话')  # no task row: a plain conversation
        self.assertTrue(waiting)

    def test_status_follows_the_task_and_audit_copies_read_as_recorded(self):
        self.collaborate(verified=False)
        task_db(self.base, 'task_a', 'RUNNING')
        agents = {a['label']: a for a in collaboration_view.snapshot(self.base)['agents']}
        self.assertEqual(agents['Antigravity']['status'], 'working')
        self.assertEqual(agents['编排主管 · Claude Code']['lead_provider'], 'claude')
        task_db(self.base, 'task_a', 'COMPLETED')
        data = collaboration_view.snapshot(self.base)
        self.assertEqual({a['status'] for a in data['agents']}, {'idle'})
        self.assertEqual(data['summary']['waiting'], 0)  # unread audit copies are not "waiting"
        thread = data['threads'][0]
        finding = next(m for m in thread['messages'] if m['kind'] == 'finding')
        self.assertEqual([r['state_label'] for r in finding['to']], ['已记录'])
        self.assertEqual(finding['kind_label'], '未验证结果')

    def test_peer_mail_without_a_task_shows_waiting(self):
        alice = self.mailbox.register_agent('10001', 'shop', 'alice', 'claude')
        bob = self.mailbox.register_agent('10001', 'shop', 'bob', 'codex')
        self.mailbox.send(alice, bob, '请审查 cart.py', kind='review', subject='审查')
        data = collaboration_view.snapshot(self.base)
        self.assertEqual({a['detail'] for a in data['agents']}, {'刚刚活动'})
        [root] = [t['tree'] for t in data['threads']]
        self.assertEqual(root['type'], 'task')
        [node] = root['children']  # no supervisor: messages hang under the conversation
        self.assertEqual((node['kind_label'], node['label'], node['mark']), ('请求审查', 'bob（Codex）', '…'))
        later = {a['agent_id']: a for a in collaboration_view.snapshot(self.base, now=time.time() + 3600)['agents']}
        self.assertEqual((later[bob]['status'], later[bob]['detail']), ('waiting', '1 条消息待处理'))
        self.assertEqual(later[alice]['status'], 'idle')

    def test_owner_scope_and_missing_database(self):
        self.collaborate(owner='10001', task='task_a')
        self.collaborate(owner='10002', task='task_b')
        mine = collaboration_view.snapshot(self.base, owner='10001')
        self.assertEqual({a['owner'] for a in mine['agents']}, {'10001'})
        self.assertEqual(len(mine['threads']), 1)
        empty = collaboration_view.snapshot(self.base / 'missing')
        self.assertEqual((empty['agents'], empty['threads']), ([], []))

    def test_same_thread_does_not_reveal_another_owners_private_messages(self):
        a = self.mailbox.register_agent('10001', 'p', 'a', 'codex')
        b = self.mailbox.register_agent('10001', 'p', 'b', 'claude')
        c = self.mailbox.register_agent('10002', 'q', 'c', 'codex')
        d = self.mailbox.register_agent('10002', 'q', 'd', 'claude')
        first = self.mailbox.send(a, b, '可见消息', kind='note')
        self.mailbox.send(c, d, '其他好友的私密数据', kind='note', thread_id=first.thread_id)
        mine = collaboration_view.snapshot(self.base, owner='10001')
        self.assertEqual([m['excerpt'] for m in mine['threads'][0]['messages']], ['可见消息'])
        self.assertEqual(mine['threads'][0]['count'], 1)


class PagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        collab = Collaboration(AgentMailbox(self.base / 'agent_mail.sqlite3'), '10001', 'task_a')
        handoff = collab.handoff('t1', '读取 calc.py', 'antigravity', 'FIRST')
        collab.result(handoff, 'antigravity', '找到 add 与 div', verified=True)
        task_db(self.base, 'task_a', 'COMPLETED')

    def test_phone_route_is_scoped_to_the_friend(self):
        import house_web
        with patch('house_settings.data_dir', return_value=self.base):
            self.assertEqual(len(house_web.GET['collaboration']('10001', {})['threads']), 1)
            self.assertEqual(house_web.GET['collaboration']('10002', {})['threads'], [])

    def test_desktop_tree_lays_out_nodes_and_opens_a_message(self):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        try:
            from PySide6.QtCore import QPointF
            from PySide6.QtWidgets import QApplication, QWidget
        except ImportError:
            self.skipTest('PySide6 not installed')
        app = QApplication.instance() or QApplication([])
        from house_qt.collaboration import CollaborationPage
        house = QWidget()
        house.current_qq = '10001'
        house.pages, house.switchTo = {'modelPage': None}, lambda page: None
        page = CollaborationPage(house, base=self.base)
        self.addCleanup(house.deleteLater)
        page.resize(1200, 800)
        page.show()
        app.processEvents()
        self.assertEqual(page.metrics['threads'].text(), '1')
        self.assertEqual(page.thread_list.count(), 1)

        app.processEvents()
        kinds = [node.get('kind') or node['type'] for _, node in page.tree.boxes]
        self.assertEqual(kinds, ['task', 'agent', 'handoff', 'response'])
        tops = [box.top() for box, _ in page.tree.boxes]
        self.assertEqual(len(set(tops)), len(tops))  # one node per row: nothing side by side
        self.assertEqual(tops, sorted(tops))
        lefts = [box.left() for box, _ in page.tree.boxes]
        self.assertEqual(lefts, sorted(lefts))  # task, lead, handoff, result: each level indented further
        self.assertEqual([node['brief'] for _, node in page.tree.boxes], ['完成', '规划', '交回', '通过'])
        result_box = page.tree.boxes[-1][0]
        page.tree.mousePressEvent(type('Click', (), {'position': lambda self: QPointF(result_box.center())})())
        self.assertIn('找到 add 与 div', page.detail.toPlainText())
        self.assertIn('Antigravity →', page.detail.toPlainText())
        page.timer.stop()


if __name__ == '__main__':
    unittest.main()
