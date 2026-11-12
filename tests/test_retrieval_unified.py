"""Callers of the unified retrieval (2026-10-26): engine floors and stale vectors, task dedupe, failure matching,
agent fix memory and its history, the cross-agent memory hub, and the side-by-side reply retrieval."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import retrieval_core
from persona_store import Unavailable


def unit(*values):
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def fake_vector(text):
    """A deterministic 'embedding': texts sharing a topic word point roughly the same way (cosine about 0.6-0.8),
    identical texts exactly the same way."""
    import hashlib
    topic = [0.0] * 9
    for n, word in enumerate(('locked', 'calc', 'README', 'certificate', '证书', 'GPU', '显存', 'CSV', 'utf-8')):
        if word.lower() in text.lower():
            topic[n] = 1.0
    noise = [b / 255 - 0.5 for b in hashlib.sha256(text.encode()).digest()[:9]]
    return unit(*[t + 0.6 * x for t, x in zip(topic, noise)])


class Models:
    """Real-looking local models without loading any: embeddings and reranker scores from keywords."""

    def __init__(self, rerank):
        self.rerank = rerank
        self.embedded = []

    def __enter__(self):
        self.patches = [patch('house_settings.local_models_enabled', return_value=True),
                        patch.object(retrieval_core, 'installed', lambda kind=None: ['BAAI/bge-m3', 'BAAI/bge-reranker-v2-m3']),
                        patch('embedding_provider.embed_unscoped', side_effect=self.embed),
                        patch('reranker.score', side_effect=lambda q, ps: [self.rerank(q, p) for p in ps])]
        for item in self.patches:
            item.start()
        return self

    def embed(self, texts, **kwargs):
        self.embedded.append(list(texts))
        return [fake_vector(text) for text in texts]

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class EngineTests(unittest.TestCase):
    def setUp(self):
        import local_retrieval_engine as lre
        self.lre = lre
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.engine = lre.LocalHybridSearchEngine(self.dir / 'retrieval_engine.sqlite3')
        self.store = lre.TaskExperienceStore(self.engine)
        retrieval_core.reset_stats()

    def record(self, problem, action, owner='alice', failed=False, shared=False):
        status = (self.lre.ExperienceResultStatus.FAILED_CONFIRMED if failed
                  else self.lre.ExperienceResultStatus.EXECUTION_FINISHED)
        return self.store.record_experience(task_id='t-' + problem[:8], owner_id=owner, project_id='', agent_provider='codex',
                                            model_name='', tools_used=['x'], problem_statement=problem,
                                            environment_conditions={}, action_taken=action, result_status=status,
                                            is_shared=shared)

    def test_experiences_floor_small_talk_and_one_fts_row(self):
        rerank = lambda q, p: 0.9 if ('locked' in q and 'locked' in p) else 0.01
        with Models(rerank):
            first = self.record('sqlite database is locked', 'enable WAL')
            self.record('git push rejected', 'rebase first')
            self.store.record_experience(task_id=first.task_id, owner_id='alice', project_id='', agent_provider='codex',
                                         model_name='', tools_used=[], problem_statement='sqlite database is locked',
                                         environment_conditions={}, action_taken='enable WAL',
                                         result_status=self.lre.ExperienceResultStatus.EXECUTION_FINISHED)
            hits = self.store.search_experiences('database is locked again', 'alice')
            self.assertEqual([h['problem_statement'] for h in hits], ['sqlite database is locked'])  # once: the retold copy
            self.assertEqual(self.store.search_experiences('hello', 'alice'), [])
            self.assertEqual(self.store.search_experiences('写一首关于秋天的诗', 'alice'), [])
        self.assertNotIn('vector_json', hits[0])
        with self.engine._get_connection() as conn:
            fts = conn.execute('SELECT experience_id, COUNT(*) FROM task_experiences_fts GROUP BY experience_id').fetchall()
        self.assertTrue(all(count == 1 for _, count in fts))

    def test_vectors_of_another_model_are_re_embedded_not_compared(self):
        with Models(lambda q, p: 0.9 if 'locked' in p else 0.0) as models:
            self.record('sqlite database is locked', 'enable WAL')
            with self.engine._get_connection() as conn:  # as if an earlier model had written a 2-dimensional vector
                conn.execute("UPDATE task_experiences SET vector_json='[1.0, 0.0]', vector_signature='old-model'")
            (self.dir / 'retrieval_cache.sqlite3').unlink()  # nothing cached: the stale row must be embedded again
            models.embedded.clear()
            hits = self.store.search_experiences('database is locked', 'alice')
        self.assertEqual(len(hits), 1)
        self.assertTrue(any('Problem: sqlite database is locked' in text for batch in models.embedded for text in batch))
        with self.engine._get_connection() as conn:
            signature, vector = conn.execute('SELECT vector_signature, vector_json FROM task_experiences').fetchone()
        self.assertEqual(signature, self.engine.vector_signature())
        self.assertEqual(len(json.loads(vector)), 9)

    def tasks(self, rows):
        with closing(sqlite3.connect(self.dir / 'tasks.sqlite3')) as db:
            db.execute('CREATE TABLE tasks (task_id TEXT, title TEXT, description TEXT, status TEXT, result TEXT, '
                       'checkpoint TEXT, created_at TEXT, owner_id TEXT)')
            for n, (task_id, title, description) in enumerate(rows):
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)', (task_id, title, description, 'RUNNING', '', '{}',
                                                                       f'2026-10-{10 + n}', 'alice'))
            db.commit()

    def test_dedupe_needs_the_reranker_and_the_same_identifiers(self):
        self.tasks([('D1', '生成calc.py的单元测试', '为calc.py写pytest测试'), ('D2', '把README翻译成英文', '翻译README.md')])
        dedup = self.lre.TaskDeduplicatorAndFailureDetector(self.engine)
        rerank = lambda q, p: 0.99 if ('calc' in q and 'calc' in p) else 0.5
        # the shortlist bar is the measured 0.80 for BGE-M3; these fake vectors are noisier
        with Models(rerank) as models, patch.object(self.lre, 'DEDUP_COSINE', 0.3):
            found, _, info = dedup.check_duplicate_task('为calc.py写pytest单元测试', '覆盖add', owner_id='alice')
            self.assertEqual((found, info['task_id']), (True, 'D1'))
            found, _, _ = dedup.check_duplicate_task('为utils.py写pytest单元测试', '覆盖add calc', owner_id='alice')
            self.assertFalse(found)  # utils.py vs calc.py: never merged, whatever the scores
            found, _, _ = dedup.check_duplicate_task('把README翻译成法语', '翻译README.md', owner_id='alice')
            self.assertFalse(found)  # similar (rerank 0.5) is not the same task
            batches = [len(batch) for batch in models.embedded]
        self.assertLessEqual(max(batches), 2)  # candidates in one batch, then cached: not one call per task

    def test_failure_needs_a_confirmed_match(self):
        dedup = self.lre.TaskDeduplicatorAndFailureDetector(self.engine)
        rerank = lambda q, p: 0.95 if ('locked' in q and 'locked' in p) else 0.005
        with Models(rerank):
            dedup.record_validated_failure_resolution('sqlite3.OperationalError: database is locked', 'db', 'use WAL', 'alice')
            self.assertTrue(dedup.diagnose_failure_and_suggest('OperationalError: database is locked while writing', 'db', 'alice')[0])
            self.assertFalse(dedup.diagnose_failure_and_suggest("KeyError: 'user_id'", 'db', 'alice')[0])

    def test_context_starts_with_the_same_text_and_labels_unverified_attempts(self):
        builder = self.lre.DynamicContextBuilder(self.store, self.lre.CapabilityDiscoveryService(self.engine))
        with Models(lambda q, p: 0.9 if 'locked' in p else 0.0):
            self.record('sqlite database is locked', 'enable WAL')
            first = builder.build_context('task_a', 'database is locked again', 'alice', max_tokens=2000)
            second = builder.build_context('task_b', 'git push fails', 'alice', max_tokens=2000)
            tight = builder.build_context('task_c', 'database is locked again', 'alice', max_tokens=150)
        layer1 = lambda text: text.split('[Layer 2')[0]
        self.assertEqual(layer1(first), layer1(second))  # a stable, cacheable prefix
        self.assertIn('task_a', first.split('[Layer 2')[1])
        self.assertIn('[UNVERIFIED] Earlier attempt', first)
        self.assertNotIn('[VERIFIED]', first)
        self.assertLessEqual(len(tight), 150 * 4 + 60)

    def test_one_engine_per_database(self):
        a = self.lre.get_local_retrieval_engine(self.dir / 'a.sqlite3')
        b = self.lre.get_local_retrieval_engine(self.dir / 'b.sqlite3')
        self.assertIs(self.lre.get_local_retrieval_engine(self.dir / 'a.sqlite3'), a)
        self.assertIsNot(a, b)


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
        self.scope, self.source = MemoryScope, MemorySource
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.store = AgentMemoryStore(self.dir / 'agent_memory.sqlite3')

    def test_chinese_requests_find_their_fix_and_unrelated_ones_none(self):
        self.store.record_failure_experience('database', '写入 tasks 数据库', 'database is locked：多个进程同时写入',
                                             '开启 WAL 并设置 busy_timeout', owner_id='alice')
        self.store.record_failure_experience('encoding', '读取中文 CSV', "'gbk' codec can't decode", '用 utf-8-sig 打开',
                                             owner_id='alice')
        with patch('embedding_provider.embed_unscoped', side_effect=Unavailable('off')), \
                patch('reranker.score', side_effect=Unavailable('off')):  # the keyword path (no models)
            found = self.store.retrieve_experience_remedies('写入 tasks 数据库时提示 database is locked', 'alice')
            self.assertEqual(len(found), 1)
            self.assertIn('WAL', found[0])
            self.assertEqual(self.store.retrieve_experience_remedies('Translate the README to French', 'alice'), [])
            self.assertEqual(self.store.search_experience('读取中文CSV报错', 'alice')[0]['solution'], '用 utf-8-sig 打开')

    def test_updates_keep_the_replaced_version(self):
        first = self.store.store_memory(self.scope.USER_PREFERENCE, 'alice', 'reply_length', '喜欢长回复')
        self.store.store_memory(self.scope.USER_PREFERENCE, 'alice', 'reply_length', '喜欢短回复')
        self.store.store_memory(self.scope.USER_PREFERENCE, 'alice', 'reply_length', '喜欢短回复')  # unchanged: no entry
        self.store.correct_memory(first, 'alice', '喜欢一句话回复', reason='用户纠正')
        history = self.store.history(first, 'alice')
        self.assertEqual([h['content'] for h in history], ['喜欢长回复', '喜欢短回复'])
        self.assertTrue(history[1]['reason'].startswith('corrected'))
        with self.assertRaises(PermissionError):
            self.store.history(first, 'bob')


class HubTests(unittest.TestCase):
    def setUp(self):
        import local_retrieval_engine as lre
        from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
        self.lre = lre
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.memory = AgentMemoryStore(self.dir / 'agent_memory.sqlite3')
        self.engine_db = self.dir / 'retrieval_engine.sqlite3'
        engine = lre.get_local_retrieval_engine(self.engine_db)
        self.experiences = lre.TaskExperienceStore(engine)
        with Models(lambda q, p: 0.0):
            self.memory.store_memory(MemoryScope.PROJECT_MEMORY, 'alice', 'db', 'GPU 项目用 sqlite 存任务', project_id='p1')
            self.memory.store_memory(MemoryScope.USER_PREFERENCE, 'alice', 'style', '显存不够时优先换 CPU', project_id='p1')
            self.memory.record_failure_experience('gpu', '加载模型', '显存不足 CUDA out of memory', '释放其他模型或改用 CPU',
                                                  owner_id='alice', project_id='p1', verified=True)
            for owner, shared, failed, problem in (('alice', 0, True, 'GPU 显存不足时关掉校验'),
                                                    ('bob', 1, False, 'GPU 显存不足：分批推理'),
                                                    ('bob', 0, False, 'GPU 显存 bob 私有项目')):
                self.experiences.record_experience(
                    task_id='t-' + problem, owner_id=owner, project_id='', agent_provider='claude', model_name='',
                    tools_used=[], problem_statement=problem, environment_conditions={}, action_taken='do it',
                    result_status=(lre.ExperienceResultStatus.FAILED_CONFIRMED if failed
                                   else lre.ExperienceResultStatus.EXECUTION_FINISHED), is_shared=bool(shared))

    def recall(self, owner, project=''):
        import memory_hub
        with Models(lambda q, p: 0.8 if ('GPU' in p or '显存' in p) else 0.0):
            return memory_hub.recall('GPU 显存不够怎么办', owner, project_id=project, limit=10,
                                     memory_db=self.dir / 'agent_memory.sqlite3', engine_db=self.engine_db)

    def test_kinds_provenance_and_who_sees_what(self):
        import memory_hub
        hits = self.recall('alice', 'p1')
        kinds = sorted(hit['kind'] for hit in hits)
        self.assertEqual(kinds, ['pitfall', 'preference', 'project_fact', 'verified_fix'])
        # bob's shared record is not verified, his private one is private: neither reaches alice
        self.assertFalse(any(hit['source']['owner'] == 'bob' for hit in hits))
        fix = next(hit for hit in hits if hit['kind'] == 'verified_fix')
        self.assertEqual((fix['source']['store'], fix['source']['project']), ('agent_memory', 'p1'))
        self.assertIn('失败做法，不要照做', memory_hub.render([next(h for h in hits if h['kind'] == 'pitfall')]))
        other_project = sorted(hit['kind'] for hit in self.recall('alice', 'p2'))
        self.assertEqual(other_project, ['pitfall', 'preference', 'verified_fix'])  # the project fact stays in p1
        self.assertEqual(self.recall('carol'), [])


class ReplyRetrievalTests(unittest.TestCase):
    def test_side_by_side_results_and_trace_reach_the_calling_thread(self):
        import hybrid_search
        import prompt_context

        def memory(directory, query):
            hybrid_search._record('Long-term memory', query, 1, [], 0, False, ['x'])
            return 'M'

        def topics(directory, query):
            hybrid_search._record('话题资料', query, 1, [], 0, False, ['x'])
            return 'T'
        with tempfile.TemporaryDirectory() as folder, patch('memory_management.recall_block', memory), \
                patch('topic_research.recall_block', topics), patch('topic_research.topic_index', return_value='idx'), \
                patch('retrieval_api.embed', return_value=[[1.0]]) as embed:
            hybrid_search.drain_trace()
            result = prompt_context.retrieved_blocks({}, folder, '团子呢', '团子呢', None, 'reply', True, False)
            trace = hybrid_search.drain_trace()
        self.assertEqual(result, ('M', [('TOPIC_INDEX', 'idx'), ('RETRIEVED_TOPICS', 'T')], ''))
        self.assertEqual([item['label'] for item in trace], ['Long-term memory', '话题资料'])
        self.assertEqual(embed.call_args.args[0], ['团子呢'])  # the prefetch: the lines both searches use


if __name__ == '__main__':
    unittest.main()
