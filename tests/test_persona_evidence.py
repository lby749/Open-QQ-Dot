import unittest
import sqlite3
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from dialogue_pairs import DialoguePairBuilder
from dialogue_turns import SessionSegmenter, build_turns
from persona_analysis_input import EvidenceSpan
from persona_distiller import PersonaDistiller
from persona_evidence import EvidenceValidator
from persona_evidence import _evidence_rows
from persona_scenes import SceneBehaviorAnalyzer
from persona_store import Conflict, ScopeMismatch
from persona_types import InvalidSource


class PersonaEvidenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_analysis_input import AnalysisInputBuilderTests
        AnalysisInputBuilderTests.setUp(self)
        self.distiller = PersonaDistiller(self.scope, self.pairs)
        self.validator = EvidenceValidator(self.scope, self.pairs)

    def _literal_result(self):
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? '
            'AND import_id=? AND message_seq=?', ('别每句话都问我。', '12345', 'imp', 0))
        messages = tuple(self.dialogues.normalizer.normalize([{'friend_id': '12345',
            'dataset_id': 'private', 'import_id': 'imp', 'record_id': 'r0', 'message_seq': 0}]))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={messages[0].id: 'evidence-literal'}))
        self.dialogues.save_session(session, self.dialogues.revision)
        return self.distiller.analyze([session.id])

    def test_private_no_target_evidence_uses_only_full_inbound_text(self):
        result = self._literal_result()
        validated = self.validator.validate(result)
        self.assertEqual(len(validated.evidence), 1)
        row = validated.evidence[0]
        self.assertEqual((row.evidence_role, row.subject, row.pair_id), ('support', 'DYAD', None))
        self.assertTrue(row.relationship_event_id)
        self.assertEqual(row.span_text, '别每句话都问我。')
        self.assertEqual((row.source_origin, row.data_scope, row.speaker_id),
                         ('REAL_RELATIONSHIP', 'PRIVATE_FRIEND', '12345'))

    def test_catalog_revision_advance_does_not_stale_source_hash(self):
        result = self._literal_result()
        old_revision = result.source_revision
        self.distiller.save_result(result, old_revision)
        validated = self.validator.validate(result)
        self.assertEqual(validated.validation_revision, old_revision + 1)
        self.assertEqual(validated.source_content_hash, result.input_hash)

    def test_forged_claim_support_or_counter_locator_is_rejected(self):
        result = self._literal_result()
        claim = result.relationship_claim_drafts[0]
        forged = replace(claim, counter_observations=('invented-episode',))
        with self.assertRaises(InvalidSource):
            self.validator.validate(replace(result, relationship_claim_drafts=(forged,)))
        with self.assertRaises(ScopeMismatch):
            self.validator.validate(replace(result, friend_id='23456'))

    def test_changed_source_content_invalidates_saved_analysis(self):
        result = self._literal_result()
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? '
            'AND import_id=? AND message_seq=?', ('原文发生变化', '12345', 'imp', 0))
        with self.assertRaises(InvalidSource):
            self.validator.validate(result)

    def test_save_validates_claims_and_evidence_atomically_with_catalog_cas(self):
        result = self._literal_result()
        self.distiller.save_result(result, self.pairs.revision)
        validated = self.validator.validate(result)
        expected = self.pairs.revision
        with self.assertRaises(Conflict):
            self.validator.save(validated, expected - 1)
        self.assertEqual(self.pairs.db.execute(
            'SELECT state FROM persona_analysis_runs WHERE id=?', (result.run_id,)).fetchone()[0],
            'DRAFT')
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM persona_claim_evidence').fetchone()[0], 0)
        self.pairs.db.execute("CREATE TEMP TRIGGER fail_evidence BEFORE INSERT ON persona_claim_evidence "
                              "BEGIN SELECT RAISE(ABORT,'fixture evidence failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.validator.save(validated, expected)
        self.assertEqual(self.pairs.revision, expected)
        self.assertEqual(self.pairs.db.execute(
            'SELECT state FROM persona_analysis_runs WHERE id=?', (result.run_id,)).fetchone()[0],
            'DRAFT')
        self.pairs.db.execute('DROP TRIGGER fail_evidence')
        self.assertEqual(self.validator.save(validated, expected), expected + 1)
        self.assertEqual(self.pairs.db.execute(
            'SELECT state FROM persona_analysis_runs WHERE id=?', (result.run_id,)).fetchone()[0],
            'COMPLETED')
        self.assertEqual(self.pairs.db.execute(
            'SELECT state FROM persona_claims WHERE run_id=?', (result.run_id,)).fetchone()[0],
            'VALIDATED')
        evidence = self.pairs.db.execute('SELECT evidence_role,episode_id,source_message_id,'
            'span_start,span_end,span_text FROM persona_claim_evidence WHERE run_id=?',
            (result.run_id,)).fetchone()
        self.assertEqual(tuple(evidence), ('support', validated.evidence[0].episode_id,
            validated.evidence[0].source_message_id, 0, len('别每句话都问我。'), '别每句话都问我。'))
        self.assertEqual(self.validator.save(validated, expected), expected + 1)

    def test_image_trigger_is_verified_but_only_real_target_is_saved_as_bot_evidence(self):
        source = dict(self.dialogues.db.execute('SELECT * FROM imported_messages WHERE friend_id=? '
            'AND import_id=? AND message_seq=?', ('12345', 'imp', 3)).fetchone())
        source.update(record_id='r5', message_seq=5, speaker_id='alias', speaker_role='human',
            event_time='2026-10-04T12:00:03+00:00', recorded_at='2026-10-04T12:00:03+00:00',
            text='这张图挺清楚', message_type='text', media_id=None,
            content_fingerprint=hashlib.sha256(b'image-trigger-target').hexdigest())
        self.dialogues.db.execute('INSERT INTO imported_messages (' + ','.join(source) + ') VALUES (' +
            ','.join('?' for _ in source) + ')', tuple(source.values()))
        messages = tuple(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in (4, 5)]))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={message.id: 'image-reaction-pair' for message in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        pair = tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), {'alias'}))[0]
        self.pairs.save_pairs((pair,), self.pairs.revision)
        scene = SceneBehaviorAnalyzer(self.scope, self.pairs).analyze(
            [session.id], target_speaker_ids=['alias'])
        observation = next(item for item in scene.observations
                          if item.dimension == 'ImageReaction')
        trigger = pair.user_bubbles[0]
        self.assertEqual(trigger.message_type, 'image')
        with_trigger = replace(observation, evidence_spans=(EvidenceSpan(
            trigger.id, 0, len(trigger.text), trigger.text), *observation.evidence_spans))
        scene = replace(scene, observations=tuple(with_trigger if item == observation else item
                                                  for item in scene.observations))
        draft = SimpleNamespace(claim_id='image-claim', support_observations=(pair.id,),
            counter_observations=(), subject='BOT_PERSONA', dimension='ImageReaction',
            scene='image_reaction', reaction=observation.response_pattern)
        rows = _evidence_rows(scene, (self.dialogues.load_session(session.id),), (draft,))
        self.assertEqual({row.source_message_id for row in rows},
                         {message.id for message in pair.target_bubbles})
        self.assertNotIn(trigger.id, {row.source_message_id for row in rows})
        self.assertTrue(all(row.source_origin == 'REAL_RELATIONSHIP' and
                            row.speaker_id == 'alias' for row in rows))


if __name__ == '__main__':
    unittest.main()
