"""The QQ task loop end to end: spec -> run -> park on approval -> decide from QQ -> resume -> deliver.

No mocked managers: real TaskManager worker, ApprovalManager and NotificationManager on temp
databases, a real second OS process deciding an approval, and the real QQ command handler. Only
the QQ transport is a capturing sink, and the task's work is a small custom executor.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from approval_manager import ApprovalCategory, ApprovalManager, ApprovalPending, ApprovalStatus  # noqa: E402
from notification_manager import NotificationManager  # noqa: E402
from task_manager import TaskManager, TaskStatus  # noqa: E402
import task_spec  # noqa: E402
from task_verifier import TaskVerifier  # noqa: E402


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class LoopFixture(unittest.TestCase):
    """Temp databases installed as the process singletons the production code looks up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self._saved = (TaskManager._instance, ApprovalManager._instance, NotificationManager._instance)
        self.tasks = TaskManager(db_path=self.root / 'tasks.sqlite3', workspace=self.root)
        self.approvals = ApprovalManager(db_path=self.root / 'approvals.sqlite3')
        self.notices = NotificationManager(self.root / 'notifications.sqlite3')
        TaskManager._instance, ApprovalManager._instance, NotificationManager._instance = \
            self.tasks, self.approvals, self.notices
        self.sent = []
        self.notices.register_channel_sink(lambda msg: self.sent.append(msg) or True, name='capture')

    def tearDown(self):
        self.tasks.stop_worker()
        TaskManager._instance, ApprovalManager._instance, NotificationManager._instance = self._saved
        self._tmp.cleanup()

    def titles(self):
        return [m.title for m in self.sent]


class TaskSpecTests(unittest.TestCase):
    def test_chinese_and_english_labels_and_path_safety(self):
        spec = task_spec.build_task_spec('修复登录；测试: pytest -q tests/test_login.py；文件: app/login.py, ../secret, C:/x.py',
                                         'code', 'codex')
        self.assertEqual(spec['test_command'], 'pytest -q tests/test_login.py')
        self.assertEqual(spec['target_files'], ['app/login.py'])
        self.assertEqual(spec['backend'], 'codex')
        spec = task_spec.build_task_spec('fix parser. test: npm test --silent; files: src/parse.ts', 'code')
        self.assertEqual((spec['test_command'], spec['target_files']), ('npm test --silent', ['src/parse.ts']))

    def test_a_chinese_full_stop_ends_the_file_list(self):
        # Claude Haiku's goal stage, 2026-10-26: the sentence after the file name became five impossible targets
        spec = task_spec.build_task_spec('创建 notes.md。文件: notes.md。验收方式：notes.md 存在，且包含三条 Git 学习建议。', 'file')
        self.assertEqual(spec['target_files'], ['notes.md'])
        spec = task_spec.build_task_spec('新建 notes.md（文件: notes.md），内容为中文 Markdown，包含标题“Git 学习建议”和三条建议。', 'file')
        self.assertEqual(spec['target_files'], ['notes.md'])
        spec = task_spec.build_task_spec('文件: docs/笔记.md, Makefile, README.md', 'file')
        self.assertEqual(spec['target_files'], ['docs/笔记.md', 'Makefile', 'README.md'])
        spec = task_spec.build_task_spec('加 mul。文件: calc.py；测试: python -m pytest -q。验收：通过', 'code')
        self.assertEqual((spec['test_command'], spec['target_files']), ('python -m pytest -q', ['calc.py']))

    def test_category_specific_fields_are_only_taken_from_the_message(self):
        self.assertEqual(task_spec.build_task_spec('打开 https://example.com/a?b=1 截图', 'browser')['url'],
                         'https://example.com/a?b=1')
        self.assertNotIn('url', task_spec.build_task_spec('打开网页截图', 'browser'))
        self.assertEqual(task_spec.build_task_spec('查订单号: ORD889900 的物流', 'ecommerce')['order_id'], 'ORD889900')


