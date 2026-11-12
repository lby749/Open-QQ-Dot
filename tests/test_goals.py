import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import house_web
from task_manager import TaskManager, TaskStatus
from task_runtime import current_execution
from notification_manager import NotificationManager


class Goals(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = TaskManager(self.root/'tasks.db', self.root)
        self.notifications = NotificationManager()
        self.npatch = patch.object(NotificationManager, 'get_instance', return_value=self.notifications)
        self.npatch.start()
        self.addCleanup(self.npatch.stop)

    def goal(self, **kwargs):
        values = dict(description='维护本地项目', scope='仅检查并修改测试目录',
                      completion_criteria='两个文件生成并通过验收', owner_id='system:local',
                      milestones=[{'title': 'stage', 'description': '创建一号文件', 'category': 'file'},
                                  {'title': 'stage', 'description': '创建二号文件', 'category': 'file'}])
        values.update(kwargs)
        return self.manager.create_goal(**values)

    def executor(self, task, progress):
        filename = task.task_id+'.txt'
        (self.root/filename).write_text('verified content', 'utf-8')
        return {'output': 'done', 'target_files': [filename], 'artifacts': [filename]}

    def run_next(self):
        task = self.manager._fetch_next_executable_task()
        self.assertIsNotNone(task)
        self.manager._process_single_task(task)
        return self.manager.get_task(task.task_id)

    def test_durable_stages_execute_verify_progress_review_notify(self):
        gid = self.goal()
        self.manager.register_custom_executor('file', self.executor)
        self.assertEqual(self.manager.get_goal(gid)['progress'], 0)
        first = self.run_next()
        self.assertEqual(first.status, TaskStatus.COMPLETED)
        self.assertEqual(self.manager.get_goal(gid)['progress'], .5)
        # Restart the real manager against the same temporary DB; never replay stage one.
        self.manager = TaskManager(self.root/'tasks.db', self.root)
        self.manager.register_custom_executor('file', self.executor)
        second = self.run_next()
        self.assertNotEqual(first.task_id, second.task_id)
        goal = self.manager.get_goal(gid)
        self.assertEqual(goal['status'], 'WAITING_REVIEW')
        self.assertEqual(goal['progress'], 1)
        self.assertEqual(len(goal['results']), 2)
        with self.assertRaises(ValueError):
            self.manager.goal_action(gid, 'system:local', 'complete')
        self.assertTrue(self.manager.goal_action(gid, 'system:local', 'complete', '已检查两个文件'))
        self.assertEqual(self.manager.get_goal(gid)['status'], 'COMPLETED')
        notices = [m for m in self.notifications.list_recent(recipient_id='system:local') if m['category']=='goal']
        self.assertEqual(len(notices), 2)
        self.manager._refresh_task_goal(second.task_id)
        self.assertEqual(len(self.manager.get_goal(gid)['events']), 3)
        self.assertIsNone(self.manager._fetch_next_executable_task())

    def test_pause_resume_and_task_resume_cannot_bypass_goal(self):
        gid = self.goal()
        tasks = self.manager.get_goal(gid)['tasks']
        self.manager.goal_action(gid, 'system:local', 'pause')
        # Individual resume is still insufficient to authorize a paused goal.
        self.manager.resume_task(tasks[0]['task_id'], owner_id=self.manager.get_task(tasks[0]['task_id']).owner_id)
        self.assertIsNone(self.manager._fetch_next_executable_task())
        self.manager._process_single_task(self.manager.get_task(tasks[0]['task_id']))
        self.assertEqual(self.manager.get_task(tasks[0]['task_id']).status, TaskStatus.PENDING)
        self.manager.goal_action(gid, 'system:local', 'resume')
        self.assertIsNotNone(self.manager._fetch_next_executable_task())
        self.manager.goal_action(gid, 'system:local', 'cancel')
        self.assertTrue(all(t['status']=='CANCELLED' for t in self.manager.get_goal(gid)['tasks']))
        self.assertFalse(self.manager.goal_action(gid, 'system:local', 'resume'))

    def test_explicit_task_pause_is_not_resumed_by_goal(self):
        gid = self.goal()
        tid = self.manager.get_goal(gid)['tasks'][0]['task_id']
        self.manager.pause_task(tid, owner_id=self.manager.get_task(tid).owner_id)
        self.manager.goal_action(gid, 'system:local', 'pause')
        self.manager.goal_action(gid, 'system:local', 'resume')
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.PAUSED)

    def test_invalid_plan_atomic_and_budget_allocation(self):
        with self.assertRaises(ValueError):
            self.goal(milestones=[{'title': 'bad'}])
        with self.assertRaises(ValueError):
            self.goal(max_calls=1)
        self.assertEqual(self.manager.list_goals('system:local'), [])
        gid = self.goal(max_calls=5, token_budget=101)
        tasks = self.manager.get_goal(gid)['tasks']
        self.assertEqual(sum(t['max_calls'] for t in tasks), 5)
        from task_manager import MIN_STAGE_TOKENS
        self.assertTrue(all(t['token_budget'] >= MIN_STAGE_TOKENS for t in tasks))
        self.assertEqual(self.manager.get_goal(gid)['token_budget'], 101)

    def test_a_stage_may_use_what_the_goal_has_left(self):
        # Claude Haiku 2026-10-26: one call ~25k tokens, a 100k goal planned in 8 stages gave each 12.5k and paused
        # inside stage 1. A stage now gets the goal's remainder; the goal as a whole stays capped.
        gid = self.goal(token_budget=100, milestones=[
            {'title': 'stage', 'description': f'创建 {n} 号文件', 'category': 'file'} for n in range(4)])
        seen = []

        def execute(task, progress):
            seen.append(self.manager.get_task(task.task_id).token_budget)
            current_execution.get().after_call('mock', {'total_tokens': 40})
            return self.executor(task, progress)

        self.manager.register_custom_executor('file', execute)
        self.assertEqual(self.run_next().status, TaskStatus.COMPLETED)
        self.assertEqual(self.run_next().status, TaskStatus.COMPLETED)
        third = self.run_next()
        self.assertEqual(seen, [100, 60, 20])
        self.assertEqual(third.status, TaskStatus.WAITING_RESOURCE)
        goal = self.manager.get_goal(gid)
        self.assertEqual(goal['status'], 'PAUSED')
        self.assertIsNone(self.manager._fetch_next_executable_task())

    def test_web_owner_isolation_and_registry(self):
        with patch.object(TaskManager, 'get_instance', return_value=self.manager):
            gid = self.goal(owner_id='10001')
            self.assertEqual(house_web.agent_goals('10002', {}), {'goals': []})
            with self.assertRaises(PermissionError):
                house_web.post_agent_goal_action('10002', {'action': 'pause', 'goal_id': gid})
            self.assertEqual(len(house_web.GET['agent_goals']('10001', {})['goals']), 1)
            self.assertTrue(house_web.POST['agent_goal_action']('10001', {'action':'pause','goal_id':gid})['changed'])

    def test_model_call_budget_stops_next_stage_without_paid_calls(self):
        gid = self.goal(max_calls=2)
        def execute(task, progress):
            execution = current_execution.get()
            execution.before_call('short')
            execution.after_call('mock', {'total_tokens': 3})
            execution.before_call('second allowed shared call')
            execution.after_call('mock', {'total_tokens': 3})
            execution.before_call('must not be sent')
            self.fail('budget must stop this')
        self.manager.register_custom_executor('file', execute)
        self.assertEqual(self.run_next().status, TaskStatus.WAITING_RESOURCE)
        goal = self.manager.get_goal(gid)
        self.assertEqual(goal['call_count'], 2)
        self.assertEqual(goal['progress'], 0)
        self.assertIsNone(self.manager._fetch_next_executable_task())

    def test_generated_plan_requires_owner_approval_and_creates_once(self):
        (self.root/'friend.json').write_text(json.dumps({'model_api':{'provider':'custom'},
            'computer_tools':{'project_path':str(self.root)}}), 'utf-8')
        tid = self.manager.propose_goal('维护项目', '仅测试目录', '测试通过', 'system:local', max_calls=4)
        output = json.dumps({'milestones':[{'title':'检查','description':'查看问题','category':'general'}]})
        with patch('multi_agent_orchestrator.call_single_agent', return_value=(output,None)) as model:
            task = self.run_next()
            self.assertEqual(task.task_id, tid)
            self.assertTrue(model.call_args.kwargs['ephemeral'])
            self.assertTrue(model.call_args.kwargs['text_only'])
        self.assertEqual(task.status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(self.manager.list_goals('system:local'), [])
        self.assertFalse(self.manager.approve_task(tid, owner_id=self.manager.get_task(tid).owner_id))
        with self.assertRaises(PermissionError):
            self.manager.accept_goal_plan(tid, '10002')
        gid = self.manager.accept_goal_plan(tid, 'system:local')
        self.assertEqual(self.manager.accept_goal_plan(tid, 'system:local'), gid)
        goal = self.manager.get_goal(gid)
        self.assertEqual(goal['scope'], '仅测试目录')
        self.assertEqual(goal['max_calls'], 4)
        self.assertEqual(len(goal['tasks']), 1)
        self.assertEqual(self.manager.get_task(tid).status, TaskStatus.COMPLETED)
        self.assertEqual(len(self.manager.list_goals('system:local')), 1)

    def test_bad_generated_plan_cannot_create_goal_or_expand_authority(self):
        (self.root/'friend.json').write_text(json.dumps({'model_api':{'provider':'custom'}}), 'utf-8')
        tid = self.manager.propose_goal('goal', 'scope', 'criteria', 'system:local')
        with patch('multi_agent_orchestrator.call_single_agent', return_value=('not JSON',None)):
            task = self.run_next()
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(self.manager.list_goals('system:local'), [])
        with self.assertRaises(ValueError):
            self.manager.accept_goal_plan(tid, 'system:local')

    def test_actual_default_executor_and_restart_checkpoint(self):
        (self.root/'friend.json').write_text(json.dumps({'model_api': {'provider': 'codex'}, 'computer_tools': {'project_path': str(self.root)}}), 'utf-8')
        gid = self.goal()
        calls = []
        def mock_model(**kwargs):
            calls.append(kwargs['prompt'])
            (self.root/f'result{len(calls)}.txt').write_text('mock model artifact', 'utf-8')
            return 'produced artifact', None
        with patch('multi_agent_orchestrator.orchestrate', side_effect=mock_model):
            self.run_next()
            self.manager = TaskManager(self.root/'tasks.db', self.root)
            self.run_next()
        self.assertEqual(len(calls), 2)
        self.assertIn('不得自行扩大目标范围', calls[0])
        self.assertEqual(self.manager.get_goal(gid)['status'], 'WAITING_REVIEW')

    def test_manual_stage_approval_advances_progress_only_after_confirmation(self):
        gid = self.goal(milestones=[{'title':'写作', 'description':'写报告'}])
        self.manager.register_custom_executor('general', lambda task, progress: {'output':'subjective draft'})
        task = self.run_next()
        self.assertEqual(task.status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(self.manager.get_goal(gid)['progress'], 0)
        self.assertTrue(self.manager.approve_task(task.task_id, owner_id=self.manager.get_task(task.task_id).owner_id))
        self.assertEqual(self.manager.get_goal(gid)['status'], 'WAITING_REVIEW')

    def test_deadline_and_direct_out_of_order_execution_blocked(self):
        import time
        gid = self.goal()
        second = self.manager.get_goal(gid)['tasks'][1]
        self.manager._process_single_task(self.manager.get_task(second['task_id']))
        self.assertEqual(self.manager.get_task(second['task_id']).status, TaskStatus.PENDING)
        self.manager.goal_action(gid, 'system:local', 'pause')
        expired = self.goal(deadline=time.time()-1)
        self.assertIsNone(self.manager._fetch_next_executable_task())
        self.assertEqual(self.manager.get_goal(expired)['status'], 'PAUSED')
        with self.assertRaises(ValueError):
            self.manager.goal_action(expired, 'system:local', 'resume')

    def test_cancellation_during_model_output_never_advances_goal(self):
        gid = self.goal()
        def cancelled(task, progress):
            self.manager.goal_action(gid, 'system:local', 'cancel')
            return self.executor(task, progress)
        self.manager.register_custom_executor('file', cancelled)
        self.run_next()
        self.assertEqual(self.manager.get_goal(gid)['status'], 'CANCELLED')
        self.assertEqual(self.manager.get_goal(gid)['progress'], 0)

    def test_recovery_reconciles_completed_stages_without_reexecution(self):
        gid = self.goal()
        with self.manager._get_connection() as db:
            db.execute("UPDATE tasks SET status='COMPLETED',verification_info=?", (json.dumps({'success':True}),))
        self.manager = TaskManager(self.root/'tasks.db', self.root)
        self.assertEqual(self.manager.get_goal(gid)['status'], 'WAITING_REVIEW')
        self.assertIsNone(self.manager._fetch_next_executable_task())

    def test_background_worker_advances_all_stages_without_manual_ticks(self):
        import threading
        gid = self.goal()
        finished = threading.Event()
        def sink(message):
            if message.category == 'goal' and message.metadata.get('goal_id') == gid:
                finished.set()
        self.notifications.register_channel_sink(sink)
        self.manager.register_custom_executor('file', self.executor)
        with patch('agenda.ProactiveActionEngine.get_instance'):
            self.manager.start_worker()
            try:
                self.assertTrue(finished.wait(5), 'worker did not complete both stages')
                self.assertEqual(self.manager.get_goal(gid)['status'], 'WAITING_REVIEW')
            finally:
                self.manager.stop_worker()

    def test_goal_default_executor_obeys_actual_command_permission(self):
        import topic_library
        from tool_registry import ToolRegistry
        (self.root/'friend.json').write_text(json.dumps({'model_api':{'provider':'custom'},
            'computer_tools':{'enabled':True, 'commands':False,'project_path':str(self.root)}}), 'utf-8')
        gid = self.goal(milestones=[{'title':'生成文件','description':'写文件但不能执行命令','category':'file'}])
        def mock_model(**kwargs):
            with self.assertRaises(ValueError):
                topic_library.call_tool(self.root, 'desk_run_command', {'command':'must not execute'})
            topic_library.call_tool(self.root, 'desk_write_file', {'path':'allowed.txt','text':'content'})
            return 'written', None
        with patch.object(ToolRegistry, 'get_instance', return_value=ToolRegistry()), patch(
                'multi_agent_orchestrator.orchestrate', side_effect=mock_model):
            self.run_next()
        self.assertTrue((self.root/'allowed.txt').is_file())
        self.assertEqual(self.manager.get_goal(gid)['status'], 'WAITING_REVIEW')

    def test_cancel_stops_children_before_synchronous_notification_sink(self):
        gid = self.goal()
        observed = []
        def sink(message):
            if message.category == 'goal':
                observed.append([t['status'] for t in self.manager.get_goal(gid)['tasks']])
        self.notifications.register_channel_sink(sink)
        self.manager.goal_action(gid, 'system:local', 'cancel')
        self.assertEqual(observed, [['CANCELLED', 'CANCELLED']])

    def test_unverified_completion_does_not_finish_goal(self):
        gid = self.goal()
        for task in self.manager.get_goal(gid)['tasks']:
            self.manager.update_task_status(task['task_id'], TaskStatus.COMPLETED)
        self.assertEqual(self.manager.get_goal(gid)['progress'], 0)
        with self.assertRaises(ValueError):
            self.manager.goal_action(gid, 'system:local', 'complete', 'model claims')


if __name__ == '__main__':
    unittest.main()
