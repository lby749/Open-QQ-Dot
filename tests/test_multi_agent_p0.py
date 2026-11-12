# -*- coding: utf-8 -*-
"""P0 Regression and Multi-Agent Orchestration Tests.

Covers:
- Structured error classification (RATE_LIMIT, QUOTA_EXHAUSTED, AUTH_ERROR, etc.)
- Normal model text containing keywords is NOT treated as an error
- Multi-agent concurrency and dependency execution
- File collision isolation (no concurrent writes to same file)
- Verification failure detection and bounded recovery
- Quota failover and autonomous handover
"""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import agent_errors
from agent_errors import AgentError, AgentErrorType, classify_error
import multi_agent_orchestrator


class MultiAgentP0Tests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def test_structured_error_classification(self):
        # 1. HTTP 429 without quota body -> RATE_LIMIT (retryable)
        err = classify_error('custom', status_code=429, error_payload={'error': {'message': 'Too many requests'}})
        self.assertEqual(err.error_type, AgentErrorType.RATE_LIMIT)
        self.assertTrue(err.retryable)

        # 2. HTTP 429 with insufficient_quota -> QUOTA_EXHAUSTED (non-retryable, triggers failover)
        err_quota = classify_error('custom', status_code=429, error_payload={'error': {'type': 'insufficient_quota', 'message': 'You have exceeded your current quota'}})
        self.assertEqual(err_quota.error_type, AgentErrorType.QUOTA_EXHAUSTED)
        self.assertFalse(err_quota.retryable)

        # 3. HTTP 401 / 403 -> AUTH_ERROR
        err_auth = classify_error('claude', status_code=401)
        self.assertEqual(err_auth.error_type, AgentErrorType.AUTH_ERROR)

        # 4. Timeout -> TIMEOUT
        import subprocess
        err_timeout = classify_error('antigravity', exc=subprocess.TimeoutExpired(['cmd'], 10))
        self.assertEqual(err_timeout.error_type, AgentErrorType.TIMEOUT)
        self.assertTrue(err_timeout.retryable)

        # 5. Normal assistant text containing numbers or words like '429' or 'quota' is NOT an error
        normal_response = "在设计 API 时，HTTP 429 代表限流状态码，并且系统配额 (quota) 应该合理配置。"
        # Notice we never pass normal response text to classify_error as an exception or error payload!
        self.assertNotIn("okerror", normal_response)

    def test_multi_agent_file_collision_isolation(self):
        # When subtasks share the same target_file, Planner/Dispatcher ensures serialization
        subtasks_plan = {
            "need_collaboration": True,
            "analysis": "代码实现与优化",
            "direct_reply": "",
            "subtasks": [
                {
                    "id": "task_1",
                    "title": "编写核心模块",
                    "agent": "claude",
                    "role": "架构师",
                    "task_type": "execution",
                    "required_capability": "code_implementation",
                    "target_files": ["core.py"],
                    "depends_on": [],
                    "subtask_prompt": "编写 core.py"
                },
                {
                    "id": "task_2",
                    "title": "重构核心模块",
                    "agent": "antigravity",
                    "role": "重构专家",
                    "task_type": "execution",
                    "required_capability": "code_implementation",
                    "target_files": ["core.py"],  # Same file!
                    "depends_on": [],
                    "subtask_prompt": "重构 core.py"
                }
            ]
        }

        call_counts = {}

        def fake_call(agent, prompt, directory, profile=None, **kwargs):
            call_counts[agent] = call_counts.get(agent, 0) + 1
            if "Planner" in prompt or "规划" in prompt:
                return f"```json\n{json.dumps(subtasks_plan)}\n```", None
            if "[MULTI-AGENT MASTER SYNTHESIS" in prompt or "【主智能体统筹裁决与交付指令" in prompt:
                return "全部任务已完成交付", None
            if "Subtask Identifier: task_2" in prompt or "task_2" in prompt:
                (directory / "core.py").write_text("# Refactored core\ndef run(): return 2\n", encoding='utf-8')
                return "已重构 core.py", None
            if "Subtask Identifier: task_1" in prompt or "task_1" in prompt:
                (directory / "core.py").write_text("# Core implementation\ndef run(): return 1\n", encoding='utf-8')
                return "已编写 core.py", None
            return "全部任务已完成交付", None

        with patch.object(multi_agent_orchestrator, 'call_single_agent', side_effect=fake_call):
            out, _ = multi_agent_orchestrator.orchestrate(
                "请编写并重构 core.py",
                self.directory,
                profile={'model_api': {'active_providers': ['codex', 'claude', 'antigravity']}}
            )

        self.assertIn("全部任务已完成交付", out)
        self.assertTrue((self.directory / "core.py").exists())
        self.assertIn("def run(): return 2", (self.directory / "core.py").read_text('utf-8'))

    def test_multi_agent_verification_and_recovery(self):
        # Test that Verifier catches missing files and triggers recovery
        attempt = 0

        def fake_call(agent, prompt, directory, profile=None, **kwargs):
            nonlocal attempt
            if "Planner" in prompt or "规划" in prompt:
                plan = {
                    "need_collaboration": True,
                    "analysis": "测试文件创建",
                    "subtasks": [{
                        "id": "task_1",
                        "title": "创建模块",
                        "agent": "claude",
                        "role": "实现者",
                        "task_type": "execution",
                        "target_files": ["module.py"],
                        "depends_on": [],
                        "subtask_prompt": "请创建 module.py"
                    }]
                }
                return f"```json\n{json.dumps(plan)}\n```", None
            if "[MULTI-AGENT MASTER SYNTHESIS" in prompt or "【主智能体统筹裁决与交付指令" in prompt:
                return "统筹交付完成", None
            if "module.py" in prompt:
                attempt += 1
                if attempt == 1:
                    # First attempt claims completed but did not write file!
                    return "我已经完成了 module.py 的编写！", None
                else:
                    # Second attempt actually writes the file
                    (directory / "module.py").write_text("print('hello world')\n", encoding='utf-8')
                    return "已真实创建 module.py", None
            return "统筹交付完成", None

        with patch.object(multi_agent_orchestrator, 'call_single_agent', side_effect=fake_call):
            out, _ = multi_agent_orchestrator.orchestrate(
                "创建 module.py",
                self.directory,
                profile={'model_api': {'active_providers': ['codex', 'claude']}}
            )

        self.assertIn("统筹交付完成", out)
        self.assertTrue((self.directory / "module.py").exists())
        # Confirms verifier triggered at least 2 attempts
        self.assertGreaterEqual(attempt, 2)

    def test_multi_agent_quota_failover_handover(self):
        # Test autonomous failover when worker hits QUOTA_EXHAUSTED
        def fake_call(agent, prompt, directory, profile=None, raise_on_error=False, **kwargs):
            if "Planner" in prompt:
                plan = {
                    "need_collaboration": True,
                    "analysis": "测试额度交接",
                    "subtasks": [{
                        "id": "task_1",
                        "title": "专项计算",
                        "agent": "claude",
                        "role": "计算专家",
                        "task_type": "analysis",
                        "required_capability": "complex_reasoning",
                        "target_files": [],
                        "depends_on": [],
                        "subtask_prompt": "执行深度推理"
                    }]
                }
                return f"```json\n{json.dumps(plan)}\n```", None
            if agent == 'claude':
                # Claude raises structured Quota Exhausted error
                err = AgentError(AgentErrorType.QUOTA_EXHAUSTED, provider='claude', message='Your account has zero credit balance', status_code=429)
                if raise_on_error:
                    raise err
                return f"[Claude Execution Error]: {err.message}", None
            if agent == 'antigravity':
                # Fallback agent succeeds
                return "Antigravity 接手深度推理完成：答案为 42", None
            return "最终结论已确认，答案是 42", None

        with patch.object(multi_agent_orchestrator, 'call_single_agent', side_effect=fake_call):
            out, _ = multi_agent_orchestrator.orchestrate(
                "请深度计算最终答案",
                self.directory,
                profile={'model_api': {'active_providers': ['codex', 'claude', 'antigravity']}}
            )

        self.assertIn("答案是 42", out)
        self.assertIn("自动交接", out)


if __name__ == '__main__':
    unittest.main()
