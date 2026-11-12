import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from store import FriendStore
import classification_review as review
import message_classification as classification


LABELS = {'intent': 'respond', 'emotion': 'calm', 'tone': 'casual', 'initiative': 'passive'}


class ClassificationReviewTests(unittest.TestCase):
    def setUp(self):
        self.enqueue_patch = patch('message_classification.enqueue_for_database')
        self.enqueue_patch.start()
        self.addCleanup(self.enqueue_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.friend = self.root / 'friends' / 'f1'
        self.store = FriendStore(self.friend)
        self.store.add_message('one', 'friend', '嗯，联系 13800138000 或 a@b.com')
        context = ()
        following = None
        self.snapshot = review._snapshot(1, '嗯，联系 13800138000 或 a@b.com', 'friend', context, following)
        self.digest = review.input_hash(self.snapshot['body'], 'friend', context, following)
        self.event = {'seq': 1, 'input_hash': self.digest, 'snapshot': self.snapshot,
                      'labels': dict(LABELS), 'reason': 'very_short_message'}
        self.store.set('message_classification:1', json.dumps(LABELS, ensure_ascii=False))
        self.store.set('classification_review:1', json.dumps({'input_hash': self.digest, 'snapshot': self.snapshot,
            'qwen_labels': LABELS, 'qwen_reason': 'very_short_message', 'qwen_self_assessment': 'uncalibrated',
            'status': 'PENDING', 'attempt': 0}, ensure_ascii=False))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_resolved_redacts_and_writes_four_labels_and_friend_docs(self):
        def model(prompt):
            self.assertNotIn('13800138000', prompt)
            self.assertNotIn('a@b.com', prompt)
            self.assertIn('REVIEWING', (self.friend / 'message_classification_review.md').read_text(encoding='utf-8'))
            self.assertEqual(json.loads(self.store.get('classification_review:1'))['attempt'], 1)
            self.assertEqual(review.review_batch(self.store.path, [self.event],
                             lambda _: self.fail('duplicate model call')), [])
            return {'results': [{'seq': 1, 'status': 'RESOLVED', 'labels': LABELS, 'reason': '结合上下文判为回应'}]}
        result = review.review_batch(self.store.path, [self.event], model)
        self.assertEqual(result[0]['status'], 'RESOLVED')
        self.assertEqual(self.store.message_metadata(1)['classification'], LABELS)
        self.assertIn('RESOLVED', (self.friend / 'message_classification_review.md').read_text(encoding='utf-8'))
        worker = classification.MessageClassificationQueue.__new__(classification.MessageClassificationQueue)
        worker.project_root = self.root
        worker._classifier = type('Clear', (), {'classify_batch_with_review': lambda *a, **kw: [
            {'labels': dict(LABELS, tone='cold'), 'needs_review': False, 'reason': 'clear'}]})()
        worker._process([(self.store.path, 1, True)])
        self.assertEqual(self.store.message_metadata(1)['classification'], LABELS)

    def test_uncertain_is_human_and_failed_attempt_does_not_retry(self):
        result = review.review_batch(self.store.path, [self.event], lambda _: {'results': [
            {'seq': 1, 'status': 'NEEDS_HUMAN', 'labels': None, 'reason': 'multiple_interpretations'}]})
        self.assertEqual(result[0]['status'], 'NEEDS_HUMAN')
        self.assertEqual(len(review.list_manual(self.store.path)), 1)
        calls = []
        review.review_batch(self.store.path, [self.event], lambda _: calls.append('duplicate'))
        saved = json.loads(self.store.get('classification_review:1'))
        self.assertEqual(saved['attempt'], 1)
        self.assertEqual(calls, [])

    def test_changed_source_rejects_late_review_and_manual_override_locks(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE messages SET body='更新后的消息' WHERE seq=1")
        self.assertEqual(review.review_batch(self.store.path, [self.event], lambda _: self.fail('stale call')),
                         [])
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE messages SET body=? WHERE seq=1", (self.snapshot['body'],))
        self.assertEqual(review.submit_manual(self.store.path, 1, LABELS, self.digest), None)
        self.assertTrue(json.loads(self.store.get('classification_review:1'))['manual_locked'])
        self.assertEqual(review.list_manual(self.store.path), [])
        worker = classification.MessageClassificationQueue.__new__(classification.MessageClassificationQueue)
        worker.project_root = self.root
        worker._classifier = None
        with patch('message_classification._get_classifier', side_effect=AssertionError('manual must stay local locked')):
            worker._process([(self.store.path, 1, True)])
        self.assertEqual(self.store.message_metadata(1)['classification'], LABELS)

    def test_changed_previous_context_rejects_cloud_result_and_manual_submission(self):
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('target', 'friend', 'yeah')
        context = [{'seq': 1, 'text': self.snapshot['body'], 'direction': 'friend'}]
        snap = review._snapshot(2, 'yeah', 'friend', context, None)
        digest = review.input_hash('yeah', 'friend', context, None)
        event = {'seq': 2, 'input_hash': digest, 'snapshot': snap, 'labels': LABELS, 'reason': 'very_short_message'}
        self.store.set('classification_review:2', json.dumps({'input_hash': digest, 'snapshot': snap,
            'qwen_labels': LABELS, 'qwen_reason': 'very_short_message', 'status': 'PENDING', 'attempt': 0}, ensure_ascii=False))
        def model(_):
            with closing(sqlite3.connect(self.store.path)) as db, db:
                db.execute("UPDATE messages SET body='前文被修改' WHERE seq=1")
            return {'results': [{'seq': 2, 'status': 'RESOLVED', 'labels': LABELS, 'reason': 'respond'}]}
        self.assertEqual(review.review_batch(self.store.path, [event], model), [])
        self.assertIsNone(self.store.message_metadata(2)['classification'])
        self.assertEqual(json.loads(self.store.get('classification_review:2'))['status'], 'HUMAN_REVIEW')
        with self.assertRaisesRegex(ValueError, 'context changed'):
            review.submit_manual(self.store.path, 2, LABELS, digest)
        self.assertEqual(review.review_batch(self.store.path, [event], lambda _: self.fail('stale retry')), [])

    def test_clear_history_removes_review_text_and_locks_but_keeps_other_metadata(self):
        review.submit_manual(self.store.path, 1, LABELS, self.digest)
        self.store.set('other:key', 'keep')
        self.store.set('classificationXreview:1', 'keep-too')
        friend_doc = self.friend / 'message_classification_review.md'
        queue_doc = self.root / 'build' / 'message-classifier-runtime' / 'review_queue.md'
        self.assertIn(self.snapshot['body'], friend_doc.read_text(encoding='utf-8'))
        self.assertIn(self.snapshot['body'], queue_doc.read_text(encoding='utf-8'))
        self.assertEqual(self.store.clear_messages(), 1)
        self.assertIsNone(self.store.get('classification_review:1'))
        self.assertIsNone(self.store.get('message_classification:1'))
        self.assertEqual(self.store.get('other:key'), 'keep')
        self.assertEqual(self.store.get('classificationXreview:1'), 'keep-too')
        self.assertTrue(friend_doc.is_file())
        self.assertTrue(queue_doc.is_file())
        for document in (friend_doc, queue_doc):
            self.assertNotIn(self.snapshot['body'], document.read_text(encoding='utf-8'))
            self.assertNotIn('消息 1', document.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
