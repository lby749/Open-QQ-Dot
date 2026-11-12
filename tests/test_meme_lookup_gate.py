import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from store import FriendStore


class MemeLookupGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.profile = main.default_profile('10001', 'test', False)
        self.store.record_inbound('one', '寿来！')
        self.payload = {'lookup_needed': True, 'messages': ['寿回✋'],
                        'usage_check': {'meaning': 'banter', 'actual_usage': '一方寿来，另一方寿回',
                                        'reply_focus': '自然接招', 'source_url': 'https://example.com/usage'}}
        self.searches = [{'query': '寿来 评论区 怎么接', 'results': [{'url': 'https://example.com/usage'}]}]
        self.patches = [patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.store.close()
        self.temp.cleanup()

    def generate(self, prompt, *a, **k):
        if 'RETRY_LOOKUP' in prompt:
            main.SERVER.last_searches = self.searches
        return json.dumps(self.payload), 'test-thread'

    def test_model_identifies_unknown_term_but_omits_search_then_searches_before_reply(self):
        with patch.object(main, 'codex_text', side_effect=self.generate) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['寿回✋'])
        self.assertEqual(model.call_count, 2)
        self.assertIn('HOW_PEOPLE_REPLY', model.call_args_list[0].args[0])
        # the retry continues the same thread; the whole current task (with the lookup requirement) is resent
        self.assertEqual(model.call_args.args[2], 'test-thread')
        self.assertIn('RETRY_LOOKUP', model.call_args.args[0])
        self.assertIn('TASK_BELOW_REPLACES_ALL_EARLIER_TASKS', model.call_args.args[0])
        self.assertNotIn(' [SYSTEM_RULES] ', model.call_args.args[0])
        self.assertEqual(self.store.get('thread_id'), 'test-thread')

    def test_still_omitting_search_fails_before_any_qq_send(self):
        with patch.object(main, 'codex_text', return_value=(json.dumps(self.payload), 'test-thread')) as model:
            with self.assertRaisesRegex(RuntimeError, '未实际搜索'):
                main.make_reply(self.store, self.profile, self.directory)
        self.assertEqual(model.call_count, 2)
        self.assertFalse(self.store.has_outbound_body('寿回✋'))

    def test_already_searched_does_not_generate_twice(self):
        def generate(*a, **k):
            main.SERVER.last_searches = self.searches
            return json.dumps(self.payload), 'test-thread'
        with patch.object(main, 'codex_text', side_effect=generate) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['寿回✋'])
        model.assert_called_once()

    def test_known_chat_does_not_force_search(self):
        self.payload.update(lookup_needed=False, messages=['晚上好呀'])
        with patch.object(main, 'codex_text', side_effect=self.generate) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['晚上好呀'])
        model.assert_called_once()

    def test_exhausted_budget_does_not_claim_search_or_retry(self):
        for provider in ('codex', 'custom'):
            self.profile['model_api'] = {'provider': provider}
            self.payload['messages'] = ['这句我还没弄明白，你是怎么用的呀？']
            with patch.object(main, 'search_budget', return_value=0), \
                    patch.object(main, 'codex_lookup') as lookup, \
                    patch.object(main, 'codex_text', side_effect=self.generate) as model:
                main.make_reply(self.store, self.profile, self.directory)
            model.assert_called_once()
            lookup.assert_not_called()

    def test_custom_api_looks_up_through_codex_then_answers_again(self):
        # 2026-10-12 user: a custom-API friend's term lookups also search through Codex
        self.profile['model_api'] = {'provider': 'custom'}
        self.payload['learned_terms'] = [{'term': '寿来', 'meaning': 'banter', 'confirmed': False,
                                          'source_url': 'https://example.com/usage'}]

        def lookup(*args):
            main.SERVER.last_searches = self.searches
            return '寿来：评论区借寿梗，回“寿回”'
        with patch.object(main, 'search_budget', return_value=5), \
                patch.object(main, 'codex_lookup', side_effect=lookup) as codex, \
                patch.object(main, 'codex_text', return_value=(json.dumps(self.payload), None)) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['寿回✋'])
        self.assertEqual(codex.call_args.args[2], '寿来！')
        self.assertEqual(model.call_count, 2)
        self.assertIn('LOOKUP_RESULT', model.call_args.args[0])
        self.assertIn('评论区借寿梗', model.call_args.args[0])
        # the Codex searches count for the term the custom model reports
        self.assertIn('寿来', json.loads(self.store.get('term_candidates', '{}')))

    def test_failed_codex_lookup_keeps_the_first_answer(self):
        self.profile['model_api'] = {'provider': 'custom'}
        with patch.object(main, 'search_budget', return_value=5), \
                patch.object(main, 'codex_lookup', side_effect=RuntimeError('额度不足')), \
                patch.object(main, 'codex_text', return_value=(json.dumps(self.payload), None)) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['寿回✋'])
        model.assert_called_once()


if __name__ == '__main__':
    unittest.main()
