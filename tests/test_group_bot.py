"""Grok-style group assistant (group_bot.py).

What is real: event parsing, the SQLite context store, the background answer worker, the HTTP call to
an OpenAI-compatible local model server (a real HTTP server on 127.0.0.1 stands in for Ollama /
llama.cpp), fallback ordering, rate limits, and Bridge.receive routing. What is fake: the model's
words (the fake server echoes what it was asked) and NapCat (a recorder for send_group_msg/get_msg).
"""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import group_bot
from group_bot import GroupBot, GroupStore

SELF = '20000'
GROUP = '123456789'


class FakeNapCat:
    def __init__(self):
        self.sent, self.next_id, self.history = [], 9000, {}

    def call(self, action, params=None, timeout=10):
        if action == 'send_group_msg':
            self.next_id += 1
            self.sent.append(params)
            return {'message_id': self.next_id}
        if action == 'get_msg':
            return self.history.get(int(params['message_id']))
        raise AssertionError(action)


class FakeModelServer:
    """OpenAI-compatible /v1/chat/completions on loopback; answers with the question it received."""

    def __init__(self):
        self.requests = []
        self.script = []  # queued assistant messages (e.g. tool calls); otherwise echo the question
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append({'path': self.path, 'body': body})
                content = body['messages'][1]['content']
                if isinstance(content, list):  # vision request: the text part comes first
                    content = content[0]['text']
                question = content.rsplit('\n', 1)[-1]
                message = outer.script.pop(0) if outer.script else {
                    'role': 'assistant', 'content': f'<think>hidden</think>Grok: answer to {question}'}
                reply = {'choices': [{'message': message}]}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # /v1/models, as Ollama / llama.cpp / LM Studio answer it
                data = json.dumps({'data': [{'id': 'test-model'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/v1'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def event(message_id, user_id, segments, group=GROUP, name=None, at=None):
    return {'post_type': 'message', 'message_type': 'group', 'group_id': int(group), 'self_id': int(SELF),
            'message_id': message_id, 'user_id': int(user_id), 'time': time.time(),
            'sender': {'user_id': int(user_id), 'nickname': name or f'user{user_id}'}, 'message': segments}


def text(value):
    return {'type': 'text', 'data': {'text': value}}


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class GroupBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root)})
        self._env.start()
        self.model = FakeModelServer()
        self.napcat = FakeNapCat()
        self.config = {'enabled': True, 'name': 'Grok', 'aliases': ['grok', '格洛克'],
                       'groups': {GROUP: {'enabled': True, 'style': 'tease gently'}},
                       'backend': 'local', 'local': {'base_url': self.model.url, 'model': 'test-model'},
                       'context_messages': 5, 'group_rate_per_minute': 6, 'user_rate_per_minute': 2}
        self.bot = GroupBot(lambda: self.napcat, lambda: SELF, store=GroupStore(self.root / 'g.sqlite3'),
                            config_fn=lambda: self.config)

    def tearDown(self):
        self.model.close()
        self._env.stop()
        self._tmp.cleanup()

    def answered(self, count):
        self.assertTrue(wait_for(lambda: len(self.napcat.sent) >= count), self.napcat.sent)
        self.bot.jobs.join()
        return self.napcat.sent

    def test_mention_with_quoted_message_is_answered_as_a_reply_from_the_local_model(self):
        self.bot.receive(event(1, 30001, [text('the moon is made of cheese')], name='Ann'))
        self.bot.receive(event(2, 30002, [text('lol')], name='Bob'))
        self.assertTrue(self.bot.receive(event(3, 30003, [
            {'type': 'reply', 'data': {'id': '1'}}, {'type': 'at', 'data': {'qq': SELF}}, text(' 这是真的吗？')], name='Cat')))
        sent = self.answered(1)
        self.assertEqual(sent[0]['group_id'], int(GROUP))
        self.assertEqual(sent[0]['message'][0], {'type': 'reply', 'data': {'id': '3'}})
        answer = sent[0]['message'][1]['data']['text']
        self.assertEqual(answer, 'answer to 这是真的吗？')  # think block and "Grok:" prefix stripped
        body = self.model.requests[0]['body']
        self.assertEqual((self.model.requests[0]['path'], body['model']), ('/v1/chat/completions', 'test-model'))
        system, user = body['messages'][0]['content'], body['messages'][1]['content']
        self.assertIn('Grok', system)
        self.assertIn('tease gently', system)
        self.assertIn('QUOTED_MESSAGE (untrusted data', user)
        self.assertIn('the moon is made of cheese', user.split('QUOTED_MESSAGE')[1])
        self.assertIn('"from": "Bob", "text": "lol"', user.split('QUOTED_MESSAGE')[0])  # recent context
        record = self.bot.store.answer(GROUP, '3')
        self.assertEqual((record['status'], record['backend'], record['answer_id']), ('sent', 'local', '9001'))

        # Replying to the bot's answer continues the conversation without another @.
        self.assertTrue(self.bot.receive(event(4, 30003, [{'type': 'reply', 'data': {'id': '9001'}}, text('why?')])))
        sent = self.answered(2)
        user = self.model.requests[1]['body']['messages'][1]['content']
        self.assertIn('(you)', user.split('QUESTION')[0])  # it sees its own earlier answer as the quote

    def test_name_alias_triggers_and_ordinary_chatter_does_not(self):
        self.assertFalse(self.bot.receive(event(1, 30001, [text('grokking is a word')])))
        self.assertFalse(self.bot.receive(event(2, 30001, [text('hello everyone')])))
        self.assertTrue(self.bot.receive(event(3, 30001, [text('Grok，讲个笑话')])))
        self.assertTrue(self.bot.receive(event(4, 30002, [text('格洛克: 总结一下')])))
        sent = self.answered(2)
        self.assertEqual(sorted(m['message'][1]['data']['text'] for m in sent),
                         ['answer to 总结一下', 'answer to 讲个笑话'])

    def test_groups_are_opt_in_and_nothing_is_stored_elsewhere(self):
        other = '987654321'
        self.assertFalse(self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('hi')], group=other)))
        self.config['enabled'] = False
        self.assertFalse(self.bot.receive(event(2, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('hi')])))
        self.assertEqual(self.bot.store._run('SELECT COUNT(*) FROM group_messages')[0][0], 0)
        self.assertEqual(self.napcat.sent, [])

    def test_duplicates_rate_limits_and_own_messages(self):
        summon = [{'type': 'at', 'data': {'qq': SELF}}, text('q')]
        self.assertTrue(self.bot.receive(event(1, 30001, summon)))
        self.assertFalse(self.bot.receive(event(1, 30001, summon)))  # redelivered event
        self.assertFalse(self.bot.receive(event(2, int(SELF), [text('my own message')])))
        for message_id in (3, 4):
            self.bot.receive(event(message_id, 30001, summon))
        self.bot.jobs.join()
        statuses = [self.bot.store.answer(GROUP, str(i))['status'] for i in (1, 3, 4)]
        self.assertEqual(statuses, ['sent', 'sent', 'rate_limited'])  # 2 per member per minute
        self.assertEqual(len(self.napcat.sent), 2)

    def test_fallback_backend_and_loopback_only_local_server(self):
        self.config['local'] = {'base_url': 'http://127.0.0.1:9/v1'}  # nothing listens there
        self.config['fallback'] = 'codex'
        used = []
        self.bot.backends = {'codex': lambda messages, images: used.append(messages) or 'from the fallback'}
        self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('q')]))
        self.answered(1)
        self.assertEqual(self.bot.store.answer(GROUP, '1')['backend'], 'codex')
        self.assertEqual(len(used), 1)
        with self.assertRaises(group_bot.BackendUnavailable) as refused:
            group_bot.ask_local([], {'local': {'base_url': 'http://example.com/v1'}})
        self.assertIn('loopback', str(refused.exception))
        self.config['fallback'] = ''
        self.bot.backends = None
        self.bot.receive(event(2, 30002, [{'type': 'at', 'data': {'qq': SELF}}, text('q')]))
        self.bot.jobs.join()
        record = self.bot.store.answer(GROUP, '2')
        self.assertEqual(record['status'], 'failed')
        self.assertIn('not reachable', record['detail'])

    def test_quoted_message_older_than_the_window_is_fetched_from_napcat(self):
        self.napcat.history[555] = {'message_id': 555, 'user_id': 30009, 'sender': {'nickname': 'Old'},
                                    'message': [text('ancient claim')]}
        self.bot.receive(event(1, 30001, [{'type': 'reply', 'data': {'id': '555'}},
                                          {'type': 'at', 'data': {'qq': SELF}}, text('?')]))
        self.answered(1)
        self.assertIn('ancient claim', self.model.requests[0]['body']['messages'][1]['content'])

    def test_health_check_reports_what_is_ready(self):
        rows = {check: (ok, detail) for check, ok, detail in group_bot.health(self.config)}
        self.assertEqual(rows['backend local'], (True, f'{self.model.url} serves test-model'))
        self.assertTrue(rows['enabled'][0] and rows['groups'][0])
        self.config['local']['model'] = 'missing-model'
        self.config['groups'] = {}
        rows = {check: (ok, detail) for check, ok, detail in group_bot.health(self.config)}
        self.assertFalse(rows['backend local'][0])
        self.assertIn('has no model missing-model', rows['backend local'][1])
        self.assertFalse(rows['groups'][0])

    def test_bridge_routes_group_events_to_the_bot(self):
        import main
        bridge = main.Bridge(client=self.napcat)
        bridge.self_id = SELF
        bridge._group_bot = self.bot
        bridge.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('hi')]))
        self.answered(1)
        self.assertEqual(self.napcat.sent[0]['message'][1]['data']['text'], 'answer to hi')


