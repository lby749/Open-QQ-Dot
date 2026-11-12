from dataclasses import asdict, replace
import hashlib
import json
import unittest
from unittest.mock import patch

from dialogue_pairs import DialoguePairBuilder, PairStore
from dialogue_turns import SessionSegmenter, build_turns
from persona_store import Conflict, ScopeMismatch
from persona_types import FriendScope, InvalidSource
from sticker_profile import StickerBehaviorProfile


class LocalClassifierFixture:
    provider_kind = 'LOCAL'
    runtime = {'base_model': 'fixture', 'model': 'fixture', 'model_revision': 'fixture-v1',
               'assets': [{'name': 'fixture', 'sha256': 'f'*64}]}

    def __init__(self):
        self.calls = []
        self.result = {'labels': {'intent': 'share', 'emotion': 'happy', 'tone': 'casual', 'initiative': 'proactive'},
                       'needs_review': False, 'reason': 'clear'}

    def classify_with_review(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return json.loads(json.dumps(self.result))


class StickerProfileTests(unittest.TestCase):
    def setUp(self):
        from tests.test_dialogue_turns import DialogueStoreTests
        fixture = DialogueStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.scope, self.dialogues = fixture.scope, fixture.dialogues
        self.pairs = PairStore(self.scope)
        self.addCleanup(self.pairs.close)
        self.classifier = LocalClassifierFixture()
        self.profile = StickerBehaviorProfile(self.scope, self.pairs, classifier=self.classifier)
        self.counter = 0

    def session(self, replies, *, input_text='Exam passed, so happy', origin='REAL_RELATIONSHIP',
                reply_origin=None, reply_speaker='12345', context=(), public=False, input_media=None, repeat=1):
        """Persist an isolated, source-verified session; no production/model/QQ calls."""
        self.counter += 1
        dataset, imported = ('public', 'pubimp') if public else ('private', 'imp')
        template = dict(self.dialogues.db.execute('SELECT * FROM imported_messages WHERE import_id=? LIMIT 1',
                                                (imported,)).fetchone())
        rows = [(f'context-{i}', 'human', origin, text, 'text', None) for i, text in enumerate(context)]
        for _ in range(repeat):
            rows.append(('other-human', 'human', origin, input_text, 'image' if input_media else 'text', input_media))
            rows.extend((reply_speaker, 'friend', reply_origin or origin, text, kind, media)
                        for text, kind, media in replies)
        locators = []
        for i, (speaker, role, source_origin, text, kind, media) in enumerate(rows):
            record = f'sticker-profile-{self.counter}-{i}'
            values = dict(template, record_id=record, message_seq=100+self.counter*100+i,
                speaker_id=speaker, speaker_role=role, text=text, message_type=kind, media_id=media,
                event_time=None, source_origin=source_origin,
                content_fingerprint=hashlib.sha256(record.encode()).hexdigest())
            self.dialogues.db.execute('INSERT INTO imported_messages (' + ','.join(values) + ') VALUES (' +
                ','.join('?' for _ in values) + ')', tuple(values.values()))
            locators.append({'friend_id': self.scope.friend_id, 'dataset_id': dataset,
                             'import_id': imported, 'record_id': record, 'message_seq': values['message_seq']})
        messages = tuple(self.dialogues.normalizer.normalize(locators))
        session = next(SessionSegmenter(self.scope).segment(messages,
            conversation_ids={m.id: 'sticker-test-' + str(self.counter) for m in messages}))
        self.dialogues.save_session(session, self.dialogues.revision)
        pairs = tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), {reply_speaker}))
        if pairs:
            self.pairs.save_pairs(pairs, self.pairs.revision)
        return session

    def asset(self, asset_id, sessions, *, kind='STICKER', status='COMPLETED', valid=1, wrong_source=False):
        refs = [asdict(b.source) for s in sessions for b in s.messages if b.media_id == asset_id]
        if wrong_source: refs = []
        self.pairs.db.execute('INSERT INTO sticker_assets '
            '(friend_id,asset_id,original_relpath,privacy_scope,source_origin,source,declared_kind,'
            'local_use_allowed,model_analysis_allowed,valid,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.scope.friend_id, asset_id, asset_id+'.bin', 'PRIVATE_FRIEND', 'REAL_RELATIONSHIP',
             json.dumps({'locator': 'fixture-only', 'source_refs': refs}), 'STICKER', 1, 1, valid, 'fixture'))
        self.pairs.db.execute('INSERT INTO sticker_metadata '
            '(friend_id,asset_id,analysis_status,sticker_kind,visual_style,text_content,analyzer_id,'
            'analysis_version,unknown_reason,analyzed_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (self.scope.friend_id, asset_id, status, kind, 'fixture-cute', 'OCR must not become reply text',
             'fixture-local', 'fixture-v1', 'UNCLASSIFIED' if status == 'UNKNOWN' else None, 'fixture'))

    def test_input_emotion_contract_is_independent_and_no_target_required(self):
        session = self.session([('TARGET secret', 'text', None), ('second target bubble', 'text', None)],
                               context=('previous real text',))
        result = self.profile.calculate([session.id, session.id])
        self.assertEqual(result['sample_info']['reply_unit_count'], 1)
        unit = result['reply_units'][0]
        self.assertEqual(unit['subject'], 'FRIEND')
        self.assertIsNone(unit['pair_id'])
        self.assertEqual(unit['emotion_label']['emotion'], 'happy')
        self.assertEqual(unit['emotion_label']['status'], 'estimated')
        self.assertEqual(self.classifier.calls, [('Exam passed, so happy', {'direction': None,
            'context': [{'text': 'previous real text', 'direction': None, 'seq': 0}], 'following': ()})])
        self.assertNotIn('TARGET secret', json.dumps(self.classifier.calls))
        group = result['groups'][0]['emotion_groups'][0]
        self.assertEqual(group['raw_counts']['text_only'], 1)
        self.assertEqual(group['rates'], dict.fromkeys(('text_only', 'text_plus_sticker', 'sticker_only')))
        self.assertEqual(group['unknown_reason'], 'LOW_SAMPLE')

    def test_unknown_review_truncation_empty_and_unavailable_are_not_guesses(self):
        cases = [('review', 'normal input', (), None), ('long', 'x'*801, (), None),
                 ('context-long', 'normal input', ('x'*161,), None), ('empty', '', (), 'image-input'),
                 ('unavailable', 'normal input', (), None), ('invalid', 'normal input', (), None), ('short', 'ok', (), None)]
        for case, text, context, media in cases:
            with self.subTest(case=case):
                session = self.session([('normal reply', 'text', None)], input_text=text, context=context, input_media=media)
                self.classifier.result['needs_review'] = case == 'review'
                self.classifier.result['reason'] = 'multiple_interpretations' if case == 'review' else 'clear'
                if case == 'invalid': self.classifier.result['labels']['emotion'] = 'invented'
                if case == 'unavailable':
                    with patch.object(self.classifier, 'classify_with_review', side_effect=RuntimeError('unavailable')):
                        result = self.profile.calculate([session.id])
                else: result = self.profile.calculate([session.id])
                label = result['reply_units'][0]['emotion_label']
                self.assertEqual(label['status'], 'unknown')
                self.assertIsNone(label['emotion'])
                self.assertEqual(result['groups'][0]['emotion_coverage']['labeled'], 0)
                self.assertEqual(result['groups'][0]['emotion_groups'][0]['unknown_reason'], 'EMOTION_UNAVAILABLE')
                self.classifier.result['labels']['emotion'] = 'happy'

    def test_complete_reply_types_face_and_missing_assets_are_separate(self):
        sessions = [self.session([('text reply', 'text', None)]),
            self.session([('text reply', 'text', None), ('native face placeholder', 'face', 'face-id')]),
            self.session([('', 'sticker', 'sticker-id'), ('face placeholder', 'face', 'face-id')])]
        result = self.profile.calculate([s.id for s in sessions])
        group = result['groups'][0]
        self.assertEqual(group['raw_counts'], {'text_only': 1, 'text_plus_sticker': 1, 'sticker_only': 1, 'unknown': 0})
        self.assertEqual(group['emotion_coverage']['rate'], 1)
        self.assertEqual(group['asset_coverage']['sticker_bubbles'], 3)
        self.assertEqual(result['occurrences'], [])
        self.assertEqual([call[0] for call in self.classifier.calls], ['Exam passed, so happy']*3)
        self.assertTrue(all(call[1]['following'] == () for call in self.classifier.calls))
        self.assertEqual(self.profile.save(result, self.pairs.revision), self.pairs.revision)

    def test_exact_image_asset_chain_ocr_and_repeat_occurrences(self):
        first_id, second_id = 'a'*64, 'b'*64
        first = self.session([('OCR is not prose', 'image', first_id), ('', 'image', second_id)])
        second = self.session([('', 'face', first_id)])
        self.asset(first_id, [first, second])
        self.asset(second_id, [first])
        result = self.profile.calculate([first.id, second.id])
        self.assertEqual(result['groups'][0]['raw_counts']['sticker_only'], 2)
        self.assertEqual(len(result['occurrences']), 3)
        self.assertEqual(sum(r['asset_id'] == first_id for r in result['occurrences']), 2)
        revision = self.pairs.revision
        self.assertEqual(self.profile.save(result, revision), revision+1)
        self.assertEqual(self.profile.save(result, revision), revision+1)
        before = len(self.classifier.calls)
        examples = self.profile.read_examples()
        self.assertEqual(len(examples), 3)
        self.assertEqual(len(self.classifier.calls), before)
        self.assertTrue(all(e['scope'] == 'PRIVATE_FRIEND' for e in examples))
        self.pairs.db.execute('UPDATE sticker_assets SET valid=0 WHERE friend_id=? AND asset_id=?',
                             (self.scope.friend_id, first_id))
        with self.assertRaises(InvalidSource): self.profile.read_examples()
        rebuilt = self.profile.calculate([first.id, second.id])
        self.assertEqual(len(rebuilt['occurrences']), 1)
        self.profile.save(rebuilt, self.pairs.revision)
        self.assertEqual(len(self.profile.read_examples()), 1)

    def test_image_nonsticker_unknown_unmapped_and_wrong_source_never_promote(self):
        cases = [('NON_STICKER', 'COMPLETED', False), ('UNKNOWN', 'UNKNOWN', False),
                 ('STICKER', 'COMPLETED', True), ('unmapped', None, False), ('unknown-type', 'COMPLETED', False)]
        for index, (kind, status, wrong) in enumerate(cases):
            asset_id = str(index+1)*64
            session = self.session([('caption/placeholder', 'unknown' if kind == 'unknown-type' else 'image', asset_id)])
            if kind != 'unmapped':
                self.asset(asset_id, [session], kind='STICKER' if kind == 'unknown-type' else kind,
                           status=status, wrong_source=wrong)
            result = self.profile.calculate([session.id])
            self.assertEqual(result['groups'][0]['raw_counts']['unknown'], 1)
            self.assertEqual(result['occurrences'], [])
            self.assertEqual(result['groups'][0]['emotion_coverage']['by_send_type']['unknown']['rate'], 1)

    def test_real_v15_serialization_ignores_policy_only_and_keeps_complete_identity(self):
        from sticker_metadata import StickerAsset, StickerMetadataStore
        asset_id = 'f'*64
        session = self.session([('', 'image', asset_id)])
        bubble = session.messages[-1]
        self.assertFalse(bubble.source.model_analysis_allowed)
        analyzed_ref = replace(bubble.source, model_analysis_allowed=True)
        declared = StickerAsset(friend_id=self.scope.friend_id, asset_id=asset_id,
            original_relpath=asset_id+'.bin', declared_kind='STICKER',
            source_origin=bubble.source.origin, source='fixture-only', source_refs=(analyzed_ref,))
        serialized = StickerMetadataStore._source_record(declared, (analyzed_ref,))
        source = json.loads(serialized)
        self.assertNotIn('pack_id', source['source_refs'][0])
        self.assertNotIn('pack_version', source['source_refs'][0])
        self.assertTrue(source['source_refs'][0]['model_analysis_allowed'])
        self.asset(asset_id, [session])
        self.pairs.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=? AND asset_id=?',
                             (serialized, self.scope.friend_id, asset_id))
        result = self.profile.calculate([session.id])
        self.assertEqual(result['groups'][0]['raw_counts']['sticker_only'], 1)
        self.assertEqual(len(result['occurrences']), 1)
        self.profile.save(result, self.pairs.revision)
        self.assertEqual(len(self.profile.read_examples()), 1)
        for field, wrong in (('friend_id', '67890'), ('origin', 'REAL_SINGLE_SPEAKER'),
                             ('record_id', 'different-record'), ('import_id', 'different-import'),
                             ('dataset_id', 'different-dataset'), ('data_scope', 'PUBLIC_DATASET'),
                             ('privacy_view', 'CLOUD_SAFE'), ('speaker_id', 'other-speaker'),
                             ('message_seq', 1), ('event_time', '2026-10-07T01:00:00+00:00'),
                             ('recorded_at', '2026-10-07T02:00:00+00:00'), ('license_id', 'other-license')):
            with self.subTest(field=field):
                corrupted = json.loads(serialized)
                corrupted['source_refs'][0][field] = wrong
                self.pairs.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=? AND asset_id=?',
                                     (json.dumps(corrupted), self.scope.friend_id, asset_id))
                result = self.profile.calculate([session.id])
                self.assertEqual(result['groups'][0]['raw_counts']['unknown'], 1)
                self.assertEqual(result['occurrences'], [])
                with self.assertRaises(InvalidSource): self.profile.read_examples()
        for field in tuple(source['source_refs'][0]):
            if field == 'model_analysis_allowed': continue
            with self.subTest(missing=field):
                incomplete = json.loads(serialized)
                del incomplete['source_refs'][0][field]
                self.pairs.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=? AND asset_id=?',
                                     (json.dumps(incomplete), self.scope.friend_id, asset_id))
                result = self.profile.calculate([session.id])
                self.assertEqual(result['occurrences'], [])
                self.assertEqual(result['groups'][0]['raw_counts']['unknown'], 1)

    def test_low_sample_gate_origin_denominators_and_style_coverage(self):
        first = self.session([('', 'face', 'native')], repeat=20)
        one_session = self.profile.calculate([first.id])['groups'][0]['emotion_groups'][0]
        self.assertEqual(one_session['denominator'], 20)
        self.assertEqual(one_session['independent_sessions'], 1)
        self.assertEqual(one_session['status'], 'unknown')
        other = self.session([('normal reply', 'text', None)], repeat=20, origin='REAL_SINGLE_SPEAKER', reply_speaker='alias')
        isolated = self.profile.calculate([first.id, other.id], target_speaker_ids=['alias'])
        self.assertEqual(len(isolated['groups']), 2)
        self.assertTrue(all(g['emotion_groups'][0]['status'] == 'unknown' for g in isolated['groups']))
        second = self.session([('normal reply', 'text', None)])
        combined = self.profile.calculate([first.id, second.id])['groups'][0]
        emotion = combined['emotion_groups'][0]
        self.assertEqual(emotion['status'], 'estimated')
        self.assertEqual(emotion['denominator'], 21)
        self.assertEqual(emotion['rates']['sticker_only'], 20/21)
        self.assertEqual(sum(emotion['rates'].values()), 1)
        self.assertEqual(emotion['preferred_styles'], [])
        self.assertEqual(emotion['preferred_style_status'], 'unknown')
        nineteen_a = self.session([('normal reply', 'text', None)], repeat=10)
        nineteen_b = self.session([('normal reply', 'text', None)], repeat=9)
        nineteen = self.profile.calculate([nineteen_a.id, nineteen_b.id])['groups'][0]['emotion_groups'][0]
        self.assertEqual(nineteen['denominator'], 19)
        self.assertEqual(nineteen['status'], 'unknown')

    def test_nonhuman_synthetic_manual_and_public_never_become_friend_preferences(self):
        for origin in ('BOT_GENERATED', 'SYNTHETIC', 'MANUAL'):
            session = self.session([('wrong target', 'text', None)], reply_origin=origin)
            result = self.profile.calculate([session.id])
            self.assertEqual(result['groups'], [])
            self.assertEqual(result['occurrences'], [])
        public = self.session([('public dialogue', 'text', None)], public=True, origin='PUBLIC_DATASET')
        result = self.profile.calculate([public.id], target_speaker_ids=['12345'])
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['sample_info']['excluded']['NOT_PRIVATE_REAL'], 1)
        class Cloud:
            provider_kind = 'CLOUD'
            def classify_with_review(self, *args, **kwargs): raise AssertionError('private cloud call')
        with self.assertRaises(InvalidSource): StickerBehaviorProfile(self.scope, self.pairs, classifier=Cloud())

    def test_source_delete_forgery_stale_cas_and_transaction_rollback(self):
        asset_id = 'c'*64
        session = self.session([('', 'image', asset_id)])
        self.asset(asset_id, [session])
        result = self.profile.calculate([session.id])
        revision = self.pairs.revision
        forged = json.loads(json.dumps(result))
        forged['reply_units'][0]['emotion_label']['emotion'] = 'sad'
        with self.assertRaises(InvalidSource): self.profile.save(forged, revision)
        with self.assertRaises(Conflict): self.profile.save(result, revision-1)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM sticker_examples').fetchone()[0], 0)
        with patch.object(self.pairs.dialogues.store, 'readonly', True):
            from persona_store import Unavailable
            with self.assertRaises(Unavailable): self.profile.save(result, revision)
        # A rejecting fixture trigger proves the whole DML batch and revision roll back.
        self.pairs.db.execute("CREATE TEMP TRIGGER reject_occurrence BEFORE INSERT ON sticker_examples BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        with self.assertRaises(Exception): self.profile.save(result, revision)
        self.assertEqual(self.pairs.revision, revision)
        self.assertEqual(self.pairs.db.execute('SELECT count(*) FROM sticker_examples').fetchone()[0], 0)
        self.pairs.db.execute('DROP TRIGGER reject_occurrence')
        self.pairs.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (self.scope.friend_id,))
        with self.assertRaises(Conflict): self.profile.save(result, self.pairs.revision)
        fresh = self.profile.calculate([session.id])
        self.profile.save(fresh, self.pairs.revision)
        locator = json.loads(session.messages[-1].id[7:])
        self.pairs.db.execute('DELETE FROM imported_messages WHERE friend_id=? AND dataset_id=? AND import_id=? AND record_id=? AND message_seq=?',
                             (self.scope.friend_id, *locator))
        with self.assertRaises(InvalidSource): self.profile.read_examples()
        with self.assertRaises(InvalidSource): self.profile.calculate([session.id])
        with self.assertRaises(InvalidSource): self.profile.save(fresh, self.pairs.revision)

    def test_cross_friend_scope_and_changed_sources_during_model_are_rejected(self):
        session = self.session([('normal reply', 'text', None)])
        alien = self.scope.friend_dir.parent / '67890'
        alien.mkdir()
        (alien / 'friend.json').write_text(json.dumps({'qq_id': '67890'}), 'utf-8')
        registry_path = self.scope.project_root / 'friends.json'
        registry = json.loads(registry_path.read_text('utf-8'))
        registry['friends']['67890'] = {'qq_id': '67890', 'directory': str(alien)}
        registry_path.write_text(json.dumps(registry), 'utf-8')
        other = FriendScope.load('67890', project_root=self.scope.project_root)
        with self.assertRaises(ScopeMismatch): StickerBehaviorProfile(other, self.pairs, classifier=self.classifier)
        other_pairs = PairStore(other)
        self.addCleanup(other_pairs.close)
        with self.assertRaises(InvalidSource):
            StickerBehaviorProfile(other, other_pairs, classifier=self.classifier).calculate([session.id])
        original = self.classifier.classify_with_review
        def mutate(*args, **kwargs):
            self.pairs.db.execute('UPDATE scope_state SET revision=revision+1 WHERE friend_id=?', (self.scope.friend_id,))
            return original(*args, **kwargs)
        with patch.object(self.classifier, 'classify_with_review', side_effect=mutate), self.assertRaises(Conflict):
            self.profile.calculate([session.id])

    def test_role_pair_occurrence_is_source_rebuilt_and_first_turn_stays_unknown(self):
        asset_id = 'd'*64
        session = self.session([('', 'sticker', asset_id)], reply_speaker='alias')
        self.asset(asset_id, [session])
        result = self.profile.calculate([session.id], target_speaker_ids=['alias'])
        self.assertEqual(result['reply_units'][0]['subject'], 'BOT_PERSONA')
        self.assertIsNotNone(result['reply_units'][0]['pair_id'])
        self.profile.save(result, self.pairs.revision)
        self.assertEqual(self.profile.read_examples()[0]['locator']['episode_id'], result['reply_units'][0]['pair_id'])
        both = self.profile.calculate([session.id], friend_speaker_ids=['other-human', 'alias'], target_speaker_ids=['alias'])
        self.assertEqual(both['sample_info']['reply_unit_count'], 2)
        initial = next(u for u in both['reply_units'] if u['emotion_label']['input_turn_id'] is None)
        self.assertEqual(initial['emotion_label']['unknown_reason'], 'INPUT_NOT_REAL_OR_MISSING')
        self.assertTrue(all(u['subject'] == 'FRIEND' for u in both['reply_units']))
        self.assertEqual({g['speaker_id'] for g in both['groups']}, {'other-human', 'alias'})
        self.assertTrue(all(g['reply_unit_count'] == 1 for g in both['groups']))
        empty = self.profile.calculate([])
        self.assertEqual(empty['status'], 'unknown')
        self.assertEqual(empty['groups'], [])
        self.assertEqual(empty['occurrences'], [])

    def test_style_preferences_count_replies_not_assets_and_have_separate_coverage(self):
        asset_id = 'e'*64
        sessions = [self.session([('', 'image', asset_id), ('', 'image', asset_id)], repeat=10)
                    for _ in range(2)]
        self.asset(asset_id, sessions)
        result = self.profile.calculate([s.id for s in sessions])
        group = result['groups'][0]
        self.assertEqual(group['raw_counts']['sticker_only'], 20)
        self.assertEqual(group['asset_coverage']['occurrences'], 40)
        emotion = group['emotion_groups'][0]
        self.assertEqual(emotion['preferred_styles'], [{'visual_style': 'fixture-cute',
            'reply_count': 20, 'denominator': 20, 'rate': 1}])
        self.assertEqual(emotion['style_evaluable_reply_count'], 20)
        self.assertEqual(emotion['preferred_style_status'], 'estimated')
        self.classifier.result['labels']['emotion'] = 'other'
        other = self.profile.calculate([s.id for s in sessions])
        self.assertEqual(other['groups'][0]['emotion_groups'][0]['emotion'], 'other')
        self.assertEqual(other['status'], 'estimated')
