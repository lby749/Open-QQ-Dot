import copy
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from local_persona_analyzer import LocalPersonaAnalyzer, _map_wire, validate_output
from persona_types import InvalidSource
from tests.test_persona_analysis_input import AnalysisInputBuilderTests as _Fixture


class LocalPersonaAnalyzerTests(unittest.TestCase):
    def setUp(self):
        _Fixture.setUp(self)
        self.analyzer = LocalPersonaAnalyzer(self.scope, self.pairs)
        self.event = self.builder.build([self.session.id]).relationship_events[0]
        span = self.event.explicit_spans[0]
        self.valid = {'subject': 'FRIEND', 'scene': 'casual_update', 'confidence': 0.8,
                      'candidates': [{'kind': 'reaction', 'subject': 'FRIEND',
                        'text': span.text, 'evidence': vars(span)}], 'unknown_reason': ''}
        self.evidence = {span.message_id: span.text}

    def test_entry_reads_selected_relationship_and_excludes_future(self):
        def run(prompt, schema, cancel):
            self.assertNotIn(self.messages[4].text, prompt)
            return json.dumps({'e0': {'scene': 'casual_update', 'kind': 'reaction', 'evidence_index': 0}}), 0.2, 100
        with patch.object(self.analyzer, '_run', side_effect=run):
            result = self.analyzer.analyze_episode(self.session.id, self.event.event_id)
        self.assertEqual(result['candidates'], self.valid['candidates'])
        self.assertEqual(result['peak_rss_bytes'], 100)

    def test_role_target_only_and_no_bot_evidence(self):
        pair = self.builder.build([self.session.id], target_speaker_ids=['alias']).role_pairs[0]
        subjects, context, inbound, refs = self.analyzer._episode(self.session.id, pair.id, ['alias'], ())
        self.assertEqual(subjects, ['BOT_PERSONA'])
        self.assertEqual(inbound, pair.target_bubbles)
        self.assertNotIn(self.messages[4], context)

    def test_validator_rejects_fabrication_subject_schema_nan_and_negation_drop(self):
        for mutate in (
            lambda v: v.update(extra=True),
            lambda v: v.update(confidence=float('nan')),
            lambda v: v.update(subject='BOT_PERSONA'),
            lambda v: v['candidates'][0]['evidence'].update(message_id='invented'),
            lambda v: v['candidates'][0]['evidence'].update(end=999),
            lambda v: v['candidates'][0].update(text='invented'),
            lambda v: v['candidates'][0].update(subject='DYAD'),
            lambda v: v['candidates'][0]['evidence'].update(start=True)):
            value = copy.deepcopy(self.valid)
            mutate(value)
            with self.assertRaises(InvalidSource):
                validate_output(value, ['FRIEND', 'DYAD'], self.evidence)
        value = copy.deepcopy(self.valid)
        value['candidates'][0].update(text='喜欢咖啡')
        value['candidates'][0]['evidence'].update(message_id='neg', start=1, end=5, text='喜欢咖啡')
        with self.assertRaises(InvalidSource):
            validate_output(value, ['FRIEND'], {'neg': '不喜欢咖啡'})

    def test_invalid_truncated_output_and_cancel_are_explicit_unknown(self):
        with patch.object(self.analyzer, '_run', return_value=('{', 0.2, 100)):
            result = self.analyzer.analyze_episode(self.session.id, self.event.event_id)
        self.assertEqual(result['unknown_reason'], 'invalid_model_output')
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.analyzer, '_run') as run:
            result = self.analyzer.analyze_episode(self.session.id, self.event.event_id, cancel=cancel)
            run.assert_not_called()
        self.assertEqual(result['unknown_reason'], 'cancelled')

    def test_model_abstention_never_asserts_scene_or_confidence(self):
        value = {'e0': {'scene': 'greeting', 'kind': 'unknown', 'evidence_index': -1}}
        with patch.object(self.analyzer, '_run', return_value=(json.dumps(value), 0.2, 100)):
            result = self.analyzer.analyze_episode(self.session.id, self.event.event_id)
        self.assertEqual((result['scene'], result['confidence'], result['candidates']),
                         ('unknown', 0.0, []))
        self.assertEqual(result['unknown_reason'], 'model_abstained')

    def test_wire_selection_maps_subject_and_exact_unicode_evidence(self):
        sources = {'real': '我不喜欢咖啡☕'}
        wire = {'scene': 'casual_update', 'kind': 'explicit_preference', 'evidence_index': 0}
        result = _map_wire(wire, ['FRIEND', 'DYAD'], sources)
        self.assertEqual(result['subject'], 'FRIEND')
        self.assertEqual(result['candidates'][0]['evidence'],
                         {'message_id': 'real', 'start': 0, 'end': len(sources['real']), 'text': sources['real']})
        self.assertEqual(_map_wire(dict(wire, kind='communication_preference'),
                         ['FRIEND', 'DYAD'], sources)['subject'], 'DYAD')
        self.assertEqual(_map_wire(wire, ['BOT_PERSONA'], sources)['subject'], 'BOT_PERSONA')
        for bad in (dict(wire, evidence_index=1), dict(wire, evidence_index=True),
                    dict(wire, extra='invented'), dict(wire, scene='invented')):
            with self.assertRaises(InvalidSource):
                _map_wire(bad, ['FRIEND'], sources)

    def test_batch_packs_episodes_per_call_and_isolates_bad_items(self):
        events = self.builder.build([self.session.id]).relationship_events
        items = [(self.session.id, e.event_id) for e in events]
        prompts = []
        def run(prompt, schema, cancel):
            data = json.loads(prompt.rsplit('\n来信列表：', 1)[1].rsplit('\nJSON: ', 1)[0])
            prompts.append(data)
            self.assertEqual(set(schema['properties']), set(data))
            good = {'scene': 'casual_update', 'kind': 'reaction', 'evidence_index': 0}
            return json.dumps({k: good if k == 'e0' else dict(good, scene='invented') for k in data}), 0.2, 0
        with patch('local_persona_analyzer.BATCH_SIZE', 2), patch.object(self.analyzer, '_run', side_effect=run):
            results = self.analyzer.analyze_batch(items)
        self.assertEqual([len(p) for p in prompts], [2, 1])
        first, second = results[events[0].event_id], results[events[1].event_id]
        self.assertEqual(first[0], {'scene': 'casual_update', 'unknown_reason': ''})
        self.assertEqual(len(first[1]['candidates']), 1)
        self.assertEqual((second[0]['scene'], second[1]['unknown_reason']), ('unknown', 'invalid_model_output'))

    def test_batch_unknown_and_invalid_output_are_controlled(self):
        for raw in ('{"e0":{"scene":"unknown","kind":"unknown","evidence_index":-1}}', '{"e1":{}}',
                    '{"e0":{"scene":"greeting","kind":"reaction","evidence_index":0,"x":1}}', '{', '{"e0":NaN}'):
            with patch.object(self.analyzer, '_run', return_value=(raw, 0.2, 100)):
                preview, result = self.analyzer.analyze_batch(
                    [(self.session.id, self.event.event_id)])[self.event.event_id]
            self.assertEqual((preview['scene'], result['confidence'], result['candidates']), ('unknown', 0.0, []))
            self.assertTrue(preview['unknown_reason'])

    def test_local_choice_uses_shared_9b_server_with_schema(self):
        server = MagicMock()
        server._request.return_value = {'choices': [{'message': {'content': '{"scene":"greeting"}'}}]}
        with patch('message_classifier.get_classifier', return_value=server):
            raw, _, _ = self.analyzer._run('prompt', {'type': 'object'}, None)
        self.assertEqual(raw, '{"scene":"greeting"}')
        payload = server._request.call_args.args[1]
        self.assertEqual(payload['response_format']['json_schema']['schema'], {'type': 'object'})
        self.assertEqual(payload['temperature'], 0)

    def test_cloud_choice_follows_friend_model_settings(self):
        profile = {'qq_id': '12345', 'model_api': {'provider': 'deepseek', 'model': 'deepseek-flash'}}
        (self.scope.friend_dir / 'friend.json').write_text(json.dumps(profile), 'utf-8')
        analyzer = LocalPersonaAnalyzer(self.scope, self.pairs, 'cloud')
        self.assertEqual(analyzer.provider, 'CLOUD')
        with patch('main.codex_text', return_value=('```json\n{"scene":"greeting"}\n```', None)) as call:
            raw, _, _ = analyzer._run('prompt', {'type': 'object'}, None)
        self.assertEqual(raw, '{"scene":"greeting"}')
        self.assertEqual(call.call_args.kwargs['profile'], profile)
        self.assertTrue(call.call_args.kwargs['ephemeral'])
        self.assertEqual(analyzer.analyze_episode(self.session.id, self.event.event_id, cancel=self._set())['model'],
                         'deepseek:deepseek-flash')

    def test_reply_scene_uses_9b_and_falls_back_to_unknown(self):
        from local_persona_analyzer import reply_scene
        server = MagicMock()
        server._request.return_value = {'choices': [{'message': {'content': '{"scene":"conflict"}'}}]}
        with patch('message_classifier.get_classifier', return_value=server):
            self.assertEqual(reply_scene('你凭什么这么说我', '上文'), 'conflict')
        with patch('message_classifier.get_classifier', side_effect=RuntimeError('down')):
            self.assertEqual(reply_scene('Hello'), 'unknown')

    @staticmethod
    def _set():
        event = threading.Event()
        event.set()
        return event


if __name__ == '__main__':
    unittest.main()
