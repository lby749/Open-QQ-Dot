"""Quota: retries must not repeat a failure blindly, and failures a retry cannot fix are not retried."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_settings
from task_manager import TaskManager, TaskStatus, _model_json, _retry_note


class RetryQuotaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        friend = root / 'friends' / '10001'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '10001'}), 'utf-8')
        self.manager = TaskManager(db_path=root / 'tasks.sqlite3', workspace=root)

    def tearDown(self):
        self.manager.stop_worker()
        self._tmp.cleanup()

    def run_once(self, executor, **kwargs):
        self.manager.register_custom_executor('general', executor)
        task_id = self.manager.create_task('t', 'do it', category='general', owner_id='10001', max_retries=2, **kwargs)
        with patch.object(self.manager, '_notify_completion'):
            self.manager._process_single_task(self.manager.get_task(task_id))
        return self.manager.get_task(task_id)

    def test_permission_and_missing_component_fail_without_retry(self):
        for error in (PermissionError('未授权'), ModuleNotFoundError("No module named 'playwright'")):
            calls = []

            def executor(task, log, error=error):
                calls.append(1)
                raise error
            task = self.run_once(executor)
            self.assertEqual((task.status, task.retry_count, len(calls)), (TaskStatus.FAILED, 0, 1))

    def test_ordinary_failure_is_retried_with_its_reason(self):
        task = self.run_once(lambda task, log: (_ for _ in ()).throw(RuntimeError('预期文件未创建: README.md')))
        self.assertEqual((task.status, task.retry_count), (TaskStatus.PENDING, 1))
        note = _retry_note(task)
        self.assertIn('上一次尝试未通过验收', note)
        self.assertIn('README.md', note)
        task.retry_count = 0
        self.assertEqual(_retry_note(task), '')  # a first attempt carries no note

    def test_unrunnable_test_command_goes_to_the_owner_not_a_retry(self):
        from task_verifier import TaskVerifier
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'a.py').write_text('x = 1\n')
            result = TaskVerifier.verify(category='code', task_info={'test_command': 'no_such_program_qq_xyz -q'},
                                         result={'modified_files': ['a.py']}, workspace=Path(folder))
        self.assertEqual((result.success, result.needs_approval, result.verifier_type),
                         (False, True, 'test_command_unrunnable'))

    def test_code_run_that_changed_nothing_is_not_auto_completed(self):
        from task_verifier import TaskVerifier
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
            result = TaskVerifier.verify(category='code', task_info={'target_files': ['calc.py']},
                                         result={'modified_files': []}, workspace=Path(folder))
        self.assertEqual((result.success, result.needs_approval, result.verifier_type),
                         (False, True, 'code_no_change'))

    def test_codex_fast_is_off_unless_chosen(self):
        self.assertEqual(codex_settings.generation_settings({})[2], 'default')
        self.assertEqual(codex_settings.generation_settings({'codex_fast': True})[2], 'fast')



class ModelJsonTests(unittest.TestCase):
    def test_raw_line_breaks_inside_strings_are_accepted(self):
        # gpt-oss-120b's research plans: a strict parse failed and each failure cost a full retry.
        reply = '```json\n{"hypothesis": "第一行\n第二行", "scope": "a\tb"}\n```'
        self.assertEqual(_model_json(reply), {'hypothesis': '第一行\n第二行', 'scope': 'a\tb'})


if __name__ == '__main__':
    unittest.main()
