import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from prompt_context import queue_summary
from store import FriendStore


class GenerationSpeedTests(unittest.TestCase):
    def test_summary_does_not_block_round_or_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            store = FriendStore(directory)
            for i in range(60):
                store.add_message(str(i), 'friend' if i % 2 == 0 else 'me', f'history_{i}')
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()

            def generate(*args, **kwargs):
                entered.set()
                release.wait(5)
                return '后台摘要', None

            model = Mock(side_effect=generate)
            import prompt_context
            original = prompt_context.refresh_summary

            def refresh(*args):
                try:
                    original(*args)
                finally:
                    finished.set()

            try:
                with patch.object(prompt_context, 'refresh_summary', side_effect=refresh):
                    job = queue_summary(store, {}, directory, model)
                    self.assertTrue(entered.wait(2))
                    queue_summary(store, {}, directory, model)
                    with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', return_value=(json.dumps({'messages': ['新的话题']}), None)):
                        self.assertEqual(main.make_round(store, {}, directory, 1), ['新的话题'])
                    self.assertFalse(finished.is_set())
                    self.assertEqual(model.call_count, 1)
                    release.set()
                    self.assertTrue(finished.wait(3))
                    self.assertEqual(store.get('context_summary'), '后台摘要')
                    self.assertEqual(store.message_count(), 60)
            finally:
                release.set()
                job.join(5)
                self.assertFalse(job.is_alive())
                store.close()

    def test_compaction_cache_reads_appended_records_and_partial_lines(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'CODEX_HOME': folder}):
            sessions = Path(folder) / 'sessions'
            sessions.mkdir()
            rollout = sessions / 'rollout-speed-thread.jsonl'
            rollout.write_bytes(b'{"type":"compacted"}\n')
            self.assertEqual(main.completed_compactions('speed-thread'), 1)
            with patch.object(Path, 'rglob', side_effect=AssertionError('Repeated directory scan')):
                self.assertEqual(main.completed_compactions('speed-thread'), 1)
                with rollout.open('ab') as stream:
                    stream.write(b'{"type":"compacted"}')
                self.assertEqual(main.completed_compactions('speed-thread'), 1)
                with rollout.open('ab') as stream:
                    stream.write(b'\n')
                self.assertEqual(main.completed_compactions('speed-thread'), 2)
                rollout.write_bytes(b'{"type":"event_msg"}\n')
                self.assertEqual(main.completed_compactions('speed-thread'), 0)
