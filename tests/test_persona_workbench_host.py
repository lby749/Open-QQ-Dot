import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import persona_workbench_host as host


class PersonaWorkbenchHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        directory = self.root / 'friends' / '123456'
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text(json.dumps({'qq_id': '123456'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'123456': {
            'qq_id': '123456', 'directory': str(directory)}}}), encoding='utf-8')
        host._children.clear()
        self.addCleanup(host._children.clear)

    def test_launch_starts_one_window_per_friend_and_tool(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(host.subprocess, 'Popen', return_value=process) as popen:
            self.assertTrue(host.launch('knowledge', '123456', self.root))
            self.assertFalse(host.launch('knowledge', '123456', self.root))  # still open
            self.assertTrue(host.launch('retrieval', '123456', self.root))
            process.poll.return_value = 0
            self.assertTrue(host.launch('knowledge', '123456', self.root))  # closed: opens again
        self.assertEqual(popen.call_count, 3)
        args = popen.call_args_list[0].args[0]
        self.assertEqual(Path(args[1]).name, 'persona_workbench_host.py')
        self.assertEqual(args[2:], ['--tool', 'knowledge', '--friend-id', '123456', '--project-root', str(self.root)])
        self.assertEqual(popen.call_args_list[0].kwargs['env']['QQ_BRIDGE_HOME'], str(self.root))
        with self.assertRaisesRegex(ValueError, '未知'):
            host.launch('shell', '123456', self.root)

    def test_job_workbench_uses_the_plain_friend_scope(self):
        scope = host.scope_for('jobs', '123456', self.root)
        self.assertEqual((scope.friend_id, scope.allowed_pack_versions), ('123456', frozenset()))

    def test_friend_without_persona_data_gets_an_empty_store_first(self):
        # Regression: these workbenches raised "persona sidecar is absent" until a persona job had run.
        sidecar = self.root / 'friends' / '123456' / 'persona.sqlite3'
        self.assertFalse(sidecar.exists())
        for tool in ('friend', 'knowledge', 'feedback', 'retrieval'):
            scope = host.scope_for(tool, '123456', self.root)
            self.assertEqual(scope.friend_id, '123456')
        self.assertTrue(sidecar.is_file())

    def test_scope_rejects_an_unregistered_friend(self):
        with self.assertRaises(Exception):
            host.scope_for('friend', '999999', self.root)

    def test_build_uses_each_workbench_with_the_given_scope(self):
        frame = Mock()
        create = Mock(return_value=frame)
        scope = SimpleNamespace(friend_id='123456', project_root=self.root)
        with patch.dict(sys.modules, {'persona_feedback_gui': SimpleNamespace(create_workbench=create)}):
            self.assertIs(host.build('feedback', 'window', scope), frame)
        create.assert_called_once_with('window', scope)
        frame.pack.assert_called_once_with(fill='both', expand=True)
        controller = Mock()
        with patch.dict(sys.modules, {'retrieval_debug_gui': SimpleNamespace(
                RetrievalDebugWindow=Mock(return_value=controller))}):
            self.assertIs(host.build('retrieval', 'window', scope), controller)
        controller.change_friend.assert_called_once_with('123456')
        controller.friend_box.configure.assert_called_once_with(state='disabled')


if __name__ == '__main__':
    unittest.main()
