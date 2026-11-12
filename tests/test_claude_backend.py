import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import claude_backend
import main
from store import FriendStore


def stream(*events):
    return SimpleNamespace(stdout='\n'.join(json.dumps(event, ensure_ascii=False) for event in events), stderr='')


RESULT = {'type': 'result', 'is_error': False, 'result': 'Good night!', 'session_id': 's1'}


class ClaudeBackendTests(unittest.TestCase):
    def test_compaction_epoch_is_known_only_from_successful_owned_streams(self):
        session='unit-compaction-session'
        self.assertIsNone(claude_backend.session_compactions(session))
        result={**RESULT,'session_id':session}
        self.run_backend([result])
        self.assertEqual(claude_backend.session_compactions(session),0)
        self.run_backend([{'type':'system','subtype':'compact_boundary'},result],session=session)
        self.assertEqual(claude_backend.session_compactions(session),1)

    """2026-10-12 user: Claude through the user's own account, like Codex."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.which = patch('shutil.which', return_value='claude')
        self.which.start()
        self.addCleanup(self.which.stop)

    def run_backend(self, events, **kwargs):
        with patch('claude_backend.run_process', return_value=stream(*events)) as run:
            value = claude_backend.generate('p', self.directory, kwargs.pop('session', None), 'opus', 'low', **kwargs)
        return value, run.call_args

    def test_isolated_account_no_builtin_tools_and_friend_mcp(self):
        (text, session, searches, calls), call = self.run_backend([RESULT])
        self.assertEqual((text, session, searches, calls), ('Good night!', 's1', [], []))
        args, env = call.args[0], call.kwargs['env']
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(claude_backend.CONFIG_DIR))  # never the user's own ~/.claude
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(args[args.index('--effort') + 1], 'low')
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(set(json.loads(args[args.index('--mcp-config') + 1])['mcpServers']),
                         {'qq_topic_library', 'qq_phone'})
        self.assertNotIn('--resume', args)
        sent = json.loads(call.kwargs['input'])
        self.assertEqual(sent['message']['content'][0], {'type': 'text', 'text': 'p'})

    def test_resume_search_and_failure(self):
        events = [{'type': 'assistant', 'message': {'content': [
                      {'type': 'tool_use', 'id': 't1', 'name': 'WebSearch', 'input': {'query': '寿来 梗'}}]}},
                  {'type': 'user', 'message': {'content': [
                      {'type': 'tool_result', 'tool_use_id': 't1', 'content': [{'type': 'text',
                                                                                'text': 'see https://example.com/a'}]}]}},
                  RESULT]
        (_, _, searches, calls), call = self.run_backend(events, session='s0', web_search=True, compact_window=300000)
        args = call.args[0]
        self.assertEqual(args[args.index('--resume') + 1], 's0')
        self.assertEqual(set(args[args.index('--tools') + 1].split(',')), {'WebSearch', 'WebFetch'})
        self.assertEqual(call.kwargs['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '300000')
        self.assertEqual(searches, [{'query': '寿来 梗', 'results': [{'url': 'https://example.com/a'}]}])
        self.assertEqual(calls[0]['name'], 'WebSearch')
        with self.assertRaisesRegex(RuntimeError, '没登录'):
            self.run_backend([{'type': 'result', 'is_error': True, 'result': 'Not logged in · Please run /login'}])

    def test_settings_default_and_validation(self):
        self.assertEqual(claude_backend.settings({}), ('claude-sonnet-5-5', 'medium'))
        with self.assertRaises(ValueError):
            claude_backend.settings({'claude_effort': 'huge'})

    def test_friend_session_continues_and_provider_switch_starts_fresh(self):
        store = FriendStore(self.directory)
        self.addCleanup(store.close)
        store.set('thread_id', 'codex-thread')
        store.set('codex_session', json.dumps({'thread': 'codex-thread', 'sent': [], 'settings': {}, 'last_seq': 0}))
        profile = {'model_api': {'provider': 'claude'}}
        seen = []

        def fake(prompt, directory, session_id, model, effort, **kwargs):
            seen.append(session_id)
            return '好', 'claude-session', [], []
        with patch.object(claude_backend, 'generate', side_effect=fake), \
                patch.object(main, 'search_budget', return_value=5):
            first = main.session_codex_text(store, '【TASK】 SOURCE=task_settings TYPE=RULE\n任务', self.directory, 'normal reply',
                                            profile, record_input=lambda *a: None)
            store.set('thread_id', first[1])
            main.session_codex_text(store, '【TASK】 SOURCE=task_settings TYPE=RULE\n任务', self.directory, 'normal reply',
                                    profile, record_input=lambda *a: None)
        self.assertEqual(seen, [None, 'claude-session'])  # the Codex thread was not resumed as Claude
        self.assertEqual(store.get('session_provider'), 'claude')

    def test_cli_path_custom_and_fallback(self):
        fake_bin = self.directory / 'claude.cmd'
        fake_bin.write_text('@echo off\necho 1.0.0')
        # Direct file path
        self.assertEqual(claude_backend.cli_path(str(fake_bin)), str(fake_bin.resolve()))
        # Directory path containing claude.cmd
        self.assertEqual(claude_backend.cli_path(str(self.directory)), str(fake_bin.resolve()))
        # Non-existent custom path falls back to PATH/candidates
        with patch('shutil.which', return_value='found-claude'):
            self.assertEqual(claude_backend.cli_path('Z:/nonexistent/claude.exe'), 'found-claude')

    def test_permission_read_only_disallows_bash_and_write(self):
        (self.directory / 'friend.json').write_text(json.dumps({
            'computer_tools': {'enabled': True, 'mode': 'read_only', 'commands': True}
        }), encoding='utf-8')
        _, call = self.run_backend([RESULT])
        args = call.args[0]
        tools_idx = args.index('--tools')
        self.assertEqual(args[tools_idx + 1], '')
        self.assertIn('--mcp-config', args)
        disallow_idx = args.index('--disallowedTools')
        self.assertEqual(set(args[disallow_idx + 1].split(',')), {'Bash','Edit','Write','Read','Glob','Grep'})

    def test_permission_commands_disabled_disallows_bash(self):
        (self.directory / 'friend.json').write_text(json.dumps({
            'computer_tools': {'enabled': True, 'mode': 'read_write', 'commands': False}
        }), encoding='utf-8')
        _, call = self.run_backend([RESULT])
        args = call.args[0]
        tools_idx = args.index('--tools')
        self.assertNotIn('Bash', args[tools_idx + 1].split(','))
        self.assertNotIn('Write', args[tools_idx + 1].split(','))
        self.assertIn('--mcp-config', args)
        disallow_idx = args.index('--disallowedTools')
        self.assertEqual(set(args[disallow_idx + 1].split(',')), {'Bash','Edit','Write','Read','Glob','Grep'})

    def test_permission_full_access_allows_all(self):
        (self.directory / 'friend.json').write_text(json.dumps({
            'computer_tools': {'enabled': True, 'mode': 'read_write', 'commands': True, 'full_computer_access': True}
        }), encoding='utf-8')
        _, call = self.run_backend([RESULT])
        args = call.args[0]
        tools_idx = args.index('--tools')
        self.assertIn('Bash', args[tools_idx + 1].split(','))
        self.assertNotIn('--disallowedTools', args)


if __name__ == '__main__':
    unittest.main()
