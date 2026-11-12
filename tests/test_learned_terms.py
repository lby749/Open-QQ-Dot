import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from store import FriendStore


class LearnedTermTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.store.set_memory('原有偏好：回复简短')
        self.term = {'term': '滚木', 'meaning': '抽象语境中表示没有、虚无；不按字面理解。',
                     'source_url': 'https://example.com/meme', 'confirmed': True}
        self.searches = [{'query': '滚木 梗 含义', 'results': [{'url': self.term['source_url']}]}]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def repeat(self):
        self.store.add_message('1', 'friend', '奖励是滚木')
        self.store.add_message('2', 'friend', '又抽到滚木')

    def test_verified_repeat_preserves_memory_deduplicates_and_survives_reopen(self):
        self.repeat()
        main.save_learned_terms(self.store, [self.term], self.searches)
        main.save_learned_terms(self.store, [self.term], self.searches)
        self.assertIn('原有偏好：回复简短', self.store.memory())
        self.assertTrue(self.store.memory().count('[checked useful terms] 滚木: ') == 1 or self.store.memory().count('[checked useful terms] 滚木: ') == 1)
        other = FriendStore(self.directory / 'other')
        self.assertEqual(other.memory(), '')
        other.close()
        self.store.close()
        self.store = FriendStore(self.directory)
        self.assertIn(self.term['source_url'], self.store.memory())

    def test_once_and_bot_repetitions_do_not_count(self):
        self.store.add_message('1', 'friend', '滚木滚木')
        self.store.add_message('2', 'me', '滚木')
        main.save_learned_terms(self.store, [self.term], self.searches)
        self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_no_search_unconfirmed_unrelated_query_or_fabricated_source_do_not_save(self):
        self.repeat()
        for terms, searches in [([self.term], []), ([dict(self.term, confirmed=False)], self.searches),
                                ([self.term], [{'query': '别的词', 'results': self.searches[0]['results']}]),
                                ([dict(self.term, source_url='https://fake.example/')], self.searches),
                                ([dict(self.term, meaning=['bad'])], self.searches)]:
            main.save_learned_terms(self.store, terms, searches)
            self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_reply_saves_only_current_search_and_never_exposes_metadata(self):
        self.repeat()
        response = json.dumps({'messages': ['那不就啥也没有嘛'], 'learned_terms': [self.term]})
        def generate(*args, **kwargs):
            main.SERVER.last_searches = self.searches
            return response, 'thread'
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', side_effect=generate):
            self.assertEqual(main.make_reply(self.store, {}, self.directory), ['那不就啥也没有嘛'])
        self.assertIn('[checked useful terms]', self.store.memory())
        self.store.set_memory('原有偏好：回复简短')
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=(response, 'thread')):
            main.make_reply(self.store, {}, self.directory)
        self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_invalid_reply_cannot_write_memory(self):
        self.repeat()
        def generate(*args, **kwargs):
            main.SERVER.last_searches = self.searches
            return json.dumps({'messages': [], 'learned_terms': [self.term]}), 'thread'
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', side_effect=generate), self.assertRaises(RuntimeError):
            main.make_reply(self.store, {}, self.directory)
        self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_memory_refresh_preserves_verified_terms_and_rejects_new_guesses(self):
        self.repeat()
        main.save_learned_terms(self.store, [self.term], self.searches)
        with patch.object(main, 'codex_text', return_value=(json.dumps({'changes': [{'op': 'add', 'id': '', 'text': '[checked useful terms] 滚木: ', 'kind': 'fact', 'keywords': [], 'sources': ['1']}]}), None)):
            main.refresh_memory_if_due(self.store, {}, self.directory, force=True)
        self.assertTrue('[checked useful terms] 滚木: ' in self.store.memory() or '[checked useful terms] 滚木: ' in self.store.memory())
        self.assertNotIn('猜测', self.store.memory())

    def test_ambiguous_candidate_survives_restart_and_promotes_on_new_context_without_search(self):
        self.store.add_message('1', 'friend', '奖励是滚木')
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.assertNotIn('[checked useful terms]', self.store.memory())
        self.assertIn('滚木', self.store.get('term_candidates'))
        self.store.close()
        self.store = FriendStore(self.directory)
        self.store.add_message('2', 'friend', '这里滚木就是说啥也没抽到')
        main.save_learned_terms(self.store, [self.term], [])
        self.assertTrue('[checked useful terms] 滚木: ' in self.store.memory() or '[checked useful terms] 滚木: ' in self.store.memory())
        self.assertEqual(json.loads(self.store.get('term_candidates')), {})

    def test_same_message_retry_and_bot_echo_cannot_promote_candidate(self):
        self.repeat()
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.store.add_message('echo', 'me', '滚木')
        main.save_learned_terms(self.store, [self.term], [])
        self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_new_occurrence_still_ambiguous_or_different_meaning_does_not_promote(self):
        self.repeat()
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.store.add_message('3', 'friend', '滚木这张卡')
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], [])
        main.save_learned_terms(self.store, [dict(self.term, meaning='游戏中的卡牌')], [])
        self.assertNotIn('[checked useful terms]', self.store.memory())
        self.assertIn('滚木', self.store.get('term_candidates'))

    def test_unsearched_candidate_or_unrelated_new_message_cannot_promote(self):
        self.repeat()
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], [])
        self.assertIsNone(self.store.get('term_candidates'))
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.store.add_message('3', 'friend', 'Hello')
        main.save_learned_terms(self.store, [self.term], [])
        self.assertNotIn('[checked useful terms]', self.store.memory())

    def test_candidate_reaches_next_reply_and_can_be_confirmed_by_deepseek(self):
        self.repeat()
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.store.add_message('3', 'friend', '这个滚木就是没有奖品的意思')
        response = json.dumps({'messages': ['抽了个寂寞嘛'], 'learned_terms': [self.term]})
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=(response, None)) as model:
            main.make_reply(self.store, {'model_api': {'provider': 'deepseek'}}, self.directory)
        self.assertIn('TERM_CANDIDATES_RULE', model.call_args.args[0])
        self.assertIn(self.term['source_url'], model.call_args.args[0])
        self.assertTrue('[checked useful terms] 滚木: ' in self.store.memory() or '[checked useful terms] 滚木: ' in self.store.memory())

    def test_clear_history_clears_candidates_but_keeps_memory(self):
        self.repeat()
        main.save_learned_terms(self.store, [dict(self.term, confirmed=False)], self.searches)
        self.store.clear_messages()
        self.assertIsNone(self.store.get('term_candidates'))
        self.assertEqual(self.store.memory(), '原有偏好：回复简短')


if __name__ == '__main__':
    unittest.main()
