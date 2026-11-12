"""Independent regression tests for authenticated approval, tenancy, persistence and cookies.

All tests use temporary directories; no real QQ accounts/network or paid operations.
"""
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

import pytest

from approval_manager import ApprovalManager, ApprovalCategory, create_trusted_identity, _INTERNAL_SYSTEM_TOKEN
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
from browser_automation import IsolatedBrowserSession
from local_retrieval_engine import LocalHybridSearchEngine, TaskExperienceStore
from task_manager import TaskManager


@pytest.fixture
def tdir():
    with tempfile.TemporaryDirectory(prefix='qqdot_security_') as d:
        yield pathlib.Path(d)


def _request(mgr, owner='alice'):
    return mgr.submit_request(task_id='task_id', category=ApprovalCategory.HIGH_RISK_COMMAND,
                              description='dangerous operation', owner_id=owner)


def test_cannot_mint_administrator_from_plain_name(tdir):
    manager=ApprovalManager(tdir/'approvals.sqlite3')
    req=_request(manager)
    with pytest.raises(PermissionError):
        create_trusted_identity('mallory', is_admin=True)
    with pytest.raises(PermissionError):
        manager.register_trusted_admin('mallory')
    with pytest.raises(PermissionError):
        manager.approve_request(req, approver_id='alice')
    with pytest.raises(PermissionError):
        manager.approve_request(req, approver_id='system:admin')
    with pytest.raises(PermissionError):
        manager.approve_request(req, _internal_token=_INTERNAL_SYSTEM_TOKEN)
    assert manager.get_request(req).status.value == 'PENDING'


def test_real_web_token_binds_signed_identity_and_admin(tdir):
    manager=ApprovalManager(tdir/'approvals.sqlite3')
    fake_web=types.SimpleNamespace(
        friend_for_token=lambda token: {'alice-token':'alice', 'admin-token':'root'}.get(token),
        is_admin_user=lambda uid: uid == 'root'
    )
    with patch.dict(sys.modules, {'house_web':fake_web}):
        with pytest.raises(PermissionError):
            create_trusted_identity('mallory', session_token='alice-token')
        with pytest.raises(PermissionError):
            create_trusted_identity('alice', is_admin=True, session_token='alice-token')
        owner=create_trusted_identity('alice', session_token='alice-token')
        admin=manager.register_trusted_admin('root', session_token='admin-token')
        req=_request(manager)
        assert manager.approve_request(req, approver_id=admin)
        assert manager.get_request(req).decided_by == 'root'
        second=_request(manager)
        assert manager.approve_request(second, approver_id=owner)
        assert manager.get_request(second).decided_by == 'alice'


def test_task_controls_require_identity(tdir):
    mgr=TaskManager(db_path=tdir/'tasks.sqlite3', workspace=tdir)
    try:
        task=mgr.create_task(title='secret task',description='test',owner_id='alice')
        with pytest.raises(PermissionError):
            mgr.cancel_task(task)
        with pytest.raises(PermissionError):
            mgr.cancel_task(task, owner_id='bob')
        with pytest.raises(PermissionError):
            mgr.pause_task(task)
        with pytest.raises(PermissionError):
            mgr.resume_task(task)
        with pytest.raises(PermissionError):
            mgr.approve_task(task)
        assert mgr.cancel_task(task, owner_id='alice')
    finally:
        mgr.stop_worker()


def test_memory_invalidation_requires_owner(tdir):
    mem=AgentMemoryStore(db_path=tdir/'memory.sqlite3')
    mid=mem.store_memory(scope=MemoryScope.USER_PREFERENCE, owner_id='alice', key='note',
                         content='private data', source=MemorySource.USER_EXPLICIT)
    with pytest.raises(PermissionError):
        mem.invalidate_memory(mid)
    with pytest.raises(PermissionError):
        mem.invalidate_memory(mid, owner_id='bob')
    with pytest.raises(PermissionError):
        mem.invalidate_memory(mid, owner_id='bob', is_admin=True)
    assert mem.invalidate_memory(mid, owner_id='alice')


