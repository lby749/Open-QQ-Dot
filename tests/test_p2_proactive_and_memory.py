"""Unit tests for P2: Event-driven proactivity, scoped long-term memory, and research tracker."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agenda import EventType, ProactiveActionEngine, ProactiveLevel
from agent_memory import AgentMemoryStore, MemoryScope, MemorySource
from research_tracker import ResearchTracker
from task_manager import TaskManager, TaskStatus


class ProactiveAndMemoryP2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.mem_db = self.temp_dir / "test_memory.sqlite3"
        self.res_db = self.temp_dir / "test_research.sqlite3"
        self.task_db = self.temp_dir / "test_tasks.sqlite3"

        self.memory_store = AgentMemoryStore(db_path=self.mem_db)
        self.research_tracker = ResearchTracker(db_path=self.res_db)
        self.task_manager = TaskManager(db_path=self.task_db, workspace=self.temp_dir)

    def tearDown(self):
        self.task_manager.stop_worker()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_proactive_action_engine_and_file_watcher(self):
        engine = ProactiveActionEngine()
        watched_file = self.temp_dir / "trigger.txt"
        watched_file.write_text("v1", encoding="utf-8")

        engine.register_file_watcher(
            target_path=watched_file,
            on_change_task_title="代码变动重新构建",
            on_change_description="重新编译并运行测试",
        )

        # 1. First poll without modification -> no tasks dispatched
        dispatched = engine.poll_events_and_act(workspace=self.temp_dir, task_manager=self.task_manager)
        self.assertEqual(len(dispatched), 0)

        # 2. Modify watched file
        time.sleep(0.05)
        watched_file.write_text("v2", encoding="utf-8")

        notifications = []
        def notify_sink(title, body):
            notifications.append((title, body))

        dispatched = engine.poll_events_and_act(
            proactive_level=ProactiveLevel.AUTONOMOUS,
            workspace=self.temp_dir,
            notification_sink=notify_sink,
            task_manager=self.task_manager,
        )
        self.assertEqual(len(dispatched), 1)

        task = self.task_manager.get_task(dispatched[0])
        self.assertIsNotNone(task)
        self.assertIn("代码变动重新构建", task.title)
        self.assertEqual(task.status, TaskStatus.PENDING)

        # 3. Cooldown check: second poll immediately should be blocked by cooldown
        dispatched_again = engine.poll_events_and_act(workspace=self.temp_dir, task_manager=self.task_manager)
        self.assertEqual(len(dispatched_again), 0)

    def test_scoped_memory_and_experience_reuse(self):
        # 1. Store private friend memories for Alice and Bob
        self.memory_store.store_memory(
            scope=MemoryScope.FRIEND_PRIVATE,
            owner_id="alice_qq",
            key="favorite_coffee",
            content="喜欢燕麦拿铁半糖",
            confidence=0.95,
        )
        self.memory_store.store_memory(
            scope=MemoryScope.FRIEND_PRIVATE,
            owner_id="bob_qq",
            key="favorite_coffee",
            content="只喝美式不加糖",
            confidence=0.9,
        )

        # Ensure strict isolation: Alice's memory must not leak into Bob's scope
        alice_mems = self.memory_store.get_scoped_memories(MemoryScope.FRIEND_PRIVATE, owner_id="alice_qq")
        self.assertEqual(len(alice_mems), 1)
        self.assertEqual(alice_mems[0].content, "喜欢燕麦拿铁半糖")

        bob_mems = self.memory_store.get_scoped_memories(MemoryScope.FRIEND_PRIVATE, owner_id="bob_qq")
        self.assertEqual(len(bob_mems), 1)
        self.assertEqual(bob_mems[0].content, "只喝美式不加糖")

        # 2. Agent Experience Reuse: Record a failure and verified remedy
        self.memory_store.record_failure_experience(
            task_category="code",
            failed_action="pip install -r requirements.txt",
            root_cause="缺少 Microsoft C++ Build Tools",
            solution="直接安装预编译的 whl 包或使用 conda 环境",
        )

        remedies = self.memory_store.retrieve_experience_remedies("requirements.txt")
        self.assertTrue(len(remedies) >= 1)
        self.assertIn("Microsoft C++ Build Tools", remedies[0])
        self.assertIn("直接安装预编译的 whl 包", remedies[0])

    def test_research_tracker_phased_advancement(self):
        # Initialize a research project
        proj = self.research_tracker.create_research_project(
            project_id="res_math_001",
            topic="优化素数筛法算法复杂度",
            goal="在现有线性筛基础上通过分块并行降低内存占用",
            task_id="task_12345",
            initial_milestones=[
                {"title": "文献调研与基准测试", "objective": "复现常规埃氏筛与欧拉筛"},
                {"title": "分块与位图设计", "objective": "设计 Cache-friendly 分块数据结构"},
            ],
            max_stages=2,
            budget_tokens=50000,
        )

        # Stage 1: Record intermediate findings and negative attempt
        should_notify, summary = self.research_tracker.record_stage_progress(
            project_id="res_math_001",
            stage_index=1,
            new_findings=["欧拉线性筛在 N=1e9 时内存瓶颈为 1.2GB", "标准位图可压缩至 125MB"],
            negative_attempts=["使用多线程直接竞争写共享数组导致锁开销超过提速"],
            tokens_consumed=8000,
            stage_achieved=True,
            verification_status="verified", sources=["synthetic benchmark"],
            evidence=["synthetic measurement log"], reviewed_by="test-reviewer",
        )

        self.assertTrue(should_notify)
        self.assertTrue("阶段目标完成" in summary or "stage milestone achieved" in summary)
        self.assertIn("欧拉线性筛在 N=1e9", summary)

        # Check prompt context injection: must contain verified findings and negative dead-ends
        prompt_ctx = self.research_tracker.build_research_context_for_prompt("res_math_001")
        self.assertTrue("已记录结论" in prompt_ctx or "Recorded Findings" in prompt_ctx)
        self.assertIn("标准位图可压缩至 125MB", prompt_ctx)
        self.assertIn("使用多线程直接竞争写共享数组导致锁开销超过提速", prompt_ctx)
        self.assertIn("使用多线程直接竞争写共享数组导致锁开销超过提速", prompt_ctx)


if __name__ == "__main__":
    unittest.main()
