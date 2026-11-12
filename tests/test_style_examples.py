import json
from contextlib import closing
from dataclasses import asdict
import hashlib
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_distiller import PersonaDistiller
from persona_evidence import EvidenceValidator
from persona_export import PersonaExporter
from persona_types import InvalidSource, PrivacyView, PrivacyViolation
from persona_versions import EXAMPLE_COLUMNS


class PersonaExporterTests(unittest.TestCase):
    def setUp(self):
        fixture = importlib.import_module('tests.test_persona_analysis_input').AnalysisInputBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for name in ('scope', 'pairs', 'dialogues', 'session', 'root'):
            setattr(self, name, getattr(fixture, name))
        self.distiller = PersonaDistiller(self.scope, self.pairs)
        self.validator = EvidenceValidator(self.scope, self.pairs)
        result = self.distiller.analyze([self.session.id], target_speaker_ids=['alias'])
        self.distiller.save_result(result, self.pairs.revision)
        self.validated = self.validator.validate(result)
        self.validator.save(self.validated, self.pairs.revision)
        self.exporter = PersonaExporter(self.scope, self.pairs)
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.build = Path(self.temp.name) / 'build'

    def test_local_build_preserves_target_and_publishes_five_files(self):
        self.exporter.build(self.validated, self.build)
        with closing(sqlite3.connect(self.build / 'examples.db')) as db:
            self.assertEqual(tuple(row[1] for row in db.execute('PRAGMA table_info(examples)')),
                             EXAMPLE_COLUMNS)
            row = db.execute('SELECT id,user,context,target_bubbles,scene,source_origin,friend_id,'
                             'relationship_tags,source_refs,version FROM examples').fetchone()
        pair = self.validated.result.scene_analysis.analysis_input.role_pairs[0]
        self.assertEqual(row[0], pair.id)
        self.assertEqual(json.loads(row[1]), [asdict(v) for v in pair.user_bubbles])
        self.assertEqual(json.loads(row[3]), [asdict(v) for v in pair.target_bubbles])
        self.assertEqual((row[5], row[6], row[7], row[9]), ('REAL_RELATIONSHIP', '12345', None, 'build'))
        active = self.exporter.publish(self.validated, Path(self.temp.name) / 'publish',
            expected_revision=self.pairs.revision, expected_active_version=None,
            expected_manual_revision=0)
        self.assertEqual(len(active['manifest']['files']), 5)
        self.assertEqual(active['profile']['run_id'], self.validated.result.run_id)
        self.assertEqual(active['manifest']['derived_indexes']['status'], 'not_built_P5')
        self.assertEqual(active['manifest']['files']['examples.db'], hashlib.sha256(
            (Path(active['directory']) / 'examples.db').read_bytes()).hexdigest())

    def test_private_cloud_safe_fails_before_creating_output(self):
        with self.assertRaises(PrivacyViolation):
            self.exporter.build(self.validated, self.build, privacy_view=PrivacyView.CLOUD_SAFE)
        self.assertFalse(self.build.exists())

    def test_wrong_scope_analysis_is_rejected(self):
        from dataclasses import replace
        from persona_store import ScopeMismatch
        with self.assertRaises((InvalidSource, ScopeMismatch)):
            self.exporter.build(replace(self.validated,
                result=replace(self.validated.result, friend_id='23456')), self.build)
        self.assertFalse(self.build.exists())

    def test_nonempty_build_directory_is_not_overwritten(self):
        self.build.mkdir()
        sentinel = self.build / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(InvalidSource):
            self.exporter.build(self.validated, self.build)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_uncompleted_analysis_is_rejected_before_output(self):
        self.pairs.db.execute("UPDATE persona_analysis_runs SET state='DRAFT'")
        with self.assertRaises(InvalidSource):
            self.exporter.build(self.validated, self.build)
        self.assertFalse(self.build.exists())


if __name__ == '__main__':
    unittest.main()
