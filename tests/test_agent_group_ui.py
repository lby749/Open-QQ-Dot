import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import main
from store import FriendStore
from PySide6.QtWidgets import QApplication
import house_qt.common as common
import house_qt.pages_friend as pages_friend
import house_qt.pages_more as pages_more
import house_qt.qq_connection as pages_connection
import house_qt.dotlink as pages_dotlink
import house_qt.window as window_module
import house_status


class AgentGroupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        common.init_ui_thread()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        self.directory.mkdir(parents=True)
        main.dump_json(self.directory / 'friend.json', main.default_profile('12345', 'testfriend', True))
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': {
            'qq_id': '12345', 'display_name': 'testfriend', 'directory': str(self.directory), 'self_id': '77777'}}})
        FriendStore(self.directory).close()
        for module in (house_status, window_module, pages_friend, pages_more, pages_connection, pages_dotlink):
            patch.object(module, 'ROOT', self.root).start()
        patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root)}).start()
        patch('agent_service.ROOT', self.root).start()
        patch('session_manager.ROOT', self.root).start()
        patch('qq_link.QQClient', side_effect=RuntimeError('UI tests do not connect to QQ')).start()
        patch('house_qt.first_run.should_show', return_value=False).start()
        patch.object(house_status, 'REGISTRY', self.root / 'friends.json').start()
        patch.object(window_module, 'REGISTRY', self.root / 'friends.json').start()
        patch.object(window_module, 'invoke_bridge', return_value=(0, 'running PID 1')).start()
        self.addCleanup(patch.stopall)
        self.window = window_module.HouseWindow()
        self.window.status_timer.stop()
        self.addCleanup(self.close)
        self.settle()

    def settle(self, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def close(self):
        self.window.exiting = True
        self.window.tray.hide()
        self.window.close()
        for pool in (common.POOL, common.LONG_POOL, self.window.bridge_pool):
            pool.submit(lambda: None).result()
        self.app.processEvents()
        self.temp.cleanup()

    def test_group_chat_and_network_are_sibling_tabs(self):
        collab = self.window.workspaces['collab']
        labels = [collab.tabs.tabText(i) for i in range(collab.tabs.count())]
        self.assertEqual(labels, ['智能体与工具', '协作网络', '协作流程', '群聊协作'])
        pending = self.window.workspaces['development'].tabs
        self.assertEqual(pending.tabText(pending.count() - 1), '账号与路由')
        group = self.window.pages['collabPage']
        self.assertEqual(group.objectName(), 'agentGroupPage')
        group.refresh()
        self.assertIn('群', group.room_title.text())

    def test_group_room_settings_persist(self):
        group = self.window.pages['collabPage']
        group._owner = lambda: '12345'
        group._base = lambda: self.root
        import agent_group
        room = agent_group.create_room(self.root, '12345', '真实群')
        group.room_id = room['id']
        group.open_room(room['id'])
        group.mode.setCurrentIndex(group.mode.findData('parallel'))
        group.summary.setChecked(True)
        group.save_settings()
        saved = agent_group.load_room(self.root, '12345', room['id'])
        self.assertEqual(saved['mode'], 'parallel')
        self.assertTrue(saved['summary'])

    def test_sequential_host_dispatch_and_summary_are_persisted(self):
        from unittest.mock import patch
        import agent_group
        group = self.window.pages['collabPage']
        group._owner = lambda: '12345'
        group._base = lambda: self.root
        room = agent_group.create_room(self.root, '12345', '主持群')
        host = agent_group.add_member(self.root, '12345', room['id'], 'codex', '主持', '分发', '')
        worker = agent_group.add_member(self.root, '12345', room['id'], 'codex', '执行', '执行', '')
        agent_group.update_room(self.root, '12345', room['id'], host_id=host['id'], mode='sequential', summary=True)
        group.room_id = room['id']
        group.open_room(room['id'])

        def fake(prompt, *args, **kwargs):
            if '候选成员：' in prompt:
                return json.dumps({'summary': '执行去完成', 'assignments': [
                    {'member_id': worker['id'], 'task': '核对执行结果', 'model': 'gpt-6-luna', 'effort': 'low'}]}), None
            if '请汇总' in prompt:
                return '[汇总] 已完成', None
            return '成员完成', None

        with patch('main.codex_text', side_effect=fake):
            group.input.setText('真实任务')
            group.send()
            end = time.monotonic() + 8
            while group.busy and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.02)
        self.assertFalse(group.busy)
        saved = agent_group.load_room(self.root, '12345', room['id'])
        contents = [m['content'] for m in saved['messages']]
        self.assertTrue(any('【分发】执行去完成' in item and 'gpt-6-luna' in item for item in contents))
        self.assertIn('成员完成', contents)
        self.assertIn('【汇总】[汇总] 已完成', contents)


    def test_commands_queue_while_members_are_replying(self):
        from unittest.mock import patch
        import time
        import agent_group
        group = self.window.pages['collabPage']
        group._owner = lambda: '12345'
        group._base = lambda: self.root
        room = agent_group.create_room(self.root, '12345', '排队群')
        agent_group.add_member(self.root, '12345', room['id'], 'codex', '执行', '执行', '')
        agent_group.update_room(self.root, '12345', room['id'], mode='sequential', summary=False)
        group.room_id = room['id']
        group.open_room(room['id'])

        def slow(prompt, *args, **kwargs):
            time.sleep(0.25)
            return '完成', None

        with patch('main.codex_text', side_effect=slow):
            group.input.setText('第一条')
            group.send()
            group.input.setText('第二条')
            group.send()
            self.assertTrue(group.busy)
            self.assertEqual(len(group.pending), 1)
            end = time.monotonic() + 10
            while (group.busy or group.pending) and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.02)
        saved = agent_group.load_room(self.root, '12345', room['id'])
        users = [m['content'] for m in saved['messages'] if m['role'] == 'user']
        self.assertEqual(users, ['第一条', '第二条'])
