from types import SimpleNamespace
import unittest

from persona_store import ScopeMismatch
from persona_types import (Candidate, ContextBudgets, DataScope, PackVersion,
    PrivacyView, RetrievalFilters, RetrievalRequest, SourceOrigin, SourceRef)
from runtime_context import RuntimeContextBuilder


NOW = '2026-10-06T00:00:00+00:00'


def request(budgets=None, allowed=()):
    return RetrievalRequest(friend_id='12345', query='q', recent_context='recent',
        scene='default', plan=None, filters=RetrievalFilters(),
        budgets=budgets or ContextBudgets(), allowed_pack_versions=frozenset(allowed), now=NOW)


def candidate(group, content, *, friend='12345', scope=DataScope.PRIVATE_FRIEND,
              pack_id=None, version=None, identifier='candidate', score=0.9):
    source = SourceRef(dataset_id='fixture', import_id='fixture-import', record_id=identifier,
        message_seq=None, speaker_id='person', origin=SourceOrigin.MANUAL,
        data_scope=scope, privacy_view=PrivacyView.LOCAL_FULL,
        friend_id=friend if scope == DataScope.PRIVATE_FRIEND else None,
        event_time=None, recorded_at=NOW, pack_id=pack_id, pack_version=version)
    return Candidate(id=identifier, namespace=group, type='fixture', content=content,
        score=score, source=source, reason='private reason', retriever='fixture',
        friend_id=friend, timestamp=None, confidence=0.9, original_scope=scope,
        pack_version=version)


def relevant(**groups):
    values = {name: () for name in ('structured', 'memory', 'persona', 'interests',
        'topics', 'examples', 'style', 'stickers')}
    values.update(groups)
    return SimpleNamespace(**values, degraded=False)


