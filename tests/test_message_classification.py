import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from store import FriendStore
import message_classification as classification


VALID = {'intent': 'share', 'emotion': 'calm', 'tone': 'casual', 'initiative': 'proactive'}


class FakeClassifier:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def classify_batch(self, texts, *, directions, contexts, following_contexts=None):
        following_contexts = following_contexts or [()] * len(texts)
        self.calls.append((texts, directions, contexts, threading.get_ident(), following_contexts))
        if self.callback:
            self.callback(texts)
        return [dict(VALID) for _ in texts]

    def classify_batch_with_review(self, texts, *, directions, contexts, following_contexts=None):
        return [{'labels': labels, 'needs_review': False, 'reason': 'clear'}
                for labels in self.classify_batch(texts, directions=directions, contexts=contexts,
                                                  following_contexts=following_contexts)]

    def close(self):
        pass


class MessageClassificationTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(classification, 'ENABLED', True)  # 2026-10-10: off in the program, tested switched on
        enabled.start()
        self.addCleanup(enabled.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runtime = self.root / 'build' / 'message-classifier-runtime'
        runtime.mkdir(parents=True)
        (runtime / 'runtime.json').write_text('{}', encoding='utf-8')
        self.friend_dir = self.root / 'friends' / 'f1'
        self.store = FriendStore(self.friend_dir)

    def tearDown(self):
        classification.close_queue(self.root, timeout=1)
        self.store.close()
        self.temp.cleanup()

    def test_only_new_inbound_and_outbound_enqueue(self):
        with patch('message_classification.enqueue_for_database') as enqueue:
            self.store.add_local_outbound('items')
            self.assertFalse(self.store.add_message('server-echo', 'me', 'items'))
            self.store.add_message('in-1', 'friend', '收到')
        self.assertEqual([call.args[1] for call in enqueue.call_args_list], [1, 2, 1])

    def test_worker_saves_four_fields_with_same_friend_context(self):
        classifier = FakeClassifier()
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('old', 'friend', '更早')
            self.store.add_message('a', 'friend', '前文甲')
            self.store.add_message('b', 'me', '前文乙')
            self.store.add_message('c', 'friend', '前文丙')
            seq = self.store.db.execute('SELECT max(seq) FROM messages').fetchone()[0] + 1
            self.store.add_message('target', 'friend', '目标')
            self.store.add_message('future', 'me', '未来一')
            self.store.add_message('future2', 'friend', '未来二')
        worker = classification._worker(self.root, create=True)
        worker._classifier = classifier
        worker.enqueue(self.store.path, seq)
        self.assertTrue(worker.flush(2))
        self.assertEqual(classifier.calls[0][:3], (['目标'], ['friend'], [(
            {'text': '前文甲', 'direction': 'friend', 'seq': 2},
            {'text': '前文乙', 'direction': 'me', 'seq': 3},
            {'text': '前文丙', 'direction': 'friend', 'seq': 4},
        )]))
        self.assertEqual(classifier.calls[0][4], [(
            {'text': '未来一', 'direction': 'me', 'seq': 6},
        )])
        self.assertNotEqual(classifier.calls[0][3], threading.get_ident())
        self.assertEqual(self.store.message_metadata(seq), {'classification': VALID})
        self.assertNotIn('classification', self.store.recent_messages()[-1])

    def test_new_message_refreshes_previous_tag_with_one_following_turn(self):
        class FollowingClassifier(FakeClassifier):
            def classify_batch(self, texts, *, directions, contexts, following_contexts=None):
                following_contexts = following_contexts or [()] * len(texts)
                super().classify_batch(texts, directions=directions, contexts=contexts,
                                       following_contexts=following_contexts)
                return [dict(VALID, initiative='passive' if following else 'proactive')
                        for following in following_contexts]

            def classify_batch_with_review(self, texts, *, directions, contexts, following_contexts=None):
                return [{'labels': labels, 'needs_review': False, 'reason': 'clear'}
                        for labels in self.classify_batch(texts, directions=directions, contexts=contexts,
                                                          following_contexts=following_contexts)]

        classifier = FollowingClassifier()
        worker = classification._worker(self.root, create=True)
        worker._classifier = classifier
        with patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root)}):
            self.store.add_message('first', 'friend', '先说')
            self.assertTrue(worker.flush(2))
            self.assertEqual(self.store.message_metadata(1)['classification']['initiative'], 'proactive')
            self.store.add_message('second', 'me', 'respond')
            self.assertTrue(worker.flush(2))
        self.assertEqual(self.store.message_metadata(1)['classification']['initiative'], 'passive')
        self.assertEqual(self.store.message_metadata(2)['classification']['initiative'], 'proactive')
        expected_following = {'text': 'respond', 'direction': 'me', 'seq': 2}
        self.assertTrue(any(expected_following in following
                            for call in classifier.calls for following in call[4]))

    def test_same_batch_current_and_refresh_are_classified_once(self):
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('once', 'friend', 'items')
        old = dict(VALID, tone='cold')
        self.store.set('message_classification:1', json.dumps(old, ensure_ascii=False))
        classifier = FakeClassifier()
        worker = classification._worker(self.root, create=True)
        worker._classifier = classifier
        worker._process([(self.store.path, 1, False), (self.store.path, 1, True)])
        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(classifier.calls[0][0], ['items'])
        self.assertEqual(self.store.message_metadata(1)['classification'], VALID)

    def test_changed_source_is_not_tagged_and_error_does_not_block_insert(self):
        def mutate(_texts):
            with closing(sqlite3.connect(self.store.path)) as db, db:
                db.execute("UPDATE messages SET body='edited' WHERE qq_id='edit-me'")
        classifier = FakeClassifier(mutate)
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('edit-me', 'friend', 'original')
        worker = classification._worker(self.root, create=True)
        worker._classifier = classifier
        worker.enqueue(self.store.path, 1)
        self.assertTrue(worker.flush(2))
        self.assertIsNone(self.store.message_metadata(1)['classification'])
        worker._classifier = type('Broken', (), {
            'classify_batch': lambda *a, **k: (_ for _ in ()).throw(RuntimeError()),
            'classify_batch_with_review': lambda *a, **k: (_ for _ in ()).throw(RuntimeError()),
            'close': lambda self: None,
        })()
        with patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root)}):
            self.assertTrue(self.store.add_message('next', 'friend', 'still stored'))
        self.assertTrue(worker.flush(2))
        self.assertEqual(self.store.message_count(), 2)

    def test_bounded_backfill_skips_existing_and_saves_cursor(self):
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('one', 'friend', '一')
            self.store.add_message('two', 'me', '二')
        self.store.db.execute("INSERT INTO metadata(key,value) VALUES('message_classification:1',?)",
                              (json.dumps(VALID, ensure_ascii=False),))
        self.store.db.commit()
        worker = classification._worker(self.root, create=True)
        worker._classifier = FakeClassifier()
        result = classification.backfill_database(self.store.path, limit=1, project_root=self.root)
        self.assertEqual((result['scanned'], result['queued']), (2, 1))
        self.assertEqual(self.store.get('message_classification_backfill_seq'), '2')
        self.assertTrue(worker.flush(2))
        result = classification.backfill_database(self.store.path, limit=1, project_root=self.root)
        self.assertEqual((result['scanned'], result['queued']), (0, 0))

    def test_backfill_retries_unclassified_after_failure(self):
        with patch('message_classification.enqueue_for_database'):
            self.store.add_message('retry', 'friend', 'Retry')
        worker = classification._worker(self.root, create=True)
        worker._classifier = FakeClassifier(lambda _texts: (_ for _ in ()).throw(RuntimeError()))
        first = classification.backfill_database(self.store.path, limit=1, project_root=self.root)
        self.assertEqual(first['queued'], 1)
        self.assertTrue(worker.flush(2))
        self.assertIsNone(self.store.message_metadata(1)['classification'])
        worker._classifier = FakeClassifier()
        second = classification.backfill_database(self.store.path, limit=1, project_root=self.root)
        self.assertEqual(second['queued'], 1)
        self.assertTrue(worker.flush(2))
        self.assertEqual(self.store.message_metadata(1)['classification'], VALID)

    def test_clear_only_removes_classification_metadata(self):
        self.store.set('message_classification:22', json.dumps(VALID, ensure_ascii=False))
        self.store.set('message_classification_backfill_seq', '22')
        self.store.set('other:key', 'keep')
        self.store.set('messageXclassification:22', 'keep-too')
        self.store.clear_messages()
        self.assertIsNone(self.store.get('message_classification:22'))
        self.assertIsNone(self.store.get('message_classification_backfill_seq'))
        self.assertEqual(self.store.get('other:key'), 'keep')
        self.assertEqual(self.store.get('messageXclassification:22'), 'keep-too')


if __name__ == '__main__':
    unittest.main()
