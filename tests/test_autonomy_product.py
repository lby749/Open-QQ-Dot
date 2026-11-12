import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from notification_manager import NotificationManager
from task_manager import TaskManager, TaskStatus


class AutonomousProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'friend.json').write_text(json.dumps({'computer_tools': {'project_path': str(self.root)}}))
        self.manager = TaskManager(self.root / 'tasks.db', self.root)
        self.notices = patch.object(NotificationManager, 'get_instance', return_value=NotificationManager(self.root / 'notices.db'))
        self.notices.start()

    def tearDown(self):
        self.manager.stop_worker()
        self.notices.stop()
        self.temp.cleanup()

    def test_planner_gets_scope_schema_and_normalizes_fenced_json(self):
        tid = self.manager.propose_goal('编写报告', '仅当前工作目录', '产物 report.txt 存在', 'system:local')
        plan = {'milestones': [{'title': '生成报告', 'description': '写入 report.txt', 'category': 'file'}]}
        with patch('multi_agent_orchestrator.call_single_agent', return_value=('```json\n' + json.dumps(plan) + '\n```', None)) as model:
            self.manager._process_single_task(self.manager.get_task(tid))
        self.assertEqual(model.call_args.args[0], 'codex')
        for required in ('仅当前工作目录', 'report.txt', 'milestones', 'category'):
            self.assertIn(required, model.call_args.args[1])
        task = self.manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(json.loads(task.result), plan)

    def test_explicit_autostart_creates_goal_once_without_approving_tool_permissions(self):
        tid = self.manager.propose_goal('编写报告', '仅当前目录', 'report.txt 存在', 'system:local', auto_start=True)
        plan = {'milestones': [{'title': '报告', 'description': '写入 report.txt', 'category': 'file'}]}
        with patch('multi_agent_orchestrator.call_single_agent', return_value=(json.dumps(plan), None)):
            self.manager._process_single_task(self.manager.get_task(tid))
        goals = self.manager.list_goals('system:local')
        self.assertEqual(len(goals), 1)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.COMPLETED)
        self.assertEqual(self.manager.accept_goal_plan(tid, 'system:local'), goals[0]['goal_id'])
        self.assertNotIn('commands', json.loads((self.root / 'friend.json').read_text())['computer_tools'])

    def test_browser_goal_stage_retains_explicit_url(self):
        gid = self.manager.create_goal('阅读网页', '只读浏览', '保存截图', [{
            'title': '阅读', 'description': '打开 https://example.com 并截图', 'category': 'browser'}], 'system:local')
        task = self.manager.get_goal(gid)['tasks'][0]
        self.assertEqual(task['checkpoint']['url'], 'https://example.com')
        import task_spec
        spec = task_spec.build_task_spec('打开 https://example.com；期望文本：Example Domain。验收：有效 PNG。', 'browser')
        self.assertEqual(spec['expected_text'], 'Example Domain')

    def test_invalid_model_stage_never_autostarts(self):
        tid = self.manager.propose_goal('任务', '只读', '有结果', 'system:local', auto_start=True)
        with patch('multi_agent_orchestrator.call_single_agent', return_value=(json.dumps({'milestones': []}), None)):
            self.manager._process_single_task(self.manager.get_task(tid))
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.FAILED)
        self.assertEqual(self.manager.list_goals('system:local'), [])

    def test_qq_goal_requires_explicit_scope_and_acceptance(self):
        import task_spec
        request = task_spec.parse_autonomous_goal('自主目标：读取网页；范围：只读；验收：截图有效')
        self.assertEqual(request, {'description': '读取网页', 'scope': '只读', 'completion_criteria': '截图有效'})
        self.assertIsNone(task_spec.parse_autonomous_goal('今天聊聊规划'))
        with self.assertRaises(ValueError):
            task_spec.parse_autonomous_goal('自主目标：随便做点事')