class RuntimeContextTests(unittest.TestCase):
    def setUp(self):
        self.builder = RuntimeContextBuilder()

    def test_disabled_returns_legacy_without_reading_relevant(self):
        class Unreadable:
            @property
            def degraded(self):
                raise AssertionError('disabled builder accessed retrieval')

        legacy = '  legacy\n原样  '
        built = self.builder.build(request(), Unreadable(), legacy, {})
        self.assertEqual(built.prompt_blocks, legacy)
        self.assertEqual(built.token_usage['method'], 'utf8_bytes')
        self.assertEqual(built.token_usage['total'], len(legacy.encode('utf-8')))
        self.assertEqual(built.token_usage['legacy'], len(legacy.encode('utf-8')))
        self.assertTrue(built.estimate)
        self.assertFalse(built.degraded)

    def test_empty_or_unavailable_retrieval_preserves_legacy_exactly(self):
        legacy = 'legacy\n'
        empty = self.builder.build(request(), relevant(), legacy, {'enabled': True})
        self.assertEqual(empty.prompt_blocks, legacy)
        self.assertFalse(empty.degraded)
        unavailable = self.builder.build(request(), None, legacy, {'enabled': True})
        self.assertEqual(unavailable.prompt_blocks, legacy)
        self.assertTrue(unavailable.degraded)
        self.assertIn('retrieval_unavailable', unavailable.trace['reason_codes'])

    def test_substantive_candidate_already_in_legacy_is_not_repeated(self):
        content = 'A verified fact from the same user memory. ' * 3
        item = candidate('memory', content, identifier='already-present')
        legacy = 'IMPORTANT PRIOR CONTEXT\n' + content
        result = self.builder.build(request(), relevant(memory=(item,)), legacy,
                                    {'enabled': True})
        self.assertEqual(result.prompt_blocks, legacy)
        self.assertIn('context_candidate_already_present', result.trace['reason_codes'])
        self.assertEqual(result.token_usage['total'], len(legacy.encode('utf-8')))

    def test_private_examples_enter_context_as_data_for_any_provider(self):
        item = candidate('example', '{"user":"","target_bubbles":["yeah"]}')
        result = self.builder.build(request(), relevant(examples=(item,)), 'legacy',
                                    {'enabled': True, 'provider_kind': 'LOCAL'})
        self.assertIn('【RETRIEVED_EXAMPLES】 SOURCE=retrieval/example TYPE=DATA', result.prompt_blocks)
        self.assertIn('SOURCE=retrieval/example', result.prompt_blocks)
        self.assertNotIn('candidate', result.prompt_blocks)
        self.assertEqual(result.token_usage['total'], len(result.prompt_blocks.encode('utf-8')))
        self.assertEqual(result.token_usage['legacy'], len('legacy'.encode('utf-8')))
        self.assertEqual(result.token_usage['examples_used'],
                         result.token_usage['sections']['examples'])
        cloud = self.builder.build(request(), relevant(examples=(item,)), 'legacy',
                                   {'enabled': True, 'provider_kind': 'CLOUD'})
        # 2026-10-09 user decision: private examples may go to the friend's cloud model.
        self.assertEqual(cloud.prompt_blocks, result.prompt_blocks)

    def test_scope_mismatch_and_unapproved_public_pack_are_hard_errors(self):
        with self.assertRaises(ScopeMismatch):
            self.builder.build(request(), relevant(persona=(candidate('persona', 'x', friend='54321'),)),
                               '', {'enabled': True})
        public = candidate('persona', 'public', scope=DataScope.PUBLIC_PACK,
                           pack_id='pack', version='v1')
        with self.assertRaises(ScopeMismatch):
            self.builder.build(request(), relevant(persona=(public,)), '', {'enabled': True})
        allowed = request(allowed=(PackVersion(pack_id='pack', version='v1'),))
        accepted = self.builder.build(allowed, relevant(persona=(public,)), '', {'enabled': True})
        self.assertIn('public', accepted.prompt_blocks)

    def test_candidate_group_namespace_mismatches_are_hard_errors(self):
        private_example = candidate('example', 'private example')
        with self.assertRaises(ScopeMismatch):
            self.builder.build(request(), relevant(memory=(private_example,)), '',
                               {'enabled': True, 'provider_kind': 'CLOUD'})
        public_structured = candidate('structured', 'public fact',
            scope=DataScope.PUBLIC_PACK, pack_id='pack', version='v1')
        allowed = request(allowed=(PackVersion(pack_id='pack', version='v1'),))
        with self.assertRaises(ScopeMismatch):
            self.builder.build(allowed, relevant(structured=(public_structured,)), '',
                               {'enabled': True})

    def test_global_budget_keeps_legacy_and_removes_whole_examples(self):
        item = candidate('example', '{"target_bubbles":["whole example"]}')
        legacy = '当前消息与最近聊天完整保留'
        cap = len(legacy.encode('utf-8'))
        built = self.builder.build(request(), relevant(examples=(item,)), legacy,
                                   {'enabled': True, 'provider_kind': 'LOCAL',
                                    'max_context_tokens': cap})
        self.assertEqual(built.prompt_blocks, legacy)
        self.assertTrue(built.degraded)
        self.assertTrue(built.trace['generation_allowed'])
        self.assertIn('global_examples_removed', built.trace['reason_codes'])
        over = self.builder.build(request(), relevant(), legacy,
                                  {'enabled': True, 'max_context_tokens': 1})
        self.assertEqual(over.prompt_blocks, legacy)
        self.assertFalse(over.trace['generation_allowed'])
        self.assertIn('protected_context_over_budget', over.trace['reason_codes'])

    def test_example_subbudget_and_shared_persona_budget(self):
        example = candidate('example', '示例内容' * 20, identifier='example')
        persona = candidate('persona', '人格内容' * 10, identifier='persona')
        tight_examples = request(budgets=ContextBudgets(persona=5000, examples=10))
        built = self.builder.build(tight_examples, relevant(examples=(example,),
            persona=(persona,)), '', {'enabled': True, 'provider_kind': 'LOCAL'})
        self.assertNotIn('示例内容', built.prompt_blocks)
        self.assertIn('人格内容', built.prompt_blocks)
        self.assertIn('examples_budget', built.trace['reason_codes'])

        tight_persona = request(budgets=ContextBudgets(persona=10, examples=10))
        shared = self.builder.build(tight_persona, relevant(examples=(example,),
            persona=(persona,)), '', {'enabled': True, 'provider_kind': 'LOCAL'})
        self.assertNotIn('示例内容', shared.prompt_blocks)
        self.assertNotIn('人格内容', shared.prompt_blocks)
        self.assertIn('persona_budget', shared.trace['reason_codes'])

    def test_shared_budget_removes_example_before_lower_scoring_persona(self):
        example = candidate('example', 'x', identifier='example', score=0.9)
        persona = candidate('persona', 'keep persona, with enough text',
                            identifier='persona', score=0.1)
        solo = self.builder.build(request(), relevant(persona=(persona,)), '',
                                  {'enabled': True})
        limited = request(budgets=ContextBudgets(
            persona=solo.token_usage['persona_used'],
            examples=solo.token_usage['persona_used']))
        shared = self.builder.build(limited, relevant(examples=(example,),
            persona=(persona,)), '', {'enabled': True, 'provider_kind': 'LOCAL'})
        self.assertIn('keep persona', shared.prompt_blocks)
        self.assertNotIn('"x"', shared.prompt_blocks)
        self.assertIn('persona_examples_budget', shared.trace['reason_codes'])

    def test_duplicate_candidate_key_is_rendered_once(self):
        item = candidate('memory', 'repeat exactly')
        built = self.builder.build(request(), relevant(memory=(item, item)), '',
                                   {'enabled': True})
        self.assertEqual(built.prompt_blocks.count('repeat exactly'), 1)
        self.assertIn('duplicate_candidate_removed', built.trace['reason_codes'])

    def test_candidate_corruption_degrades_without_leaking_content_to_trace(self):
        bad = object()
        good = candidate('memory', 'safe body')
        built = self.builder.build(request(), relevant(memory=(bad, good)), '', {'enabled': True})
        self.assertIn('safe body', built.prompt_blocks)
        self.assertTrue(built.degraded)
        self.assertNotIn('safe body', str(built.trace))
        self.assertNotIn('PRIVATE', str(built.trace))


if __name__ == '__main__':
    unittest.main()
