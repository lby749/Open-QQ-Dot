import json
from contextlib import closing
from dataclasses import asdict, replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import persona_retriever
from persona_retriever import (_claim_source, _private_style_source, _readonly_pairs,
    _select_claims, _version_candidates)
from persona_retriever import PersonaRetriever
from persona_versions import (EXAMPLE_COLUMNS, PersonaVersionStore, claim_documents,
    render_derived)
from persona_types import (ContextBudgets, DataScope, FriendScope, PrivacyView,
    InvalidSource, RetrievalFilters, RetrievalRequest, ScopeMismatch, SourceOrigin, SourceRef)
from persona_store import PersonaStore
from persona_pack_bindings import PackBindingStore
from persona_types import PackVersion


NOW = '2026-10-06T00:00:00+00:00'


def _request(scene='daily_chat', query='music'):
    return RetrievalRequest(friend_id='12345', query=query, recent_context='', scene=scene,
        plan=None, filters=RetrievalFilters(), budgets=ContextBudgets(),
        allowed_pack_versions=frozenset(), now=NOW)


def _source(friend_id='12345'):
    return SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=1,
        speaker_id=friend_id, origin=SourceOrigin.REAL_RELATIONSHIP,
        data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
        friend_id=friend_id, event_time=NOW, recorded_at=NOW)


def _candidate(identifier, *, locked=False, confidence=0.1, scene='daily_chat'):
    source = _source()
    content = {'claim_id': identifier, 'claim_key': identifier, 'scene': scene,
        'dimension': 'ReplyPattern', 'trigger': 'music conversation',
        'response_pattern': 'brief reply', 'claim': 'music reply',
        'quality': 'MANUAL_LOCK' if locked else 'observed_structure'}
    from persona_types import Candidate
    return Candidate(id=identifier, namespace='persona', type='persona_claim',
        content=json.dumps(content), score=0, source=source, reason='fixture',
        retriever='persona', friend_id='12345', timestamp=None, confidence=confidence,
        original_scope=DataScope.PRIVATE_FRIEND, pack_version=None)


