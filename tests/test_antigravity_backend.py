import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import antigravity_backend as ag
import main
import topic_research


class AntigravityTests(unittest.TestCase):
    def test_tool_followups_resume_and_only_send_new_results(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder); requests=[]
            responses=[{'final':'','tool_calls':[{'name':'get_today_chat','arguments':{'query':''}}]},
                       {'final':'完成','tool_calls':[]}]
            def invoke(args,**kwargs):
                requests.append((args,kwargs['input']))
                return SimpleNamespace(returncode=0,stdout=json.dumps({'event':'result','result':{
                    'status':'SUCCESS','conversation_id':'known-conversation','structured_output':responses[len(requests)-1]}}))
            with patch.object(ag,'ROOT',directory),patch.object(ag,'cli_path',return_value='agy'), \
                    patch.object(ag,'_invoke',side_effect=invoke),patch.object(ag.api_backend,'_run_tool',return_value='本轮工具结果'):
                self.assertEqual(ag.generate('ORIGINAL_TASK_'*300,directory,{},library=True)[0],'完成')
            self.assertIn('ORIGINAL_TASK_',requests[0][1])
            self.assertNotIn('ORIGINAL_TASK_',requests[1][1])
            self.assertIn('本轮工具结果',requests[1][1])
            self.assertEqual(requests[1][0][requests[1][0].index('--conversation')+1],'known-conversation')
            self.assertEqual(requests[0][0][requests[0][0].index('--agent')+1],requests[1][0][requests[1][0].index('--agent')+1])

    def test_cli_isolated_and_local_tool_result_returns_to_model(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            answers = [json.dumps({'final': '', 'tool_calls': [
                {'name': 'get_today_chat', 'arguments': {'query': ''}}]}),
                       json.dumps({'final': '桃子七号', 'tool_calls': []})]
            requests = []

            def run(args, **kwargs):
                requests.append((args, kwargs))
                self.assertNotEqual(kwargs['cwd'], directory)
                name = args[args.index('--agent') + 1]
                self.assertTrue(name.startswith('qq-house-'))
                home_agent = directory / 'data/antigravity-home/.gemini/antigravity-cli/agents' / f'{name}.md'
                agent = (kwargs['cwd'] / '.agents/agents' / f'{name}.md').read_text('utf-8')
                self.assertEqual(home_agent.read_text('utf-8'), agent)
                self.assertIn('commandExecutionPolicy: off', agent)
                self.assertNotIn('run_command', agent)
                self.assertNotIn('call_mcp_tool', agent)
                self.assertNotIn('search_web', agent)
                self.assertEqual(kwargs['env']['USERPROFILE'], str(directory / 'data/antigravity-home'))
                result = {'status': 'SUCCESS', 'structured_output': json.loads(answers[len(requests)-1])}
                return SimpleNamespace(returncode=0, stdout=json.dumps({'event': 'result', 'result': result}))

            with patch.object(ag, 'ROOT', directory), patch.object(ag, 'cli_path', return_value='agy'), \
                    patch.object(ag, '_invoke', side_effect=run), \
                    patch.object(ag.api_backend, '_run_tool', return_value='桃子七号') as tool:
                text, searches, calls = ag.generate('取口令', directory, {}, library=True)
            self.assertEqual(text, '桃子七号')
            tool.assert_called_once_with(directory, 'get_today_chat', {'query': ''})
            self.assertIn('桃子七号', requests[1][1]['input'])
            self.assertEqual(searches, [])
            self.assertEqual(calls[-1]['name'], 'get_today_chat')
            self.assertEqual(list((directory / 'data/antigravity-home/.gemini/antigravity-cli/agents').iterdir()), [])

    def test_unknown_tool_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ag, '_run', return_value=(json.dumps({'final': '', 'tool_calls': [
                    {'name': 'run_command', 'arguments': {}}]}), [], [])), \
                patch.object(ag.api_backend, '_run_tool') as tool:
            with self.assertRaisesRegex(RuntimeError, '格式无效'):
                ag.generate('p', Path(folder), {}, library=True)
            tool.assert_not_called()

    def test_search_is_counted_even_when_cli_fails(self):
        event = {'event': 'step_update', 'step_update': {'step_type': 'tool', 'state': 'ACTIVE',
                 'tool_name': 'search_web', 'tool_info': {'parameters': {'query': '公开资料'}}}}
        done = SimpleNamespace(returncode=1, stdout=json.dumps(event))
        with tempfile.TemporaryDirectory() as folder, patch.object(ag, 'ROOT', Path(folder)), \
                patch.object(ag, 'cli_path', return_value='agy'), patch.object(ag, '_invoke', return_value=done):
            seen = []
            with self.assertRaises(RuntimeError):
                ag._run('p', Path(folder), ag.MODELS[0], 'low', ['search_web'], None, seen.append)
            self.assertEqual(seen, [{'query': '公开资料', 'results': []}])

    def test_main_routes_chat_and_research_without_codex(self):
        profile = {'model_api': {'provider': 'antigravity', 'antigravity_model': 'gemini-3.8-flash-low'}}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(main, 'search_budget', return_value=2) as budget, \
                patch.object(ag, 'generate', return_value=('{}', [], [])) as generate:
            self.assertEqual(main.codex_text(' [WEB_SEARCH] test', Path(folder), profile=profile), ('{}', None))
            self.assertTrue(generate.call_args.kwargs['library'])
            self.assertTrue(generate.call_args.kwargs['web_search'])
            callback = generate.call_args.kwargs['on_search']
            callback({'query': '公开一'}); callback({'query': '公开二'})
            self.assertEqual(sum(call.kwargs.get('record', False) for call in budget.call_args_list), 1)
            main.codex_text('整理', Path(folder), profile=topic_research._codex_profile(profile),
                            ephemeral=True, web_search=True)
            self.assertFalse(generate.call_args.kwargs['library'])
            self.assertTrue(generate.call_args.kwargs['web_search'])
        self.assertEqual(topic_research._codex_profile(profile)['model_api']['provider'], 'antigravity')
        self.assertEqual(topic_research._codex_profile(profile)['model_api']['antigravity_effort'], 'low')

    def test_cli_flags_and_policy_are_values_agy_accepts(self):
        """agy 1.4.7 drops an agent whose policy is outside off/sandbox/auto ("unknown agent"), and refuses --effort
        for models that name their own level or for Claude models; these failed every real call."""
        seen = []

        def run(args, **kwargs):
            name = args[args.index('--agent') + 1]
            agent = (kwargs['cwd'] / '.agents/agents' / f'{name}.md').read_text('utf-8')
            seen.append((args, agent.split('commandExecutionPolicy: ')[1].split('\n')[0]))
            result = {'status': 'SUCCESS', 'response': '你好', 'structured_output': {}}
            return SimpleNamespace(returncode=0, stdout=json.dumps({'event': 'result', 'result': result}))

        cases = [({}, 'gpt-oss-120b-medium', 'off', False), ({'commands': True}, 'gemini-4-pro', 'sandbox', True),
                 ({'commands': True, 'full_computer_access': True}, 'claude-sonnet-4-6', 'auto', False)]
        for computer, model, policy, effort in cases:
            with tempfile.TemporaryDirectory() as folder, patch.object(ag, 'ROOT', Path(folder)), \
                    patch.object(ag, 'cli_path', return_value='agy'), patch.object(ag, '_invoke', side_effect=run), \
                    patch('workspace_tools.enabled', return_value={'enabled': True, **computer}):
                text, _, _ = ag._run('p', Path(folder), model, 'low', ['finish'], None, None, directory=Path(folder))
            args, used = seen[-1]
            self.assertEqual(text, '你好')  # an empty structured_output without a schema is not the reply
            self.assertEqual(used, policy)
            self.assertEqual('--effort' in args, effort, model)

    def test_endless_empty_output_retries_are_stopped(self):
        """agy re-asks the model after every empty answer; each retry is a paid request, so the call is stopped."""
        import sys
        import time
        step = json.dumps({'step_update': {'step_type': 'error_message', 'state': 'DONE'}})
        fake_agy = [sys.executable, '-c', 'import sys, time\nsys.stdin.read()\nwhile True:\n'
                    f'    print({step!r}, flush=True)\n    time.sleep(.05)']
        started = time.monotonic()
        done = ag._invoke(fake_agy, input='{}\n', cwd=None, env=None, timeout=60)
        self.assertTrue(done.stalled)
        self.assertGreaterEqual(done.stdout.count('error_message'), ag.EMPTY_RETRIES)  # plus lines already piped
        self.assertLess(time.monotonic() - started, 30)
        with tempfile.TemporaryDirectory() as folder, patch.object(ag, 'ROOT', Path(folder)), \
                patch.object(ag, 'cli_path', return_value='agy'), patch.object(ag, '_invoke', return_value=done):
            with self.assertRaisesRegex(RuntimeError, '空内容'):
                ag._run('p', Path(folder), ag.MODELS[0], 'low', ['finish'], None, None)

    def test_settings_reject_bad_models_and_efforts(self):
        for api in ({'antigravity_model': '--bad'}, {'antigravity_model': 'a b'}, {'antigravity_effort': 'huge'}):
            with self.assertRaises(ValueError):
                ag.settings(api)

    def test_fenced_json_is_usable_by_memory_and_continuation_parsers(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ag, '_run', return_value=('```json\n{"decisions": []}\n```', [], [])):
            text, _, _ = ag.generate('p', Path(folder), {})
            self.assertEqual(json.loads(text), {'decisions': []})

    def test_phone_model_setting_roundtrip(self):
        import house_web
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'friend.json').write_text('{}', encoding='utf-8')
            body = {'provider': 'antigravity', 'antigravity_model': ag.MODELS[0], 'antigravity_effort': 'medium',
                    'codex_model': 'gpt-6-luna', 'codex_effort': 'low', 'research_effort': 'low'}
            with patch.object(house_web, '_directory', return_value=directory), \
                    patch('codex_settings.model_options', return_value={'gpt-6-luna': ['low']}):
                house_web.post_model('fixture', body)
                result = house_web.model('fixture', {})
                self.assertEqual(result['provider'], 'antigravity')
                self.assertEqual(result['antigravity_effort'], 'medium')
                self.assertEqual(result['antigravity_model'], ag.MODELS[0])
                self.assertFalse(result['key_saved'])


class FinalResponseTests(unittest.TestCase):
    def test_text_before_finish_is_not_part_of_the_reply(self):
        # agy 1.4.7 with Gemini Flash after view_file: "{}" step, finish step, then the answer (2026-10-26 probe)
        def step(index, kind, text=None):
            return {'event': 'step_update', 'step_update': {'step_index': index, 'step_type': kind, 'state': 'DONE',
                                                            **({'text_delta': text} if text is not None else {})}}
        events = [step(0, 'user_input'), step(1, 'agent_response', ''), step(2, 'tool'), step(3, 'agent_response', '{}\n'),
                  step(4, 'finish'), step(5, 'agent_response', '明天见'), step(5, 'agent_response', '。\n')]
        self.assertEqual(ag._final_response(events, {'response': '{}\n明天见。\n'}), '明天见。\n')
        self.assertEqual(ag._final_response([step(1, 'agent_response', '你好')], {'response': '你好'}), '你好')
        self.assertEqual(ag._final_response([step(3, 'finish')], {'response': '只在 response 里'}), '只在 response 里')


if __name__ == '__main__':
    unittest.main()
