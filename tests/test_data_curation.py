from dataclasses import replace
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from data_curation import RuleFilter
from persona_types import DataScope, SourceOrigin


class RuleFilterTests(unittest.TestCase):
    def setUp(self):
        fixture = importlib.import_module('tests.test_dialogue_pairs').DialoguePairBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def pair(self, target='你好呀', *, origin=SourceOrigin.PUBLIC_DATASET,
             data_scope=DataScope.PUBLIC_DATASET):
        input_scope = data_scope
        input_origin = (SourceOrigin.REAL_RELATIONSHIP if data_scope == DataScope.PRIVATE_FRIEND
                        else SourceOrigin.REAL_SINGLE_SPEAKER)
        turns = self.fixture.turns([
            ('input', 'person-a', 'human', input_origin,
             '2026-10-04T11:59:00+00:00', input_scope),
            ('target', 'person-b', 'human', origin,
             '2026-10-04T12:00:00+00:00', data_scope)])
        pair, = self.fixture.builder.build_pairs(turns, {'person-b'})
        bubble = replace(pair.target_bubbles[0], text=target)
        return replace(pair, target_bubbles=(bubble,))

    def test_mixed_chinese_english_emoji_kept_and_english_only_filtered(self):
        mixed = self.pair('Hi呀🙂')
        english = self.pair('Hello there!')
        filt = RuleFilter()
        self.assertEqual(list(filt.iter_filter([mixed, english])), [mixed])
        self.assertEqual((filt.stats['language_checked'], filt.stats['language_rejected'],
                          filt.stats['accepted']), (2, 1, 1))

    def test_exact_and_normalized_near_duplicate_are_counted_separately(self):
        first = self.pair('你好！')
        punctuation_variant = replace(first, target_bubbles=(
            replace(first.target_bubbles[0], text='你好'),))
        filt = RuleFilter()
        self.assertEqual(list(filt.iter_filter([first, first, punctuation_variant])), [first])
        self.assertEqual((filt.stats['exact_duplicate'], filt.stats['near_duplicate'],
                          filt.stats['accepted']), (1, 1, 1))

    def test_generator_is_lazy_and_temp_signature_database_is_removed_on_close(self):
        first = self.pair('你好')
        paths = []
        original = tempfile.NamedTemporaryFile

        def record_path(*args, **kwargs):
            handle = original(*args, **kwargs)
            paths.append(handle.name)
            return handle

        def source():
            yield first
            raise AssertionError('filter pulled beyond the requested result')

        filt = RuleFilter()
        with patch('data_curation.tempfile.NamedTemporaryFile', side_effect=record_path):
            stream = filt.iter_filter(source())
            self.assertIs(next(stream), first)
            self.assertEqual(filt.stats['input'], 1)
            stream.close()
        self.assertEqual(len(paths), 1)
        self.assertFalse(Path(paths[0]).exists())

    def test_private_and_forbidden_target_sources_are_rejected(self):
        private = self.pair('你好', origin=SourceOrigin.REAL_RELATIONSHIP,
                            data_scope=DataScope.PRIVATE_FRIEND)
        filt = RuleFilter()
        self.assertEqual(list(filt.iter_filter([private])), [])
        self.assertEqual((filt.stats['input'], filt.stats['provenance_rejected']), (1, 1))


if __name__ == '__main__':
    unittest.main()