class CrossProcessApprovalTests(LoopFixture):
    def test_decision_recorded_by_another_os_process_releases_the_waiter(self):
        result = {}

        def wait():
            result['ok'], result['reason'] = self.approvals.check_and_request_approval(
                't-cross', ApprovalCategory.HIGH_RISK_COMMAND, 'run deploy', {'cmd': 'deploy'},
                timeout_seconds=30, owner_id='10001')
        waiter = threading.Thread(target=wait)
        waiter.start()
        pending = wait_for(lambda: self.approvals.list_pending_requests(owner_id='10001'))
        self.assertTrue(pending)
        self.assertIn(f'需要操作确认（{pending[0].req_id}）', self.titles())
        script = textwrap.dedent(f'''
            import sys; sys.path.insert(0, {str(ROOT)!r})
            from pathlib import Path
            from approval_manager import ApprovalManager, open_identity_channel
            manager = ApprovalManager(db_path=Path({str(self.root / "approvals.sqlite3")!r}))
            channel = open_identity_channel("test-panel")
            print(manager.approve_request({pending[0].req_id!r}, approver_id=channel.identity("10001")))
        ''')
        env = dict(os.environ, QQ_BRIDGE_HOME=str(self.root))  # the child never touches the real data dir
        out = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.stdout.strip(), 'True', out.stderr)
        waiter.join(10)
        self.assertTrue(result.get('ok'), result)

    def test_once_approval_authorises_exactly_one_identical_action(self):
        args = ('t-once', ApprovalCategory.HIGH_RISK_COMMAND, 'rm build', {'cmd': 'rm build'})
        with self.assertRaises(ApprovalPending) as parked:
            self.approvals.check_and_request_approval(*args, owner_id='10001', defer_after=0.05)
        req_id = parked.exception.req_id
        # Asking again while undecided reuses the request instead of spamming the owner.
        with self.assertRaises(ApprovalPending) as again:
            self.approvals.check_and_request_approval(*args, owner_id='10001', defer_after=0.05)
        self.assertEqual(again.exception.req_id, req_id)
        import main
        self.assertIn('已批准', main.handle_approval_command(f'批准 {req_id}', '10001')[0])
        ok, _ = self.approvals.check_and_request_approval(*args, owner_id='10001', sync_wait=False)
        self.assertTrue(ok)  # consumes the approval
        ok, _ = self.approvals.check_and_request_approval(*args, owner_id='10001', sync_wait=False)
        self.assertFalse(ok)  # a second execution needs a new decision

    def test_qq_commands_cannot_decide_someone_elses_request(self):
        import main
        _, msg = self.approvals.check_and_request_approval('t-x', ApprovalCategory.HIGH_RISK_COMMAND, 'x', {},
                                                           owner_id='10001', sync_wait=False)
        req_id = msg.split('Request ID: ')[1].rstrip(')')
        self.assertIn('没有属于你的待确认请求', main.handle_approval_command(f'批准 {req_id}', '10002')[0])
        self.assertEqual(self.approvals.get_request(req_id).status, ApprovalStatus.PENDING)
        listing = main.handle_approval_command('待审批', '10001')[0]
        self.assertIn(req_id, listing)
        self.assertIn('没有等待你确认', main.handle_approval_command('pending approvals', '10002')[0])
        self.assertIsNone(main.handle_approval_command('批准一下这个想法', '10001'))


class ParkAndResumeTests(LoopFixture):
    def test_task_parks_on_approval_resumes_after_qq_decision_and_delivers_result(self):
        import main
        from task_runtime import current_execution
        runs = []

        def deploy(task, log):
            runs.append(task.task_id)
            execution = current_execution.get()
            self.approvals.check_and_request_approval(
                task.task_id, ApprovalCategory.HIGH_RISK_COMMAND, 'deploy to staging', {'target': 'staging'},
                owner_id=task.owner_id, should_stop=execution.check, defer_after=0.2)
            return {'output': json.dumps({'deployed': 'staging'})}

        self.tasks.register_custom_executor('data', deploy)
        tid = self.tasks.create_task(title='Deploy', description='deploy to staging', category='data', owner_id='10001')
        self.assertTrue(self.tasks.start_worker())
        parked = wait_for(lambda: (t := self.tasks.get_task(tid)) and t.status == TaskStatus.WAITING_APPROVAL and t)
        self.assertTrue(parked, self.tasks.get_task(tid))
        req_id = parked.checkpoint['pending_approval']
        self.assertTrue(any(t.startswith(f'需要操作确认（{req_id}）') for t in self.titles()))
        self.assertTrue(wait_for(lambda: tid not in self.tasks._in_flight_tasks, timeout=5))  # worker released

        reply = main.handle_approval_command(f'批准 {tid}', '10001')[0]  # approving the task decides its request
        self.assertIn('已批准', reply)
        done = wait_for(lambda: (t := self.tasks.get_task(tid)) and t.status == TaskStatus.COMPLETED and t)
        self.assertTrue(done, self.tasks.get_task(tid))
        self.assertEqual(len(runs), 2)
        # The notice is published right after the status commits; wait for it rather than racing it.
        completed = wait_for(lambda: [m for m in self.sent if m.title.startswith('任务已完成：Deploy')], timeout=5)
        self.assertTrue(completed, self.titles())
        self.assertIn('staging', completed[0].content)  # the result itself, not just a status line

    def test_rejected_approval_fails_the_task_and_says_so(self):
        import main

        def needs_ok(task, log):
            self.approvals.check_and_request_approval(task.task_id, ApprovalCategory.FILE_DELETE, 'delete cache', {},
                                                      owner_id=task.owner_id, defer_after=0.2)
            return {'output': '{}'}

        self.tasks.register_custom_executor('data', needs_ok)
        tid = self.tasks.create_task(title='Clean', description='delete cache', category='data', owner_id='10001')
        self.tasks.start_worker()
        parked = wait_for(lambda: (t := self.tasks.get_task(tid)) and t.status == TaskStatus.WAITING_APPROVAL and t)
        main.handle_approval_command(f"拒绝 {parked.checkpoint['pending_approval']} not now", '10001')
        failed = wait_for(lambda: (t := self.tasks.get_task(tid)) and t.status == TaskStatus.FAILED and t)
        self.assertTrue(failed)
        self.assertTrue(wait_for(lambda: any(t.startswith('任务失败：Clean') for t in self.titles()), timeout=5))

    def test_result_review_from_qq(self):
        import main
        tid = self.tasks.create_task(title='Essay', description='write', category='general', owner_id='10001')
        with self.tasks._get_connection() as db:
            db.execute("UPDATE tasks SET status='WAITING_APPROVAL', result='Draft text' WHERE task_id=?", (tid,))
        self.assertIn('没有属于你的待处理任务', main.handle_approval_command(f'reject {tid} too short', '10002')[0])
        self.assertEqual(self.tasks.get_task(tid).status, TaskStatus.WAITING_APPROVAL)
        self.assertIn('已拒绝', main.handle_approval_command(f'reject {tid} too short', '10001')[0])
        failed = self.tasks.get_task(tid)
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertIn('too short', failed.error)


