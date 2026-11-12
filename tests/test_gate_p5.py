from dataclasses import replace
import json
import unittest

from persona_store import ScopeMismatch
from persona_types import RetrievalFilters
from retrieval_orchestrator import RetrievalOrchestrator
from tests import test_structured_profile as fixtures


class GateP5StructuredIntegrationTests(unittest.TestCase):
    setUp = fixtures.StructuredProfileTests.setUp

    def test_real_orchestrator_structured_source_filter_and_isolation(self):
        profile_path = self.scope.friend_dir / 'friend.json'
        profile = json.loads(profile_path.read_text('utf-8'))
        profile['basic_info']['birthday'] = '10months1'
        profile_path.write_text(json.dumps(profile, ensure_ascii=False), 'utf-8')
        request = replace(fixtures.StructuredProfileTests.request(self), query='生日是什么', scene='any')
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        orchestrator = RetrievalOrchestrator(self.scope)
        default = orchestrator.retrieve(request)
        self.assertIn([item.content for item in default.structured], (['生日: 10months1'], ['生日：10months1'], ['生日：10月1日']))
        actual_chat = orchestrator.retrieve(replace(request,
            filters=RetrievalFilters(namespace=('structured',), source_type=('REAL_CHAT',))))
        self.assertEqual(actual_chat.structured, ())
        self.assertFalse(default.degraded)
        with self.assertRaises(ScopeMismatch):
            orchestrator.retrieve(replace(request, friend_id='23456'))
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
        self.assertFalse((self.scope.friend_dir / 'persona.sqlite3').exists())

