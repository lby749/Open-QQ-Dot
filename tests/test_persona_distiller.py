from dataclasses import replace
import hashlib
import json
import math
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dialogue_pairs import DialoguePairBuilder
from dialogue_turns import SessionSegmenter, build_turns
from persona_distiller import PersonaDistiller, _drafts
from persona_scenes import BehaviorObservation
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import DataScope, InvalidSource


class PersonaDistillerTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_analysis_input import AnalysisInputBuilderTests
        AnalysisInputBuilderTests.setUp(self)
        self.distiller = PersonaDistiller(self.scope, self.pairs)

    def _session(self, seqs, conversation, dataset='private', imported='imp', targets=()):
        messages = tuple(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': dataset, 'import_id': imported,
             'record_id': f'r{i}', 'message_seq': i} for i in seqs]))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={m.id: conversation for m in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        pairs = tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), targets))
        if pairs:
            self.pairs.save_pairs(pairs, self.pairs.revision)
        return session

    def _literal_result(self):
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? '
            'AND import_id=? AND message_seq=?', ('别每句话都问我。', '12345', 'imp', 0))
        session = self._session((0,), 'literal-request')
        return self.distiller.analyze([session.id])

    def _pure_scene(self, count=12):
        observations, metadata, scenes = [], {}, []
        for i in range(count):
            episode, session = f'e{i}', f's{i // 4}'
            observations.append(BehaviorObservation(episode, session, episode, None,
                'BOT_PERSONA', 'LanguageStyle', 'greeting', 'greeting input',
                'short_1_10' if i % 3 else 'longer_than_10', (), (), 'observed_statistic'))
            metadata[episode] = {'origin': 'REAL_SINGLE_SPEAKER', 'speaker': 'target',
                'scope': DataScope.PRIVATE_FRIEND, 'session': session, 'cohort': 'chosen-pool', 'role': True}
            scenes.append(SimpleNamespace(episode_id=episode, scene='greeting'))
        return SimpleNamespace(friend_id='12345', observations=tuple(observations),
                               scenes=tuple(scenes), model_proposals=()), metadata

    def test_support_counter_abstention_cap_formula_and_episode_deduplication(self):
        scene, meta = self._pure_scene()
        # One abstaining same-scene opportunity affects coverage, never the strength denominator.
        meta['abstain'] = dict(meta['e0'], session='s3')
        scene.scenes += (SimpleNamespace(episode_id='abstain', scene='greeting'),)
        scene.observations += (scene.observations[0], replace(scene.observations[0],
            episode_id='abstain', session_id='s3', quality='rule_estimate'))
        result = _drafts(scene, 'run', meta)
        self.assertEqual(len(result), 1)
        draft = result[0]
        self.assertEqual((draft.support_count, draft.opportunity_count), (8, 12))
        self.assertEqual((draft.effective_support_count, draft.effective_opportunity_count), (6., 9.))
        self.assertEqual(len(draft.counter_observations), 4)
        self.assertAlmostEqual(draft.strength, 2 / 3)
        z, n, p = 1.96, 9., 2 / 3
        width = 2*z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        self.assertAlmostEqual(draft.confidence, (12/13)*.85*(1-width))
        self.assertEqual(draft.source_origins, ('REAL_SINGLE_SPEAKER',))

    def test_low_samples_single_session_and_distinct_origin_cohorts_do_not_merge(self):
        scene, meta = self._pure_scene(4)
        self.assertEqual(_drafts(scene, 'run', meta), ())
        scene, meta = self._pure_scene(12)
        for value in meta.values():
            value.update(origin='PUBLIC_DATASET', scope=DataScope.PUBLIC_DATASET)
        # The same public actor across multiple sessions cannot manufacture support.
        self.assertEqual(_drafts(scene, 'run', meta), ())
        scene, meta = self._pure_scene(12)
        scene.observations = tuple(replace(o, session_id='one') for o in scene.observations)
        for value in meta.values():
            value['session'] = 'one'
        self.assertEqual(_drafts(scene, 'run', meta), ())
        scene, meta = self._pure_scene(12)
        for i, value in enumerate(meta.values()):
            value['origin'] = 'PUBLIC_DATASET' if i % 2 else 'REAL_SINGLE_SPEAKER'
            value['cohort'] = 'pool-A' if i % 4 else 'pool-B'
        self.assertEqual(_drafts(scene, 'run', meta), ())

    def test_private_literal_draft_and_public_role_input_never_becomes_dyad(self):
        result = self._literal_result()
        self.assertEqual(result.role_claim_drafts, ())
        claim = result.relationship_claim_drafts[0]
        self.assertEqual((claim.subject, claim.claim, claim.strength), ('DYAD', '别每句话都问我。', None))
        self.assertEqual(claim.support_count, 1)
        self.assertEqual(len(result.sample_status['dimensions']), 13)
        for seq, body, actor in ((2, '别每句话都问我。', 'public-user'), (3, 'ok', 'public-target')):
            row = dict(self.dialogues.db.execute('SELECT * FROM imported_messages WHERE friend_id=? '
                'AND import_id=? AND message_seq=?', ('12345', 'imp', seq)).fetchone())
            row.update(import_id='pubimp', dataset_id='public', source_origin='PUBLIC_DATASET',
                       text=body, speaker_id=actor, speaker_role='human',
                       content_fingerprint=hashlib.sha256(f'pub-{seq}'.encode()).hexdigest())
            self.dialogues.db.execute('INSERT INTO imported_messages (' + ','.join(row) + ') VALUES (' +
                ','.join('?' for _ in row) + ')', tuple(row.values()))
        session = self._session((2, 3), 'public-role', 'public', 'pubimp', ('public-target',))
        public = self.distiller.analyze([session.id], target_speaker_ids=('public-target',))
        self.assertEqual(public.relationship_claim_drafts, ())
        self.assertFalse(any(o.subject == 'DYAD' for o in public.scene_analysis.observations))

    def test_model_proposals_never_support_drafts_or_count_bot_context_as_actor(self):
        with patch('persona_scenes.LocalPersonaAnalyzer') as local_type:
            local = local_type.return_value
            span = self.builder.build([self.session.id]).relationship_events[0].explicit_spans[0]
            local.analyze_batch.side_effect = lambda items, **kwargs: {
                episode_id: ({'scene': 'celebration', 'unknown_reason': ''}, {
                    'confidence': 0.0, 'candidates': [
                        {'kind': 'explicit_preference', 'subject': 'FRIEND', 'text': span.text,
                         'evidence': vars(span)}]}) for _, episode_id in items}
            result = self.distiller.analyze([self.session.id], target_speaker_ids=('alias',), use_local_model=True)
        self.assertTrue(result.scene_analysis.model_proposals)
        self.assertEqual(result.sample_status['model_proposals_excluded'], 4)
        self.assertEqual((*result.role_claim_drafts, *result.relationship_claim_drafts), ())
        self.assertNotIn('BOT_GENERATED', result.settings['source_reliability'])

    def test_save_transaction_revision_idempotence_conflict_and_readonly(self):
        result = self._literal_result()
        revision = self.pairs.revision
        self.assertEqual(self.distiller.save_result(result, revision), revision + 1)
        self.assertEqual(self.distiller.save_result(result, revision), revision + 1)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM persona_analysis_runs').fetchone()[0], 1)
        body = self.pairs.db.execute('SELECT result_json FROM persona_analysis_runs').fetchone()[0]
        self.assertNotIn('analysis_input', body)
        self.assertNotIn('"target_bubbles":[', body)
        self.assertEqual(json.loads(body)['data_scope'], 'PRIVATE_FRIEND')
        self.assertTrue(json.loads(body)['source_refs'])
        with self.assertRaises(Conflict):
            self.distiller.save_result(replace(result, created_at='2026-10-05T00:00:00+00:00'), revision)
        fresh = self.distiller.analyze(result.settings['session_ids'])
        with self.assertRaises(Conflict):
            self.distiller.save_result(fresh, revision)
        self.pairs.db.execute('PRAGMA query_only=ON')
        with self.assertRaises(Unavailable):
            self.distiller.save_result(fresh, self.pairs.revision)
        self.pairs.db.execute('PRAGMA query_only=OFF')

    def test_save_rechecks_scope_claims_and_current_persisted_source(self):
        result = self._literal_result()
        with self.assertRaises(ScopeMismatch):
            self.distiller.save_result(replace(result, friend_id='23456'), self.pairs.revision)
        bad = replace(result, relationship_claim_drafts=(replace(result.relationship_claim_drafts[0], claim='伪造'),))
        with self.assertRaises(InvalidSource):
            self.distiller.save_result(bad, self.pairs.revision)
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? '
            'AND import_id=? AND message_seq=?', ('原文已变更', '12345', 'imp', 0))
        with self.assertRaises(InvalidSource):
            self.distiller.save_result(result, self.pairs.revision)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM persona_analysis_runs').fetchone()[0], 0)

    def test_claim_insert_failure_rolls_back_entire_run_and_revision(self):
        result = self._literal_result()
        revision = self.pairs.revision
        self.pairs.db.execute("CREATE TEMP TRIGGER fail_claim BEFORE INSERT ON persona_claims "
                              "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.distiller.save_result(result, revision)
        self.assertEqual(self.pairs.revision, revision)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM persona_analysis_runs').fetchone()[0], 0)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM persona_claims').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