class CodexApprovalAnswerTests(LoopFixture):
    """app_server used to fail the whole turn on any server request; it now asks the owner."""

    def _server(self):
        import io
        from app_server import CodexAppServer
        server = CodexAppServer(10000)
        server.process = type('P', (), {'stdin': io.StringIO()})()
        return server

    def test_command_approval_in_a_task_waits_for_the_owner_and_answers_accept(self):
        import main

        class Execution:
            task_id, owner_id = 'task_codex_1', '10001'

            def check(self):
                pass
        server = self._server()
        request = {'id': 7, 'method': 'item/commandExecution/requestApproval',
                   'params': {'threadId': 't', 'command': ['pytest', '-q'], 'cwd': '.'}}
        worker = threading.Thread(target=server._answer_server_request, args=(request, Execution()))
        worker.start()
        pending = wait_for(lambda: self.approvals.list_pending_requests(owner_id='10001'))
        self.assertIn('pytest -q', pending[0].description)
        main.handle_approval_command(f'批准 {pending[0].req_id}', '10001')
        worker.join(10)
        self.assertEqual(json.loads(server.process.stdin.getvalue()), {'id': 7, 'result': {'decision': 'accept'}})

    def test_chat_turns_decline_and_unknown_requests_get_an_error(self):
        server = self._server()
        server._answer_server_request({'id': 1, 'method': 'item/fileChange/requestApproval', 'params': {}}, None)
        server._answer_server_request({'id': 2, 'method': 'item/permissions/requestApproval', 'params': {}}, None)
        first, second = [json.loads(line) for line in server.process.stdin.getvalue().splitlines()]
        self.assertEqual(first, {'id': 1, 'result': {'decision': 'decline'}})
        self.assertEqual(second['error']['code'], -32601)


class VerifierAndWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_changed_files_without_any_objective_check_go_to_owner_review(self):
        (self.ws / 'x.py').write_text('x = 1\n', encoding='utf-8')
        result = TaskVerifier.verify('code', {}, {'modified_files': ['x.py', '__pycache__/x.cpython-313.pyc']}, self.ws)
        self.assertFalse(result.success)
        self.assertTrue(result.needs_approval)
        self.assertEqual(result.verifier_type, 'code_review_needed')

    def test_only_incidental_changes_are_not_progress(self):
        result = TaskVerifier.verify('code', {}, {'modified_files': ['__pycache__/a.pyc', 'run.log']}, self.ws)
        self.assertFalse(result.success)
        self.assertFalse(result.needs_approval)

    def test_project_tests_are_detected_and_rerun_independently(self):
        (self.ws / 'tests').mkdir()
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
        (self.ws / 'tests' / 'test_calc.py').write_text(
            'import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n'
            'from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n', encoding='utf-8')
        ok = TaskVerifier.verify('code', {}, {'modified_files': ['calc.py']}, self.ws)
        self.assertTrue(ok.success, ok.details)
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        bad = TaskVerifier.verify('code', {}, {'modified_files': ['calc.py']}, self.ws)
        self.assertFalse(bad.success)
        self.assertEqual(bad.verifier_type, 'code_automated_test')

    def test_one_worker_per_task_database(self):
        a = TaskManager(db_path=self.ws / 'tasks.sqlite3', workspace=self.ws)
        b = TaskManager(db_path=self.ws / 'tasks.sqlite3', workspace=self.ws)
        try:
            self.assertTrue(a.start_worker())
            self.assertFalse(b.start_worker())
            a.stop_worker()
            self.assertTrue(b.start_worker())
        finally:
            a.stop_worker()
            b.stop_worker()


if __name__ == '__main__':
    unittest.main()
