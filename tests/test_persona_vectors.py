import unittest
from unittest.mock import patch

import numpy

import persona_vectors as pv

H = 3600


def message(uin, seconds, text):
    return {'uin': uin, 'name': '', 'time': seconds, 'bubble': text}


class PersonaVectorsTests(unittest.TestCase):
    def test_late_answers_are_replies_and_only_real_openers_are_openers(self):
        messages = [message('2', 0, 'what score did you get'), message('1', 3 * H, '719'),
                    message('1', 4 * H, 'and you'),
                    message('2', 5 * H, 'good night'), message('1', 30 * H, 'morning'),
                    message('2', 30 * H + 60, 'morning'), message('1', 30 * H + 120, 'had breakfast')]
        entries = pv.entries(messages, '1')
        replies = [(e['user'], e['target']) for e in entries if e['kind'] == 'reply']
        openers = [e['target'] for e in entries if e['kind'] == 'opener']
        self.assertEqual(replies, [(['what score did you get'], ['719']), (['morning'], ['had breakfast'])])
        self.assertEqual(openers, [['and you'], ['morning']])
        self.assertEqual(pv.query_text(entries[1]), 'morning\nmorning')

    def test_hybrid_search_floor_scene_metadata_and_opener_without_floor(self):
        def item(user, kind='reply', scene=None):
            return {'user': [user], 'target': ['ok'], 'context': [], 'kind': kind,
                    'labels': {'scene': [[pv.SCENE_NAMES.get(scene, scene), 90]]} if scene else {}}
        items = [item('completely irrelevant'), item('weather today'), item('roach fell from ac'), item('opener topic', 'opener'),
                 item('exam today', scene='concrete_question')]
        matrix = numpy.array([[0.50], [0.70], [0.66], [0.30], [0.40]], dtype=numpy.float32)
        texts = [pv.query_text(i) for i in items]
        rerank = {'roach fell from ac': 0.9, 'weather today': 0.25, 'completely irrelevant': 0.05, 'exam today': 0.3, 'opener topic': 0.01}
        fake = lambda query, passages: [rerank[p] for p in passages]
        from hybrid_search import BM25
        with patch.object(pv, '_load', return_value=(items, matrix, texts, BM25(texts), set())), \
                patch('embedding_provider.embed_unscoped', return_value=[[1.0]]), \
                patch('house_settings.local_models_enabled', return_value=True), \
                patch('reranker.score', fake):
            replies = pv.search('t', 'a roach fell from ac just now', 'reply', 'concrete_question', 5)
            openers = pv.search('t', 'caring naturally', 'opener', 'unknown', 5)
        self.assertEqual([r['user'][0] for r in replies], ['roach fell from ac', 'exam today', 'weather today'])
        self.assertEqual([o['user'][0] for o in openers], ['opener topic'])


if __name__ == '__main__':
    unittest.main()
