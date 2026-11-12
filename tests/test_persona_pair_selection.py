from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
from dataclasses import replace

from dialogue_pairs import DialoguePairBuilder
from dialogue_turns import build_turns
from persona_distiller import PersonaDistiller
from persona_evidence import EvidenceValidator
from persona_export import PersonaExporter
from persona_types import InvalidSource
from tests.test_persona_analysis_input import AnalysisInputBuilderTests


class PairSelectionTests(unittest.TestCase):
    def setUp(self):
        fixture = AnalysisInputBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_selection_survives_save_validation_export_and_source_recheck(self):
        f = self.fixture
        targets = ['alias', '12345']
        candidates = tuple(DialoguePairBuilder(f.scope).build_pairs(build_turns(f.session), targets))
        f.pairs.save_pairs(candidates, f.pairs.revision)
        selected = [pair.id for pair in candidates if pair.target_speaker_id == 'alias']
        whole = f.builder.build([f.session.id], targets)
        subset = f.builder.build([f.session.id], targets, pair_ids=selected)
        self.assertGreater(len(whole.role_pairs), len(subset.role_pairs))
        self.assertEqual(subset.relationship_events, whole.relationship_events)
        self.assertEqual([pair.id for pair in subset.role_pairs], selected)
        for ids in ([], ['missing']):
            with self.assertRaises(InvalidSource):
                f.builder.build([f.session.id], targets, pair_ids=ids)
        distiller = PersonaDistiller(f.scope, f.pairs)
        result = distiller.analyze([f.session.id], targets, pair_ids=selected)
        self.assertEqual(result.settings['pair_ids'], selected)
        distiller.save_result(result, f.pairs.revision)
        validator = EvidenceValidator(f.scope, f.pairs)
        validated = validator.validate(result)
        validator.save(validated, f.pairs.revision)
        exporter = PersonaExporter(f.scope, f.pairs)
        active = exporter.publish(validated, f.root / 'selected-build',
            expected_revision=f.pairs.revision, expected_active_version=None,
            expected_manual_revision=0)
        with closing(sqlite3.connect(Path(active['directory']) / 'examples.db')) as db:
            self.assertEqual([row[0] for row in db.execute('SELECT id FROM examples')], selected)
        self.assertEqual(exporter.versions.read_active()['manifest']['version'],
                         active['manifest']['version'])
        broadened = dict(result.settings)
        broadened.pop('pair_ids')
        with self.assertRaises(InvalidSource):
            validator.validate(replace(result, settings=broadened))
        self.assertNotIn('pair_ids', distiller.analyze([f.session.id], targets).settings)


if __name__ == '__main__':
    unittest.main()