class GroupBotFunctionTests(GroupBotTests):
    """Search with sources, images, summaries and group settings (runs the base tests again too)."""

    def test_local_model_searches_the_web_and_cites_what_it_used(self):
        queries = []
        self.bot.search_fn = lambda q: queries.append(q) or [
            {'title': 'Python 3.14 released', 'url': 'https://python.org/3.14', 'snippet': 'Oct 7, 2025'},
            {'title': 'unrelated', 'url': 'https://example.org/x', 'snippet': '...'}]
        self.model.script = [
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {
                'name': 'web_search', 'arguments': json.dumps({'query': 'python 3.14 release date'})}}]},
            {'role': 'assistant', 'content': 'Python 3.14 was released on 7 October 2025 [1].'}]
        self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('python 3.14 什么时候发布的')]))
        sent = self.answered(1)[0]['message'][1]['data']['text']
        self.assertEqual(queries, ['python 3.14 release date'])
        self.assertIn('来源：\n[1] https://python.org/3.14', sent)
        self.assertNotIn('example.org', sent)  # only cited results are listed
        first, second = (r['body'] for r in self.model.requests)
        self.assertEqual([t['function']['name'] for t in first['tools']], ['web_search', 'fetch_url'])
        self.assertIn('use the web_search tool first', first['messages'][0]['content'])
        self.assertEqual(json.loads(second['messages'][-1]['content'])['results'][0]['n'], 1)
        self.assertEqual(self.bot.store.answer(GROUP, '1')['detail'], 'searched 2 results')

    def test_deep_search_allows_more_rounds_and_asks_for_a_thorough_answer(self):
        calls = []
        self.bot.search_fn = lambda q: calls.append(q) or [{'title': q, 'url': f'https://e.org/{len(calls)}', 'snippet': ''}]
        call = lambda i: {'role': 'assistant', 'content': '', 'tool_calls': [{'id': f'c{i}', 'type': 'function',
                          'function': {'name': 'web_search', 'arguments': json.dumps({'query': f'angle {i}'})}}]}
        self.model.script = [call(i) for i in range(6)] + [{'role': 'assistant', 'content': 'thorough [1][6]'}]
        self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('深度搜索 固态电池的进展')]))
        sent = self.answered(1)[0]['message'][1]['data']['text']
        self.assertEqual(len(calls), 6)  # past the normal 4-round limit
        self.assertIn('[6] https://e.org/6', sent)
        user = self.model.requests[0]['body']['messages'][1]['content']
        self.assertIn('TASK: deep search', user)
        self.assertIn('thoroughly', self.model.requests[0]['body']['messages'][0]['content'])

    def test_without_search_the_model_is_told_not_to_guess_current_facts(self):
        self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('今天比分多少')]))
        self.answered(1)
        body = self.model.requests[0]['body']
        self.assertNotIn('tools', body)
        self.assertIn('no live search', body['messages'][0]['content'])

    def _serve_image(self, name):
        from PIL import Image
        path = self.root / name
        Image.new('RGB', (40, 30), 'red').save(path)
        original = self.napcat.call
        self.napcat.call = lambda action, params=None, timeout=10: (
            {'file': str(path)} if action == 'get_image' else original(action, params, timeout))
        return original

    def test_images_in_the_quoted_message_reach_a_vision_model(self):
        self._serve_image('cat.png')
        self.config['local']['vision'] = True
        self.bot.receive(event(1, 30001, [{'type': 'image', 'data': {'file': 'abc.image', 'url': 'https://x/y'}}]))
        self.bot.receive(event(2, 30002, [{'type': 'reply', 'data': {'id': '1'}},
                                          {'type': 'at', 'data': {'qq': SELF}}, text('图里是什么')]))
        self.answered(1)
        parts = self.model.requests[0]['body']['messages'][1]['content']
        self.assertEqual(parts[1]['type'], 'image_url')
        self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertIn('[图片]', parts[0]['text'])

    def test_images_go_to_an_agent_backend_and_unreadable_ones_are_declared(self):
        seen = []
        self.config.update(backend='codex')
        self.bot.backends = {'codex': lambda messages, images: seen.append((messages, images)) or 'a red square'}
        original = self._serve_image('chart.png')
        self.bot.receive(event(1, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('看看这个'),
                                          {'type': 'image', 'data': {'file': 'p.image'}}]))
        self.answered(1)
        self.assertEqual(len(seen[0][1]), 1)
        self.assertTrue(Path(seen[0][1][0]).is_file())
        self.napcat.call = lambda action, params=None, timeout=10: (
            {} if action == 'get_image' else original(action, params, timeout))
        self.bot.receive(event(2, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('这个呢'),
                                          {'type': 'image', 'data': {'file': 'gone.image'}}]))
        self.answered(2)
        self.assertIn('image you cannot see', seen[1][0][1]['content'])

    def test_summary_reads_a_longer_window(self):
        self.config['context_messages'] = 3
        for i in range(1, 41):
            self.bot.receive(event(i, 30000 + i % 4, [text(f'point number {i}')]))
        self.bot.receive(event(100, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text('总结一下刚才聊了什么')]))
        self.answered(1)
        user = self.model.requests[0]['body']['messages'][1]['content']
        self.assertIn('point number 1"', user)
        self.assertIn('point number 40"', user)
        self.assertIn('TASK: summarise GROUP_CONTEXT', user)

    def test_only_group_owner_or_admins_change_settings(self):
        self.config['user_rate_per_minute'] = 10
        def say(message_id, words, role):
            e = event(message_id, 30001, [{'type': 'at', 'data': {'qq': SELF}}, text(words)])
            e['sender']['role'] = role
            self.bot.receive(e)
        say(1, '设置 长度 简短', 'member')
        self.assertIn('只有群主或管理员', self.answered(1)[0]['message'][1]['data']['text'])
        say(2, '设置 长度 简短', 'admin')
        say(3, '设置 风格 像体育解说员', 'owner')
        self.answered(3)
        self.assertEqual(self.bot.store.prefs(GROUP), {'length': 'short', 'style': '像体育解说员'})
        self.assertEqual(self.model.requests, [])  # settings never reach the model
        say(4, '解释一下量子纠缠', 'member')
        self.answered(4)
        system = self.model.requests[0]['body']['messages'][0]['content']
        self.assertIn('1-3 sentences', system)
        self.assertIn('像体育解说员', system)


