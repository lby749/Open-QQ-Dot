import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
import topic_research
from store import FriendStore
from topic_library import call_tool


def item(topic, url, title='标题', summary='摘要内容', keywords=('关键词',)):
    return {'topic': topic, 'summary': summary, 'source_title': title, 'url': url, 'published': '2026-10-01',
            'keywords': list(keywords)}


class TopicResearchTests(unittest.TestCase):
    """2026-10-11 user: per-friend topic library, web research at the user's frequencies, used by ordinary replies."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.profile = {'qq_id': '12345', 'basic_info': {'interests': 'Genshin', 'deep_interests': '明朝历史'}}
        (self.directory / 'friend.json').write_text(json.dumps(self.profile, ensure_ascii=False), 'utf-8')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        embedder = patch('embedding_provider.embed_unscoped', side_effect=RuntimeError('no model'))  # URL check only
        self.embed = embedder.start()
        self.addCleanup(embedder.stop)
        self.research_items = []
        self.calls = []

    def generate(self, prompt, directory, **kwargs):
        self.calls.append((prompt, kwargs))
        if ' [INTEREST_TOPICS] ' in prompt:
            return json.dumps({'topics': ['Genshin', '明朝历史']}, ensure_ascii=False), None
        return json.dumps({'items': self.research_items}, ensure_ascii=False), None

    def run_kind(self, kind):
        return topic_research.research(self.directory, self.profile, kind, self.generate)

    def test_normal_research_stores_checked_items_and_waits_for_its_hours(self):
        self.research_items = [item('Genshin', 'https://a.example/1', '新版本前瞻', keywords=['Genshin', '新版本', '角色']),
                               item('Genshin', 'not a url'), item('无关主题', 'https://a.example/2'),
                               item('Genshin', 'https://a.example/1')]
        run = self.run_kind('normal')
        self.assertEqual((run['status'], run['added'], run['dropped']), ('ready', 1, 2))
        prompt, kwargs = self.calls[-1]
        self.assertIn(' [TOPIC_RESEARCH] ', prompt)
        self.assertTrue(kwargs['web_search'])
        self.assertIn('"topics": ["Genshin"]', prompt)  #  is a deep topic
        self.assertFalse(any(' [INTEREST_TOPICS] ' in p for p, _ in self.calls))  # fields used as written, no model
        [stored] = topic_research.items(self.directory)
        self.assertEqual((stored['title'], stored['kind']), ('新版本前瞻', 'normal'))
        self.assertTrue(stored['expires_at'])  # normal items expire
        self.assertIsNone(self.run_kind('normal'))  # not due again for 24 h
        topic_research.request_refresh(self.directory, 'normal')
        self.research_items = [item('Genshin', 'https://a.example/1')]  # the same URL again
        self.assertEqual(self.run_kind('normal')['added'], 0)
        self.assertEqual(len(topic_research.items(self.directory)), 1)

    def test_memory_interests_are_picked_by_the_model_and_cached(self):
        self.profile['basic_info'] = {'notes': '（明朝历史）只是备注'}  # notes are plain notes now
        from memory_management import edit_entry, entries
        self.store.set_memory('最近在玩原神')
        edit_entry(self.store, entries(self.store)[0]['id'], self.store.memory_revision(), category='兴趣')
        self.assertEqual(topic_research.interests(self.directory, self.profile, self.generate), (['Genshin', '明朝历史'], []))
        self.assertEqual(sum(' [INTEREST_TOPICS] ' in p for p, _ in self.calls), 1)
        self.assertNotIn('只是备注', self.calls[0][0])
        topic_research.interests(self.directory, self.profile, self.generate)
        self.assertEqual(sum(' [INTEREST_TOPICS] ' in p for p, _ in self.calls), 1)  # cached until memory changes

    def test_deep_topics_come_from_their_field_and_do_not_expire(self):
        self.research_items = [item('明朝历史', f'https://b.example/{i}') for i in range(7)]
        run = self.run_kind('deep')
        self.assertEqual(run['added'], topic_research.PER_TOPIC['deep'])
        self.assertTrue(all(row['expires_at'] is None for row in topic_research.items(self.directory)))
        self.assertEqual(topic_research.state(self.directory)['interests']['deep'], ['明朝历史'])

    def test_retold_articles_are_dropped_on_the_first_run_and_later(self):
        # stub embedder: the same text is the same vector (cosine 1.0), other texts are orthogonal
        texts = {}
        self.embed.side_effect = lambda values, priority=1: [
            [1.0 if i == texts.setdefault(value.split(': ', 1)[1].split('. ', 1)[1], len(texts)) else 0.0
             for i in range(8)] for value in values]
        self.research_items = [item('Genshin', 'https://a.example/1', '版本前瞻', '同一篇文章'),
                               item('Genshin', 'https://a.example/2', '版本前瞻（转载）', '同一篇文章')]
        self.assertEqual(self.run_kind('normal')['added'], 1)  # first run, two copies in one batch
        topic_research.request_refresh(self.directory, 'normal')
        self.research_items = [item('Genshin', 'https://a.example/3', '又一次转载', '同一篇文章'),
                               item('Genshin', 'https://a.example/4', '新角色', '另一篇文章')]
        self.assertEqual(self.run_kind('normal')['added'], 1)  # a copy of a stored item
        self.assertEqual(sorted(row['title'] for row in topic_research.items(self.directory)), ['新角色', '版本前瞻'])

    def test_new_interests_are_researched_on_manual_refresh(self):
        # 2026-10-11 user: no automatic rerun when interests change;  re-reads them and searches now
        self.profile['basic_info'] = {}
        self.assertEqual(self.run_kind('normal')['status'], 'no_interests')
        self.profile['basic_info']['interests'] = 'Genshin'
        self.assertIsNone(self.run_kind('normal'))  # waits for its hours
        topic_research.request_refresh(self.directory, 'normal')
        self.research_items = [item('Genshin', 'https://a.example/1')]
        self.assertEqual((self.run_kind('normal')['status'], len(topic_research.items(self.directory))), ('ready', 1))

    def test_zero_hours_is_manual_only_and_switch_off_does_nothing(self):
        self.profile['research_refresh_hours'] = {'normal': 0}
        self.assertIsNone(self.run_kind('normal'))
        self.assertEqual(self.calls, [])
        topic_research.request_refresh(self.directory, 'normal')
        self.assertEqual(self.run_kind('normal')['status'], 'ready')
        self.profile['daily_research_enabled'] = False
        topic_research.request_refresh(self.directory, 'normal')
        self.assertIsNone(self.run_kind('normal'))

    def test_failure_keeps_items_and_retries_later(self):
        self.research_items = [item('Genshin', 'https://a.example/1')]
        self.run_kind('normal')
        topic_research.request_refresh(self.directory, 'normal')
        with patch.object(self, 'generate', side_effect=RuntimeError('Codex 模型账号额度不足')):
            run = topic_research.research(self.directory, self.profile, 'normal', self.generate)
        self.assertEqual((run['status'], run['error']), ('failed', '模型账号额度不足'))
        self.assertLessEqual(run['next_refresh'], time.time() + topic_research.RETRY_SECONDS + 1)
        self.assertEqual(len(topic_research.items(self.directory)), 1)

    def test_index_recall_and_the_models_own_lookup(self):
        self.research_items = [item('Genshin', 'https://a.example/1', '枫丹剧情解析', '水神芙宁娜的审判剧情回顾',
                                    ['Genshin', '芙宁娜', '枫丹', '剧情']),
                               item('Genshin', 'https://a.example/2', '新角色前瞻', '下个版本的新角色和卡池',
                                    ['Genshin', '卡池', '抽卡'])]
        self.run_kind('normal')
        self.assertTrue(topic_research.topic_index(self.directory) in ('Genshin（2）', 'Genshin (2) ', 'Genshin(2)'))
        with patch('embedding_provider.embed_unscoped', side_effect=RuntimeError('no model')), \
                patch('reranker.score', side_effect=lambda q, texts: [0.9 if '芙宁娜' in t else 0.01 for t in texts]):
            block = topic_research.recall_block(self.directory, '芙宁娜那段剧情我哭了')
        self.assertIn('枫丹剧情解析', block)
        self.assertNotIn('新角色前瞻', block)
        found = call_tool(self.directory, 'search_topic_library', {'query': '抽卡'})['items']
        self.assertEqual([row['title'] for row in found], ['新角色前瞻'])
        self.assertEqual(call_tool(self.directory, 'search_topic_library', {'query': '足球比赛'})['items'], [])

    def test_removed_items_leave_the_index_and_are_not_stored_again(self):
        self.research_items = [item('Genshin', 'https://a.example/1')]
        self.run_kind('normal')
        topic_research.remove(self.directory, topic_research.items(self.directory)[0]['id'])
        self.assertEqual(topic_research.topic_index(self.directory), '')
        topic_research.request_refresh(self.directory, 'normal')
        self.assertEqual(self.run_kind('normal')['added'], 0)

    def test_only_ordinary_replies_get_the_library(self):
        self.research_items = [item('Genshin', 'https://a.example/1', '枫丹剧情解析')]
        self.run_kind('normal')
        self.store.record_inbound('m1', '最近在玩原神')
        model = Mock(return_value=(json.dumps({'messages': ['好耶']}), None))
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', model):
            main.make_reply(self.store, self.profile, self.directory)
            reply = model.call_args.args[0]
            main.make_round(self.store, self.profile, self.directory, 1)
            proactive = model.call_args.args[0]
        self.assertTrue('【TOPIC_LIBRARY】' in reply or ' [TOPIC_LIBRARY] ' in reply)
        self.assertTrue('【TOPIC_INDEX】' in reply or ' [TOPIC_INDEX] ' in reply)
        self.assertNotIn(' [TOPIC_INDEX] ', proactive)
        self.assertNotIn(' [TOPIC_LIBRARY] ', proactive)


if __name__ == '__main__':
    unittest.main()
