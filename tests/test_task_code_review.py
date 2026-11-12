"""Post-delivery checks run through the actual verifier and durable worker."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from task_code_review import review_instruction, review_sources, describe_review
from task_manager import TaskManager, TaskStatus
from task_verifier import TaskVerifier


class CodeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, name, text):
        (self.root / name).write_text(text, encoding='utf-8')
        return name

    def verify(self, name, category='code'):
        return TaskVerifier.verify(category, {'target_files': [name]},
                                   {'modified_files': [name]}, self.root)

    def test_tests_passing_does_not_hide_injection_risk(self):
        self.write('app.py', 'def calculate(value):\n    return eval(value)\n')
        with patch('task_runtime.run_process') as run:
            run.return_value.returncode = 0
            verdict = TaskVerifier.verify('code', {'target_files': ['app.py'], 'test_command': 'test'},
                                          {'modified_files': ['app.py']}, self.root)
        self.assertFalse(verdict.success)
        self.assertTrue(verdict.needs_approval)
        self.assertEqual(verdict.verifier_type, 'code_audit_review')
        self.assertEqual(verdict.code_review['findings'][0]['line'], 2)
        self.assertEqual(verdict.code_review['findings'][0]['kind'], 'security')

    def test_correcting_real_bug_and_risk_clears_review(self):
        name = self.write('app.py', 'def collect(x, values=[]):\n    values.append(x)\n    return values\n')
        self.assertEqual(self.verify(name).code_review['findings'][0]['kind'], 'bug')
        self.write(name, 'def collect(x, values=None):\n    return (list(values) if values else []) + [x]\n')
        verdict = self.verify(name)
        self.assertTrue(verdict.success)
        self.assertEqual(verdict.code_review['status'], 'checked')
        self.assertIn('有限静态', verdict.details)

    def test_safe_yaml_loader_and_disabled_shell_are_not_flagged(self):
        name = self.write('app.py', 'import yaml, subprocess\nyaml.load("{}", Loader=yaml.SafeLoader)\n'
                          'subprocess.run(["hello"], shell=False)\n')
        self.assertTrue(self.verify(name).success)

    def test_shell_tls_and_unsafe_yaml_have_line_evidence(self):
        name = self.write('app.py', 'import yaml, requests, subprocess\nyaml.load(data)\n'
                          'requests.get(url, verify=False)\nsubprocess.run(command, shell=True)\n')
        items = self.verify(name).code_review['findings']
        self.assertEqual({item['line'] for item in items}, {2, 3, 4})

    def test_valid_static_page_passes(self):
        name = self.write('index.html', '<html><head><title>学习平台</title>'
                          '<meta name="description" content="学习资料"></head><body>内容</body></html>')
        verdict = self.verify(name)
        self.assertTrue(verdict.success)
        self.assertIn('已检查静态', verdict.code_review['seo_status'])

    def test_missing_metadata_is_exposed(self):
        name = self.write('index.html', '<html><head><title> </title></head><body>内容</body></html>')
        verdict = self.verify(name)
        self.assertFalse(verdict.success)
        self.assertEqual(len(verdict.code_review['findings']), 2)
        self.assertTrue(all(item['kind'] == 'seo' for item in verdict.code_review['findings']))

    def test_malformed_meta_and_deleted_source_do_not_claim_a_clean_review(self):
        name = self.write('index.html', '<html><head><meta name><meta name="description" content></head></html>')
        self.assertTrue(self.verify(name).needs_approval)
        deleted = review_sources(self.root, ['deleted.py'])
        self.assertEqual(deleted['status'], 'needs_review')
        self.assertIn('未进行内容检查', describe_review(deleted))

    def test_private_noindex_is_reported_without_changing_it(self):
        html = '<html><head><title>私人控制台</title><meta name="description" content="管理">' \
               '<meta name="robots" content="noindex"></head><body></body></html>'
        name = self.write('index.html', html)
        verdict = self.verify(name)
        self.assertTrue(verdict.needs_approval)
        self.assertIn('内部或隐私页面可保留', verdict.details)
        self.assertEqual((self.root / name).read_text('utf-8'), html)

    def test_components_are_not_falsely_certified_for_seo(self):
        name = self.write('page.tsx', 'export default function Page() {return <h1>内容</h1>;}')
        verdict = self.verify(name)
        self.assertTrue(verdict.needs_approval)
        self.assertIn('需补渲染检查', verdict.details)

    def test_report_does_not_echo_credentials(self):
        credential = 'sensitive_' + 'x' * 24
        name = self.write('app.py', 'api_key = "' + credential + '"\n')
        report = self.verify(name).to_dict()
        self.assertTrue(report['code_review']['findings'])
        self.assertNotIn(credential, json.dumps(report))

    def test_code_delivered_as_file_is_also_checked(self):
        name = self.write('helper.py', 'exec(user_input)\n')
        verdict = self.verify(name, category='file')
        self.assertFalse(verdict.success)
        self.assertTrue(verdict.needs_approval)

    def test_non_code_files_and_chat_get_no_extra_review_instruction(self):
        name = self.write('notes.txt', '普通文档内容')
        self.assertTrue(self.verify(name, 'file').success)
        self.assertEqual(review_instruction('general'), '')
        self.assertEqual(review_instruction('agent_message'), '')
        self.assertIn('交付前自审', review_instruction('code'))

    def test_oversized_source_is_not_silently_passed(self):
        name = self.write('large.js', '// large\n' + ' ' * 260_000)
        verdict = self.verify(name)
        self.assertTrue(verdict.needs_approval)
        self.assertIn('未检查', verdict.details)

    def test_unimplemented_language_analyzer_is_explicit(self):
        name = self.write('main.go', 'package main\nfunc main() {}\n')
        verdict = self.verify(name)
        self.assertTrue(verdict.needs_approval)
        self.assertIn('尚无对应语言', verdict.details)

    def test_static_report_size_is_bounded(self):
        name = self.write('many.js', 'eval(userInput);\n' * 205)
        report = review_sources(self.root, [name])
        self.assertEqual(len(report['findings']), 200)
        self.assertIn('仅保留前 200 项', describe_review(report))

    def test_outside_project_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / 'other.py'
            outside.write_text('eval(secret)\n', encoding='utf-8')
            report = review_sources(self.root, [str(outside)])
        self.assertEqual(report['files_checked'], [])
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['status'], 'needs_review')

    def test_worker_preserves_findings_and_does_not_mark_risky_delivery_completed(self):
        manager = TaskManager(db_path=self.root / 'tasks.sqlite3', workspace=self.root)
        self.addCleanup(manager.stop_worker)
        tid = manager.create_task(title='交付代码', description='生成计算代码', category='code', owner_id='default')

        def deliver(task, progress):
            # Execution.workspace() for owner default is the manager workspace.
            self.write('app.py', 'def calculate(text):\n    return eval(text)\n')
            return {'output': '文件已写入', 'modified_files': ['app.py'], 'target_files': ['app.py']}

        manager._task_executors['code'] = deliver
        with patch.object(manager, '_undo_begin', return_value=False):
            manager._process_single_task(manager.get_task(tid))
        task = manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.WAITING_APPROVAL)
        self.assertEqual(task.verification_info['code_review']['findings'][0]['kind'], 'security')
        restored = TaskManager(db_path=self.root / 'tasks.sqlite3', workspace=self.root)
        self.addCleanup(restored.stop_worker)
        self.assertEqual(restored.get_task(tid).verification_info, task.verification_info)
        self.assertIn('app.py:2', describe_review(task.verification_info['code_review']))


if __name__ == '__main__':
    unittest.main()
