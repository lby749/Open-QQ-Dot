import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from dataset_annotation import dataset_loaders, offline_feed


class FakeClassifier:
    def __init__(self, seen):
        self.seen = seen
        self.closed = False

    def classify_batch_with_review(self, texts, *, directions, contexts, following_contexts):
        self.seen.extend(zip(texts, directions, contexts, following_contexts))
        return [{"labels": {"intent": "respond", "emotion": "calm",
                             "tone": "casual", "initiative": "passive"},
                 "needs_review": text == "b", "reason": "uncertain" if text == "b" else "clear"}
                for text in texts]

    def close(self):
        self.closed = True


class TestOfflineFeed(unittest.TestCase):
    def test_feed_preserves_boundary_context_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / "source.csv"
            source.write_text("fixture", encoding="utf-8")
            spec = dataset_loaders.DatasetSpec("cped", tmp_path, "test", (source,), "fixture")
            records = [
                dataset_loaders.Utterance("a1", "CPED", "test", "c1", 0, "speaker-a", "a",
                                          gold={"emotion": "anger"}, source_file=str(source)),
                dataset_loaders.Utterance("a2", "CPED", "test", "c1", 1, "speaker-b", "b",
                                          gold={"emotion": "sadness"}, source_file=str(source)),
                dataset_loaders.Utterance("a3", "CPED", "test", "c1", 2, "speaker-a", "c",
                                          gold={"emotion": "joy"}, source_file=str(source)),
                dataset_loaders.Utterance("b1", "CPED", "test", "c2", 0, "solo", "d",
                                          gold={"emotion": "neutral"}, source_file=str(source)),
                dataset_loaders.Utterance("c1", "CPED", "test", "c3", 0, "speaker-a", "e",
                                          gold={"emotion": "neutral"}, source_file=str(source)),
                dataset_loaders.Utterance("c2", "CPED", "test", "c3", 1, "speaker-b", "f",
                                          gold={"emotion": "neutral"}, source_file=str(source)),
                dataset_loaders.Utterance("c3", "CPED", "test", "c3", 2, "speaker-c", "g",
                                          gold={"emotion": "neutral"}, source_file=str(source)),
            ]
            seen, instances = [], []
            def factory():
                item = FakeClassifier(seen)
                instances.append(item)
                return item

            db = tmp_path / "annotations.sqlite3"
            with patch.object(dataset_loaders, "discover", return_value={"cped-test": spec}), \
                 patch.object(dataset_loaders, "load", return_value=records), \
                 patch.object(offline_feed, "_model_identity", return_value=("model-fp", {"model": "fixture"})):
                result = offline_feed.run(tmp_path, db, batch_size=2,
                                          classifier_factory=factory, progress=lambda _: None)
                self.assertEqual((result["processed"], result["total"]), (7, 7))
                self.assertEqual(len(seen), 7)
                # Context keeps source speakers, actual sequence, and same-dialogue following.
                self.assertEqual(seen[1][2][0]["speaker"], "speaker-a")
                self.assertEqual(seen[1][3][0]["seq"], 2)
                self.assertTrue(instances[0].closed)

                second = offline_feed.run(tmp_path, db, batch_size=2,
                                          classifier_factory=factory, progress=lambda _: None)
                queue = offline_feed.export_review_queue(db, run_id=result["run_id"])
            self.assertEqual(second["processed"], 7)
            self.assertEqual(len(seen), 7)
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute("SELECT speaker_id,message_seq,gold_json,needs_review FROM annotations "
                                   "WHERE annotation_id='a2'").fetchone()
            self.assertEqual(row[:2], ("speaker-b", 1))
            self.assertEqual(json.loads(row[2]), {"emotion": "sadness"})
            self.assertEqual(row[3], 1)
            queued = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row["pipeline_needs_review"] for row in queued))
            self.assertTrue(all(row["run_id"] == result["run_id"] for row in queued))


if __name__ == "__main__":
    unittest.main()
