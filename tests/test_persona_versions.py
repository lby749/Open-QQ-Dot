import json
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from persona_versions import PersonaVersionStore, claim_documents, render_derived, EXAMPLE_COLUMNS
from persona_store import Conflict
from persona_types import InvalidSource


class PersonaVersionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_evidence import PersonaEvidenceTests
        PersonaEvidenceTests.setUp(self)
        result = PersonaEvidenceTests._literal_result(self)
        self.distiller.save_result(result, self.pairs.revision)
        self.validated = self.validator.validate(result)
        self.validator.save(self.validated, self.pairs.revision)
        self.versions = PersonaVersionStore(self.scope, self.pairs)
        self.build = self.scope.friend_dir / 'fixture-build'
        self.build.mkdir()
        self.profile = dict(schema_version='persona-profile-v1', friend_id=self.scope.friend_id,
            version='fixture', run_id=result.run_id, source_content_hash=self.validated.source_content_hash,
            claims=claim_documents(self.validated, 'fixture'), sample_status=result.sample_status,
            quality='validated_drafts', manual_revision=0, manual_overlay={}, tombstones=[])
        self.style = dict(schema_version='style-profile-v1', friend_id=self.scope.friend_id,
                          version='fixture', observed=result.style_profile, manual_overrides={})
        self._build()

    def _build(self):
        for name, value in [('persona_profile.json', self.profile), ('style_profile.json', self.style)]:
            (self.build / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        for name, value in zip(('persona_skill.md', 'persona_report.md'), render_derived(self.profile, self.style)):
            (self.build / name).write_text(value, encoding='utf-8')
        with closing(sqlite3.connect(self.build / 'examples.db')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS examples (' + ','.join(c + ' TEXT' for c in EXAMPLE_COLUMNS) + ')')

    def _cas(self):
        current = self.versions.read_active()
        return dict(expected_revision=self.pairs.revision,
            expected_active_version=current['manifest']['version'] if current else None,
            expected_manual_revision=current['manifest']['manual_revision'] if current else 0)

    def _publish(self):
        return self.versions.publish_version(self.validated, self.build, **self._cas())

    def test_completed_real_evidence_and_five_files_required(self):
        with self.assertRaises(InvalidSource):
            self.versions.publish_version({}, self.build, **self._cas())
        self.pairs.db.execute("UPDATE persona_analysis_runs SET state='DRAFT'")
        with self.assertRaises(InvalidSource):
            self._publish()
        self.pairs.db.execute("UPDATE persona_analysis_runs SET state='COMPLETED'")
        (self.build / 'examples.db').rename(self.build / 'missing.db')
        with self.assertRaises(InvalidSource):
            self._publish()
        (self.build / 'missing.db').rename(self.build / 'examples.db')
        active = self._publish()
        self.assertEqual(len(active['manifest']['files']), 5)
        self.assertEqual(active['profile']['claims'][0]['evidence'][0]['span_text'], '别每句话都问我。')
        self.assertEqual(active['style']['observed'], json.loads(json.dumps(self.style['observed'])))

    def test_manual_layer_late_worker_and_immutable_history(self):
        first = self._publish()
        old_cas = self._cas()
        original = (Path(first['directory']) / 'persona_profile.json').read_bytes()
        key = first['profile']['claims'][0]['claim_key']
        active = self.versions.edit_manual({key: {'claim': '人工设定'}},
            style_overrides={'generated_at': None}, **old_cas)
        self.assertEqual(active['profile']['claims'][0]['quality'], 'MANUAL_LOCK')
        self.assertEqual(active['profile']['claims'][0]['evidence'], [])
        self.assertEqual(active['style']['manual_overrides']['generated_at']['source'], 'MANUAL_LOCK')
        with self.assertRaises(Conflict):
            self.versions.publish_version(self.validated, self.build, **old_cas)
        deleted = self.versions.edit_manual({}, [key], **self._cas())
        self.assertEqual(deleted['profile']['claims'], [])
        self.assertEqual((Path(first['directory']) / 'persona_profile.json').read_bytes(), original)

    def test_rollback_retains_latest_manual_and_regenerates(self):
        first = self._publish()
        key = first['profile']['claims'][0]['claim_key']
        self.versions.edit_manual({key: {'claim': '保留人工'}}, style_overrides={'generated_at': None}, **self._cas())
        active = self.versions.rollback(first['manifest']['version'], **self._cas())
        self.assertEqual(active['profile']['claims'][0]['claim'], '保留人工')
        self.assertEqual(active['profile']['manual_revision'], 1)
        self.assertEqual(active['manifest']['derived_indexes']['status'], 'not_built_P5')
        self.assertIn(active['manifest']['version'], (Path(active['directory']) / 'persona_skill.md').read_text(encoding='utf-8'))

    def test_manual_claim_survives_new_automatic_result_without_key(self):
        from dialogue_turns import SessionSegmenter
        first = self._publish()
        key = first['profile']['claims'][0]['claim_key']
        self.versions.edit_manual({key: {'claim': '人工保留'}}, **self._cas())
        self.dialogues.db.execute('UPDATE imported_messages SET text=? WHERE friend_id=? '
            'AND import_id=? AND message_seq=0', ('Hello. ', self.scope.friend_id, 'imp'))
        messages = tuple(self.dialogues.normalizer.normalize([{'friend_id': self.scope.friend_id,
            'dataset_id': 'private', 'import_id': 'imp', 'record_id': 'r0', 'message_seq': 0}]))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={messages[0].id: 'new-automatic'}))
        self.dialogues.save_session(session, self.dialogues.revision)
        result = self.distiller.analyze([session.id])
        self.assertFalse(result.relationship_claim_drafts)
        self.distiller.save_result(result, self.pairs.revision)
        self.validated = self.validator.validate(result)
        self.validator.save(self.validated, self.pairs.revision)
        self.profile.update(run_id=result.run_id, source_content_hash=self.validated.source_content_hash,
                            claims=[], sample_status=result.sample_status)
        self.style['observed'] = result.style_profile
        self._build()
        active = self._publish()
        claim = active['profile']['claims'][0]
        self.assertEqual((claim['claim'], claim['source_origin']), ('人工保留', ['MANUAL']))
        self.assertEqual(active['profile']['automatic_claims'], [])
        skill = (Path(active['directory']) / 'persona_skill.md').read_text(encoding='utf-8')
        self.assertIn('"confidence":0', skill)
        self.assertIn('"scene":', skill)
        self.assertIn('question_behavior', skill)
        self.assertIn('sticker_behavior', skill)
        deleted = self.versions.edit_manual({}, [key], **self._cas())
        self.assertEqual(deleted['profile']['claims'], [])

    def test_crash_before_and_after_pointer_reconciles_manifest(self):
        first = self._publish()
        with patch.object(self.versions, '_switch_pointer', side_effect=RuntimeError('before')):
            with self.assertRaises(RuntimeError):
                self._publish()
        self.assertEqual(self.versions.reconcile()['manifest']['version'], first['manifest']['version'])
        self.assertEqual(self.pairs.db.execute("SELECT count(*) FROM artifact_versions WHERE state='INVALID'").fetchone()[0], 1)
        with patch.object(self.versions, '_reconcile_locked', side_effect=RuntimeError('after')):
            with self.assertRaises(RuntimeError):
                self._publish()
        switched = self.versions.read_active()['manifest']['version']
        self.assertNotEqual(switched, first['manifest']['version'])
        self.versions.reconcile()
        revision = self.pairs.revision
        self.versions.reconcile()
        self.assertEqual(self.pairs.revision, revision)
        self.assertEqual(self.pairs.db.execute('SELECT state FROM artifact_versions WHERE id=?', (switched,)).fetchone()[0], 'COMPLETED')

    def test_dirty_json_and_busy_lock_fail_closed(self):
        first = self._publish()
        with self.versions._lock():
            with self.assertRaises(Conflict):
                self.versions.reconcile()
        (Path(first['directory']) / 'persona_profile.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(Conflict):
            self.versions.read_active()
        with self.assertRaises(Conflict):
            self.versions.reconcile()


if __name__ == '__main__':
    unittest.main()
