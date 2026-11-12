import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dialogue_pairs import DialoguePair, DialoguePairBuilder
from dialogue_turns import Session, build_turns
from persona_judge import PERSONA_FIELDS, JudgeRunStore, PersonaJudge
from persona_store import PersonaStore
from persona_types import (DataScope, FriendScope, NormalizedMessage, PrivacyView,
                           PrivacyViolation, SourceOrigin, SourceRef)


class PersonaJudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friend = self.root / 'friends' / '12345'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(friend)}}}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        self.addCleanup(self.store.close)
        self.runs = JudgeRunStore(self.scope, self.store)
        self.calls = []

    def pair(self, *, private=False):
        scope = DataScope.PRIVATE_FRIEND if private else DataScope.PUBLIC_DATASET
        def msg(identity, speaker, text, role):
            source = SourceRef(dataset_id='dataset-a', import_id='import-a', record_id=identity,
                message_seq=1 if private else None, speaker_id=speaker, origin=SourceOrigin.PUBLIC_DATASET,
                data_scope=scope, privacy_view=PrivacyView.LOCAL_FULL if private else PrivacyView.CLOUD_SAFE,
                friend_id='12345' if private else None, event_time=None,
                recorded_at=datetime.now(timezone.utc).isoformat(), license_id=None if private else 'cc-by',
                model_analysis_allowed=not private)
            return NormalizedMessage(id=identity, timestamp=None, speaker_id=speaker,
                speaker_role='human', text=text, message_type='text', media_id=None, source=source,
                target_role=role, target_selected=(role == 'target'))
        user = msg('input', 'user', '最近怎么样？', 'input')
        target = msg('target', 'target', '还不错呀', 'target')
        return DialoguePair(id='pair-a', friend_id='12345', session_id='session-a',
            input_turn_ids=('input-turn',), context_turn_ids=(), target_turn_id='target-turn',
            target_speaker_id='target', user_bubbles=(user,), target_bubbles=(target,),
            context_turns=(), source_refs=(user.source, target.source), time_gap=None,
            time_quality='UNKNOWN')

    def response(self, usage=None):
        return {'content': json.dumps({'items': [{'pair_id': 'pair-a',
            'scores': {name: 0.8 for name in PERSONA_FIELDS}, 'quality_score': 0.9,
            'needs_review': False, 'abstain_reason': None}]}),
            'model': 'mock-model', 'usage': usage}

    def built_pair(self):
        messages = []
        for identity, speaker, text in (('real-user', 'user', '最近怎么样？'),
                                        ('real-target', 'target', '还不错呀')):
            source = SourceRef(dataset_id='dataset-a', import_id='import-a', record_id=identity,
                message_seq=None, speaker_id=speaker, origin=SourceOrigin.PUBLIC_DATASET,
                data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.CLOUD_SAFE,
                friend_id=None, event_time=None, recorded_at=datetime.now(timezone.utc).isoformat(),
                license_id='cc-by', model_analysis_allowed=True)
            messages.append(NormalizedMessage(id=identity, timestamp=None, speaker_id=speaker,
                speaker_role='human', text=text, message_type='text', media_id=None,
                source=source, target_role='unmapped', target_selected=False))
        session = Session(id='real-session', friend_id='12345', boundary_key='{}',
            speaker_set=('target', 'user'), start_time=None, end_time=None,
            gap_seconds=1800.0, time_quality='UNKNOWN', messages=tuple(messages))
        return tuple(DialoguePairBuilder(self.scope).build_pairs(build_turns(session), {'target'}))[0]

    def judge(self, call):
        return PersonaJudge(self.scope, self.runs, call, model='mock-model').judge(
            [self.pair()], run_id='run-a', source_revision=0, target_spec={'traits': ['warm']})

    def test_public_batch_records_complete_scores_and_known_usage(self):
        result = self.judge(lambda prompt: self.response(
            {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}))
        row = self.store.db.execute('SELECT * FROM dataset_curation_runs WHERE id=?',
                                    ('run-a',)).fetchone()
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(set(result['results'][0]['scores']), set(PERSONA_FIELDS))
        self.assertEqual(json.loads(row['usage_json'])['total_tokens'], 30)
        self.assertEqual(json.loads(row['input_json'])['sources'][0]['data_scope'], 'PUBLIC_DATASET')
        self.assertNotIn('还不错呀', row['input_json'])
        self.assertEqual(row['prompt_version'], result['prompt_version'])

    def test_private_source_is_rejected_before_call_and_run_record(self):
        judge = PersonaJudge(self.scope, self.runs,
            lambda prompt: self.calls.append(prompt), model='mock-model')
        with self.assertRaises(PrivacyViolation):
            judge.judge([self.pair(private=True)], run_id='private', source_revision=0,
                        target_spec={'traits': ['warm']})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM dataset_curation_runs').fetchone()[0], 0)

    def test_unknown_usage_marks_terminal_and_same_run_never_calls_again(self):
        calls = []
        result = self.judge(lambda prompt: (calls.append(prompt) or self.response(None)))
        self.assertEqual(result['status'], 'USAGE_UNKNOWN')
        with self.assertRaises(ValueError):
            self.judge(lambda prompt: (calls.append(prompt) or self.response(
                {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.db.execute(
            'SELECT status FROM dataset_curation_runs WHERE id=?', ('run-a',)).fetchone()[0],
            'USAGE_UNKNOWN')

    def test_real_builder_pair_bad_content_keeps_usage_and_stale_revision_is_failed(self):
        pair = self.built_pair()
        self.assertFalse(pair.target_bubbles[0].target_selected)
        self.assertEqual(pair.target_bubbles[0].target_role, 'unmapped')
        calls = []
        def call(prompt):
            calls.append(prompt)
            self.store.db.execute('UPDATE scope_state SET revision=1 WHERE friend_id=?',
                                  (self.scope.friend_id,))
            return {'content': 'not json', 'model': 'mock-model',
                    'usage': {'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15}}
        judge = PersonaJudge(self.scope, self.runs, call, model='mock-model')
        result = judge.judge([pair], run_id='stale-run', source_revision=0,
                             target_spec={'traits': ['warm']})
        row = self.store.db.execute('SELECT * FROM dataset_curation_runs WHERE id=?',
                                    ('stale-run',)).fetchone()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(result['error_code'], 'source_revision_changed')
        self.assertEqual(json.loads(row['usage_json'])['total_tokens'], 15)
        self.assertEqual(row['status'], 'FAILED')
        with self.assertRaises(ValueError):
            judge.judge([pair], run_id='stale-run', source_revision=0,
                        target_spec={'traits': ['warm']})
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
