"""Research execution receipts, stop gates and cumulative budget accounting."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from research_tracker import ResearchExecutionUncertain, ResearchTracker, ToolExecutionStatus, VerificationPlan
from task_manager import TaskManager


@pytest.fixture
def research(tmp_path):
    manager = TaskManager(tmp_path / 'tasks.db', tmp_path)
    tracker = ResearchTracker(tmp_path / 'research.db')
    friend = tmp_path / 'friends/12345'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text(json.dumps({'computer_tools': {'enabled': True, 'commands': True}}), 'utf-8')
    task = manager.create_task('研究', '测试研究', owner_id='12345', category='research',
                               initial_checkpoint={'research_project_id': 'research', 'stage_index': 1})
    tracker.create_research_project('research', '有限研究', '验证实验数据', task,
        [{'title': '实验', 'objective': '核对素数'}], max_stages=2, owner_id='12345')
    hypothesis = tracker.create_hypothesis('research', 1, 'There are 10 primes in interval [2, 30]', owner_id='12345')
    yield tracker, manager, hypothesis
    manager.stop_worker()


def execute(tracker, hypothesis, **kwargs):
    return tracker.execute_experiment('research', 1, hypothesis.hypothesis_id,
        'python_numeric', kwargs.pop('code', "print('ok')"), owner_id=kwargs.pop('owner', '12345'),
        execution_key='stage:research:1', workspace=tracker.db_path.parent, **kwargs)


@pytest.mark.parametrize('kind', ['hypothesis', 'decision', 'branch'])
@pytest.mark.parametrize('clock_step', [0, 1000])
def test_research_records_remain_distinct_when_the_clock_repeats(research, kind, clock_step):
    import research_tracker
    tracker, _, _ = research
    ids = []
    with patch.object(research_tracker, 'time', wraps=research_tracker.time) as clock:
        for index in range(3):
            clock.time.return_value = 1000.0 + index * clock_step
            if kind == 'hypothesis':
                identifier = tracker.create_hypothesis('research', 1, f'claim {index}', owner_id='12345').hypothesis_id
            elif kind == 'decision':
                identifier = tracker.evaluate_next_step('research', 1)['decision_id']
            else:
                identifier = tracker.create_branch('research', f'branch {index}', f'claim {index}', owner_id='12345').branch_id
            ids.append(identifier)
    assert len(set(ids)) == 3
    table, column = {'hypothesis': ('research_hypotheses', 'hypothesis_id'),
                     'decision': ('research_decisions', 'decision_id'),
                     'branch': ('research_branches', 'branch_id')}[kind]
    with ResearchTracker(tracker.db_path)._get_connection() as db:
        saved = db.execute(f'SELECT {column} FROM {table} WHERE {column} IN (?,?,?)', ids).fetchall()
    assert {row[0] for row in saved} == set(ids)


def test_completed_receipt_survives_restart_without_reexecuting_or_promoting_claim(research):
    tracker, _, hypothesis = research
    with patch.object(tracker, '_execute_sandboxed_python', return_value=(True, 'ok', '', ToolExecutionStatus.SUCCEEDED)) as tool:
        first = execute(tracker, hypothesis)
    reopened = ResearchTracker(tracker.db_path)
    with patch.object(reopened, '_execute_sandboxed_python', side_effect=AssertionError('must not rerun')):
        second = execute(reopened, hypothesis)
    assert first.experiment_id == second.experiment_id
    assert tool.call_count == 1
    assert second.tool_execution_success
    assert not second.experiment_valid and not second.claim_supported


def test_interrupted_receipt_keeps_snapshot_and_refuses_blind_retry(research):
    tracker, _, hypothesis = research
    with patch.object(tracker, '_execute_sandboxed_python', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(tracker, hypothesis)
    reopened = ResearchTracker(tracker.db_path)
    with patch.object(reopened, '_execute_sandboxed_python', side_effect=AssertionError('must not rerun')):
        with pytest.raises(ResearchExecutionUncertain, match='禁止自动重复执行'):
            execute(reopened, hypothesis)
    experiment = reopened.list_experiments('research')[0]
    assert experiment.tool_status == 'RUNNING' and not experiment.claim_supported
    with reopened._get_connection() as db:
        saved = json.loads(db.execute('SELECT input_snapshot FROM research_experiment_receipts').fetchone()[0])
    assert saved['owner_id'] == '12345'
    assert saved['code_or_input'] == "print('ok')"
    assert saved['hypothesis_statement'] == hypothesis.statement


def test_interrupted_stage_returns_wait_instead_of_queueing_duplicate_experiment(research):
    tracker, manager, _ = research
    with patch.object(tracker, '_execute_sandboxed_python', side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            tracker.run_autonomous_stage_cycle('research', 1, manager, experiment_code="print('ok')")
    reopened = ResearchTracker(tracker.db_path)
    with patch.object(reopened, '_execute_sandboxed_python', side_effect=AssertionError('must not rerun')):
        result = reopened.run_autonomous_stage_cycle('research', 1, manager, experiment_code="print('ok')")
    assert result['decision']['action'] == 'WAIT'
    assert not result['verification']['verified'] and result['next_task_id'] is None


def test_two_trackers_cannot_execute_same_reserved_input_concurrently(research):
    tracker, _, hypothesis = research
    entered, finish = Event(), Event()
    def pending(**kwargs):
        entered.set()
        assert finish.wait(5)
        return True, 'ok', '', ToolExecutionStatus.SUCCEEDED
    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(tracker, '_execute_sandboxed_python', side_effect=pending):
        future = pool.submit(execute, tracker, hypothesis)
        try:
            assert entered.wait(5)
            with pytest.raises(ResearchExecutionUncertain):
                execute(ResearchTracker(tracker.db_path), hypothesis)
        finally:
            finish.set()
        assert future.result().tool_execution_success
    assert len(tracker.list_experiments('research')) == 1


def test_changed_code_or_verification_contract_is_a_distinct_experiment(research):
    tracker, _, hypothesis = research
    with patch.object(tracker, '_execute_sandboxed_python', return_value=(True, 'ok', '', ToolExecutionStatus.SUCCEEDED)) as tool:
        execute(tracker, hypothesis)
        execute(tracker, hypothesis, code="print('done')")
        execute(tracker, hypothesis, verification_plan=VerificationPlan(hypothesis.hypothesis_id, hypothesis.statement, scope='[2,30]'))
    assert tool.call_count == 3


def test_receipt_does_not_bypass_friend_or_stage_access(research):
    tracker, _, hypothesis = research
    with patch.object(tracker, '_execute_sandboxed_python', return_value=(True, 'ok', '', ToolExecutionStatus.SUCCEEDED)):
        execute(tracker, hypothesis)
    with pytest.raises(PermissionError):
        execute(tracker, hypothesis, owner='67890')
    with pytest.raises(ValueError, match='阶段'):
        tracker.execute_experiment('research', 2, hypothesis.hypothesis_id, 'python_numeric', "print('ok')", owner_id='12345')


@pytest.mark.parametrize('status', ['CANCELLED', 'BLOCKED', 'WAITING_APPROVAL', 'PAUSED', 'COMPLETED'])
def test_stopped_project_never_synthesizes_or_executes(research, status):
    tracker, manager, _ = research
    tracker.update_project_status('research', status)
    with patch.object(tracker, 'synthesize_stage_plan_and_code', side_effect=AssertionError('must stop')):
        assert tracker.run_autonomous_stage_cycle('research', 1, manager)['action'] == 'STOP'
    assert tracker.evaluate_next_step('research', 1)['action'] == 'STOP'


def test_budget_sync_counts_failed_and_retried_usage_only_once(research):
    tracker, manager, _ = research
    project = tracker.get_project('research')
    manager.record_token_usage(project.task_id, 31)
    tracker.sync_task_usage(manager, 'research', project.task_id)
    tracker.sync_task_usage(manager, 'research', project.task_id)
    assert tracker.get_project('research').used_tokens == 31
    manager.record_token_usage(project.task_id, 17)
    tracker.sync_task_usage(manager, 'research', project.task_id)
    assert tracker.get_project('research').used_tokens == 48


def test_legacy_aggregate_budget_migrates_without_recharging_history(research):
    tracker, manager, _ = research
    project = tracker.get_project('research')
    project.used_tokens = 31
    tracker._save_project(project)
    manager.record_token_usage(project.task_id, 31)
    with tracker._get_connection() as db:
        db.execute('DROP TABLE research_task_usage')
        db.execute('CREATE TABLE research_task_usage(project_id TEXT,task_id TEXT,tokens INTEGER,PRIMARY KEY(project_id,task_id))')
    reopened = ResearchTracker(tracker.db_path)
    reopened.sync_task_usage(manager, 'research', project.task_id)
    assert reopened.get_project('research').used_tokens == 31
    manager.record_token_usage(project.task_id, 17)
    reopened.sync_task_usage(manager, 'research', project.task_id)
    assert reopened.get_project('research').used_tokens == 48


def test_failed_model_usage_is_saved_then_retry_adds_only_delta(research):
    tracker, manager, _ = research
    project = tracker.get_project('research')
    task = manager.get_task(project.task_id)
    task.checkpoint.update({'agent_research_query_plan': {'search_query': 'finite primes'},
        'agent_research_sources': [{'title': 'Supplied test fixture', 'link': 'https://example.org/paper', 'summary': 'Not a live source'}]})
    execution = SimpleNamespace(manager=manager, check=lambda: None,
        friend_directory=lambda: tracker.db_path.parent / 'friends/12345', workspace=lambda: tracker.db_path.parent)
    def failure(*args, **kwargs):
        manager.record_token_usage(task.task_id, 31)
        raise RuntimeError('model temporarily failed')
    with patch('multi_agent_orchestrator.call_single_agent', side_effect=failure):
        with pytest.raises(RuntimeError, match='temporarily failed'):
            tracker.plan_agent_stage(task, execution, {'model_api': {'provider': 'codex'}}, lambda _: None)
    assert tracker.get_project('research').used_tokens == 31
    def success(*args, **kwargs):
        manager.record_token_usage(task.task_id, 17)
        return json.dumps({'hypothesis': '待验证', 'scope': '有界', 'report': '只分析给定摘要 [1]。', 'experiment_code': ''}), None
    with patch('multi_agent_orchestrator.call_single_agent', side_effect=success):
        tracker.plan_agent_stage(task, execution, {'model_api': {'provider': 'codex'}}, lambda _: None)
    with patch.object(ResearchTracker, 'get_instance', return_value=tracker):
        manager._record_research_attempt(task, {'output': '测试产物，不作为结论'})
    assert tracker.get_project('research').used_tokens == 48


def test_next_stage_budget_accounts_current_task_before_queueing(research):
    tracker, manager, _ = research
    project = tracker.get_project('research')
    project.current_stage = 2
    project.milestones[0].status = 'ACHIEVED'
    project.milestones[0].evidence = json.dumps({'sources': ['test'], 'evidence': ['verified fixture'], 'reviewed_by': 'test'})
    tracker._save_project(project)
    manager.record_token_usage(project.task_id, 75)
    tid = tracker.schedule_next_stage(manager, 'research', owner_id='12345')
    assert manager.get_task(tid).token_budget == project.budget_tokens - 75
    assert tracker.get_project('research').used_tokens == 75


def test_budget_is_checked_before_any_experiment(research):
    tracker, manager, _ = research
    project = tracker.get_project('research')
    project.budget_tokens = 20
    tracker._save_project(project)
    manager.record_token_usage(project.task_id, 20)
    result = tracker.run_autonomous_stage_cycle('research', 1, manager)
    assert result['status'] == 'BUDGET_EXHAUSTED'
    assert not tracker.list_experiments('research')


def test_prompt_context_is_bounded_with_negative_evidence_and_record_ids(research):
    tracker, _, _ = research
    project = tracker.get_project('research')
    project.global_verified_findings = [('finding' + str(i)) * 500 for i in range(200)]
    project.failed_hypotheses = [('negative' + str(i)) * 500 for i in range(200)]
    tracker._save_project(project)
    with tracker._get_connection() as db:
        db.executemany('INSERT INTO research_evidence VALUES (?,?,?,?,?,?,?,?)',
            [('research', 1, 'finding'+str(i), 'inconclusive', '["source"]', '"'+'data'*5000+'"', '', str(i).zfill(4)) for i in range(200)])
    context = tracker.build_research_context_for_prompt('research')
    assert len(context) < 14000
    assert 'negative199' in context and 'finding199' in context
    assert '证据 200' in context and '不能把历史自述当成验证通过' in context
    assert 'negative0negative0' not in context


def test_research_detail_exposes_receipt_and_distinguishes_support_from_execution():
    from house_qt.autonomy import describe_record
    text = describe_record('task', {'title': '研究', 'status': 'PENDING', 'description': '实验'}, research={
        'status': 'WAITING_VERIFICATION', 'hypotheses_count': 1, 'experiments_count': 1,
        'used_tokens': 31, 'budget_tokens': 100, 'recent_experiments': [
            {'experiment_id': 'exp_demo', 'tool_status': 'RUNNING', 'scope': '整数2至30', 'claim_supported': False}]})
    assert '31/100' in text and '验收范围：整数2至30' in text
    assert '续作不会自动重复执行' in text and '尚无可核验的结论支持' in text
    assert 'WAITING_VERIFICATION' not in text and 'RUNNING' not in text
