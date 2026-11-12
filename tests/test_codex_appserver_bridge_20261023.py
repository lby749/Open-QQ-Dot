"""Production-path contract tests: no remote Codex account required.

Fake client verifies wiring only. It MUST NOT be reported as live Codex e2e.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from codex_app_server import CodexAppServerClient, CodexSessionInfo, UnifiedTaskEvent
from task_manager import TaskManager, TaskStatus


class RecordingClient:
    def __init__(self, change_files=True, event='COMPLETED'):
        self.calls = []
        self.change_files = change_files
        self.event = event

    def start_thread(self, **kwargs):
        self.calls.append(('thread/start', kwargs))
        return 'thread-contract'

    def execute_turn(self, thread_id, prompt, *, task_id, owner_id, on_event, timeout_sec):
        self.calls.append(('turn/start', dict(thread_id=thread_id, task_id=task_id, owner_id=owner_id)))
        if self.change_files:
            self.calls[0][1]['cwd'].joinpath('result.py').write_text('ANSWER = 42\n', encoding='utf8')
        on_event(UnifiedTaskEvent.STARTED, {'turn_id': 'turn-contract'})
        return dict(event=self.event, status='completed' if self.event == 'COMPLETED' else 'failed',
                    output='Created result.py' if self.change_files else 'No changes',
                    usage={'total_tokens': 35}, tokens_used=35, turn_id='turn-contract')


def build_manager(root, *, enabled=True, commands=True, backend='codex_app_server'):
    friends = root/'friends'/'alice'
    friends.mkdir(parents=True)
    (friends/'friend.json').write_text(json.dumps({'computer_tools': {
        'enabled': enabled, 'commands': commands, 'project_path': str(root/'project'),
    }, 'model_api': {'provider': backend, 'codex_model': 'gpt-test'}}), encoding='utf8')
    (root/'project').mkdir()
    return TaskManager(db_path=root/'tasks.sqlite3', workspace=root)


def test_normal_task_worker_reaches_codex_app_server_and_verifies_file():
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp)
        manager=build_manager(root)
        fake=RecordingClient()
        try:
            task_id=manager.create_task('Create a module', 'Write result.py', category='code', owner_id='alice',
                                        initial_checkpoint={'target_files':['result.py']},max_retries=0)
            with patch('codex_app_server.get_codex_app_server_client',return_value=fake), \
                 patch.object(manager,'_notify_completion'):
                manager._process_single_task(manager.get_task(task_id))
            result=manager.get_task(task_id)
            assert result.status == TaskStatus.COMPLETED, (result.status,result.error)
            assert result.verification_info['success'] is True
            assert (root/'project'/'result.py').read_text('utf8') == 'ANSWER = 42\n'
            assert [x[0] for x in fake.calls] == ['thread/start','turn/start']
            assert fake.calls[0][1]['owner_id']=='alice'
            assert fake.calls[0][1]['sandbox_policy']=={'mode':'workspace-write'}
            assert manager.get_task(task_id).checkpoint['codex_thread_id']=='thread-contract'
            assert manager.get_task(task_id).consumed_tokens == 35
        finally:
            manager.stop_worker()


def test_false_complete_without_artifacts_does_not_verify():
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp);manager=build_manager(root)
        try:
            task_id=manager.create_task('Do nothing','No output',category='code',owner_id='alice',max_retries=0)
            with patch('codex_app_server.get_codex_app_server_client',return_value=RecordingClient(change_files=False)), \
                 patch.object(manager,'_notify_completion'):
                manager._process_single_task(manager.get_task(task_id))
            assert manager.get_task(task_id).status == TaskStatus.FAILED
        finally: manager.stop_worker()


def test_unapproved_commands_and_unrestricted_sandbox_rejected_before_start():
    with TemporaryDirectory() as temp:
        root=Path(temp);mgr=build_manager(root,commands=False)
        try:
            task_id=mgr.create_task('no tools','attempt code',category='code',owner_id='alice',max_retries=0)
            fake=RecordingClient()
            with patch('codex_app_server.get_codex_app_server_client',return_value=fake):
                mgr._process_single_task(mgr.get_task(task_id))
            assert mgr.get_task(task_id).status == TaskStatus.FAILED
            assert fake.calls == []
        finally: mgr.stop_worker()

    client=CodexAppServerClient()
    with patch.object(client,'start'), patch.object(client,'_send_request') as send:
        with pytest.raises(PermissionError):
            client.start_thread(Path(temp), task_id='t1',owner_id='alice', sandbox_policy={'mode':'danger-full-access'})
        send.assert_not_called()


def test_thread_owner_and_task_immutable_and_unknown_resume_denied():
    client=CodexAppServerClient()
    client._active_sessions['known']=CodexSessionInfo('known','task-a','alice','/tmp','test-model')
    with pytest.raises(PermissionError):
        client.execute_turn('known','prompt',task_id='task-b',owner_id='alice')
    with pytest.raises(PermissionError):
        client.execute_turn('known','prompt',task_id='task-a',owner_id='bob')
    with pytest.raises(PermissionError):
        client.execute_turn('known','prompt',task_id='task-a')
    with pytest.raises(PermissionError):
        client.resume_thread('foreign',Path('/tmp'),task_id='task-a',owner_id='alice')


def test_actual_turn_completed_notification_parses_nested_turn_id():
    client=CodexAppServerClient()
    client._active_sessions['thread']=CodexSessionInfo('thread','task','alice','/tmp','model')
    client._handle_server_notification({'method':'turn/completed','params':{
        'threadId':'thread','turn':{'id':'turn-real','status':'completed'},
    }})
    assert client._completed_turns['turn-real'][0] == UnifiedTaskEvent.COMPLETED

@pytest.mark.skipif(__import__('os').name == 'nt',reason='POSIX executable fixture; Windows requires actual Codex CLI integration')
def test_json_rpc_subprocess_handshake_and_live_event_reader():
    """Exercise a real JSON-RPC stdio subprocess without consuming a Codex account."""
    import sys
    import os
    with TemporaryDirectory() as temp:
        script = Path(temp)/'fake-codex'
        script.write_text('#!'+sys.executable+'\n'+'''import json,sys

def send(x):
 print(json.dumps(x),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 method=m.get('method')
 if method=='initialize':
  send({'id':m['id'],'result':{'serverInfo':{'name':'test-only'}}})
 elif method=='initialized':
  pass
 elif method=='thread/start':
  send({'id':m['id'],'result':{'thread':{'id':'thread-simulated'}}})
 elif method=='turn/start':
  send({'id':m['id'],'result':{'turn':{'id':'turn-simulated'}}})
  send({'method':'item/agentMessage/delta','params':{'threadId':'thread-simulated','turnId':'turn-simulated','delta':'work done'}})
  send({'method':'turn/completed','params':{'threadId':'thread-simulated','turn':{'id':'turn-simulated','status':'completed'},'usage':{'total_tokens':37}}})
 elif method=='turn/interrupt':
  send({'id':m['id'],'result':{}})
''',encoding='utf8')
        script.chmod(0o700)
        client=CodexAppServerClient(custom_codex_path=str(script))
        try:
            client.start(timeout_sec=4)
            tid=client.start_thread(Path(temp),task_id='task-alpha',owner_id='alice')
            assert tid == 'thread-simulated'
            reply=client.execute_turn(tid,'please help',task_id='task-alpha',owner_id='alice',timeout_sec=3)
            assert reply['event']=='COMPLETED'
            assert reply['turn_id']=='turn-simulated'
            assert reply['output']=='work done'
            assert reply['tokens_used']==37
            assert client._active_listeners.get('task-alpha') is None
        finally:
            client.close()


def test_forged_completion_metadata_does_not_promote_experience():
    import sqlite3
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp)
        manager=build_manager(root)
        try:
            task_id=manager.create_task('forged evidence','trust my pass',category='code',owner_id='alice')
            assert manager.update_task_status(task_id, TaskStatus.COMPLETED,
                verification_info={'success':True,'verifier_type':'code_complete_check'},
                artifacts=['FAKE PASS'])
            with sqlite3.connect(str(root/'retrieval_engine.sqlite3')) as con:
                row=con.execute('SELECT result_status,confidence_status FROM task_experiences WHERE task_id=?',(task_id,)).fetchone()
            # Unverified completion metadata is kept in task history, not the experience library.
            assert row is None
        finally:manager.stop_worker()


def test_real_worker_verification_can_promote_experience():
    import sqlite3
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp);manager=build_manager(root)
        try:
            # An objective check (target file exists and compiles) is what earns verified experience;
            # a bare file change with no test or target now goes to owner review instead.
            task_id=manager.create_task('test verifier','write result.py',category='code',owner_id='alice',
                                        initial_checkpoint={'target_files':['result.py']},max_retries=0)
            with patch('codex_app_server.get_codex_app_server_client',return_value=RecordingClient()), \
                 patch.object(manager,'_notify_completion'):
                manager._process_single_task(manager.get_task(task_id))
            with sqlite3.connect(str(root/'retrieval_engine.sqlite3')) as con:
                row=con.execute('SELECT result_status,confidence_status FROM task_experiences WHERE task_id=?',(task_id,)).fetchone()
            assert row == ('success_verified','high_verified')
        finally:manager.stop_worker()


def test_token_usage_notification_absolute_total_not_double_counted():
    client=CodexAppServerClient()
    client._active_sessions['th']=CodexSessionInfo('th','task','alice','/tmp','model')
    client._turn_usage_baselines['th']=100
    client._handle_server_notification({'method':'thread/tokenUsage/updated','params':{
        'threadId':'th','tokenUsage':{'total':{'totalTokens':145}}
    }})
    client._handle_server_notification({'method':'turn/completed','params':{
        'threadId':'th','turn':{'id':'turn','status':'completed'}
    }})
    event,payload=client._completed_turns['turn']
    assert event==UnifiedTaskEvent.COMPLETED
    assert payload['tokens_used']==45


def test_mutable_friend_profile_cannot_grant_administrator_privileges(monkeypatch):
    from trusted_admin_policy import is_admin_user
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        root=Path(temp)
        monkeypatch.setenv('QQ_BRIDGE_HOME', str(root))
        monkeypatch.delenv('QQ_ADMINS',raising=False)
        monkeypatch.delenv('ADMIN_QQ',raising=False)
        manager=build_manager(root)
        try:
            profile=root/'friends'/'alice'/'friend.json'
            settings=json.loads(profile.read_text('utf8'))
            settings['is_admin']=True
            profile.write_text(json.dumps(settings),encoding='utf8')
            task_id=manager.create_task('test','test',owner_id='alice')
            from task_runtime import Execution
            context=Execution(manager,task_id,'run-id',__import__('threading').Event())
            assert context.effective_permissions()['is_admin'] is False
            assert is_admin_user('alice') is False
            (root/'house_web.local.json').write_text('{"admin_qqs":["alice"]}',encoding='utf8')
            assert context.effective_permissions()['is_admin'] is True
        finally:manager.stop_worker()
