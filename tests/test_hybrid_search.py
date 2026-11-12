import unittest
from unittest.mock import patch

import hybrid_search as hs
from persona_store import Unavailable


class HybridSearchTests(unittest.TestCase):
    """2026-10-10 user: BM25 + dense + RRF + reranker + metadata ranking replace label + pure-vector search."""

    def test_tokens_cover_chinese_characters_bigrams_and_words(self):
        self.assertEqual(hs.tokens('蟑螂 OK2'), ['蟑', '螂', '蟑螂', 'ok2'])

    def test_bm25_prefers_shared_rare_terms(self):
        bm25 = hs.BM25(['daysdays', '蟑螂从空调上飞下来', '今天吃什么'])
        scores = bm25.scores('空调上有蟑螂', range(3))
        self.assertEqual(max(scores, key=scores.get), 1)
        self.assertNotIn(0, scores)

    def test_complete_dialogue_turn_survives_qa_reranking_but_not_its_floor(self):
        query = '下班以后想去河边走走放松一下'
        texts = ['之前的话\n' + query, '工作很辛苦，去运动会舒服一点']
        def score(_query, passages, *_args):
            return [.3 if text == texts[0] else .9 for text in passages]
        with patch('retrieval_core.rerank', side_effect=score):
            self.assertEqual(hs.search(query, texts, [.8, .9], task='examples', min_score=.2)[0][0], 0)
            self.assertEqual(hs.search(query, texts, [.8, .9], task='memory', min_score=.2)[0][0], 1)
            self.assertEqual([i for i, _ in hs.search(query, texts, [.8, .9], task='examples', min_score=.5)], [1])

    def test_short_or_paraphrased_queries_keep_semantic_order(self):
        pairs = [(0, .2), (1, .8)]
        self.assertEqual(hs.order_reranked('你好', ['你好', '下午好'], pairs, 'examples')[0][0], 1)
        self.assertEqual(hs.order_reranked('周末计划去公园看看小动物', ['周末逛动物园', '到公园看鸟的安排'], pairs, 'examples')[0][0], 1)

    def test_example_reranking_retains_recall_consensus_and_keeps_raw_score_gate(self):
        texts = ['钥匙落在屋里了', '上次让你带钥匙', '今天吃饭没']
        def score(_query, passages, *_args):
            return [{texts[0]: .2, texts[1]: .8, texts[2]: .01}[text] for text in passages]
        with patch('retrieval_core.rerank', side_effect=score):
            self.assertEqual(hs.search('钥匙忘家里了', texts, [.8, .6, .2], task='examples')[0][0], 0)
            self.assertEqual(hs.search('钥匙忘家里了', texts, [.8, .6, .2], task='memory')[0][0], 1)
            self.assertEqual([i for i, _ in hs.search('钥匙忘家里了', texts, [.8, .6, .2],
                task='examples', min_score=.5)], [1])

    def test_rrf_rewards_agreement_between_rankings(self):
        self.assertEqual(hs.rrf([1, 2, 3], [3, 1, 2], [3])[0], 3)

    def test_reranker_orders_and_floors_the_fused_candidates(self):
        texts = ['完全无关的话', '蟑螂从空调上飞下来', '空调坏了']
        rerank = {texts[0]: 0.01, texts[1]: 0.95, texts[2]: 0.4}
        with patch('reranker.score', lambda query, passages: [rerank[p] for p in passages]):
            hits = hs.search('空调上有蟑螂', texts, [0.9, 0.5, 0.6], limit=5, min_score=0.2)
        self.assertEqual(hits, [(1, 0.95), (2, 0.4)])

    def test_metadata_pool_and_ranking(self):
        texts = ['甲', '乙', '丙']
        with patch('reranker.score', lambda query, passages: [0.5] * len(passages)):
            hits = hs.search('丁', texts, [0.1, 0.2, 0.3], pool=[0, 1], metadata_rank=[2, 0], limit=5)
        self.assertEqual(sorted(i for i, _ in hits), [0, 1])  # index 2 is filtered out by metadata

    def test_without_reranker_fused_order_and_dense_floor(self):
        def unavailable(query, passages):
            raise Unavailable('no CUDA')
        with patch('reranker.score', unavailable):
            hits = hs.search('空调', ['空调坏了', 'days'], [0.7, 0.3], fallback_floor=0.5)
        self.assertEqual(hits, [(0, None)])


if __name__ == '__main__':
    unittest.main()
