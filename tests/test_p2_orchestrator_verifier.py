import unittest
import tempfile
from pathlib import Path

from multi_agent_orchestrator import (
    detect_and_break_dependency_cycles,
    is_subtask_blocked,
)
from task_verifier import TaskVerifier, VerificationResult


class TestP2OrchestratorAndVerifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_cycle_dependency_detection_and_breaking(self):
        """Cyclic dependencies between tasks must be detected and resolved without infinite loop."""
        # Task A depends on Task B, Task B depends on Task A
        subtasks = [
            {
                "id": "task_a",
                "title": "Task A",
                "depends_on": ["task_b"],
            },
            {
                "id": "task_b",
                "title": "Task B",
                "depends_on": ["task_a"],
            },
        ]

        cleaned, cycle_found = detect_and_break_dependency_cycles(subtasks)
        self.assertTrue(cycle_found, "循环依赖必须被检测到并报告")
        # Ensure that at least one circular edge was broken so DAG can be scheduled
        deps_a = next(t["depends_on"] for t in cleaned if t["id"] == "task_a")
        deps_b = next(t["depends_on"] for t in cleaned if t["id"] == "task_b")
        self.assertFalse("task_b" in deps_a and "task_a" in deps_b, "循环边已被安全切断")

    def test_cascade_blocked_status_when_dependency_fails(self):
        """Downstream subtasks must be marked as BLOCKED when any upstream dependency fails."""
        child_subtask = {
            "id": "task_child",
            "title": "Migrate database",
            "depends_on": ["task_parent"],
        }

        # Case 1: Parent succeeded
        completed_success = {
            "task_parent": {"success": True, "status": "COMPLETED", "output": "Done"}
        }
        blocked, failing_dep = is_subtask_blocked(child_subtask, completed_success)
        self.assertFalse(blocked)
        self.assertIsNone(failing_dep)

        # Case 2: Parent failed
        completed_failed = {
            "task_parent": {"success": False, "status": "FAILED", "output": "SyntaxError"}
        }
        blocked, failing_dep = is_subtask_blocked(child_subtask, completed_failed)
        self.assertTrue(blocked, "父任务失败时，下游子任务必须被判定为阻断 (BLOCKED)")
        self.assertEqual(failing_dep, "task_parent")

        # Case 3: Parent was itself BLOCKED
        completed_parent_blocked = {
            "task_parent": {"success": False, "status": "BLOCKED", "output": "Cascade blocked"}
        }
        blocked_cascade, failing_dep2 = is_subtask_blocked(child_subtask, completed_parent_blocked)
        self.assertTrue(blocked_cascade, "父任务为 BLOCKED 时，孙子任务也必须级联阻断")
        self.assertEqual(failing_dep2, "task_parent")

    def test_task_verifier_rejects_empty_code_modifications(self):
        """Code verification MUST reject zero modifications and zero tests as false positives."""
        empty_info = {
            "target_files": [],
            "test_command": "",
        }
        empty_result = {
            "modified_files": [],
            "target_files": [],
            "test_command": "",
        }

        verif = TaskVerifier.verify(
            category="code",
            task_info=empty_info,
            result=empty_result,
            workspace=self.root,
        )

        self.assertFalse(verif.success, "未产生任何代码修改或测试执行的代码任务，绝不能被判为成功！")
        self.assertTrue("拒绝假阳性" in verif.details or "refuse" in verif.details)

    def test_task_verifier_routes_unverified_research_to_approval(self):
        """Open research tasks without objective artifacts must require user approval."""
        res_info = {
            "topic": "Competitive Landscape of Vector DBs",
            "min_words": 100,
        }
        # Result has substantive text (> 100 chars) but no structured artifact files
        res_result = {
            "summary": "Conducted comprehensive research comparing Milvus, Qdrant, and Chroma vector databases across distributed horizontal scalability, HNSW indexing retrieval performance, and metadata payload filtering. Milvus excels in enterprise scale, Qdrant offers top memory efficiency in Rust, and Chroma provides lightweight embedded integration.",
            "output_file": "",
            "verified_findings": [],
        }

        verif = TaskVerifier.verify(
            category="research",
            task_info=res_info,
            result=res_result,
            workspace=self.root,
        )

        self.assertTrue(verif.needs_approval, "Research without objective files requires approval")
        self.assertFalse(verif.success, "Unapproved research cannot succeed directly")


if __name__ == "__main__":
    unittest.main()
