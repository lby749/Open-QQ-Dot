"""retrieval_core: model choice, caches, vector spaces and result hygiene (2026-10-26)."""
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import hybrid_search
import retrieval_api
import retrieval_core as rc
from persona_store import Unavailable


def unit(*values):
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.friend = Path(self.temp.name)
        (self.friend / 'friend.json').write_text('{}', 'utf-8')
        for target in (patch('house_settings.local_models_enabled', return_value=True),
                       patch.object(rc, '_free_vram_mb', return_value=None), patch.object(rc, '_loaded', return_value=False),
                       patch.object(rc, 'overrides', return_value={})):
            target.start()
            self.addCleanup(target.stop)

    def installed(self, *names):
        return patch.object(rc, 'installed', lambda kind=None: sorted(
            name for name in names if kind is None or rc.registry()[name].kind == kind))

    def test_registry_holds_the_five_embedding_and_four_rerank_presets(self):
        import small_models
        models = rc.registry()
        for kind in ('embedding', 'rerank'):
            self.assertEqual({name for name, model in models.items() if model.kind == kind}, set(small_models.PRESETS[kind]))

    def test_best_installed_by_default_fast_one_for_much_text(self):
        with self.installed('BAAI/bge-reranker-v2-m3', 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'):
            self.assertEqual(rc.choose_reranker('memory', '团子', ['橘猫'], self.friend).model, 'BAAI/bge-reranker-v2-m3')
            big = ['很长的一段话' * 400] * 10
            choice = rc.choose_reranker('chat', '团子', big, self.friend)
        self.assertEqual(choice.model, 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1')
        self.assertEqual(choice.kind, 'preset')
        self.assertTrue(choice.value['model_path'].endswith('mmarco-mMiniLMv2-L12-H384-v1'))
        self.assertIn('fastest', choice.reason)

    def test_english_only_reranker_is_not_used_for_chinese(self):
        with self.installed('cross-encoder/ms-marco-MiniLM-L6-v2', 'BAAI/bge-reranker-base'):
            self.assertEqual(rc.choose_reranker('tools', 'deploy the container', ['k8s deploy tool'], self.friend).model,
                             'BAAI/bge-reranker-base')  # better quality wins for English too
            with patch.object(rc, 'installed', lambda kind=None: ['cross-encoder/ms-marco-MiniLM-L6-v2']):
                zh = rc.choose_reranker('memory', '团子今天又拆家了', ['养了一只橘猫'], self.friend)
        self.assertEqual(zh.kind, 'none')  # automatic choice must not score Chinese with an English-only model
        self.assertIn('language', zh.reason)

    def test_low_gpu_memory_picks_a_smaller_model_unless_the_big_one_is_loaded(self):
        with self.installed('BAAI/bge-reranker-v2-m3', 'BAAI/bge-reranker-base'), \
                patch.object(rc, '_free_vram_mb', return_value=1500):
            self.assertEqual(rc.choose_reranker('memory', '团子', ['橘猫'], self.friend).model, 'BAAI/bge-reranker-base')
            with patch.object(rc, '_free_vram_mb', return_value=300):  # nothing fits: the smallest
                self.assertIn('smallest', rc.choose_reranker('memory', '团子', ['橘猫'], self.friend).reason)
            with patch.object(rc, '_loaded', lambda name: name == 'BAAI/bge-reranker-v2-m3'):
                self.assertEqual(rc.choose_reranker('memory', '团子', ['橘猫'], self.friend).model, 'BAAI/bge-reranker-v2-m3')

    def test_overrides_and_friend_setting_win(self):
        with self.installed('BAAI/bge-reranker-v2-m3', 'BAAI/bge-reranker-base'):
            with patch.object(rc, 'overrides', return_value={'rerank': 'BAAI/bge-reranker-base'}):
                self.assertEqual(rc.choose_reranker('memory', 'a', ['b'], self.friend).reason, 'house override')
            with patch.object(rc, 'overrides', return_value={'rerank': 'off'}):
                self.assertEqual(rc.choose_reranker('memory', 'a', ['b'], self.friend).kind, 'none')
            with patch.object(rc, 'overrides', return_value={'rerank': 'cross-encoder/ms-marco-MiniLM-L6-v2'}):
                self.assertIn('not installed', rc.choose_reranker('memory', 'a', ['b'], self.friend).reason)
            (self.friend / 'friend.json').write_text(json.dumps({'retrieval_api': {'rerank': {
                'enabled': True, 'mode': 'api', 'base_url': 'https://rerank.example/v1', 'model': 'r1'}}}), 'utf-8')
            self.assertEqual(rc.choose_reranker('memory', 'a', ['b'], self.friend).kind, 'configured')

    def test_local_models_off_still_routes_to_the_builtin_reranker_which_refuses(self):
        with patch('house_settings.local_models_enabled', return_value=False):
            choice = rc.choose_reranker('memory', 'a', ['b'], self.friend)
        self.assertEqual((choice.kind, choice.model), ('builtin', 'BAAI/bge-reranker-v2-m3'))

    def test_another_embedding_preset_names_another_vector_space(self):
        from embedding_provider import VERSION
        with self.installed('BAAI/bge-m3'):
            self.assertEqual(retrieval_api.signature(self.friend), 'local:' + VERSION)  # unchanged: old caches stay valid
        with self.installed('BAAI/bge-small-zh-v1.5'), patch('small_models.signature', return_value='abc'):
            self.assertEqual(retrieval_api.signature(self.friend), 'local-preset:BAAI/bge-small-zh-v1.5:abc')
            with patch('small_models.embed', return_value=[[1.0, 0.0]]) as local:
                retrieval_api.embed(['团子'], self.friend, is_query=True)
        self.assertEqual(local.call_args.args[1]['query_prefix'], '为这个句子生成表示以用于检索相关文章：')
        self.assertTrue(local.call_args.args[2])  # is_query reaches the model (documents get no query prefix)


class CacheTests(unittest.TestCase):
    def setUp(self):
        rc.reset_stats()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_query_vectors_are_cached_per_embedder(self):
        with patch('embedding_provider.embed_unscoped', side_effect=lambda texts, **k: [[1.0, 0.0]] * len(texts)) as first:
            retrieval_api.embed(['团子', '橘猫'], None, is_query=True)
            retrieval_api.embed(['团子'], None, is_query=True)
        self.assertEqual([len(call.args[0]) for call in first.call_args_list], [2])
        self.assertEqual(first.call_args.kwargs['priority'], 0)  # queries run ahead of document batches
        with patch('embedding_provider.embed_unscoped', side_effect=lambda texts, **k: [[0.0, 1.0]] * len(texts)):
            self.assertEqual(retrieval_api.embed(['团子'], None, is_query=True), [[0.0, 1.0]])  # never another's vector

    def test_rerank_pairs_are_scored_once_and_batched_by_query(self):
        calls = []

        def score(query, passages):
            calls.append((query, list(passages)))
            return [0.5] * len(passages)
        with patch('house_settings.local_models_enabled', return_value=False), patch('reranker.score', score):
            rc.rerank_groups([('a', ['x', 'y']), ('b', ['x'])])
            rc.rerank_groups([('a', ['x', 'y', 'z'])])
        self.assertEqual(calls, [('a', ['x', 'y']), ('b', ['x']), ('a', ['z'])])
        self.assertEqual(rc.stats()['rerank_cache_hits'], 2)
        with patch.object(rc, 'overrides', return_value={'rerank': 'off'}), \
                patch('house_settings.local_models_enabled', return_value=True), \
                self.assertRaises(Unavailable):
            rc.rerank_groups([('a', ['x'])])

    def test_document_vectors_are_kept_per_model_signature(self):
        cache = rc.DocVectors(self.dir / 'cache.sqlite3')
        embedded = []

        def embed(texts):
            embedded.append(list(texts))
            return [[float(len(text)), 1.0] for text in texts]
        self.assertEqual(cache.get(['ab', 'c'], 'model-a', embed), [(2.0, 1.0), (1.0, 1.0)])
        cache.get(['ab', 'c', 'def'], 'model-a', embed)
        cache.get(['ab'], 'model-b', embed)  # another model: embedded again, never the other's vector
        self.assertEqual(embedded, [['ab', 'c'], ['def'], ['ab']])

    def test_cached_vectors_migrate_switch_spaces_and_switch_back(self):
        path = self.dir / 'memory_vectors.sqlite3'
        with closing(sqlite3.connect(path)) as db:  # the layout before 2026-10-26
            db.execute('CREATE TABLE vector_backend(signature TEXT)')
            db.execute("INSERT INTO vector_backend VALUES('old-model')")
            db.execute('CREATE TABLE vectors(id TEXT PRIMARY KEY, text TEXT NOT NULL, vector BLOB NOT NULL)')
            db.execute('INSERT INTO vectors VALUES(?,?,?)', ('e1', '团子', struct.pack('2f', 1.0, 0.0)))
            db.commit()
        embedded = []

        def embed(texts, directory=None, is_query=False):
            embedded.append(list(texts))
            return [[0.0, 1.0]] * len(texts)
        with patch('retrieval_api.signature', return_value='old-model'), patch('retrieval_api.embed', embed):
            self.assertEqual(hybrid_search.cached_vectors(path, {'e1': '团子'}), {'e1': (1.0, 0.0)})  # migrated, reused
        with patch('retrieval_api.signature', return_value='new-model'), patch('retrieval_api.embed', embed):
            self.assertEqual(hybrid_search.cached_vectors(path, {'e1': '团子'}), {'e1': (0.0, 1.0)})
        with patch('retrieval_api.signature', return_value='old-model'), patch('retrieval_api.embed', embed):
            self.assertEqual(hybrid_search.cached_vectors(path, {'e1': '团子'}), {'e1': (1.0, 0.0)})  # back: no embedding
        self.assertEqual(embedded, [['团子']])
        with patch('retrieval_api.signature', return_value='third-model'), patch('retrieval_api.embed', embed):
            hybrid_search.cached_vectors(path, {'e1': '团子'})
        with closing(sqlite3.connect(path)) as db:
            spaces = {row[0] for row in db.execute('SELECT signature FROM vector_spaces')}
            self.assertNotIn('vectors', {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertEqual(spaces, {'old-model', 'third-model'})  # KEEP_SPACES = 2: the least recent one went


class HygieneTests(unittest.TestCase):
    def test_small_talk_is_not_searched(self):
        for text in ('好的', '在吗', '哈哈哈哈', 'OK', 'hello', '晚安', '嗯嗯'):
            self.assertFalse(rc.informative(text), text)
        for text in ('猫呢', '团子今天又拆家了', 'API 429', "how's your Japanese exam prep going"):
            self.assertTrue(rc.informative(text), text)

    def test_mmr_dynamic_cut_and_identifier_conflicts(self):
        vectors = {0: unit(1, 0), 1: unit(1, 0.05), 2: unit(0, 1)}
        self.assertEqual(rc.mmr_unique([0, 1, 2], vectors, None, 0.9), [0, 2])
        self.assertEqual(rc.mmr_unique([0, 1], None, ['冰美式，不加糖', '冰美式 不加糖！'], 0.9), [0])
        self.assertEqual(rc.dynamic_cut([('a', 0.9), ('b', 0.05), ('c', 0.3)], 0.1, 5), [('a', 0.9), ('c', 0.3)])
        self.assertEqual(rc.dynamic_cut([('a', 0.9), ('c', 0.3)], 0.1, 5, ratio=0.5), [('a', 0.9)])
        self.assertTrue(rc.anchors_conflict('为utils.py写pytest单元测试', '为calc.py中的add和div写pytest测试'))
        self.assertTrue(rc.anchors_conflict('备份PostgreSQL数据库', '每天备份sqlite数据库'))
        self.assertFalse(rc.anchors_conflict('为calc.py写pytest单元测试', '为calc.py中的add和div写pytest测试'))
        self.assertFalse(rc.anchors_conflict('整理会议纪要', '整理本周会议纪要并发给团队'))

    def test_rank_texts_floor_small_talk_and_lexical_fallback(self):
        texts = ['开启 WAL 并设置 busy_timeout 解决 database is locked', '用 utf-8-sig 打开中文 CSV']
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache.sqlite3'
            with patch('embedding_provider.embed_unscoped', side_effect=lambda t, **k: [unit(1, len(x) % 3) for x in t]), \
                    patch('reranker.score', lambda query, passages: [0.9 if 'WAL' in p else 0.01 for p in passages]):
                self.assertEqual([i for i, _ in rc.rank_texts('写数据库时提示 database is locked', texts, cache_path=cache)], [0])
                self.assertEqual(rc.rank_texts('好的', texts, cache_path=cache), [])
            with patch('embedding_provider.embed_unscoped', side_effect=Unavailable('off')), \
                    patch('reranker.score', side_effect=Unavailable('off')):
                self.assertEqual([i for i, _ in rc.rank_texts('读取中文CSV乱码', texts, cache_path=cache)], [1])


class GatedRecallTests(unittest.TestCase):
    def test_small_talk_lines_skipped_one_batch_and_duplicates_left_out(self):
        items = [('a', '养了一只叫团子的橘猫，爱拆家', ['团子', '橘猫']), ('b', '养的橘猫叫团子，很爱拆家', ['团子']),
                 ('c', '对芒果过敏', ['芒果'])]
        vectors = {'a': unit(1, 0.02), 'b': unit(1, 0.03), 'c': unit(0, 1)}
        scored = []

        def score(query, passages):
            scored.append(query)
            return [0.9 if '团子' in p else 0.0 for p in passages]
        with patch('embedding_provider.embed_unscoped', side_effect=lambda t, **k: [unit(1, 0)] * len(t)), \
                patch('house_settings.local_models_enabled', return_value=True), patch('reranker.score', score), \
                patch.object(rc, 'installed', lambda kind=None: ['BAAI/bge-m3', 'BAAI/bge-reranker-v2-m3']):
            hits = hybrid_search.gated_recall(['好的', '团子今天又拆家了', '在吗'], items, lambda texts: vectors, 'test', 3)
        self.assertEqual(hits, [0])  # b retells a (cosine > DUP_COSINE)
        self.assertEqual(scored, ['团子今天又拆家了'])  # small talk never reached the reranker


if __name__ == '__main__':
    unittest.main()


class SemanticFallbackTests(unittest.TestCase):
    def test_cosine_accepts_numpy_and_empty_vectors_without_ambiguous_truth_value(self):
        import numpy as np
        self.assertAlmostEqual(rc.cosine(np.array([1., 0.]), np.array([1., 0.])), 1.)
        self.assertEqual(rc.cosine(np.array([]), np.array([])), 0.)

    def test_reranker_failure_preserves_cross_language_dense_recall(self):
        from types import SimpleNamespace
        vectors = [[1.0, 0.0], [0.0, 1.0]]
        with patch.object(rc, 'embed_queries', return_value=[[1.0, 0.0]]), \
             patch('retrieval_api.signature', return_value='test-space'), \
             patch.object(rc, 'doc_cache', return_value=SimpleNamespace(get=lambda *args: vectors)), \
             patch.object(rc, 'rerank', side_effect=Unavailable('reranker is unavailable')):
            result = rc.rank_texts('显卡显存不够用了', ['GPU memory allocation exhausted', 'a completely unrelated recipe'])
        self.assertEqual([index for index, score in result], [0])
        self.assertGreater(result[0][1], 0.9)
