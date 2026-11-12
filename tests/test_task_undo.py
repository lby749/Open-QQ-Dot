import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_undo
from task_manager import TaskManager, TaskStatus


class TaskUndoJournalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.work = self.root / 'work'
        (self.work / 'src').mkdir(parents=True)
        (self.work / 'src' / 'keep.py').write_text('keep')
        (self.work / 'src' / 'edit.py').write_text('original')
        (self.work / 'gone.txt').write_text('to be deleted')

    def run_task(self, task_id='task_1', prune=True):
        task_undo.begin(self.root, task_id, self.work)
        (self.work / 'src' / 'edit.py').write_text('changed by task')
        (self.work / 'src' / 'new.py').write_text('created by task')
        (self.work / 'gone.txt').unlink()
        return task_undo.finish(self.root, task_id, prune=prune)

    def test_undo_reverses_created_modified_and_deleted_files(self):
        journal = self.run_task()
        self.assertEqual({k: v['kind'] for k, v in journal['changes'].items()},
                         {'src/edit.py': 'modified', 'src/new.py': 'created', 'gone.txt': 'deleted'})
        # backups of untouched files are pruned once the task is final
        self.assertFalse((task_undo.journal_dir(self.root, 'task_1') / 'before' / 'src' / 'keep.py').exists())
        summary = task_undo.summary(self.root, 'task_1')
        self.assertEqual((summary['created'], summary['modified'], summary['deleted']),
                         (['src/new.py'], ['src/edit.py'], ['gone.txt']))
        report = task_undo.undo(self.root, 'task_1')
        self.assertEqual(sorted(report['restored']), ['gone.txt', 'src/edit.py'])
        self.assertEqual(report['removed'], ['src/new.py'])
        self.assertEqual((self.work / 'src' / 'edit.py').read_text(), 'original')
        self.assertEqual((self.work / 'gone.txt').read_text(), 'to be deleted')
        self.assertFalse((self.work / 'src' / 'new.py').exists())
        aside = Path(report['kept_aside'])  # nothing is destroyed: the task's versions are kept
        self.assertEqual((aside / 'src' / 'new.py').read_text(), 'created by task')
        self.assertEqual((aside / 'src' / 'edit.py').read_text(), 'changed by task')
        with self.assertRaisesRegex(RuntimeError, '已经撤销'):
            task_undo.undo(self.root, 'task_1')
        self.assertTrue(task_undo.summary(self.root, 'task_1')['undone'])

    def test_browser_profile_and_retrieval_index_are_not_task_output(self):
        # Live run: undoing a web task would have deleted the friend's 223-file browser profile (logins).
        task_undo.begin(self.root, 'task_web', self.work)
        (self.work / 'data' / 'browser_sessions' / '10001').mkdir(parents=True)
        (self.work / 'data' / 'browser_sessions' / '10001' / 'Cookies').write_text('login')
        (self.work / 'data' / 'retrieval_engine.sqlite3').write_text('index')
        (self.work / 'data' / 'result.csv').write_text('a,b')  # a project's own data/ still counts
        journal = task_undo.finish(self.root, 'task_web', prune=True)
        self.assertEqual(set(journal['changes']), {'data/result.csv'})

    def test_files_edited_after_the_task_are_left_alone(self):
        self.run_task()
        (self.work / 'src' / 'edit.py').write_text('user kept working')
        report = task_undo.undo(self.root, 'task_1')
        self.assertEqual((self.work / 'src' / 'edit.py').read_text(), 'user kept working')
        self.assertTrue(any(item.startswith('src/edit.py') for item in report['refused']))
        self.assertFalse((self.work / 'src' / 'new.py').exists())

    def test_baseline_survives_resumed_runs(self):
        self.run_task(prune=False)
        task_undo.begin(self.root, 'task_1', self.work)  # resumed run keeps the first baseline
        (self.work / 'src' / 'keep.py').write_text('changed on the second run')
        task_undo.finish(self.root, 'task_1', prune=True)
        task_undo.undo(self.root, 'task_1')
        self.assertEqual((self.work / 'src' / 'keep.py').read_text(), 'keep')

    def test_large_workspace_can_only_remove_created_files(self):
        with patch.object(task_undo, 'MAX_BACKUP_FILES', 1):
            self.run_task()
        summary = task_undo.summary(self.root, 'task_1')
        self.assertTrue(summary['limited'])
        report = task_undo.undo(self.root, 'task_1')
        self.assertEqual(report['removed'], ['src/new.py'])
        self.assertEqual((self.work / 'src' / 'edit.py').read_text(), 'changed by task')
        self.assertEqual(len(report['refused']), 2)

    def test_task_ids_cannot_escape_the_journal_folder(self):
        for bad in ('../x', 'a/b', 'c:\\d', ''):
            with self.assertRaises(ValueError):
                task_undo.journal_dir(self.root, bad)


class TaskManagerUndoTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'data').mkdir()
        self.manager = TaskManager(db_path=self.root / 'data' / 'tasks.sqlite3', workspace=self.root)

    def tearDown(self):
        self.manager.stop_worker()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_real_run_is_journaled_and_undone_only_by_its_owner_after_it_ends(self):
        (self.root / 'notes.txt').write_text('before')

        def executor(task, log):
            (self.root / 'notes.txt').write_text('after')
            (self.root / 'report.md').write_text('# report\n' + 'word ' * 80)
            return {'output': 'done ' * 80, 'usage_recorded': True}

        self.manager.register_custom_executor('general', executor)
        tid = self.manager.create_task('write report', 'write a report', category='general', owner_id='default',
                                       max_retries=0)
        self.manager._process_single_task(self.manager.get_task(tid))
        task = self.manager.get_task(tid)
        self.assertIn(task.status, (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.WAITING_APPROVAL))
        if task.status == TaskStatus.WAITING_APPROVAL:
            self.manager.reject_task(tid, owner_id='default', reason='test')
        summary = task_undo.summary(self.root, tid)
        self.assertEqual((summary['created'], summary['modified']), (['report.md'], ['notes.txt']))
        with self.assertRaises(PermissionError):
            self.manager.undo_task(tid, 'someone_else')
        self.manager.undo_task(tid, 'default')
        self.assertEqual((self.root / 'notes.txt').read_text(), 'before')
        self.assertFalse((self.root / 'report.md').exists())
        self.assertTrue(self.manager.get_task(tid).checkpoint.get('undone'))
        self.assertTrue(any(log['step_name'] == 'Undo' for log in self.manager.get_task_logs(tid)))

    def test_unfinished_task_must_stop_first(self):
        tid = self.manager.create_task('t', 'd', category='general', owner_id='default')
        with self.assertRaisesRegex(RuntimeError, '尚未结束'):
            self.manager.undo_task(tid, 'default')

    def test_system_tasks_are_not_journaled(self):
        self.manager.register_custom_executor('general', lambda task, log: {'output': 'x ' * 80})
        tid = self.manager.create_task('t', 'd', category='general', owner_id='system:test', max_retries=0)
        self.manager._process_single_task(self.manager.get_task(tid))
        self.assertFalse(task_undo.summary(self.root, tid)['available'])


if __name__ == '__main__':
    unittest.main()
