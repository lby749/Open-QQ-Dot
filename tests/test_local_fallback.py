import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_chat
import main

LIMIT = RuntimeError("Codex failed: {'message': 'You've hit your usage limit. try again at 11:45 PM.', "
                     "'codexErrorInfo': 'usageLimitExceeded'}")
CHOSEN = {'model_api': {'provider': 'codex', 'local_fallback': True}}


class LocalFallbackTests(unittest.TestCase):
    def setUp(self):
        local_chat._limit_until = None
        self.addCleanup(setattr, local_chat, '_limit_until', None)

    def test_limit_time_is_parsed_and_rolls_to_next_day(self):
        now = datetime(2026, 10, 8, 23, 9)
        self.assertEqual(local_chat.mark_limited(LIMIT, now), datetime(2026, 10, 8, 23, 45))
        self.assertEqual(local_chat.mark_limited(LIMIT, datetime(2026, 10, 8, 23, 50)),
                         datetime(2026, 10, 9, 23, 45))
        self.assertEqual(local_chat.mark_limited(RuntimeError('Codex 模型账号额度不足'), now),
                         datetime(2026, 10, 8, 23, 39))

    def test_usage_limit_switches_selected_friend_to_local_until_reset(self):
        with patch.object(main, '_codex_text', side_effect=LIMIT) as codex, \
                patch.object(local_chat, 'generate', return_value='{"messages":["好"]}') as local:
            self.assertEqual(main.codex_text('p', Path('f'), profile=CHOSEN), ('{"messages":["好"]}', None))
            self.assertEqual(main.codex_text('p2', Path('f'), profile=CHOSEN), ('{"messages":["好"]}', None))
        self.assertEqual(codex.call_count, 1)  # second call skips Codex while limited
        self.assertEqual(local.call_count, 2)

    def test_other_friends_pause_without_calling_any_model(self):
        with patch.object(main, '_codex_text', side_effect=LIMIT) as codex, \
                patch.object(local_chat, 'generate') as local:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    main.codex_text('p', Path('f'), profile={})
        self.assertEqual(codex.call_count, 1)
        local.assert_not_called()

    def test_proactive_and_care_never_use_local_model(self):
        with patch.object(main, '_codex_text', side_effect=LIMIT), \
                patch.object(local_chat, 'generate') as local:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    main.codex_text('p', Path('f'), profile=CHOSEN, local_ok=False)
        local.assert_not_called()

    def test_other_errors_and_web_search_do_not_fall_back(self):
        with patch.object(main, '_codex_text', side_effect=RuntimeError('Codex app-server 已退出')), \
                patch.object(local_chat, 'generate') as local, self.assertRaises(RuntimeError):
            main.codex_text('p', Path('f'), profile=CHOSEN)
        with patch.object(main, '_codex_text', side_effect=LIMIT), \
                patch.object(local_chat, 'generate') as local, self.assertRaises(RuntimeError):
            main.codex_text('p', Path('f'), ephemeral=True, web_search=True, profile=CHOSEN)
        local.assert_not_called()

    def test_qq_expression_markup_is_normalized(self):
        server = type('S', (), {'_ensure_loaded': lambda self: None,
            '_request': lambda self, *a, **k: {'choices': [{'message': {'content': '{"messages":["[[QQ 表情：偷笑]]"]}'}}]}})()
        with patch('message_classifier.get_classifier', return_value=server):
            self.assertEqual(local_chat.generate('p'), '{"messages": ["[[QQ表情:偷笑]]"]}')

    def test_thinking_level_reaches_request_and_defaults_off(self):
        sent = []
        server = type('S', (), {'_ensure_loaded': lambda self: None,
            '_request': lambda self, path, payload, **k: (sent.append(payload),
                {'choices': [{'message': {'content': '{"messages":["好"]}'}}]})[1]})()
        with patch('message_classifier.get_classifier', return_value=server):
            local_chat.generate('p')
            local_chat.generate('p', thinking='medium')
        self.assertEqual(sent[0]['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn('thinking_budget_tokens', sent[0])
        self.assertEqual((sent[1]['chat_template_kwargs'], sent[1]['thinking_budget_tokens'], sent[1]['max_tokens']),
                         ({'enable_thinking': True}, 1024, 2048 + 1024))
        profile = {'model_api': {**CHOSEN['model_api'], 'local_thinking': 'high'}}
        with patch.object(main, '_codex_text', side_effect=LIMIT), \
                patch.object(local_chat, 'generate', return_value='{}') as local:
            main.codex_text('p', Path('f'), profile=profile)
        self.assertEqual(local.call_args.kwargs['thinking'], 'high')

    def test_tool_calls_run_against_friend_then_answer(self):
        replies = iter([
            {'choices': [{'message': {'content': '', 'tool_calls': [{'id': 'c1', 'function': {
                'name': 'search_friend_memory', 'arguments': '{"query":"cat"}'}}]}}]},
            {'choices': [{'message': {'content': '好的我想想'}}]},  # free text -> forced JSON retry
            {'choices': [{'message': {'content': '{"messages":["睡吧！]]"]}'}}]}])
        sent = []
        server = type('S', (), {'_ensure_loaded': lambda self: None,
                                '_request': lambda self, path, payload, **k: (sent.append(payload), next(replies))[1]})()
        with patch('message_classifier.get_classifier', return_value=server), \
                patch('topic_library.call_tool', return_value={'items': ['喜欢猫']}) as tool:
            text = local_chat.generate('p', directory=Path('f'), library=True)
        tool.assert_called_once_with(Path('f'), 'search_friend_memory', {'query': 'cat'})
        self.assertEqual(text, '{"messages": ["睡吧！"]}')
        self.assertIn('tools', sent[0])
        self.assertEqual(sent[2]['response_format'], {'type': 'json_object'})
        self.assertNotIn('tools', sent[2])

    def test_idle_server_is_unloaded_unless_busy(self):
        import tempfile, threading, time
        import message_classifier as mc
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / 'build/message-classifier-runtime'
            runtime.mkdir(parents=True)
            (runtime / 'runtime.json').write_text('{"server":"s","model":"m","idle_unload_seconds":0.05}', 'utf-8')
            classifier = mc.LocalMessageClassifier(temp)
            try:
                busy = [True]
                classifier._busy = lambda: busy[0]
                classifier._process = process = Mock(pid=1)
                process.poll.return_value = None
                classifier._last_used = time.monotonic() - 10
                threading.Thread(target=classifier._idle_watch, daemon=True).start()
                time.sleep(0.2)
                self.assertIs(classifier._process, process)  # a request in flight keeps it loaded
                busy[0] = False
                time.sleep(0.2)
                self.assertIsNone(classifier._process)
                process.terminate.assert_called_once()
            finally:
                classifier.close()

    def test_codex_is_used_again_after_reset(self):
        local_chat._limit_until = datetime(2000, 1, 1)
        with patch.object(main, '_codex_text', return_value=('ok', 't')) as codex:
            self.assertEqual(main.codex_text('p', Path('f'), profile={}), ('ok', 't'))
        codex.assert_called_once()


if __name__ == '__main__':
    unittest.main()
