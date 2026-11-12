"""Regressions for defects that only the live Codex run (tools/e2e_live.py) exposed.

1. MCP tool-server scripts were looked up under QQ_BRIDGE_HOME (the data home) instead of the code
   folder, so any separate data home started Codex with no tools at all.
2. In scoped mode Codex's own approval for our MCP tools, under approvalPolicy "never", declined every
   desk_write_file / desk_run_command call. Our server enforces permissions and owner approvals itself.
3. Seeing a read-only sandbox, the model concluded it could not write; the instructions now say the
   desk tools are the permitted way to modify the workspace.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class _Captured(Exception):
    pass


class LiveRunFindingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name)  # a data home with no copy of the code
        self.friend = self.home / 'friends' / '10001'
        (self.friend / 'workspace').mkdir(parents=True)
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': '10001', 'computer_tools': {
            'enabled': True, 'commands': True, 'project_path': str(self.friend / 'workspace')}}), encoding='utf-8')
        self._env = patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _codex_thread_config(self):
        from app_server import CodexAppServer
        server = CodexAppServer(compact_limit=0)
        captured = {}

        def request(method, params, deadline):
            captured[method] = params
            raise _Captured

        with patch.object(server, 'start'), patch.object(server, '_request', side_effect=request):
            with self.assertRaises(_Captured):
                server.generate('fix calc.py', self.friend, None)
        return captured['thread/start']

    def test_codex_tool_servers_start_from_the_code_folder_and_are_pre_approved(self):
        config = self._codex_thread_config()
        servers = config['config']['mcp_servers']
        for name, script in (('qq_topic_library', 'topic_library.py'), ('qq_phone', 'phone_tools.py')):
            self.assertEqual(Path(servers[name]['args'][3]), ROOT / script)
            self.assertTrue(Path(servers[name]['args'][3]).is_file())
            self.assertEqual(servers[name]['default_tools_approval_mode'], 'approve')
            # Codex passes MCP servers a whitelisted environment; the house must be named explicitly.
            self.assertEqual(servers[name]['env']['QQ_BRIDGE_HOME'], str(self.home))
        self.assertEqual(config['sandbox'], 'read-only')  # scoped mode: writes only through our tools
        self.assertIn('desk_write_file', config['baseInstructions'])
        self.assertIn('desk_run_command', config['baseInstructions'])

    def test_claude_and_mailbox_tool_servers_start_from_the_code_folder(self):
        import claude_backend
        from agent_mailbox_mcp import server_config
        servers = json.loads(claude_backend._mcp_config(self.friend))['mcpServers']
        self.assertEqual(Path(servers['qq_topic_library']['args'][3]), ROOT / 'topic_library.py')
        for name in ('qq_topic_library', 'qq_phone'):
            self.assertEqual(servers[name]['env']['QQ_BRIDGE_HOME'], str(self.home))
        entry = server_config('10001/shop/a')
        self.assertEqual(Path(entry['args'][3]), ROOT / 'agent_mailbox_mcp.py')
        self.assertEqual(entry['env']['QQ_BRIDGE_HOME'], str(self.home))  # data still comes from the home

    def test_work_tasks_get_only_work_tools(self):
        # The phone, chat-log and memory tools were 7 of 13 definitions in every code-task prompt.
        import api_backend
        import claude_backend
        import task_runtime

        def names():
            return {tool['name'] for tool in api_backend._tools(self.friend)}

        chat_tools = {'phone_now', 'app_history', 'search_friend_memory', 'get_today_chat'}
        self.assertTrue(chat_tools <= names())  # a chat reply keeps them
        with patch.object(task_runtime, 'work_task', return_value=True):
            self.assertFalse(chat_tools & names())
            self.assertTrue({'desk_read_file', 'desk_write_file', 'search_topic_library'} <= names())
            codex = self._codex_thread_config()['config']['mcp_servers']
            claude = json.loads(claude_backend._mcp_config(self.friend))['mcpServers']
        for servers in (codex, claude):
            self.assertNotIn('qq_phone', servers)
            self.assertEqual(servers['qq_topic_library']['env']['QQ_TASK_KIND'], 'work')  # its own process filters
        with patch.dict(os.environ, {'QQ_TASK_KIND': 'work'}):
            self.assertFalse({'search_friend_memory', 'get_today_chat'} & names())

    def test_tool_refusal_reaches_the_model_as_a_tool_error(self):
        # Live run (gpt-6-luna): a hidden "File already exists" made the model answer that it had no tools.
        import io
        import sys
        import topic_library
        call = {'jsonrpc': '2.0', 'method': 'tools/call',
                'params': {'name': 'desk_write_file', 'arguments': {'path': 'a.txt', 'text': 'x'}}}
        lines = ''.join(json.dumps({**call, 'id': n}) + '\n' for n in (1, 2))
        stdin, stdout = io.TextIOWrapper(io.BytesIO(lines.encode())), io.TextIOWrapper(io.BytesIO())
        with patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', stdout):
            topic_library.serve(self.friend)
            stdout.flush()
            replies = [json.loads(line) for line in stdout.buffer.getvalue().decode('utf-8').splitlines()]
        self.assertFalse(replies[0]['result']['isError'])
        self.assertTrue((self.friend / 'workspace' / 'a.txt').is_file())
        self.assertTrue(replies[1]['result']['isError'])
        self.assertIn('overwrite', replies[1]['result']['content'][0]['text'])

    def test_expected_text_in_the_page_title_counts(self):
        # Live run: example.com's "Example Domain" is only in <title>; both checks rejected the real page.
        from PIL import Image
        from browser_automation import _page_shows
        from task_verifier import TaskVerifier
        self.assertTrue(_page_shows({'title': 'Example Domain', 'content': 'This domain is for use in docs'},
                                    'Example Domain'))
        shot = self.home / 'page.png'
        Image.new('RGB', (200, 200)).save(shot)
        receipt = {'url': 'https://example.com/', 'title': 'Example Domain', 'content': 'This domain is for use',
                   'screenshot': str(shot), 'expected_text': 'Example Domain', 'actions': []}
        result = TaskVerifier.verify(category='browser', task_info={},
                                     result={'browser_receipt': receipt, 'artifacts': [str(shot)]}, workspace=self.home)
        self.assertNotIn('不匹配', result.details)

    def test_read_only_friends_are_not_told_to_write(self):
        import workspace_tools
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': '10001', 'computer_tools': {
            'enabled': True, 'mode': 'read_only'}}), encoding='utf-8')
        self.assertNotIn('desk_write_file', workspace_tools.instructions(self.friend))


if __name__ == '__main__':
    unittest.main()
