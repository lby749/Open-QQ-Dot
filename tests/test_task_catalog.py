import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_catalog
import task_undo
from task_manager import TaskManager, TaskStatus

OWNER = '12345'


class TaskCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'data').mkdir()
        self.friend = self.root / 'friends' / OWNER
        self.friend.mkdir(parents=True)
        self.write_profile({})
        self.manager = TaskManager(db_path=self.root / 'data' / 'tasks.sqlite3', workspace=self.root)
        for target, value in ((TaskManager, 'get_instance'), (TaskManager, 'start_worker')):
            patcher = patch.object(target, value, return_value=self.manager if value == 'get_instance' else None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.manager.stop_worker()
        shutil.rmtree(self.root, ignore_errors=True)

    def write_profile(self, extra):
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': OWNER, **extra}), encoding='utf-8')

    def items(self):
        return {item['key']: item for item in task_catalog.catalog(self.root, OWNER, self.friend)}

    def create(self, key, values, **options):
        return task_catalog.create(self.root, OWNER, self.friend, key, values, **options)

    def test_catalog_covers_every_executor_and_checks_real_requirements(self):
        categories = {t['category'] for t in task_catalog.TEMPLATES.values() if t['kind'] == 'task'}
        self.assertTrue({'code', 'file', 'data', 'research', 'browser', 'video', 'general'} <= categories)
        self.assertTrue(any(t['kind'] == 'goal' for t in task_catalog.TEMPLATES.values()))
        items = self.items()
        self.assertFalse(items['dev_project']['available'])
        self.assertIn('电脑工作台', items['dev_project']['reason'])
        self.assertTrue(items['research_problem']['available'])
        self.assertFalse(items['ecommerce']['available'])  # sandbox data only: never offered
        self.write_profile({'computer_tools': {'enabled': True, 'commands': True},
                            'model_api': {'multi_agent': {'enabled': True}}})
        with patch.object(task_catalog.shutil, 'which', return_value='git'):
            items = self.items()
        self.assertTrue(items['dev_project']['available'] and items['git_maintenance']['available'])
        self.assertTrue(items['multi_agent']['available'])
        self.write_profile({'computer_tools': {'enabled': True, 'commands': True, 'mode': 'read_only'}})
        self.assertFalse(self.items()['code_change']['available'])
        with self.assertRaisesRegex(PermissionError, '还不能创建'):
            self.create('code_change', {'description': 'fix'})
        with self.assertRaisesRegex(ValueError, '模拟'):
            self.create('ecommerce', {'description': 'phone'})

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is not installed')
    def test_task_template_fills_the_executor_inputs(self):
        self.write_profile({'computer_tools': {'enabled': True, 'commands': True}})
        result = self.create('code_change', {'description': '修复登录', 'target_files': 'app/login.py, ../secret, C:/x',
                                             'test_command': 'pytest -q'}, notify_qq=True, max_calls=7)
        task = self.manager.get_task(result['task_id'])
        self.assertEqual((task.category, task.owner_id), ('code', OWNER))
        self.assertEqual(task.checkpoint['target_files'], ['app/login.py'])  # unsafe paths dropped
        self.assertEqual((task.checkpoint['test_command'], task.checkpoint['template'], task.checkpoint['notify_qq']),
                         ('pytest -q', 'code_change', True))
        self.assertNotIn('workspace_id', task.checkpoint)
        review = self.manager.get_task(self.create('code_review', {'description': 'src'})['task_id'])
        self.assertEqual(review.checkpoint['target_files'], ['CODE_REVIEW.md'])
        self.assertIn('不修改任何源码', review.description)
        with self.assertRaisesRegex(ValueError, '网页地址'):
            self.create('web_task', {'description': 'look', 'url': 'ftp://x'})
        web = self.manager.get_task(self.create('web_task', {'description': 'look', 'url': 'https://example.com',
                                                             'expected_text': 'Example'})['task_id'])
        self.assertEqual((web.checkpoint['url'], web.checkpoint['browser_autoplan'], web.max_retries),
                         ('https://example.com', True, 0))
        video = self.manager.get_task(self.create('video', {'description': '介绍', 'duration_sec': 12})['task_id'])
        self.assertEqual(video.checkpoint['duration_sec'], 12)
        research = self.manager.get_task(self.create('literature_review', {'description': '图神经网络'})['task_id'])
        self.assertTrue(research.checkpoint['research_autoplan'])
        with self.assertRaisesRegex(ValueError, '要做什么'):
            self.create('general', {'description': ' '})

    def test_goal_template_carries_its_settings_to_every_stage(self):
        self.write_profile({'computer_tools': {'enabled': True, 'commands': True}})
        spaces = [{'id': 'default'}, {'id': 'ws_proj1', 'path': str(self.root)}]
        with patch('session_manager.list_workspaces', return_value=spaces), \
                patch.object(task_catalog.shutil, 'which', return_value='git'):
            result = self.create('git_maintenance', {'workspace': 'ws_proj1', 'test_command': 'pytest -q'},
                                 notify_qq=True, auto_start=False)
            with self.assertRaisesRegex(ValueError, '已失效'):
                self.create('dev_project', {'description': 'x', 'workspace': 'ws_gone'})
        plan = self.manager.get_task(result['task_id'])
        self.assertEqual(plan.category, 'goal_plan')
        request = plan.checkpoint['goal_request']
        self.assertIn('不 push', request['scope'])  # template default scope
        self.assertEqual(request['stage_defaults'], {'notify_qq': True, 'workspace_id': 'ws_proj1',
                                                     'input_source': 'workbench', 'template': 'git_maintenance',
                                                     'test_command': 'pytest -q'})
        milestones = [{'title': '检查状态', 'description': '运行 git status', 'category': 'general'},
                      {'title': '跑测试', 'description': '运行测试；测试: tests/test_calc.py', 'category': 'code'}]
        self.manager.update_task_status(plan.task_id, TaskStatus.WAITING_APPROVAL,
                                        result=json.dumps({'milestones': milestones}))
        goal = self.manager.get_goal(self.manager.accept_goal_plan(plan.task_id, OWNER))
        for stage in goal['tasks']:
            checkpoint = self.manager.get_task(stage['task_id']).checkpoint
            self.assertEqual((checkpoint['workspace_id'], checkpoint['input_source'], checkpoint['notify_qq']),
                             ('ws_proj1', 'workbench', True))
            # Live run: the planner wrote a test FILE as the command; the owner's command wins on code stages only
            self.assertEqual(checkpoint.get('test_command'), 'pytest -q' if stage['category'] == 'code' else None)

    def test_stage_defaults_cannot_widen_permissions(self):
        from task_manager import clean_stage_defaults
        self.assertEqual(clean_stage_defaults({'notify_qq': 'yes', 'backend': 'codex', 'sandbox_mode': 'off',
                                               'template': 't'}), {'notify_qq': False, 'template': 't'})
        with self.assertRaises(ValueError):
            clean_stage_defaults({'workspace_id': '../etc'})

    def test_custom_templates_are_saved_per_friend(self):
        key = task_catalog.save_custom(self.root, OWNER, '每周论文', 'literature_review', {'description': '大模型推理'})
        item = self.items()[key]
        self.assertEqual((item['title'], item['group'], item['defaults']['description']),
                         ('每周论文', '我的模板', '大模型推理'))
        task = self.manager.get_task(self.create(key, {})['task_id'])
        self.assertIn('大模型推理', task.description)
        with self.assertRaises(ValueError):
            task_catalog.save_custom(self.root, OWNER, 'x', 'ecommerce', {})
        task_catalog.delete_custom(self.root, OWNER, key)
        self.assertNotIn(key, self.items())

    def test_undo_stops_running_work_then_reverses_its_files(self):
        tid = self.create('general', {'description': '写报告'})['task_id']
        preview = task_catalog.undo_preview(self.root, OWNER, 'task', tid)
        self.assertEqual(preview['action'], 'stop')
        task_catalog.undo(self.root, OWNER, 'task', tid)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.CANCELLED)
        work = self.root / 'work'
        work.mkdir()
        task_undo.begin(self.root, tid, work)
        (work / 'report.md').write_text('x')
        task_undo.finish(self.root, tid, prune=True)
        preview = task_catalog.undo_preview(self.root, OWNER, 'task', tid)
        self.assertEqual((preview['action'], preview['changes']['created']), ('undo', ['report.md']))
        self.assertIn('已撤销', task_catalog.undo(self.root, OWNER, 'task', tid)['message'])
        self.assertFalse((work / 'report.md').exists())
        self.assertEqual(task_catalog.undo_preview(self.root, OWNER, 'task', tid)['action'], 'none')
        with self.assertRaises(PermissionError):
            task_catalog.undo_preview(self.root, '99999', 'task', tid)

    def test_result_awaiting_review_is_stopped_and_reverted_in_one_step(self):
        tid = self.create('general', {'description': '写报告'})['task_id']
        work = self.root / 'work'
        work.mkdir()
        task_undo.begin(self.root, tid, work)
        (work / 'report.md').write_text('x')
        task_undo.finish(self.root, tid, prune=False)
        self.manager.update_task_status(tid, TaskStatus.WAITING_APPROVAL, result='done')
        preview = task_catalog.undo_preview(self.root, OWNER, 'task', tid)
        self.assertEqual((preview['action'], preview['stop_first']), ('undo', True))
        self.assertTrue(preview['text'].startswith('终止任务并'))
        task_catalog.undo(self.root, OWNER, 'task', tid)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.CANCELLED)
        self.assertFalse((work / 'report.md').exists())
        running = self.create('general', {'description': '另一个'})['task_id']
        task_undo.begin(self.root, running, work)
        (work / 'other.md').write_text('x')
        task_undo.finish(self.root, running)
        self.manager.update_task_status(running, TaskStatus.RUNNING)
        self.assertEqual(task_catalog.undo_preview(self.root, OWNER, 'task', running)['action'], 'stop')

    def test_goal_undo_reverses_stages_newest_first(self):
        gid = self.manager.create_goal('写两章', '仅工作目录', '两章完成',
                                       [{'title': '一', 'description': 'a'}, {'title': '二', 'description': 'b'}], OWNER)
        self.assertEqual(task_catalog.undo_preview(self.root, OWNER, 'goal', gid)['action'], 'stop')
        task_catalog.undo(self.root, OWNER, 'goal', gid)
        stages = [task['task_id'] for task in self.manager.get_goal(gid)['tasks']]
        work = self.root / 'book'
        work.mkdir()
        (work / 'ch.md').write_text('draft 0')
        for index, stage in enumerate(stages):
            self.manager.update_task_status(stage, TaskStatus.COMPLETED, result='ok')
            task_undo.begin(self.root, stage, work)
            (work / 'ch.md').write_text(f'draft {index + 1}')
            task_undo.finish(self.root, stage, prune=True)
        self.assertEqual(task_catalog.undo_preview(self.root, OWNER, 'goal', gid)['action'], 'undo')
        task_catalog.undo(self.root, OWNER, 'goal', gid)
        self.assertEqual((work / 'ch.md').read_text(), 'draft 0')


if __name__ == '__main__':
    unittest.main()
