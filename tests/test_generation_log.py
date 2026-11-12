import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generation_log
import hybrid_search
import prompt_context as pc
from store import FriendStore

CONTEXT = '【SYSTEM_RULES】 SOURCE=program TYPE=RULE\nRULES\n\n【PERSONA】 SOURCE=friend.json/style TYPE=RULE\n{persona}'


class GenerationLogTests(unittest.TestCase):
    """2026-10-10 4stage: every generation keeps blocks, version, provider text, retrieval hits, output, time."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FriendStore(Path(self.temp.name))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def test_record_prompt_keeps_blocks_retrieval_output_and_delivery(self):
        with patch('reranker.score', lambda query, passages: [0.9] * len(passages)):
            hybrid_search.search('空调', ['空调坏了', 'days'], [0.8, 0.1], label='收藏表情')
        pc.record_prompt(self.store, 'normal reply', CONTEXT.format(persona='gentle'), 'GOAL: REPLY')
        pc.record_prompt(self.store, 'normal reply', '', '', final_prompt='只发新增部分', evidence={'provider': 'codex'})
        generation_log.finish(self.store, response='{"messages":["好"]}')
        generation_log.delivered(self.store)
        [row] = generation_log.recent(self.store)
        record = generation_log.get(self.store, row['id'])
        self.assertEqual(record['version'], pc.PROMPT_VERSION)
        self.assertEqual([b['key'] for b in json.loads(record['blocks'])], ['SYSTEM_RULES', 'PERSONA', 'TASK'])
        self.assertEqual((record['sent'], record['provider']), ('只发新增部分', 'codex'))
        self.assertEqual(record['response'], '{"messages":["好"]}')
        self.assertIsNotNone(record['ms'])
        self.assertEqual(record['delivered'], 1)
        [search] = json.loads(record['retrieval'])
        self.assertEqual((search['label'], search['hits'][0][0]), ('收藏表情', '空调坏了'))
        self.assertEqual(hybrid_search.drain_trace(), [])  # taken by the record

    def test_the_models_own_lookups_join_the_retrieval_list(self):
        pc.record_prompt(self.store, 'normal reply', CONTEXT.format(persona='gentle'), 'GOAL: REPLY')
        generation_log.finish(self.store, response='{}', tool_calls=[
            {'type': 'mcpToolCall', 'tool': 'search_friend_memory', 'status': 'completed',
             'arguments': {'query': '周末'}, 'result': {'content': [{'text': '周末常去爬山'}]}}])
        [row] = generation_log.recent(self.store)
        [lookup] = json.loads(generation_log.get(self.store, row['id'])['retrieval'])
        self.assertEqual((lookup['label'], lookup['query'], lookup['tool']), ('模型自己查：search_friend_memory', '周末', True))
        self.assertIn('周末常去爬山', lookup['hits'][0][0])

    def test_session_modes_without_their_own_recorder_still_record_what_was_sent(self):
        import main
        pc.record_prompt(self.store, '普通聊天续话判断', CONTEXT.format(persona='gentle'), 'GOAL: CONTINUE')

        def provider(prompt, directory, thread_id, profile=None, record_input=None, **_):
            record_input('续话增量', {'provider': 'codex'})
            return '{"action":"wait"}', 't1'
        with patch.object(main, 'codex_text', provider), \
                patch('prompt_context.session_prompt', lambda store, prompt, mode, compactions=None: ('续话增量', 't1', lambda _: None)):
            main.session_codex_text(self.store, 'FULL', Path(self.temp.name), '普通聊天续话判断',
                                    {'model_api': {'provider': 'codex'}})
        [row] = generation_log.recent(self.store)
        record = generation_log.get(self.store, row['id'])
        self.assertEqual((record['mode'], record['sent'], record['provider']), ('普通聊天续话判断', '续话增量', 'codex'))
        self.assertEqual(record['response'], '{"action":"wait"}')

    def test_compare_marks_changed_blocks_and_old_rows_are_pruned(self):
        pc.record_prompt(self.store, 'normal reply', CONTEXT.format(persona='gentle'), 'GOAL: REPLY')
        pc.record_prompt(self.store, 'normal reply', CONTEXT.format(persona='活泼'), 'GOAL: REPLY')
        new, old = (generation_log.get(self.store, r['id']) for r in generation_log.recent(self.store))
        changes = {key: changed for key, _, _, changed in generation_log.compare(old, new)}
        self.assertEqual(changes, {'SYSTEM_RULES': False, 'PERSONA': True, 'TASK': False})
        with patch.object(generation_log, 'KEEP', 3):
            for _ in range(5):
                pc.record_prompt(self.store, 'proactivedays', CONTEXT.format(persona='gentle'), 'GOAL: CHAT')
        self.assertEqual(len(generation_log.recent(self.store)), 3)


if __name__ == '__main__':
    unittest.main()
