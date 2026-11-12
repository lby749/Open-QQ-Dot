import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from persona_analysis_input import AnalysisInputBuilder, EvidenceSpan
from persona_store import Unavailable
from persona_types import AnalysisPurpose, ProviderKind
from persona_scenes import (SceneBehaviorAnalyzer, _observations, _structural_scene)


class PersonaSceneTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_analysis_input import AnalysisInputBuilderTests
        _Fixture = AnalysisInputBuilderTests
        _Fixture.setUp(self)
        self.addCleanup(self.dialogues.close)
        self.addCleanup(self.pairs.close)
        self.analyzer = SceneBehaviorAnalyzer(self.scope, self.pairs)

    def test_role_style_and_relationship_evidence_are_separate_and_read_only(self):
        revision = self.pairs.revision
        result = self.analyzer.analyze([self.session.id], target_speaker_ids=['alias'])
        self.assertEqual(result.friend_id, '12345')
        self.assertEqual(result.source_revision, revision)
        self.assertEqual(result.source_refs, result.analysis_input.source_refs)
        self.assertEqual(result.style_profile['sample_info']['denominators']['pairs'], 1)
        self.assertEqual(len(result.scenes), 4)
        self.assertEqual(sum(row.opportunity_count for row in result.dimension_status
                             if row.dimension == 'LanguageStyle'), 1)
        self.assertTrue(all(row.source_refs for row in result.observations))
        self.assertEqual(self.pairs.revision, revision)

    def test_no_target_relationship_event_retains_full_explicit_request(self):
        message = self.messages[2]
        request = SimpleNamespace(id=message.id, text='别每句话都问我。',
                                  message_type='text', source=message.source)
        event = SimpleNamespace(event_id='e', id='e', time_gap=None)
        observed = _observations('e', self.session.id, False, (request,), (), (), event,
                                 (message.source,))
        item = next(row for row in observed if row.subject == 'DYAD')
        self.assertEqual(item.response_pattern, request.text)
        self.assertEqual(item.evidence_spans, (EvidenceSpan(message.id, 0,
            len(request.text), request.text),))
        self.assertIsNone(item.pair_id)
        self.assertEqual(item.relationship_event_id, 'e')

    def test_role_behavior_uses_target_and_never_creates_dyad_from_user(self):
        pair = self.builder.build([self.session.id], ['alias']).role_pairs[0]
        incoming = SimpleNamespace(id='input', text='哈哈！别每句话都问我。', message_type='text')
        target = SimpleNamespace(id='target', text='ok', message_type='text')
        observed = _observations(pair.id, pair.session_id, True, (incoming,), (target,), (),
                                 pair, pair.source_refs)
        self.assertFalse(any(row.subject == 'DYAD' or row.dimension == 'Humor' for row in observed))
        self.assertEqual({row.dimension for row in observed}, {'LanguageStyle', 'ReplyPattern', 'Silence'})
        for row in observed:
            self.assertTrue(all(span.message_id == 'target' for span in row.evidence_spans))

    def test_scene_rules_only_use_explicit_structure_and_never_guess_image_content(self):
        def msg(text, kind='text'):
            return SimpleNamespace(text=text, message_type=kind)
        self.assertEqual(_structural_scene((msg('你这说的是真的吗？'),)), 'concrete_question')
        self.assertEqual(_structural_scene((msg('你可真行啊'),)), 'unknown')
        self.assertEqual(_structural_scene((msg('[image]', 'image'),)), 'image_reaction')
        self.assertEqual(_structural_scene((msg('[表情包]', 'sticker'),)), 'sticker_exchange')

    def test_model_scene_is_preview_only_and_validation_is_local(self):
        calls = []
        original = self.analyzer.builder.build
        inputs = original([self.session.id], (), ())
        with patch.object(self.analyzer.builder, 'build', return_value=inputs), \
             patch.object(type(inputs), 'validate_analysis_call', autospec=True,
                          side_effect=lambda obj, purpose, provider: calls.append(
                              (purpose, provider)) or ProviderKind.LOCAL), \
             patch('persona_scenes.LocalPersonaAnalyzer') as local_type:
            local = local_type.return_value
            event = inputs.relationship_events[0]
            span = event.explicit_spans[0]
            local.analyze_batch.side_effect = lambda items, **kwargs: {
                event_id: ({'scene': 'emotion_low', 'unknown_reason': ''}, {
                    'confidence': 0.0,
                    'candidates': ([{'kind': 'reaction', 'subject': 'FRIEND', 'text': span.text,
                                     'evidence': {'message_id': span.message_id, 'start': span.start,
                                                  'end': span.end, 'text': span.text}}]
                                   if event_id == event.event_id else []),
                    'unknown_reason': ''}) for _, event_id in items}
            result = self.analyzer.analyze([self.session.id], use_local_model=True)
        self.assertTrue(all(row.scene != 'emotion_low' for row in result.scenes))
        self.assertTrue(all(row.model_scene_proposal == 'emotion_low' for row in result.scenes))
        self.assertEqual(len(result.model_proposals), 1)
        self.assertTrue(all(row.quality != 'model_estimate_uncalibrated_preview_only'
                            for row in result.observations))
        self.assertEqual(set(calls), {(AnalysisPurpose.SCENE_ANALYSIS, ProviderKind.LOCAL)})
        self.assertIn(result.model_version, ('Qwen_Qwen3.5-9B-Q4_K_M', 'qwen2.5-local'))

    def test_batch_limit_and_cancel_return_no_partial_result(self):
        inputs = SimpleNamespace(role_pairs=tuple(SimpleNamespace(id=str(i)) for i in range(65)),
                                 relationship_events=())
        self.analyzer.builder = SimpleNamespace(build=lambda *a, **kw: inputs)
        with self.assertRaises(Unavailable):
            self.analyzer.analyze(['unused'])
        event = threading.Event()
        event.set()
        self.analyzer.builder = AnalysisInputBuilder(self.scope, self.pairs)
        with self.assertRaises(Unavailable):
            self.analyzer.analyze([self.session.id], cancel=event)


if __name__ == '__main__':
    unittest.main()
