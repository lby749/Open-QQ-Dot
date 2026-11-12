"""Low latency wakeup correctness: fast in-process, durable cross-process fallback.

These tests intentionally check behavior rather than fragile millisecond thresholds.
The separate tools/benchmark_response_latency.py measures timing distributions.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_messaging import AgentMailbox
from task_manager import TaskManager


class FastMailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'mail.sqlite3'
        self.mail = AgentMailbox(self.path)
        self.a = self.mail.register_agent('alice', 'proj', 'first', 'unit')
        self.b = self.mail.register_agent('alice', 'proj', 'second', 'unit')

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_message_notifies_waiting_threads(self):
        finished = threading.Event()
        output = []
        start_generation = self.mail._change_generation

        def wait():
            output.extend(self.mail.wait_for_inbox(self.b, timeout_seconds=2))
            finished.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        time.sleep(0.04)
        sent = self.mail.send(self.a, self.b, 'hello')
        self.assertTrue(finished.wait(2))
        self.assertEqual([m.message_id for m in output], [sent.message_id])
        self.assertGreater(self.mail._change_generation, start_generation)
        thread.join(1)

    def test_polling_still_detects_other_process_writes(self):
        # A different mailbox object models an MCP process using the same DB;
        # its Condition does not notify this mailbox and fallback must work.
        remote = AgentMailbox(self.path)
        finished = threading.Event()
        output = []

        def wait():
            output.extend(self.mail.wait_for_inbox(self.b, timeout_seconds=2))
            finished.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        time.sleep(0.04)
        sent = remote.send(self.a, self.b, 'cross process')
        self.assertTrue(finished.wait(2))
        self.assertEqual([m.message_id for m in output], [sent.message_id])
        thread.join(1)

    def test_wait_for_reply_is_notified_by_same_process_sender(self):
        req = self.mail.send(self.a, self.b, 'do task')
        output = []
        thread = threading.Thread(target=lambda: output.append(self.mail.wait_for_reply(req.message_id, timeout=2)),
                                  daemon=True)
        thread.start()
        time.sleep(0.04)
        response = self.mail.reply(self.b, req.message_id, 'completed')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(output[0].message_id, response.message_id)


class FastTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = TaskManager(db_path=root / 'tasks.sqlite3', workspace=root)

    def tearDown(self):
        self.manager.stop_worker()
        self.tmp.cleanup()

    def test_mail_wakeup_and_new_tasks_signal_dispatch_event(self):
        self.manager._task_ready_event.clear()
        tid = self.manager.enqueue_agent_mail_task('alice', 'alice/proj/worker', 'worker')
        self.assertTrue(tid)
        self.assertTrue(self.manager._task_ready_event.is_set())
        self.manager._task_ready_event.clear()
        other = self.manager.create_task('other', 'safe no-op task', owner_id='alice')
        self.assertTrue(other)
        self.assertTrue(self.manager._task_ready_event.is_set())

    def test_status_change_to_pending_wakes_worker(self):
        from task_manager import TaskStatus
        tid = self.manager.create_task('waiting', 'waiting', owner_id='alice')
        self.manager.update_task_status(tid, TaskStatus.PAUSED)
        self.manager._task_ready_event.clear()
        self.assertTrue(self.manager.resume_task(tid, owner_id='alice'))
        self.assertTrue(self.manager._task_ready_event.is_set())

    def test_worker_dispatches_mail_task_after_idle_without_lost_wakeup(self):
        from task_manager import TaskStatus
        got = threading.Event()
        def task_runner(task):
            # Minimal execution substitute: test dispatcher, not a remote model.
            with self.manager._db_lock, self.manager._get_connection() as db:
                db.execute("UPDATE tasks SET status='COMPLETED' WHERE task_id=?", (task.task_id,))
            got.set()
        self.manager._process_single_task = task_runner
        from agenda import ProactiveActionEngine
        with patch.object(ProactiveActionEngine, 'get_instance') as agenda:
            agenda.return_value.ensure_builtin_triggers.return_value = None
            agenda.return_value.poll_events_and_act.return_value = None
            self.assertTrue(self.manager.start_worker())
            # Give the worker time to sleep on an empty queue, then enqueue.
            time.sleep(0.05)
            tid = self.manager.enqueue_agent_mail_task('alice', 'alice/proj/worker', 'worker')
            self.assertTrue(got.wait(3))
            self.assertEqual(self.manager.get_task(tid).status, TaskStatus.COMPLETED)


if __name__ == '__main__':
    unittest.main()