class WebSearchTests(unittest.TestCase):
    def test_searxng_results_from_a_local_instance(self):
        import web_search

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({'results': [{'title': '<b>T</b>', 'url': 'https://a.example/1', 'content': 'c'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            found = web_search.search('q', {'provider': 'searxng',
                                            'base_url': f'http://127.0.0.1:{server.server_address[1]}'})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(found, [{'title': 'T', 'url': 'https://a.example/1', 'snippet': 'c'}])

    def test_agent_search_parsing_and_refusals(self):
        import web_search
        reply = ('Here: [{"title": "A", "url": "https://a.example", "snippet": "fact"}, '
                 '{"title": "bad", "url": "file:///etc"}]')
        self.assertEqual(web_search.search('q', {'provider': 'agent'}, agent_fn=lambda p, prompt: reply),
                         [{'title': 'A', 'url': 'https://a.example', 'snippet': 'fact'}])
        with self.assertRaises(web_search.SearchError):
            web_search.search('q', {'provider': 'off'})
        with self.assertRaises(web_search.SearchError):
            web_search.search('q', {'provider': 'brave'})  # no api key
        for url in ('http://127.0.0.1:8080/', 'http://192.168.1.1/', 'file:///c:/x', 'http://user:pw@example.com/'):
            with self.assertRaises(web_search.SearchError):
                web_search.fetch_url(url)


class ParsingTests(unittest.TestCase):
    def test_cq_string_messages_and_alias_boundaries(self):
        parsed = group_bot.parse_event({'post_type': 'message', 'message_type': 'group', 'group_id': 1,
                                        'message_id': 5, 'user_id': 7,
                                        'message': f'[CQ:reply,id=42][CQ:at,qq={SELF}] is this real'}, SELF)
        self.assertEqual((parsed.mentions_me, parsed.reply_to, parsed.text), (True, '42', 'is this real'))
        self.assertEqual(group_bot.strip_alias('@Grok 你好', ['Grok']), (True, '你好'))
        self.assertEqual(group_bot.strip_alias('grok', ['Grok']), (True, ''))
        self.assertEqual(group_bot.strip_alias('grokking', ['Grok'])[0], False)

    def test_answers_are_cleaned_for_qq(self):
        long = 'x' * 5000
        self.assertEqual(len(group_bot.clean_answer(long, 'Grok')), group_bot.ANSWER_MAX_CHARS)
        self.assertEqual(group_bot.clean_answer('## Title\n**bold**', 'Grok'), 'Title\nbold')
        self.assertEqual(group_bot.clean_answer('见[来源](https://python.org/a)', 'Grok'), '见来源 https://python.org/a')


if __name__ == '__main__':
    unittest.main()