def test_experience_hmac_key_persists_across_processes(tdir):
    db=tdir/'retrieval.sqlite3'
    store=TaskExperienceStore(LocalHybridSearchEngine(db))
    sig=store._compute_verifier_signature('experience','task','alice','hash')
    assert sig.startswith('v1:')
    check_script='''from pathlib import Path
from local_retrieval_engine import LocalHybridSearchEngine, TaskExperienceStore
import sys
s=TaskExperienceStore(LocalHybridSearchEngine(Path(sys.argv[1])))
print(s._verify_signature('experience','task','alice','hash',sys.argv[2]))
'''
    child=subprocess.run([sys.executable,'-c',check_script,str(db),sig],
                         cwd=pathlib.Path(__file__).parents[1],capture_output=True,text=True,timeout=25)
    assert child.returncode==0, child.stderr
    assert child.stdout.strip()=='True'
    assert not store._verify_signature('experience','task','bob','hash',sig)
    if os.name != 'nt':
        assert not ((tdir/'.experience_signing_key_v1').stat().st_mode & 0o077)


def test_cookie_host_scopes_do_not_cross_origins(tdir):
    session=IsolatedBrowserSession('alice',base_dir=tdir/'sessions')
    session.current_url='http://127.0.0.1:1234/login'
    session.save_cookies({'session':'ip-only-token'})
    assert session.load_cookies().get('session') == 'ip-only-token'
    session.current_url='http://localhost:1234/login'
    assert 'session' not in session.load_cookies()
    second=IsolatedBrowserSession('bob',base_dir=tdir/'sessions')
    second.current_url='http://localhost:1234/login'
    assert second.load_cookies() == {}


def test_private_network_denied_by_default_and_local_file_denied(tdir):
    browser=IsolatedBrowserSession('alice',base_dir=tdir/'sessions')
    with pytest.raises(PermissionError):
        browser._validate_target_url('http://127.0.0.1:8000/')
    with pytest.raises(PermissionError):
        browser._validate_target_url('file:///etc/passwd')
    with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
        with pytest.raises(PermissionError):
            browser._validate_target_url('http://evil.example/')


def test_real_browser_absent_never_generates_synthetic_success(tdir):
    session=IsolatedBrowserSession('alice',base_dir=tdir/'sessions',use_headless_chromium=True,
                                   allowed_local_origins=['http://127.0.0.1:8081'])
    try:
        with patch('browser_playwright_adapter.PlaywrightBrowserAdapter.run',return_value={'status':'failed','error':'chromium unavailable'}):
            result=session.navigate('http://127.0.0.1:8081')
            assert result['status']=='failed'
            assert result['engine']=='playwright_chromium'
    finally:
        # The mocked adapter has a worker executor, but no chromium instance.
        session.close()


def test_friend_approval_cannot_borrow_desktop_admin_role(tdir):
    """Friend endpoints remain owner-scoped, even for a desktop administrator."""
    import house_web
    manager = ApprovalManager(tdir/'approvals.sqlite3')
    req = _request(manager, owner='alice')
    with patch.object(house_web, '_tokens', return_value={'root':'root-token','alice':'alice-token'}), \
         patch.object(house_web, 'friend_for_token', side_effect=lambda token: {'root-token':'root','alice-token':'alice'}.get(token)), \
         patch.object(house_web, 'is_admin_user', side_effect=lambda uid: uid=='root'), \
         patch.object(ApprovalManager, 'get_instance', return_value=manager):
        with pytest.raises(PermissionError):
            house_web.post_agent_approval_decision('root',{'req_id':req,'decision':'approve'})
        assert manager.get_request(req).status.value == 'PENDING'
        answer=house_web.post_agent_approval_decision('alice',{'req_id':req,'decision':'approve'})
        assert answer['message'] == '操作已批准'
    assert manager.get_request(req).decided_by == 'alice'
