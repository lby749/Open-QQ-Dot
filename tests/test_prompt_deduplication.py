import json
import threading
from unittest.mock import patch

import pytest

from agent_errors import AgentError,AgentErrorType
import main
import multi_agent_orchestrator as orchestration
from prompt_context import task_session_prompt
from task_manager import TaskManager,TaskStatus
from task_runtime import Execution,current_execution


@pytest.fixture
def execution(tmp_path,monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME',str(tmp_path))
    directory=tmp_path/'friends/12345'; directory.mkdir(parents=True)
    (directory/'friend.json').write_text('{}','utf-8')
    manager=TaskManager(tmp_path/'data/tasks.sqlite3',tmp_path)
    tid=manager.create_task('测试','明确任务',category='file',owner_id='12345',max_duration_seconds=0)
    from model_dispatcher import ModelDispatcher
    monkeypatch.setattr(ModelDispatcher,'_instance',ModelDispatcher())
    manager.update_task_status(tid,TaskStatus.RUNNING)
    ctx=Execution(manager,tid,'',threading.Event())
    token=current_execution.set(ctx)
    yield ctx,directory
    current_execution.reset(token)
    manager.stop_worker()


def test_task_reuses_only_its_own_role_and_sends_new_retry_feedback(execution):
    ctx,directory=execution
    task='UNIQUE_TASK_'*1000
    calls=[]
    def model(prompt,path,thread_id,**kwargs):
        calls.append((prompt,thread_id))
        return 'ok','trusted-session'
    with patch.object(main,'codex_text',side_effect=model),patch.object(main,'completed_compactions',return_value=0):
        orchestration.call_single_agent('codex',task,directory,{},ephemeral=False,raise_on_error=True)
        orchestration.call_single_agent('codex',task+'\n验收失败：缺少 report.md',directory,{},ephemeral=False,raise_on_error=True)
        orchestration.call_single_agent('codex',task+'\n验收失败：格式错误',directory,{},ephemeral=False,raise_on_error=True)
        orchestration.call_single_agent('codex',task,directory,{},ephemeral=False,raise_on_error=True,session_key='another-role')
    assert calls[0][1] is None and calls[3][1] is None
    assert calls[1][1]==calls[2][1]=='trusted-session'
    assert task in calls[0][0] and task in calls[3][0]
    assert task not in calls[1][0] and '缺少 report.md' in calls[1][0]
    assert '格式错误' in calls[2][0] and '缺少 report.md' not in calls[2][0]
    assert len(calls[1][0])<len(calls[0][0])//10


def test_settings_change_new_baseline_and_compaction_restore_full_context(execution):
    ctx,_=execution
    prompt='ORIGINAL_'*300
    with patch.object(main,'completed_compactions',return_value=0):
        text,thread,commit,_=task_session_prompt(ctx,'codex','main',prompt,{'permission':'read'})
        commit('s1')
        assert task_session_prompt(ctx,'codex','main',prompt,{'permission':'write'})[1] is None
        assert task_session_prompt(ctx,'codex','main','OTHER_'+prompt,{'permission':'read'})[1] is None
    with patch.object(main,'completed_compactions',return_value=1):
        text,thread,_,_=task_session_prompt(ctx,'codex','main',prompt,{'permission':'read'})
        assert thread=='s1' and prompt in text


def test_live_permission_change_invalidates_a_callers_old_profile_snapshot(execution):
    ctx,directory=execution
    with patch.object(main,'completed_compactions',return_value=0):
        _,_,commit,_=task_session_prompt(ctx,'codex','main','TASK'*300,{})
        commit('s1')
        (directory/'friend.json').write_text('{"computer_tools":{"mode":"read_only"}}','utf-8')
        assert task_session_prompt(ctx,'codex','main','TASK'*300,{})[1] is None


@pytest.mark.parametrize('error',[AgentError(AgentErrorType.QUOTA_EXHAUSTED,message='quota limit'),
    AgentError(AgentErrorType.NETWORK_ERROR,message='connection dropped',retryable=True),TimeoutError('unknown outcome')])
def test_errors_do_not_create_an_extra_full_prompt_attempt(execution,error):
    _,directory=execution
    with patch.object(main,'codex_text',side_effect=error) as call:
        with pytest.raises((AgentError,InterruptedError)):
            orchestration.call_single_agent('codex','任务',directory,{},ephemeral=False,raise_on_error=True)
    assert call.call_count==1


def test_only_definite_missing_session_restarts_with_full_prompt(execution):
    _,directory=execution
    with patch.object(main,'completed_compactions',return_value=0),patch.object(main,'codex_text',
        side_effect=[('ok','old'),RuntimeError('thread not found'),('ok','new')]) as call:
        orchestration.call_single_agent('codex','TASK'*300,directory,{},ephemeral=False,raise_on_error=True)
        orchestration.call_single_agent('codex','TASK'*300,directory,{},ephemeral=False,raise_on_error=True)
    assert call.call_count==3
    assert call.call_args_list[-1].args[2] is None
    assert 'TASK'*300 in call.call_args_list[-1].args[0]


def test_untrusted_checkpoint_cannot_supply_a_session(execution):
    ctx,directory=execution
    task=ctx.manager.get_task(ctx.task_id)
    ctx.manager.save_checkpoint(task.task_id,{'agent_prompt_sessions':{'thread':'another-owner'}})
    with patch.object(main,'codex_text',return_value=('ok','own')) as call:
        orchestration.call_single_agent('codex','任务',directory,{},ephemeral=False,raise_on_error=True)
    assert call.call_args.args[2] is None


def test_native_backend_reuses_client_bound_thread_instead_of_starting_again(execution):
    ctx,directory=execution
    (directory/'friend.json').write_text(json.dumps({'computer_tools':{'enabled':True,'commands':True}}),'utf-8')
    task=ctx.manager.get_task(ctx.task_id); task.description='NATIVE_BASE_'*200
    calls=[]
    class Client:
        def start_thread(self,**kw): calls.append(('start',kw)); return 'native-owned'
        def resume_thread(self,thread_id,**kw): calls.append(('resume',thread_id,kw)); return thread_id
        def execute_turn(self,thread_id,prompt,**kw):
            calls.append(('turn',prompt)); return {'event':'COMPLETED','status':'completed','output':'ok','usage':{'input_tokens':1,'output_tokens':1}}
    with patch('codex_app_server.get_codex_app_server_client',return_value=Client()),patch.object(main,'completed_compactions',return_value=0):
        ctx.manager._execute_codex_app_server(task,ctx,{},lambda _:None)
        task.description+='\n修复验收错误'
        ctx.manager._execute_codex_app_server(task,ctx,{},lambda _:None)
    assert len([c for c in calls if c[0]=='start'])==1
    assert len([c for c in calls if c[0]=='resume'])==1
    turns=[c[1] for c in calls if c[0]=='turn']
    assert 'NATIVE_BASE_' in turns[0] and 'NATIVE_BASE_' not in turns[1]
    assert '修复验收错误' in turns[1]


def test_chat_quota_error_does_not_restart_the_conversation(tmp_path):
    from store import FriendStore
    store=FriendStore(tmp_path)
    try:
        store.set('thread_id','s1')
        with patch('local_chat.limited',return_value=False),patch('prompt_context.session_prompt',
            return_value=('delta','s1',lambda _:None)),patch.object(main,'codex_text',
            side_effect=RuntimeError('quota exhausted')) as call:
            with pytest.raises(RuntimeError,match='quota'):
                main._session_codex_text(store,'FULL',tmp_path,'reply',{'model_api':{'provider':'codex'}})
        assert call.call_count==1
    finally:
        store.close()
