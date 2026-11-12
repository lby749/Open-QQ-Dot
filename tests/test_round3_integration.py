import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import house_web
from task_manager import TaskManager, TaskStatus
from research_tracker import ResearchTracker
from task_runtime import current_execution
from tool_registry import ToolRegistry


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.manager=TaskManager(self.root/'tasks.db',self.root)
        self.addCleanup(self.temp.cleanup)

    def test_web_blank_owner_and_other_owner_cannot_be_read_or_changed(self):
        for owner in ('','10002','default','system:legacy'):
            tid=self.manager.create_task('private','task',owner_id=owner)
            with patch.object(TaskManager,'get_instance',return_value=self.manager), patch.object(house_web,'is_admin_user',return_value=False):
                with self.assertRaises(PermissionError):
                    house_web.agent_task_detail('10001',{'task_id':[tid]})
                with self.assertRaises(PermissionError):
                    house_web.post_agent_task_action('10001',{'action':'cancel','task_id':tid})
                self.assertEqual(house_web.agent_tasks('10001',{}),{'tasks':[]})

    def test_web_own_and_admin_access(self):
        tid=self.manager.create_task('private','task',owner_id='10001')
        with patch.object(TaskManager,'get_instance',return_value=self.manager),patch.object(house_web,'is_admin_user',return_value=False):
            self.assertEqual(house_web.agent_task_detail('10001',{'task_id':[tid]})['task']['owner_id'],'10001')
        with patch.object(TaskManager,'get_instance',return_value=self.manager),patch.object(house_web,'is_admin_user',return_value=True):
            # A friend-scoped request stays scoped even if that account is an administrator.
            with self.assertRaises(PermissionError):
                house_web.agent_task_detail('99999',{'task_id':[tid]})

    def test_cross_owner_dependency_rejected(self):
        tid=self.manager.create_task('private','task',owner_id='10002')
        with patch.object(TaskManager,'get_instance',return_value=self.manager),patch.object(house_web,'is_admin_user',return_value=False):
            with self.assertRaises(PermissionError):
                house_web.post_agent_task_action('10001',{'action':'create','title':'own','depends_on':[tid]})

    def test_blank_approval_not_public(self):
        from approval_manager import ApprovalManager,ApprovalCategory
        approvals=ApprovalManager(self.root/'approvals.db')
        approvals.check_and_request_approval('synthetic',ApprovalCategory.HIGH_RISK_COMMAND,'synthetic',owner_id='10002',sync_wait=False)
        request=approvals.list_pending_requests()[0]
        with approvals._get_connection() as db:
            db.execute("UPDATE approval_requests SET owner_id='' WHERE req_id=?",(request.req_id,))
        with patch.object(ApprovalManager,'get_instance',return_value=approvals),patch.object(house_web,'is_admin_user',return_value=False):
            with self.assertRaises(PermissionError):
                house_web.post_agent_approval_decision('10001',{'req_id':request.req_id,'decision':'approve'})

    def test_system_notifications_not_broadcast_to_friends(self):
        from notification_manager import NotificationManager
        manager=NotificationManager()
        manager.publish('system','private')
        manager.publish('own','visible',recipient_id='10001')
        self.assertEqual([m['title'] for m in manager.list_recent(recipient_id='10001')],['own'])
        self.assertEqual(manager.list_recent(recipient_id='10002'),[])

    def test_tool_actual_path_uses_registry_and_rechecks_permissions(self):
        import topic_library
        friend=self.root/'10001';friend.mkdir()
        profile=friend/'friend.json'
        profile.write_text(json.dumps({'computer_tools':{'enabled':True,'commands':False,'project_path':str(self.root)}}),'utf-8')
        registry=ToolRegistry()
        with patch.object(ToolRegistry,'get_instance',return_value=registry),patch.object(registry,'execute_tool',wraps=registry.execute_tool) as gate:
            topic_library.call_tool(friend,'desk_write_file',{'path':'written.txt','text':'synthetic'})
            self.assertEqual(gate.call_count,1)
            profile.write_text(json.dumps({'computer_tools':{'enabled':True,'commands':True,'mode':'read_only','project_path':str(self.root)}}),'utf-8')
            with self.assertRaises(ValueError):
                topic_library.call_tool(friend,'desk_run_command',{'command':'must not execute'})
            # Read-only changes now remove the command from discovery before the registry invocation.
            self.assertEqual(gate.call_count,1)
            self.assertNotIn('desk_run_command',{tool['name'] for tool in topic_library.available_tools(friend)})

    def test_unverified_research_is_saved_without_breakthrough(self):
        tracker=ResearchTracker(self.root/'research.db')
        tracker.create_research_project('r','topic','goal','t',initial_milestones=[{'title':'stage','objective':'evidence'}])
        notify,_=tracker.record_stage_progress('r',1,['model claim one','model claim two'],[],stage_achieved=True)
        self.assertFalse(notify)
        self.assertEqual(tracker.get_project('r').global_verified_findings,[])
        self.assertEqual(tracker.list_evidence('r')[0]['status'],'pending')
        self.assertEqual(tracker.get_project('r').milestones[0].status,'WAITING_VERIFICATION')
        with self.assertRaises(ValueError):
            tracker.record_stage_progress('r',1,['claim'],[],verification_status='verified')

    def test_verified_stage_notifies_only_once(self):
        tracker=ResearchTracker(self.root/'research.db')
        tracker.create_research_project('r','topic','goal','t',initial_milestones=[{'title':'stage','objective':'evidence'}])
        kw=dict(stage_achieved=True,verification_status='verified',sources=['synthetic source'],evidence=['test log'],reviewed_by='test-reviewer')
        self.assertTrue(tracker.record_stage_progress('r',1,['checked finding'],[],**kw)[0])
        self.assertFalse(tracker.record_stage_progress('r',1,['checked finding'],[],**kw)[0])

    def test_model_dispatcher_gates_actual_adapter(self):
        import multi_agent_orchestrator,main
        from model_dispatcher import ModelDispatcher
        dispatcher=ModelDispatcher()
        with patch.object(ModelDispatcher,'get_instance',return_value=dispatcher),patch.object(main,'_claude_text',return_value=('synthetic output',None)) as adapter:
            output,_=multi_agent_orchestrator.call_single_agent('claude','prompt',self.root)
        self.assertEqual(output,'synthetic output');adapter.assert_called_once()
        self.assertEqual(dispatcher.get_metrics_report()['claude']['successful_calls'],1)

    def test_actual_usage_replaces_estimate(self):
        tid=self.manager.create_task('budget','test',token_budget=100)
        def executor(task,progress):
            runtime=current_execution.get();runtime.before_call('x'*30)
            runtime.after_call('text',usage={'input_tokens':5,'output_tokens':7})
            return {'output':'draft'}
        self.manager.register_custom_executor('general',executor)
        self.manager._process_single_task(self.manager.get_task(tid))
        task=self.manager.get_task(tid)
        self.assertEqual(task.consumed_tokens,12);self.assertEqual(task.estimated_tokens,0)

    def test_call_count_budget_checked_before_call(self):
        tid=self.manager.create_task('budget','test',max_calls=1);calls=[]
        def executor(task,progress):
            runtime=current_execution.get();runtime.before_call('a');calls.append(1)
            runtime.after_call('a');runtime.before_call('b');calls.append(2)
        self.manager.register_custom_executor('general',executor)
        self.manager._process_single_task(self.manager.get_task(tid))
        self.assertEqual(calls,[1]);self.assertEqual(self.manager.get_task(tid).status,TaskStatus.WAITING_RESOURCE)

    def test_session_and_workspace_ids_cannot_escape_friend(self):
        import session_manager
        with patch.object(session_manager,'ROOT',self.root):
            for value in ('../10002','..\\10002','/absolute','C:/outside'):
                with self.assertRaises(ValueError):
                    session_manager.load_session('10001',value)
                with self.assertRaises(ValueError):
                    session_manager.list_sessions('10001',value)
            session=session_manager.create_session('10001')
            self.assertEqual(session_manager.load_session('10001',session['id'])['messages'],[])
            self.assertFalse((self.root/'friends'/'10002').exists())

    def test_verifier_rejects_artifact_outside_authorized_workspace(self):
        from task_verifier import TaskVerifier
        workspace=self.root/'workspace';workspace.mkdir()
        outside=self.root/'outside.txt';outside.write_text('private')
        result=TaskVerifier.verify('file',{'target_files':['../outside.txt']},{},workspace)
        self.assertFalse(result.success)
        self.assertEqual(result.verifier_type,'artifact_permission')

    def test_reviewed_research_schedules_owned_next_stage_once(self):
        tracker=ResearchTracker(self.root/'research.db')
        tid=self.manager.create_task('research','goal',category='research',owner_id='10001')
        tracker.create_research_project('r','topic','goal',tid,max_stages=2,
            initial_milestones=[{'title':'one','objective':'first'},{'title':'two','objective':'second'}])
        with self.assertRaises(ValueError):
            tracker.schedule_next_stage(self.manager,'r')
        tracker.record_stage_progress('r',1,['checked'],[],stage_achieved=True,
            verification_status='verified',sources=['source'],evidence=['test evidence'],reviewed_by='reviewer')
        follow=tracker.schedule_next_stage(self.manager,'r',delay_seconds=60)
        self.assertEqual(follow,tracker.schedule_next_stage(self.manager,'r',delay_seconds=60))
        task=self.manager.get_task(follow)
        self.assertEqual(task.owner_id,'10001')
        self.assertEqual(task.checkpoint['stage_index'],2)
        self.manager._process_single_task(task)
        self.assertEqual(self.manager.get_task(follow).status,TaskStatus.PENDING)

    def test_research_review_route_rejects_other_friend(self):
        tracker=ResearchTracker(self.root/'research.db')
        tid=self.manager.create_task('research','goal',owner_id='10002')
        tracker.create_research_project('r','topic','goal',tid,initial_milestones=[{'title':'one','objective':'first'}])
        with patch.object(ResearchTracker,'get_instance',return_value=tracker),patch.object(TaskManager,'get_instance',return_value=self.manager),patch.object(house_web,'is_admin_user',return_value=False):
            with self.assertRaises(PermissionError):
                house_web.post_agent_research_action('10001',{'project_id':'r','action':'stop'})

    def test_notification_dedup_is_scoped_by_recipient(self):
        from notification_manager import NotificationManager
        manager=NotificationManager()
        a=manager.publish('one','body',recipient_id='10001',metadata={'dedup_key':'task:done'})
        self.assertEqual(a,manager.publish('one','body',recipient_id='10001',metadata={'dedup_key':'task:done'}))
        b=manager.publish('one','body',recipient_id='10002',metadata={'dedup_key':'task:done'})
        self.assertNotEqual(a,b)

    def test_api_and_antigravity_program_tool_dispatch_cannot_skip_registry(self):
        import api_backend
        friend=self.root/'10001';friend.mkdir()
        (friend/'friend.json').write_text(json.dumps({'computer_tools':{'enabled':True,'commands':False,'project_path':str(self.root)}}),'utf-8')
        registry=ToolRegistry()
        with patch.object(ToolRegistry,'get_instance',return_value=registry),patch.object(registry,'execute_tool',wraps=registry.execute_tool) as gate:
            result=json.loads(api_backend._run_tool(friend,'desk_write_file',{'path':'api-written.txt','text':'synthetic'}))
        self.assertEqual(gate.call_count,1)
        self.assertIn('written',result)
        names=[t['name'] for t in api_backend._tools(friend)]
        self.assertEqual(names.count('desk_write_file'),1)

    def test_orchestrator_resume_reuses_plan_and_completed_subtask(self):
        import multi_agent_orchestrator as orchestration
        from agent_memory import AgentMemoryStore
        tid=self.manager.create_task('resume','synthetic')
        profile={'model_api':{'provider':'codex','active_providers':['codex','claude']}}
        plan={'need_collaboration':True,'subtasks':[
            {'id':'one','agent':'claude','task_type':'analysis','subtask_prompt':'FIRST_STEP','depends_on':[]},
            {'id':'two','agent':'claude','task_type':'analysis','subtask_prompt':'SECOND_STEP','depends_on':['one']}]}
        calls=[];paused=[False]
        def fake(provider,prompt,directory,*args,**kw):
            if 'need_collaboration' in prompt:
                calls.append('planner');return json.dumps(plan),None
            if 'FIRST_STEP' in prompt and 'SECOND_STEP' in prompt:
                return 'synthetic final report '*5,None
            if 'FIRST_STEP' in prompt:
                calls.append('one');return 'synthetic completed analysis '*4,None
            if 'SECOND_STEP' in prompt:
                calls.append('two')
                if not paused[0]:
                    paused[0]=True;self.manager.pause_task(tid, owner_id=self.manager.get_task(tid).owner_id);raise InterruptedError('synthetic pause')
                return 'synthetic completed second analysis '*4,None
            return 'synthetic final report '*5,None
        def executor(task,progress):
            output,_=orchestration.orchestrate('synthetic task',self.root,profile=profile,on_step=progress)
            return {'output':output}
        self.manager.register_custom_executor('general',executor)
        with patch.object(orchestration,'call_single_agent',side_effect=fake),patch.object(orchestration,'check_agent_quota_critical',return_value=(False,'')),patch.object(AgentMemoryStore,'get_instance',side_effect=RuntimeError('test skips optional memory')):
            self.manager._process_single_task(self.manager.get_task(tid))
            self.assertEqual(self.manager.get_task(tid).status,TaskStatus.PAUSED)
            self.assertIn('one',self.manager.get_task(tid).checkpoint['subtask_results'])
            self.assertTrue(self.manager.resume_task(tid, owner_id=self.manager.get_task(tid).owner_id))
            self.manager._process_single_task(self.manager.get_task(tid))
        self.assertEqual(calls.count('planner'),1)
        self.assertEqual(calls.count('one'),1)
        self.assertEqual(calls.count('two'),2)

    def test_model_concurrency_gate_is_enforced(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        from model_dispatcher import ModelDispatcher
        dispatcher=ModelDispatcher();lock=threading.Lock();active=[0];peak=[0]
        def runner():
            with lock:
                active[0]+=1;peak[0]=max(peak[0],active[0])
            time.sleep(.03)
            with lock:
                active[0]-=1
            return 'synthetic',None
        with ThreadPoolExecutor(max_workers=6) as workers:
            futures=[workers.submit(dispatcher.execute_with_budget_and_gate,'claude',runner,1) for _ in range(6)]
            for future in futures:
                self.assertEqual(future.result()[0],'synthetic')
        self.assertLessEqual(peak[0],2)
        self.assertEqual(dispatcher.get_metrics_report()['claude']['total_calls'],6)

    def test_legacy_achieved_research_requires_evidence_before_continue(self):
        tracker=ResearchTracker(self.root/'research.db')
        tid=self.manager.create_task('research','goal',owner_id='10001')
        project=tracker.create_research_project('r','topic','goal',tid,max_stages=2,
            initial_milestones=[{'title':'one','objective':'first'},{'title':'two','objective':'second'}])
        project.current_stage=2;project.milestones[0].status='ACHIEVED';tracker._save_project(project)
        with self.assertRaises(ValueError):
            tracker.schedule_next_stage(self.manager,'r')


if __name__=='__main__':
    unittest.main()
