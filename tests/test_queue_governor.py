"""Tests for QueueGovernor zero-backlog and consolidation logic."""
import json
import tempfile
import time
import unittest
from pathlib import Path

import main
from queue_governor import QueueGovernor
from store import FriendStore


class QueueGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.friend_dir = self.root / "friends" / "12345"
        self.friend_dir.mkdir(parents=True)
        self.store = FriendStore(self.friend_dir)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_fast_command_detection(self):
        gov = QueueGovernor.get_instance()
        self.assertTrue(gov.is_fast_command("批准 tk_1234"))
        self.assertTrue(gov.is_fast_command("拒绝 tk_5678"))
        self.assertTrue(gov.is_fast_command("待审批"))
        self.assertTrue(gov.is_fast_command("/status"))
        self.assertTrue(gov.is_fast_command("状态"))
        self.assertTrue(gov.is_fast_command("/stop"))
        self.assertFalse(gov.is_fast_command("帮我写一个快速排序算法"))
        self.assertFalse(gov.is_fast_command("今天天气怎么样"))

    def test_single_message_inbound(self):
        gov = QueueGovernor.get_instance()
        self.store.record_inbound("m1", "你好呀")
        res = gov.consolidate_friend_inbound(self.store)
        self.assertIsNotNone(res)
        m_ids, body, img = res
        self.assertEqual(m_ids, ["m1"])
        self.assertEqual(body, "你好呀")

    def test_multi_message_consolidation_collapses_backlog(self):
        gov = QueueGovernor.get_instance()
        # Simulate user sending 3 rapid messages while bot was busy
        self.store.record_inbound("m1", "在吗")
        self.store.record_inbound("m2", "帮我查一下昨天那个报错")
        self.store.record_inbound("m3", "挺急的")

        res = gov.consolidate_friend_inbound(self.store)
        self.assertIsNotNone(res)
        m_ids, body, img = res
        self.assertEqual(m_ids, ["m1", "m2", "m3"])
        self.assertIn("用户连续发送了 3 条消息", body)
        self.assertIn("1. 在吗", body)
        self.assertIn("2. 帮我查一下昨天那个报错", body)
        self.assertIn("3. 挺急的", body)

    def test_queue_drainage_and_flush(self):
        gov = QueueGovernor.get_instance()
        self.store.record_inbound("m_stale", "旧消息")
        self.store.set_reply_attempt("m_stale", "processing", "卡住的处理")
        self.store.set("reply_waiting", "1")

        # Flush all queues
        registry = {"12345": {"directory": str(self.friend_dir)}}
        res = gov.flush_all(registry)
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["drained_count"], 1)

        # Verify state reset
        self.assertEqual(self.store.get("reply_waiting", "0"), "0")
        attempt = self.store.reply_attempt("m_stale")
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(self.store.db.execute("SELECT handled FROM messages WHERE qq_id='m_stale'").fetchone()[0], 0)

    def test_group_async_dispatch_fifo_without_cross_group_serialization(self):
        gov = QueueGovernor(max_group_workers=3)
        order = []
        def work(item):
            import time
            if item == 1:
                time.sleep(.04)
            order.append(item)
        futures = [gov.dispatch_group_event(lambda i=i: work(i), group_id='group-1')
                   for i in range(1, 7)]
        other = gov.dispatch_group_event(lambda: 'other-group', group_id='group-2')
        self.assertEqual(other.result(timeout=5), 'other-group')
        for fut in futures:
            fut.result(timeout=5)
        self.assertEqual(order, list(range(1, 7)))
        gov._group_pool.shutdown(wait=True)

    def test_queue_health_inspection(self):
        gov = QueueGovernor.get_instance()
        self.store.record_inbound("m10", "测试消息")
        registry = {"12345": {"directory": str(self.friend_dir), "display_name": "TestFriend"}}
        health = gov.inspect_queue_health(registry)
        self.assertIn("status", health)
        self.assertEqual(health["total_unhandled"], 1)
        self.assertEqual(len(health["friends"]), 1)
        self.assertEqual(health["friends"][0]["qq_id"], "12345")
        self.assertEqual(health["friends"][0]["unhandled_count"], 1)
