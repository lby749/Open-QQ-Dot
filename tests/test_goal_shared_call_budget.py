import json

from task_manager import TaskManager, TaskStatus
from task_runtime import current_execution


def test_stage_can_use_remaining_goal_calls_and_finish_at_exact_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    directory = tmp_path/'friends/12345'
    directory.mkdir(parents=True)
    (directory/'friend.json').write_text('{"qq_id":"12345"}', 'utf-8')
    manager = TaskManager(tmp_path/'data/tasks.sqlite3', tmp_path)
    stages = [{'title': str(i), 'description': '仅检查预算记账', 'category': 'data',
               'spec': {'expected_keys': ['ok']}} for i in (1, 2)]
    goal_id = manager.create_goal('两步共享四次调用', '只做本地预算记账', '两步均返回 ok', stages, '12345', max_calls=4)
    def execute(task, _log):
        execution = current_execution.get()
        for _ in range(3 if task.title == '1' else 1):
            execution.before_call('本地测试，不调用任何模型')
            execution.after_call('ok', {'total_tokens': 1})
        return {'output': json.dumps({'ok': True})}
    manager.register_custom_executor('data', execute)
    for _ in range(2):
        manager._process_single_task(manager._fetch_next_executable_task())
    goal = manager.get_goal(goal_id)
    assert goal['call_count'] == 4
    assert [task['call_count'] for task in goal['tasks']] == [3, 1]
    assert goal['status'] == 'WAITING_REVIEW' and goal['progress'] == 1
    manager.stop_worker()


def test_explicit_goal_resume_restores_resource_wait_without_adding_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    manager = TaskManager(tmp_path/'data/tasks.sqlite3', tmp_path)
    gid = manager.create_goal('继续', '只做本地检查', '返回 ok',
        [{'title': '阶段', 'description': '本地测试', 'category': 'data'}], '12345', max_calls=4)
    task = manager.get_goal(gid)['tasks'][0]
    manager.update_task_status(task['task_id'], TaskStatus.WAITING_RESOURCE, error='等待预算')
    manager._refresh_task_goal(task['task_id'])
    assert manager.get_goal(gid)['status'] == 'PAUSED'
    assert manager.goal_action(gid, '12345', 'resume')
    assert manager.get_task(task['task_id']).status == TaskStatus.PENDING
    assert manager.get_goal(gid)['max_calls'] == 4
    manager.stop_worker()