class PersonaRetrieverTests(unittest.TestCase):
    def test_non_persona_namespace_filter_skips_retrieval(self):
        from tests.test_persona_types import PersonaTypesTests
        fixture = PersonaTypesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        retriever = PersonaRetriever.__new__(PersonaRetriever)
        retriever.scope = fixture.scope()
        request = _request()
        request = RetrievalRequest(friend_id=request.friend_id, query=request.query,
            recent_context=request.recent_context, scene=request.scene, plan=request.plan,
            filters=RetrievalFilters(namespace=('memory',)), budgets=request.budgets,
            allowed_pack_versions=request.allowed_pack_versions, now=request.now)
        with patch.object(retriever, '_private', side_effect=AssertionError(
                'non-Persona namespace must not load a Persona version')):
            batch = retriever.retrieve(request, 6)
        self.assertEqual(batch.candidates, ())

    def test_private_style_source_requires_selected_human_target(self):
        unmapped = SimpleNamespace(speaker_id='12345', target_selected=False,
                                   source=_source())
        synthetic = replace(_source(), origin=SourceOrigin.SYNTHETIC)
        nonhuman = SimpleNamespace(speaker_id='12345', target_selected=True, source=synthetic)
        target = SimpleNamespace(speaker_id='12345', target_selected=True, source=_source())
        self.assertIs(_private_style_source((unmapped, nonhuman, target), {'12345'}), target.source)

    def test_readonly_pair_setup_closes_both_stores_when_normalizer_fails(self):
        from tests.test_persona_evidence import PersonaEvidenceTests
        PersonaEvidenceTests.setUp(self)
        closed, original_close = [], PersonaStore.close

        def track_close(store):
            closed.append(store.readonly)
            original_close(store)

        with patch.object(persona_retriever.DatasetNormalizer, '__init__',
                side_effect=RuntimeError('fixture failure')), \
                patch.object(PersonaStore, 'close', track_close):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                _readonly_pairs(self.scope)
        self.assertEqual(closed, [True, True])

    def test_ordinary_and_complex_caps_and_manual_lock_priority(self):
        candidates = [_candidate(str(i), locked=(i == 8), confidence=i / 20)
            for i in range(13)]
        ordinary = _select_claims(candidates, _request(), 20)
        self.assertEqual(len(ordinary), 6)
        self.assertEqual(ordinary[0].id, '8')

        complex_candidates = [_candidate(str(i), scene='complex_chat') for i in range(13)]
        complex_result = _select_claims(complex_candidates,
            _request(scene='complex_chat'), 20)
        self.assertEqual(len(complex_result), 12)

    def test_rejects_claim_evidence_from_another_friend(self):
        source = _source('98765')
        claim = {'claim_id': 'c1', 'claim_key': 'k1', 'quality': 'observed_structure'}
        evidence = [{'claim_id': 'c1', 'evidence_role': 'support',
            'source_message_id': 'm1', 'speaker_id': '98765',
            'source_origin': 'REAL_RELATIONSHIP', 'data_scope': 'PRIVATE_FRIEND'}]
        scope = SimpleNamespace(friend_id='12345')
        with self.assertRaises(ScopeMismatch):
            _claim_source(claim, evidence, {'m1': source}, scope, DataScope.PRIVATE_FRIEND,
                None, None)

    def test_returns_only_relevant_claim_fields_and_compact_manual_style(self):
        source = _source()
        claim = {'claim_id': 'c1', 'claim_key': 'k1', 'subject': 'BOT_PERSONA',
            'dimension': 'ReplyPattern', 'scene': 'daily_chat', 'trigger': 'music',
            'response_pattern': 'brief reply', 'claim': 'short music replies',
            'strength': 0.5, 'confidence': 0.4, 'quality': 'MANUAL_LOCK',
            'source_origin': ['MANUAL'], 'evidence': []}
        base = dict(claim, quality='observed_structure', source_origin=['REAL_RELATIONSHIP'],
            evidence=[{'claim_id': 'c1', 'evidence_role': 'support',
                'source_message_id': 'm1', 'speaker_id': '12345',
                'source_origin': 'REAL_RELATIONSHIP', 'data_scope': 'PRIVATE_FRIEND'}])
        profile = {'schema_version': 'persona-profile-v1', 'friend_id': '12345',
            'version': 'v1', 'claims': [claim],
            'manual_claims': {'k1': base}, 'private_secret': 'must not be returned'}
        style = {'schema_version': 'style-profile-v1', 'friend_id': '12345',
            'version': 'v1', 'observed': {
            'sample_info': {'sample_status': 'low', 'pair_count': 3},
            'message_length': {'reply': {'median': 4, 'p25': 2, 'p75': 6}},
            'bubble_behavior': {'rates': {'single': 0.8, 'two': 0.2}},
            'question_behavior': {'question_rate': {'rate': 0.1}},
            'punctuation': {'period_rate': {'rate': 0.2}},
            'fragment_usage': {'short_no_terminator_rate': {'rate': 0.6}},
            'common_expressions': {'items': [{'text': '啊'}]}},
            'manual_overrides': {'question_behavior.ask': {'value': False,
                'source': 'MANUAL_LOCK'}}}
        result = _version_candidates(profile, style, {'version': 'v1'}, {'m1': source},
            SimpleNamespace(friend_id='12345'), _request(), 6,
            source_scope=DataScope.PRIVATE_FRIEND, style_source=source)
        claim_output, style_output = result
        claim_json = json.loads(claim_output.content)
        self.assertEqual(claim_json['quality'], 'MANUAL_LOCK')
        self.assertNotIn('private_secret', claim_output.content)
        self.assertNotIn('evidence', claim_output.content)
        style_json = json.loads(style_output.content)
        self.assertEqual(style_json['manual_overrides']['question_behavior.ask']['value'], False)
        self.assertLessEqual(len(style_output.content), 300)

    def test_reads_active_private_json_and_rechecks_its_source(self):
        from tests.test_persona_evidence import PersonaEvidenceTests
        PersonaEvidenceTests.setUp(self)
        result = PersonaEvidenceTests._literal_result(self)
        self.distiller.save_result(result, self.pairs.revision)
        validated = self.validator.validate(result)
        self.validator.save(validated, self.pairs.revision)
        store = PersonaVersionStore(self.scope, self.pairs)
        build = self.scope.friend_dir / 'retriever-fixture-build'
        build.mkdir()
        profile = {'schema_version': 'persona-profile-v1', 'friend_id': self.scope.friend_id,
            'version': 'build', 'run_id': result.run_id,
            'source_content_hash': validated.source_content_hash,
            'claims': claim_documents(validated, 'build'), 'sample_status': result.sample_status,
            'quality': 'validated_drafts', 'manual_revision': 0, 'manual_overlay': {},
            'tombstones': []}
        style = {'schema_version': 'style-profile-v1', 'friend_id': self.scope.friend_id,
            'version': 'build', 'observed': result.style_profile, 'manual_overrides': {}}
        (build / 'persona_profile.json').write_text(json.dumps(profile, ensure_ascii=False), 'utf-8')
        (build / 'style_profile.json').write_text(json.dumps(style, ensure_ascii=False), 'utf-8')
        skill, report = render_derived(profile, style)
        (build / 'persona_skill.md').write_text(skill, 'utf-8')
        (build / 'persona_report.md').write_text(report, 'utf-8')
        with closing(sqlite3.connect(build / 'examples.db')) as db:
            db.execute('CREATE TABLE examples (' + ','.join(
                column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
        current = store.read_active()
        store.publish_version(validated, build, expected_revision=self.pairs.revision,
            expected_active_version=current['manifest']['version'] if current else None,
            expected_manual_revision=current['manifest']['manual_revision'] if current else 0)
        self.addCleanup(store.pairs.close)

        active = store.read_active()
        claim = active['profile']['claims'][0]
        request = _request(scene=claim['scene'], query='问我')
        original_connect = PersonaStore._connect
        connections, closed_states = [], []
        original_close = PersonaStore.close

        def track_connect(store, readonly):
            connection = original_connect(store, readonly)
            connections.append((readonly, connection))
            return connection

        def track_close(store):
            closed_states.append((store.readonly,
                store.db.execute('PRAGMA query_only').fetchone()[0],
                store.db.in_transaction))
            original_close(store)

        with patch.object(PersonaStore, '_migrate', side_effect=AssertionError(
                'runtime retrieval must not migrate the catalog')), \
                patch.object(PersonaStore, '_connect', track_connect), \
                patch.object(PersonaStore, 'close', track_close):
            result = PersonaRetriever(self.scope).retrieve(request, 6)
        self.assertTrue(closed_states)
        self.assertTrue(all(readonly for readonly, _ in connections))
        self.assertTrue(all(state == (True, 1, False) for state in closed_states))
        self.assertEqual([item.namespace for item in result.candidates], ['persona'])
        self.assertEqual(json.loads(result.candidates[0].content)['quality'],
                         claim['quality'])

    def test_rejects_profile_or_style_schema_and_scope_mismatch(self):
        profile = {'schema_version': 'persona-profile-v1', 'friend_id': '12345',
            'version': 'v1', 'claims': []}
        style = {'schema_version': 'style-profile-v1', 'friend_id': '12345',
            'version': 'v1'}
        scope = SimpleNamespace(friend_id='12345')

        for target, field, value, error in (
                (profile, 'schema_version', 'bad', InvalidSource),
                (style, 'schema_version', 'bad', InvalidSource),
                (profile, 'friend_id', '98765', ScopeMismatch),
                (style, 'friend_id', '98765', ScopeMismatch)):
            with self.subTest(target='style' if target is style else 'profile', field=field):
                changed_profile, changed_style = dict(profile), dict(style)
                (changed_style if target is style else changed_profile)[field] = value
                with self.assertRaises(error):
                    _version_candidates(changed_profile, changed_style,
                        {'version': 'v1'}, {}, scope, _request(), 6,
                        source_scope=DataScope.PRIVATE_FRIEND)

    def test_retrieves_only_bound_and_allowed_public_pack_version_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
            scope = FriendScope.load('12345', project_root=root)
            pack_id, version = 'public-fixture', 'v1'
            directory = root / 'data/persona_packs' / pack_id / 'versions' / version
            directory.mkdir(parents=True)
            ref = SourceRef(dataset_id='fixture', import_id='run1', record_id='target',
                message_seq=None, speaker_id='public-speaker', origin=SourceOrigin.PUBLIC_DATASET,
                data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=None, event_time=None, recorded_at=NOW, license_id='CC-BY',
                model_analysis_allowed=True)
            claim = {'claim_id': 'claim1', 'claim_key': 'claim-key', 'subject': 'BOT_PERSONA',
                'dimension': 'ReplyPattern', 'scene': 'daily_chat', 'trigger': 'music',
                'response_pattern': 'brief music reply', 'claim': 'short music replies',
                'strength': 0.7, 'confidence': 0.8, 'quality': 'observed_structure',
                'source_origin': ['PUBLIC_DATASET'], 'evidence': [{'claim_id': 'claim1',
                    'evidence_role': 'support', 'source_message_id': 'target1',
                    'speaker_id': ref.speaker_id, 'source_origin': 'PUBLIC_DATASET',
                    'data_scope': 'PUBLIC_DATASET'}]}
            profile = {'schema_version': 'persona-profile-v1', 'pack_id': pack_id,
                'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None,
                'claims': [claim]}
            style = {'schema_version': 'style-profile-v1', 'pack_id': pack_id,
                'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None,
                'observed': {'sample_info': {'sample_status': 'low', 'pair_count': 3},
                    'message_length': {'reply': {'median': 4, 'p25': 2, 'p75': 6}},
                    'bubble_behavior': {'rates': {'single': 1.0}}}, 'manual_overrides': {}}
            for name, value in (('persona_profile.json', profile),
                                ('style_profile.json', style)):
                (directory / name).write_text(json.dumps(value, ensure_ascii=False), 'utf-8')
            for name in ('persona_skill.md', 'persona_report.md'):
                (directory / name).write_text('derived public fixture', 'utf-8')
            def bubble(identifier, text):
                return {'id': identifier, 'text': text, 'source': asdict(ref)}
            example = {'id': 'pair1', 'user': [bubble('user1', 'music?')], 'context': [],
                'target_bubbles': [bubble('target1', '听歌很开心')], 'scene': 'daily_chat',
                'emotion': None, 'topic': 'music', 'reaction_type': 'respond',
                'source_origin': 'PUBLIC_DATASET', 'friend_id': None,
                'relationship_tags': None, 'source_refs': [asdict(ref)],
                'version': version}
            with closing(sqlite3.connect(directory / 'examples.db')) as db, db:
                db.execute('CREATE TABLE examples (' + ','.join(
                    column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
                db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                    tuple(json.dumps(example[column], ensure_ascii=False)
                        if column in ('user', 'context', 'target_bubbles', 'source_refs')
                        else example[column] for column in EXAMPLE_COLUMNS))
            files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir()}
            manifest = {'pack_id': pack_id, 'version': version, 'scope': 'PUBLIC_PACK',
                'friend_id': None, 'source_refs': [asdict(ref)],
                'selected_pair_ids': ['pair1'], 'sources': [{'dataset_id': 'fixture',
                    'local_use_allowed': True, 'derived_stats_allowed': True,
                    'license_data': 'CC-BY', 'raw_export_allowed': False}],
                'raw_export_allowed': False, 'raw_export_reasons': ['fixture'], 'files': files}
            (directory / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')

            bindings = PackBindingStore(scope)
            try:
                bindings.bind(pack_id, version)
                bound_scope = bindings.bound_scope()
            finally:
                bindings.close()
            request = RetrievalRequest(friend_id='12345', query='music', recent_context='',
                scene='daily_chat', plan=None, filters=RetrievalFilters(
                    namespace=('persona', 'style'), privacy_scope=DataScope.PUBLIC_PACK),
                budgets=ContextBudgets(), allowed_pack_versions=bound_scope.allowed_pack_versions,
                now=NOW)
            original_init = PackBindingStore.__init__
            readonly_values = []

            def track_init(store, current_scope, readonly=False):
                readonly_values.append(readonly)
                original_init(store, current_scope, readonly=readonly)

            with patch.object(PackBindingStore, '__init__', track_init):
                result = PersonaRetriever(bound_scope).retrieve(request, 6)
            self.assertTrue(readonly_values and all(readonly_values))
            self.assertEqual([item.namespace for item in result.candidates], ['persona', 'style'])
            for item in result.candidates:
                self.assertEqual(item.friend_id, '12345')
                self.assertEqual(item.original_scope, DataScope.PUBLIC_PACK)
                self.assertEqual(item.source.data_scope, DataScope.PUBLIC_PACK)
                self.assertEqual(item.source.friend_id, None)
                self.assertEqual(item.source.pack_id, pack_id)
                self.assertEqual(item.source.pack_version, version)
                self.assertEqual(item.pack_version, version)
            self.assertLessEqual(len(result.candidates[1].content), 300)

            for allowed in (frozenset(), frozenset({PackVersion(pack_id=pack_id, version='v2')})):
                denied = replace(request, allowed_pack_versions=allowed)
                self.assertEqual(PersonaRetriever(bound_scope).retrieve(denied, 6).candidates, ())


if __name__ == '__main__':
    unittest.main()
