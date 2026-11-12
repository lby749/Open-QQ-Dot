"""Unit tests for P1 - Persistent Autonomous Task Engine and Verifier."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_errors import AgentError, AgentErrorType
from model_dispatcher import ModelCapability, ModelDispatcher, TaskCategory
from task_manager import TaskItem, TaskManager, TaskStatus
from task_verifier import TaskVerifier, VerificationResult


class TaskManagerP1Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "tasks_test.sqlite3"
        self.manager = TaskManager(db_path=self.db_path, workspace=self.temp_dir)

    def tearDown(self):
        self.manager.stop_worker()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_task_creation_and_status_transitions(self):
        tid = self.manager.create_task(
            title="数据分析任务",
            description="清洗并分析用户日志",
            category="data",
            priority=80,
        )
        task = self.manager.get_task(tid)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.priority, 80)

        # Status transition to RUNNING
        self.manager.update_task_status(tid, TaskStatus.RUNNING)
        task = self.manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.RUNNING)

        # Save checkpoint
        self.manager.save_checkpoint(tid, {"processed_batches": [1, 2, 3]})
        task = self.manager.get_task(tid)
        self.assertEqual(task.checkpoint.get("processed_batches"), [1, 2, 3])

        # Complete task
        self.manager.update_task_status(tid, TaskStatus.COMPLETED, result="分析完毕")
        task = self.manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.result, "分析完毕")

        # Check logs
        logs = self.manager.get_task_logs(tid)
        self.assertTrue(len(logs) >= 2)

    def test_dependency_scheduling(self):
        # Task 1: Generate schema
        t1 = self.manager.create_task(title="T1 model", description="生成数据模型", category="general")
        # Task 2 depends on Task 1
        t2 = self.manager.create_task(title="T2 数据填充", description="填充数据", category="general", depends_on=[t1])

        # Initially, only t1 should be executable
        next_task = self.manager._fetch_next_executable_task()
        self.assertIsNotNone(next_task)
        self.assertEqual(next_task.task_id, t1)

        # Complete t1
        self.manager.update_task_status(t1, TaskStatus.COMPLETED)

        # Now t2 should be executable
        next_task = self.manager._fetch_next_executable_task()
        self.assertIsNotNone(next_task)
        self.assertEqual(next_task.task_id, t2)

    def test_task_verifier_code_and_subjective(self):
        # 1. Code verification with valid python file
        code_file = self.temp_dir / "valid.py"
        code_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        res_code = TaskVerifier.verify(
            category="code",
            task_info={"target_files": ["valid.py"]},
            result={"modified_files": ["valid.py"]},
            workspace=self.temp_dir,
        )
        self.assertTrue(res_code.success)
        self.assertFalse(res_code.needs_approval)

        # 2. Code verification with syntax error
        bad_file = self.temp_dir / "bad.py"
        bad_file.write_text("def broken(): return (\n", encoding="utf-8")
        res_bad = TaskVerifier.verify(
            category="code",
            task_info={"target_files": ["bad.py"]},
            result={},
            workspace=self.temp_dir,
        )
        self.assertFalse(res_bad.success)
        self.assertIn("Python 代码编译语法错误", res_bad.details)

        # 3. Subjective task: MUST require human approval and cannot falsely claim success
        res_subj = TaskVerifier.verify(
            category="subjective",
            task_info={},
            result="方案设计初稿",
            workspace=self.temp_dir,
        )
        self.assertTrue(res_subj.needs_approval)
        self.assertEqual(res_subj.verifier_type, "human_approval_required")

    def test_quota_exhaustion_saves_checkpoint_and_enters_waiting_resource(self):
        tid = self.manager.create_task(
            title="highmodel",
            description="重构超长代码",
            category="code",
        )

        def mock_executor(task: TaskItem, log_fn):
            raise AgentError(
                error_type=AgentErrorType.QUOTA_EXHAUSTED,
                provider="claude",
                message="Anthropic credit quota exhausted"
            )

        self.manager.register_custom_executor("code", mock_executor)

        task = self.manager.get_task(tid)
        self.manager._process_single_task(task)

        task = self.manager.get_task(tid)
        self.assertEqual(task.status, TaskStatus.WAITING_RESOURCE)
        self.assertIn("QUOTA_EXHAUSTED", task.error)

    def test_model_dispatcher_selection_and_cooldown(self):
        dispatcher = ModelDispatcher()

        # Classification task should pick cost-effective model (custom / dsh), not claude
        selected = dispatcher.select_best_model(
            task_category=TaskCategory.CLASSIFICATION,
            available_providers=["claude", "custom", "antigravity"],
        )
        self.assertEqual(selected, "custom")

        # Code execution should pick capable code engine
        selected_code = dispatcher.select_best_model(
            task_category=TaskCategory.CODE,
            available_providers=["claude", "custom"],
            prefer_cost_effective=False,
        )
        self.assertEqual(selected_code, "claude")

        # Mark cooldown on claude
        dispatcher.mark_cooldown("claude", duration_sec=10.0)
        self.assertFalse(dispatcher.is_available("claude"))

        # In cooldown, fallback to alternative
        selected_fallback = dispatcher.select_best_model(
            task_category=TaskCategory.CODE,
            available_providers=["claude", "antigravity"],
            prefer_cost_effective=False,
        )
        self.assertEqual(selected_fallback, "antigravity")


if __name__ == "__main__":
    unittest.main()
