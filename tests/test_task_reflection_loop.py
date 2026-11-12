import json

import pytest

from task_manager import TaskManager, TaskStatus, _retry_note


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import house_settings
    import local_retrieval_engine as lre
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.setattr(house_settings, 'root', lambda: tmp_path)
    monkeypatch.setattr(lre.LocalHybridSearchEngine, 'embed_documents', lambda *_: None)
    folder = tmp_path / 'friends/12345'
    folder.mkdir(parents=True)
    profile = {'qq_id': '12345', 'computer_tools': {'enabled': True, 'workspace': 'workspace'},
               'model_api': {'provider': 'codex', 'codex_model': 'fast', 'codex_effort': 'low'},
               'background_model': {'provider': 'codex', 'model': 'fast', 'effort': 'low'}}
    (folder / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    import codex_settings
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low']})
    manager = TaskManager(tmp_path / 'data/tasks.sqlite3', tmp_path)
    monkeypatch.setattr(manager, '_notify_status', lambda *_: None)
    yield manager, folder, profile
    manager.stop_worker()


def proposal():
    return json.dumps({'root_cause': '目标文件没有落地', 'correction': '生成实际文件并检查内容',
                       'prevention': '完成前检查文件存在且不为空'}, ensure_ascii=False), None


def task(manager, **kwargs):
    identifier = manager.create_task('文件验收', '生成实际文件并检查内容', category='file', owner_id='12345',
                               max_retries=1, initial_checkpoint={'notify_qq': False}, **kwargs)
    return manager.get_task(identifier)


def test_failure_reflection_retry_verification_and_private_recall(setup, monkeypatch):
    manager, _, _ = setup
    calls, contexts = [], []
    import multi_agent_orchestrator
    def model(*args, **kwargs):
        calls.append((args, kwargs))
        return proposal()
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', model)
    def execute(item, log):
        from task_runtime import current_execution
        workspace = current_execution.get().workspace()
        workspace.mkdir(parents=True, exist_ok=True)
        contexts.append(_retry_note(item))
        if len(contexts) == 2:
            (workspace / 'result.txt').write_text('独立文件验收内容', 'utf-8')
        return {'output': '文件任务结果', 'target_files': ['result.txt'], 'artifacts': ['result.txt']}
    manager.register_custom_executor('file', execute)
    item = task(manager)
    manager._process_single_task(item)
    failed = manager.get_task(item.task_id)
    assert failed.status == TaskStatus.PENDING
    assert failed.checkpoint['reflections'][0]['state'] == 'proposed'
    import memory_hub
    path = manager.db_path.parent / 'retrieval_engine.sqlite3'
    before = memory_hub._experience_items(path, '12345', 'default')
    assert len(before) == 1 and before[0]['kind'] == 'pitfall' and not before[0]['source']['verified']
    assert memory_hub._experience_items(path, '54321', 'default') == []
    assert memory_hub._experience_items(path, '12345', 'other-project') == []
    manager._process_single_task(failed)
    done = manager.get_task(item.task_id)
    assert done.status == TaskStatus.COMPLETED, done.error
    assert done.checkpoint['reflection_resolution']['state'] == 'validated'
    assert '完成前检查文件存在' in contexts[1] and contexts[0] == ''
    assert len(calls) == 1 and calls[0][1]['text_only'] and calls[0][1]['max_retries'] == 0
    after = memory_hub._experience_items(path, '12345', 'default')
    verified = [hit for hit in after if hit['source']['verified']]
    assert len(verified) == 1 and '预防检查（模型建议）' in verified[0]['text']
    assert memory_hub._experience_items(path, '54321', 'default') == []
    assert memory_hub._experience_items(path, '12345', 'other-project') == []


@pytest.mark.parametrize('answer', [('not-json', None), RuntimeError('model unavailable')])
def test_bad_or_failed_reflection_is_not_replayed_or_promoted(setup, monkeypatch, answer):
    manager, _, _ = setup
    calls = []
    import multi_agent_orchestrator
    def model(*args, **kwargs):
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', model)
    manager.register_custom_executor('file', lambda *_: {'output': '', 'target_files': ['missing.txt']})
    item = task(manager)
    manager._process_single_task(item)
    manager._process_single_task(manager.get_task(item.task_id))
    done = manager.get_task(item.task_id)
    assert done.status == TaskStatus.FAILED and len(calls) == 1
    assert done.checkpoint['reflections'][0]['state'] == 'unavailable'
    assert 'reflection_resolution' not in done.checkpoint
    with manager._get_connection() as db:
        assert '预期文件未创建' in db.execute('SELECT error FROM tasks WHERE task_id=?', (item.task_id,)).fetchone()[0]
    import sqlite3
    with sqlite3.connect(manager.db_path.parent / 'retrieval_engine.sqlite3') as db:
        assert not db.execute("SELECT 1 FROM task_experiences WHERE lifecycle_state='VALIDATED'").fetchone()
        assert not db.execute('SELECT 1 FROM failure_diagnostics').fetchone()


def test_forged_completion_never_resolves_reflection(setup, monkeypatch):
    manager, _, _ = setup
    import multi_agent_orchestrator
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', lambda *_args, **_kw: proposal())
    manager.register_custom_executor('file', lambda *_: {'target_files': ['missing.txt']})
    item = task(manager)
    manager._process_single_task(item)
    assert manager.update_task_status(item.task_id, TaskStatus.COMPLETED,
        verification_info={'success': True, 'objective_verified': True, 'verifier_type': 'self-claim'},
        expected_statuses=[TaskStatus.PENDING])
    done = manager.get_task(item.task_id)
    assert 'reflection_resolution' not in done.checkpoint
    assert not done.verification_info.get('objective_verified')


def test_zero_call_budget_does_not_call_model(setup, monkeypatch):
    manager, _, _ = setup
    import multi_agent_orchestrator
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', lambda *_args, **_kw: pytest.fail('不应调用模型'))
    def execute(item, log):
        with manager._get_connection() as db:
            db.execute('UPDATE tasks SET call_count=1 WHERE task_id=?', (item.task_id,))
        return {'target_files': ['missing.txt']}
    manager.register_custom_executor('file', execute)
    item = task(manager, max_calls=1)
    manager._process_single_task(item)
    assert 'reflections' not in manager.get_task(item.task_id).checkpoint


def test_pause_during_model_call_discards_proposal_and_does_not_replay(setup, monkeypatch):
    manager, _, _ = setup
    calls = []
    import multi_agent_orchestrator
    def model(*args, **kwargs):
        from task_runtime import current_execution
        identifier = current_execution.get().task_id
        calls.append(1)
        manager.update_task_status(identifier, TaskStatus.PAUSED)
        return proposal()
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', model)
    manager.register_custom_executor('file', lambda *_: {'target_files': ['missing.txt']})
    item = task(manager)
    manager._process_single_task(item)
    interrupted = manager.get_task(item.task_id)
    assert interrupted.status == TaskStatus.PAUSED
    assert interrupted.checkpoint['reflections'][0]['state'] == 'calling'
    assert 'experience_id' not in interrupted.checkpoint['reflections'][0]
    assert manager.retry_task(item.task_id)
    manager._process_single_task(manager.get_task(item.task_id))
    assert len(calls) == 1 and manager.get_task(item.task_id).status == TaskStatus.PENDING


@pytest.mark.parametrize('category,checkpoint', [('agent_message', {}), ('data', {'agent_group_room':'room'}),
                                               ('general', {'task_mode':'chat'})])
def test_conversation_history_is_not_automatically_an_experience_or_workflow(setup, monkeypatch, category, checkpoint):
    manager, _, _ = setup
    from task_workflows import WorkflowStore
    monkeypatch.setattr(WorkflowStore, 'learn', lambda *_args, **_kw: pytest.fail('不能自动把会话学成流程'))
    identifier = manager.create_task('普通对话记录', '整段聊天只应留在会话历史里', category=category,
        owner_id='12345', initial_checkpoint={**checkpoint, 'notify_qq':False})
    manager.update_task_status(identifier, TaskStatus.COMPLETED, result='这是完整聊天记录'*1000,
        expected_statuses=[TaskStatus.PENDING], verification_info={'success':True, 'verifier_type':'data_payload'})
    WorkflowStore(manager).capture(manager.get_task(identifier))
    import sqlite3
    with sqlite3.connect(manager.db_path.parent/'retrieval_engine.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM task_experiences').fetchone()[0] == 0


def test_unverified_task_completion_stays_in_task_history(setup):
    manager, _, _ = setup
    item = task(manager)
    manager.update_task_status(item.task_id, TaskStatus.COMPLETED, result='模型说完成了但没验收',
                               expected_statuses=[TaskStatus.PENDING])
    import sqlite3
    with sqlite3.connect(manager.db_path.parent/'retrieval_engine.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM task_experiences').fetchone()[0] == 0
