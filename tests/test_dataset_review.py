import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dataset_annotation import offline_feed, review_uncertain as review

LABELS = {'intent': 'respond', 'emotion': 'calm', 'tone': 'casual', 'initiative': 'passive'}


class DatasetReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'annotations.sqlite3'
        self.quota = self.root / 'quota.json'
        self.quota.write_text('{"status":"RUNNING"}', encoding='utf-8')
        with closing(sqlite3.connect(self.path)) as db:
            offline_feed._init_db(db)
            db.execute("INSERT INTO runs(run_id,source_fingerprint,contract_fingerprint,model_fingerprint,model_json,"
                       "sources_json,created_at,status) VALUES('run','source','contract','model','{}','[]','now','complete')")
            for index, uncertain, pipeline in [(1, 0, 0), (2, 1, 0), (3, 0, 1)]:
                db.execute('''INSERT INTO annotations(run_id,annotation_id,data_source,split,source_file,
                    source_record_id,conversation_id,speaker_id,message_seq,text,direction,direction_policy,
                    context_json,following_json,gold_json,original_json,intent,emotion,tone,initiative,
                    needs_review,pipeline_needs_review,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    ('run', f'a{index}', 'CPED', 'test', 'source.csv', f'origin{index}', 'dialogue', 'speaker-b',
                     index, f'msg_{index} 13800138000', 'out', 'synthetic_two_speaker_identity',
                     '[{"text":"前文","speaker":"speaker-a","seq":0,"direction":"in"}]', '[]',
                     '{"secret_gold":"do_not_send"}', '{"original_gold":"do_not_send"}',
                     *LABELS.values(), uncertain, pipeline, 'uncertain'))
            db.commit()
        self.marker = self.path.with_name('COMPLETE.json')
        self.marker.write_text(json.dumps({'run_id': 'run', 'db': str(self.path), 'processed': 3, 'total': 3}), 'utf-8')

    def invoke(self, **kwargs):
        return review.run(self.path, 'run', quota_path=self.quota, quota_checker=lambda: True,
                          progress=lambda _: None, **kwargs)

    def test_complete_gate_uncertain_only_speaker_privacy_and_once_attempt(self):
        self.marker.write_text('{"run_id":"different","processed":3,"total":3}', 'utf-8')
        with self.assertRaisesRegex(ValueError, 'completed run'):
            self.invoke(model_call=lambda _: self.fail('incomplete cloud'))
        self.marker.write_text(json.dumps({'run_id': 'run', 'processed': 3, 'total': 3}), 'utf-8')
        calls = []
        def model(prompt):
            calls.append(prompt)
            self.assertNotIn('secret_gold', prompt)
            self.assertNotIn('original_gold', prompt)
            self.assertNotIn('13800138000', prompt)
            self.assertNotIn('消息1', prompt)
            self.assertIn('speaker-a', prompt)
            return {'results': [{'seq': 1, 'status': 'RESOLVED', 'labels': dict(LABELS, tone='serious'), 'reason': '有把握'},
                                {'seq': 2, 'status': 'NEEDS_HUMAN', 'labels': None, 'reason': 'multiple_interpretations'}]}
        with patch.object(review, '_document', wraps=review._document) as documents:
            result = self.invoke(model_call=model, clock=lambda: 0)
            self.assertEqual(documents.call_count, 2)  # Initial and final, no per-batch rewrites.
        self.assertEqual(result['counts'], {'RESOLVED': 1, 'HUMAN_REVIEW': 1})
        self.invoke(model_call=lambda _: self.fail('second attempt'))
        self.assertEqual(len(calls), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT DISTINCT tone FROM annotations').fetchall(), [('casual',)])
            self.assertEqual(db.execute('SELECT DISTINCT attempt FROM annotation_reviews').fetchall(), [(1,)])
        self.assertIn('origin2', (Path(result['output']) / 'review_results.md').read_text('utf-8'))
        self.assertIn('a3', (Path(result['output']) / 'human_review.md').read_text('utf-8'))
        self.assertTrue((Path(result['output']) / 'history.sqlite3').is_file())
        with closing(sqlite3.connect(self.path)) as db:
            digest = db.execute("SELECT input_hash FROM annotation_reviews WHERE annotation_id='a3'").fetchone()[0]
        review.submit_manual(self.path, 'run', 'a3', digest, LABELS)
        self.invoke(model_call=lambda _: self.fail('manual locked'))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status,manual_locked FROM annotation_reviews WHERE annotation_id='a3'").fetchone(),
                             ('MANUAL', 1))

    def test_quota_failure_stops_and_unknown_failure_is_human_once(self):
        calls = []
        def fail(_):
            calls.append(1)
            raise RuntimeError('Codex 模型账号额度不足')
        result = self.invoke(model_call=fail, batch_size=1)
        self.assertTrue(result['paused'])
        self.assertEqual(result['counts'], {'HUMAN_REVIEW': 1, 'PENDING': 1})
        self.assertEqual(len(calls), 1)
        result = self.invoke(model_call=lambda _: (_ for _ in ()).throw(ValueError('invalid')), batch_size=1)
        self.assertFalse(result['paused'])
        self.assertEqual(result['counts'], {'HUMAN_REVIEW': 2})
        self.invoke(model_call=lambda _: self.fail('failed attempts must not retry'))
        self.quota.write_text('{"status":"PAUSED_QUOTA"}', 'utf-8')
        self.assertTrue(self.invoke(model_call=lambda _: self.fail('paused call'))['paused'])


if __name__ == '__main__':
    unittest.main()
