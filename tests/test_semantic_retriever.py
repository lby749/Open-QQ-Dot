import json
from pathlib import Path
import tempfile
import unittest

from persona_types import ContextBudgets, FriendScope, RetrievalFilters, RetrievalRequest
from semantic_retriever import SemanticRetriever


class SemanticRetrieverTests(unittest.TestCase):
    def test_missing_query_provenance_is_degraded_and_query_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
            scope = FriendScope.load('12345', project_root=root)
            request = RetrievalRequest(friend_id='12345', query='相似感觉', recent_context='',
                scene='any', plan=None, filters=RetrievalFilters(), budgets=ContextBudgets(),
                allowed_pack_versions=frozenset(), now='2026-10-06T00:00:00+00:00')
            result = SemanticRetriever(scope).retrieve(request, 5)
            self.assertTrue(result.degraded)
            self.assertEqual(result.candidates, ())
            self.assertFalse((friend / 'persona.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
