import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import time
from codex_app_server import CodexAppServerClient, UnifiedTaskEvent, CodexSessionInfo, set_codex_app_server_client
from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus, create_trusted_identity
from unittest.mock import patch
import house_web
from task_manager import TaskManager, TaskStatus
from model_dispatcher import ModelDispatcher
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
from tests.test_e2e_codex_app_server_full_flow import MockProcess

tmp_path = Path(tempfile.mkdtemp())
db_file = tmp_path / 'test_e2e.db'
workspace_dir = tmp_path / 'workspace'
workspace_dir.mkdir(parents=True, exist_ok=True)

print('Step 1 starting...', flush=True)
approval_mgr = ApprovalManager.get_instance(db_path=tmp_path / 'approvals.sqlite3')
client = CodexAppServerClient(approval_manager=approval_mgr)
set_codex_app_server_client(client)
mock_proc = MockProcess()
client.process = mock_proc
client._running = True
import threading
client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
client._reader_thread.start()

init_res = client._send_request('initialize', {'clientInfo': {'name': 'test'}}, timeout_sec=5.0)
print('Init res:', init_res, flush=True)
client._send_notification('initialized', {})
thread_id = client.start_thread(cwd=workspace_dir, model='gpt-5', task_id='task_remote_001', owner_id='admin_user')
print('Step 1 ok, thread_id:', thread_id, flush=True)

print('Step 2 starting...', flush=True)
captured_events = []
def on_task_event(evt, payload):
    captured_events.append((evt, payload))
turn_outcome = client.execute_turn(thread_id=thread_id, prompt='test', task_id='task_remote_001', owner_id='admin_user', on_event=on_task_event, timeout_sec=5.0)
print('Step 2 ok, turn_outcome:', turn_outcome, flush=True)

print('Step 3 starting...', flush=True)
steer_ok = client.steer_turn(thread_id=thread_id, turn_id='turn_xyz987', additional_instruction='float')
print('Steer ok:', steer_ok, flush=True)

task_mgr = TaskManager(db_path=db_file, workspace=workspace_dir)
created_task_id = task_mgr.create_task(title='float calc', description='calc', category='code', owner_id='admin_user')
print('Task created:', created_task_id, flush=True)
client._active_turns[created_task_id] = 'turn_xyz987'
client._active_sessions[thread_id].task_id = created_task_id

mgr_steer_ok = task_mgr.steer_task(created_task_id, 'test', owner_id=task_mgr.get_task(created_task_id).owner_id)
print('Step 3 ok, mgr_steer_ok:', mgr_steer_ok, flush=True)

print('Step 4 starting...', flush=True)
decision = client._process_approval_request(method='item/commandExecution/requestApproval', params={'threadId': thread_id, 'turnId': 'turn_xyz987', 'command': 'pip install numpy', 'reason': 'calc'})
print('Step 4 ok, decision:', decision, flush=True)

print('Step 5 starting...', flush=True)
interrupt_ok = client.interrupt_turn(thread_id, 'turn_xyz987')
print('Interrupt ok:', interrupt_ok, flush=True)
bound = client._active_sessions[thread_id]
resumed_thread_id = client.resume_thread(thread_id, cwd=workspace_dir, task_id=bound.task_id, owner_id=bound.owner_id)
print('Step 5 ok, resumed:', resumed_thread_id, flush=True)

print('Step 6 starting...', flush=True)
mem_store = AgentMemoryStore(db_path=tmp_path / 'memory.db')
mem_store.store_memory(scope=MemoryScope.PROJECT_MEMORY, owner_id='admin_user', key='k', content='c')
print('Step 6 ok', flush=True)

print('Step 7 starting...', flush=True)
needs_appr, msg, req_id = approval_mgr.check_admin_multitenant_subscription_risk(admin_id='admin_user', new_user_id='friend_bob', provider='codex')
print('Step 7 ok, needs_appr:', needs_appr, flush=True)
client.close()
print('ALL FINISHED SUCCESSFULLY', flush=True)
