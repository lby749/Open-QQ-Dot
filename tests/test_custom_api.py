import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import api_backend
from store import FriendStore


def today():
    return datetime.now().astimezone().strftime('%Y-%m-%d')


class CustomApiTests(unittest.TestCase):
    """2026-10-12 user: any API (three protocols) with the program's own tools; reasoning effort low by default."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'friend.json').write_text(json.dumps({'basic_info': {}}), encoding='utf-8')
        store = FriendStore(self.directory)
        store.add_message('now', 'friend', '今天正在谈论陶艺', today() + 'T12:00:00+09:00')
        store.close()
        api_backend._retry_until.clear()

    def run_api(self, api, responses, image=None):
        requests = []

        def respond(request, timeout):
            requests.append((request.full_url, dict(request.header_items()), json.loads(request.data)))
            answer = responses[len(requests) - 1]
            if isinstance(answer, int):
                raise urllib.error.HTTPError(request.full_url, answer, 'bad', {}, io.BytesIO(b'{}'))
            return io.StringIO(json.dumps(answer))

        with patch.object(api_backend, 'load_key', return_value='test-key'), \
                patch.object(api_backend.urllib.request, 'urlopen', side_effect=respond):
            text = api_backend.generate('按需聊天', self.directory, api, image, library=True)
        return text, requests

    def test_custom_provider_reaches_api_with_friend_settings(self):
        import main
        api = {'provider': 'custom', 'api_base_url': 'https://integrate.api.nvidia.com/v1',
               'model': 'z-ai/glm-5.3-flash', 'api_protocol': 'openai-completions'}
        with patch.object(main, 'deepseek_generate', return_value='好') as generate:
            self.assertEqual(main.codex_text('test', self.directory, profile={'model_api': api}), ('好', None))
        generate.assert_called_once_with('test', self.directory, api, None, False, library=True)

    def test_settings_default_to_deepseek_with_low_effort(self):
        self.assertEqual(api_backend.settings({}), ('openai-completions', 'https://api.deepseek.com',
                                                    api_backend.MODELS[0], 'low'))
        for bad in ({'api_protocol': 'grpc'}, {'api_base_url': 'ftp://x'}, {'api_effort': 'huge'}):
            with self.assertRaises(ValueError):
                api_backend.settings(bad)

    def test_openai_completions_runs_library_and_phone_tools(self):
        api = {'api_base_url': 'https://integrate.api.nvidia.com/v1/', 'model': 'z-ai/glm-5.3'}
        calls = [{'id': 'c1', 'type': 'function', 'function': {'name': 'get_today_chat', 'arguments': '{"query":"陶艺"}'}},
                 {'id': 'c2', 'type': 'function', 'function': {'name': 'phone_now', 'arguments': '{}'}}]
        text, requests = self.run_api(api, [
            {'choices': [{'finish_reason': 'tool_calls',
                          'message': {'content': None, 'reasoning_content': '想想', 'tool_calls': calls}}]},
            {'choices': [{'finish_reason': 'stop', 'message': {'content': '自然接话'}}]}])
        self.assertEqual(text, '自然接话')
        url, headers, first = requests[0]
        self.assertEqual(url, 'https://integrate.api.nvidia.com/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer test-key')
        self.assertEqual(first['reasoning_effort'], 'low')
        self.assertIn('phone_now', [tool['function']['name'] for tool in first['tools']])
        history = requests[1][2]['messages']
        self.assertEqual(history[1]['reasoning_content'], '想想')
        chat, phone = json.loads(history[2]['content']), json.loads(history[3]['content'])
        self.assertEqual(chat['items'], [{'speaker': 'friend', 'text': '今天正在谈论陶艺'}])
        self.assertIn('配对', phone['status'])  # unpaired phone: the model is told, nothing breaks

    def test_model_without_reasoning_switch_is_asked_again_without_it(self):
        text, requests = self.run_api({'api_base_url': 'http://127.0.0.1:11434/v1', 'model': 'qwen3'}, [
            400, {'choices': [{'finish_reason': 'stop', 'message': {'content': '好'}}]}])
        self.assertEqual(text, '好')
        self.assertIn('reasoning_effort', requests[0][2])
        self.assertNotIn('reasoning_effort', requests[1][2])

    def test_openai_responses_tool_round(self):
        api = {'api_protocol': 'openai-responses', 'api_base_url': 'https://api.openai.com/v1', 'model': 'gpt-x',
               'api_effort': 'medium'}
        text, requests = self.run_api(api, [
            {'status': 'completed', 'output': [
                {'type': 'reasoning', 'id': 'r1', 'summary': []},
                {'type': 'function_call', 'id': 'f1', 'call_id': 'c1', 'name': 'get_today_chat',
                 'arguments': '{"query":"陶艺"}'}]},
            {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '聊陶艺'}]}]}])
        self.assertEqual(text, '聊陶艺')
        self.assertEqual(requests[0][0], 'https://api.openai.com/v1/responses')
        self.assertEqual(requests[0][2]['reasoning'], {'effort': 'medium'})
        follow = requests[1][2]['input']
        self.assertEqual(follow[1]['type'], 'function_call')
        self.assertEqual(follow[2]['call_id'], 'c1')
        self.assertIn('今天正在谈论陶艺', follow[2]['output'])

    def test_anthropic_messages_tool_round_and_image(self):
        image = self.directory / 'p.png'
        image.write_bytes(b'png')
        api = {'api_protocol': 'anthropic-messages', 'api_base_url': 'https://api.anthropic.com', 'model': 'claude-x'}
        text, requests = self.run_api(api, [
            {'stop_reason': 'tool_use', 'content': [
                {'type': 'thinking', 'thinking': '...', 'signature': 's'},
                {'type': 'tool_use', 'id': 't1', 'name': 'get_today_chat', 'input': {'query': '陶艺'}}]},
            {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '看到图了'}]}], image)
        self.assertEqual(text, '看到图了')
        url, headers, first = requests[0]
        self.assertEqual(url, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(headers['X-api-key'], 'test-key')
        self.assertEqual(first['thinking'], {'type': 'enabled', 'budget_tokens': 1024})
        self.assertEqual(first['messages'][0]['content'][0]['type'], 'image')
        messages = requests[1][2]['messages']
        self.assertEqual(messages[1]['content'][0]['type'], 'thinking')
        self.assertEqual(messages[2]['content'][0]['tool_use_id'], 't1')

    def test_no_tools_for_ephemeral_calls_and_effort_none_skips_thinking(self):
        api = {'api_protocol': 'anthropic-messages', 'api_base_url': 'https://x.example/v1', 'api_effort': 'none'}
        with patch.object(api_backend, 'load_key', return_value='k'), \
                patch.object(api_backend.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(
                    {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'yeah'}]}))) as call:
            self.assertEqual(api_backend.generate('p', self.directory, api), 'yeah')
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://x.example/v1/messages')
        sent = json.loads(request.data)
        self.assertNotIn('thinking', sent)
        self.assertNotIn('tools', sent)

    def test_compaction_size_widens_the_recent_chat_for_custom_apis_only(self):
        # 2026-10-12 user: one per-friend compaction size (default 256k); a custom API gets half of it as recent chat
        import prompt_context
        store = FriendStore(self.directory)
        try:
            for index in range(60):
                store.add_message(f'm{index}', 'friend', f'sentence_{index}', today() + 'T12:00:00+09:00')
            self.assertEqual(len(prompt_context.recent_rounds(store)), 40)
            profile = json.loads((self.directory / 'friend.json').read_text('utf-8'))
            profile['model_api'] = {'provider': 'custom'}
            (self.directory / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
            self.assertEqual(len(prompt_context.recent_rounds(store)), 61)
            profile['model_api']['provider'] = 'codex'
            (self.directory / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
            self.assertEqual(len(prompt_context.recent_rounds(store)), 40)
        finally:
            store.close()

    def test_compaction_size_default_and_codex_thread_setting(self):
        from codex_settings import compact_tokens
        self.assertEqual(compact_tokens({}), 900_000)
        self.assertEqual(compact_tokens({'compact_tokens': 400_000}), 360_000)
        self.assertEqual(compact_tokens({'compact_tokens': 100}), 900_000)  # below the floor: default
        import main
        settings = {'model_api': {'provider': 'codex', 'codex_model': 'm', 'codex_effort': 'low',
                                  'compact_tokens': 300_000}}
        with patch('codex_settings.model_options', return_value={'m': ['low']}), \
                patch.object(main.SERVER, 'generate', return_value=('好', 't')) as generate:
            main.codex_text('p', self.directory, None, profile=settings)
        self.assertEqual(generate.call_args.kwargs['compact_limit'], 270_000)


if __name__ == '__main__':
    unittest.main()
