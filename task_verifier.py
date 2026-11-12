"""Task verification subsystem for autonomous agent tasks.

Provides objective verification for code, file, data, external, and research tasks.
Tasks that cannot be verified objectively are explicitly marked as requiring user approval
rather than falsely claiming success.
"""
from __future__ import annotations

import json
import logging
import os
import py_compile
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Paths a run touches as a side effect; they are not evidence that the task's code changed.
_INCIDENTAL_PARTS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules', '.git', '.venv'}
_INCIDENTAL_SUFFIXES = ('.pyc', '.pyo', '.log', '.tmp')


def meaningful_changes(files: list[str]) -> list[str]:
    """Modified files that can count as the task's own output (caches, logs, bytecode removed)."""
    kept = []
    for name in files or []:
        path = Path(str(name))
        if _INCIDENTAL_PARTS.intersection(path.parts) or path.name.endswith(_INCIDENTAL_SUFFIXES):
            continue
        kept.append(str(name))
    return kept


def detect_test_command(workspace: Path) -> str:
    """The project's own test command when it has an obvious one, else ''."""
    import sys
    workspace = Path(workspace)
    package = workspace / 'package.json'
    if package.is_file():
        try:
            script = (json.loads(package.read_text('utf-8')).get('scripts') or {}).get('test', '')
        except (OSError, ValueError):
            script = ''
        if script and 'no test specified' not in script:
            return 'npm test --silent'
    has_python_tests = (workspace / 'pytest.ini').is_file() or (workspace / 'tests').is_dir() and any(
        (workspace / 'tests').glob('test_*.py'))
    if has_python_tests:
        return f'"{sys.executable}" -m pytest -q'
    return ''


@dataclass
class VerificationResult:
    success: bool
    needs_approval: bool
    verifier_type: str
    details: str
    timestamp: str = ""
    code_review: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskVerifier:
    """Verifies task completion with type-specific validation suites."""

    @classmethod
    def verify(
        cls,
        category: str,
        task_info: dict[str, Any],
        result: dict[str, Any] | str,
        workspace: Path,
    ) -> VerificationResult:
        """Verify the execution output according to task category.

        Categories supported:
        - 'code': tests, py_compile syntax, modified files existence, git diff.
        - 'file': target file existence, non-empty, format check.
        - 'data': JSON schema / structured payload validation.
        - 'external': receipt checking & external status confirmations.
        - 'research': deliverable substance, structure and citation checks.
        - 'subjective' / other: marks as needs_approval without false claiming.
        """
        cat = (category or "").lower().strip()
        result_dict = result if isinstance(result, dict) else {"output": str(result)}
        from tool_registry import validate_path_in_bounds
        from task_runtime import current_execution
        from workspace_tools import enabled
        execution=current_execution.get()
        config=enabled(execution.friend_directory()) if execution else {}
        for name in list(task_info.get('target_files') or []) + list(result_dict.get('artifacts') or []) + list(result_dict.get('modified_files') or []):
            try:
                validate_path_in_bounds(workspace / name,workspace,config)
            except (PermissionError,TypeError) as exc:
                return VerificationResult(False,False,'artifact_permission',str(exc))

        if cat == "agent_message":
            import agent_messaging
            ok, details = agent_messaging.verify_delivery(result_dict.get('agent_id', ''),
                                                          result_dict.get('handled_message_ids') or [])
            return VerificationResult(ok, False, 'agent_mail_receipts', details)
        if cat == "code":
            verification = cls._verify_code(task_info, result_dict, workspace)
            return cls._review_delivery(verification, task_info, result_dict, workspace)
        elif cat == "file":
            verification = cls._verify_file(task_info, result_dict, workspace)
            return cls._review_delivery(verification, task_info, result_dict, workspace)
        elif cat in ('data', 'goal_patrol', 'config_check'):
            return cls._verify_data(task_info, result_dict, workspace)
        elif cat == "external":
            return cls._verify_external(task_info, result_dict, workspace)
        elif cat == "research":
            return cls._verify_research(task_info, result_dict, workspace)
        elif cat == 'browser' and result_dict.get('browser_receipt'):
            receipt = result_dict['browser_receipt']
            from PIL import Image
            screenshot = Path(receipt.get('screenshot', ''))
            try:
                if str(screenshot) not in result_dict.get('artifacts', []) or not receipt.get('url') or not receipt.get('content'):
                    raise ValueError('缺少真实页面、内容或截图回执')
                with Image.open(screenshot) as image:
                    if image.format != 'PNG' or min(image.size) < 100:
                        raise ValueError('浏览器截图格式或尺寸无效')
                    image.verify()
                expected = receipt.get('expected_text')
                # the real page title counts too (example.com's "Example Domain" is only in <title>)
                if expected and expected not in receipt['content'] and expected not in receipt.get('title', ''):
                    raise ValueError('页面实际内容与指定验收文本不匹配')
            except (OSError, ValueError) as exc:
                return VerificationResult(False, False, 'browser_receipt_check', str(exc))
            if not expected and any(action in ('click', 'input') for action in receipt.get('actions', [])):
                return VerificationResult(False, True, 'browser_business_review', '浏览器已执行并保存截图；未设置业务验收文本，需要确认操作结果。')
            return VerificationResult(True, False, 'browser_receipt_check', '真实页面读取、PNG 产物及指定验收文本核验通过。')
        else:
            return cls._verify_subjective(task_info, result_dict, workspace)

    @classmethod
    def _review_delivery(cls, verification, task_info, result, workspace):
        from task_code_review import review_sources, describe_review
        report = review_sources(workspace, list(task_info.get('target_files') or []) +
                                list(result.get('modified_files') or []) + list(result.get('artifacts') or []))
        verification.code_review = report
        summary = describe_review(report)
        if summary:
            verification.details += '\n' + summary
        if report['status'] == 'needs_review' and (verification.success or verification.needs_approval):
            verification.success, verification.needs_approval = False, True
            verification.verifier_type = 'code_audit_review'
        return verification

    @classmethod
    def _verify_code(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        target_files = task_info.get("target_files") or []
        modified_files = meaningful_changes(result.get("modified_files") or [])
        test_command = task_info.get("test_command") or ""
        # Re-run the project's own tests independently when the task gave none (never the agent's claim).
        detected_test = not test_command and bool(modified_files) and detect_test_command(workspace)
        if detected_test:
            test_command = detected_test

        # 1. Target files existence
        if target_files:
            missing = []
            for tf in target_files:
                tf_path = workspace / tf
                if not tf_path.exists() or tf_path.stat().st_size == 0:
                    missing.append(tf)
            if missing:
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="code_file_check",
                    details=f"目标代码文件未生成或内容为空: {', '.join(missing)}",
                )

        # 2. Syntax validation for Python files
        checked_py_files = []
        for f_name in set(list(target_files) + list(modified_files)):
            f_path = workspace / f_name
            if f_path.exists() and f_path.suffix == ".py":
                checked_py_files.append(f_path)
                try:
                    py_compile.compile(str(f_path), doraise=True)
                except py_compile.PyCompileError as exc:
                    return VerificationResult(
                        success=False,
                        needs_approval=False,
                        verifier_type="code_syntax_check",
                        details=f"Python 代码编译语法错误 ({f_name}): {exc.msg}",
                    )
                except Exception as exc:
                    return VerificationResult(
                        success=False,
                        needs_approval=False,
                        verifier_type="code_syntax_check",
                        details=f"Python 编译检查异常 ({f_name}): {exc}",
                    )

        # 3. Optional automated test execution
        if test_command:
            try:
                from task_runtime import run_process, current_execution
                from workspace_tools import enabled
                from tool_registry import check_permission_allowed, ToolPermissionLevel
                execution=current_execution.get()
                if execution:
                    allowed,reason=check_permission_allowed(ToolPermissionLevel.SENSITIVE_EXEC,enabled(execution.friend_directory()))
                    if not allowed:
                        return VerificationResult(False,True,'test_command_permission',reason)
                proc = run_process(
                    test_command,
                    shell=True,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=task_info.get("test_timeout", 300 if detected_test else 60),
                )
                if proc.returncode != 0:
                    err_snippet = (proc.stderr or proc.stdout)[-500:]
                    if proc.returncode in (9009, 127) or 'is not recognized as an internal or external command' in err_snippet:
                        # The command itself cannot start (cmd 9009 / sh 127): no retry of the model can fix it,
                        # and each retry re-ran finished work. The owner corrects the command instead.
                        return VerificationResult(
                            success=False,
                            needs_approval=True,
                            verifier_type="test_command_unrunnable",
                            details=f"测试命令无法启动（程序不存在，或路径含空格未加引号），未重试：{err_snippet.strip()[:300]}",
                        )
                    return VerificationResult(
                        success=False,
                        needs_approval=False,
                        verifier_type="code_automated_test",
                        details=f"测试用例执行未通过 (exit code {proc.returncode}): {err_snippet}",
                    )
            except subprocess.TimeoutExpired:
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="code_automated_test",
                    details="自动化测试执行超时",
                )
            except Exception as exc:
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="code_automated_test",
                    details=f"测试执行异常: {exc}",
                )
        if not checked_py_files and not test_command and not target_files and not modified_files:
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="code_complete_check",
                details="代码任务未产生任何代码修改、目标文件或测试验证，拒绝假阳性通过 (refuse false positives)",
            )
        if not test_command and not target_files:
            # Files changed, but nothing objective says they are right: the owner decides, not the verifier.
            return VerificationResult(
                success=False,
                needs_approval=True,
                verifier_type="code_review_needed",
                details=(f"已修改 {len(modified_files)} 个文件（{', '.join(modified_files[:5])}），"
                         "但未指定测试命令或目标文件，项目中也未检测到可运行的测试；请检查修改后确认验收。"),
            )

        if not modified_files and "modified_files" in result:
            # Live run (gpt-6-luna): the model never saw the project and changed nothing, yet the existing
            # target files and tests passed. Checks that were already true before the run prove nothing.
            return VerificationResult(
                success=False,
                needs_approval=True,
                verifier_type="code_no_change",
                details="本次运行没有修改任何文件；目标文件与测试在运行前就已满足，无法证明任务已完成，请查看结果后人工确认",
            )

        files_desc = f"{len(checked_py_files)} 个 Python 文件语法通过" if checked_py_files else "代码文件已校验"
        test_desc = "，自动化测试通过" if test_command else ""
        return VerificationResult(
            success=True,
            needs_approval=False,
            verifier_type="code_complete_check",
            details=f"代码验证成功 ({files_desc}{test_desc})",
        )

    @classmethod
    def _verify_file(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        target_files = task_info.get("target_files") or []
        required_keywords = task_info.get("required_keywords") or []

        if not target_files:
            # Fall back to artifacts or modified files in result
            target_files = result.get("artifacts") or result.get("modified_files") or []

        if not target_files:
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="file_check",
                details="任务未指定目标文件，且未检测到产物落地",
            )

        for tf in target_files:
            tf_path = workspace / tf if not Path(tf).is_absolute() else Path(tf)
            if not tf_path.exists():
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="file_check",
                    details=f"预期文件未创建: {tf}",
                )
            if tf_path.stat().st_size == 0:
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="file_check",
                    details=f"生成的文件为空 (0 字节): {tf}",
                )

            if required_keywords:
                try:
                    content = tf_path.read_text(encoding="utf-8", errors="ignore")
                    missing_kw = [kw for kw in required_keywords if kw not in content]
                    if missing_kw:
                        return VerificationResult(
                            success=False,
                            needs_approval=False,
                            verifier_type="file_keyword_check",
                            details=f"文件 {tf} 缺少必要关键词: {', '.join(missing_kw)}",
                        )
                except Exception as exc:
                    return VerificationResult(
                        success=False,
                        needs_approval=False,
                        verifier_type="file_read_error",
                        details=f"读取验证文件异常 ({tf}): {exc}",
                    )

        if task_info.get('test_command'):
            verification = cls._verify_code(task_info, result, workspace)
            if verification.success:
                verification.verifier_type = 'file_automated_test'
                verification.details = '目标文件存在，且指定的内容验收命令执行通过'
            return verification

        return VerificationResult(
            success=True,
            needs_approval=False,
            verifier_type="file_check",
            details=f"目标文件全部存在且校验通过 ({len(target_files)} 个文件)",
        )

    @classmethod
    def _verify_data(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        expected_keys = task_info.get("expected_keys") or []
        raw_output = result.get("output", "")
        data_payload = result.get("data")

        # If data is not parsed, attempt parsing from output
        if data_payload is None and isinstance(raw_output, str):
            text = raw_output.strip()
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            elif text.startswith("```") and text.endswith("```"):
                text = text[3:-3].strip()
            try:
                data_payload = json.loads(text)
            except Exception:
                pass

        if data_payload is None:
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="data_schema_check",
                details="任务交付物未能解析为有效的结构化数据 (JSON)",
            )

        if expected_keys and not isinstance(data_payload, dict):
            return VerificationResult(False, False, 'data_schema_check', '指定了必要字段，交付物必须是 JSON 对象。')
        if expected_keys and isinstance(data_payload, dict):
            missing_keys = [k for k in expected_keys if k not in data_payload]
            if missing_keys:
                return VerificationResult(
                    success=False,
                    needs_approval=False,
                    verifier_type="data_schema_check",
                    details=f"数据缺少预期必要字段: {', '.join(missing_keys)}",
                )

        return VerificationResult(
            success=True,
            needs_approval=False,
            verifier_type="data_schema_check",
            details="结构化数据格式与字段校验通过",
        )

    @classmethod
    def _verify_external(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        receipt = result.get("receipt") or result.get("receipt_id")
        status_code = result.get("status_code")

        if not receipt and status_code is None:
            return VerificationResult(
                success=False,
                needs_approval=True,
                verifier_type="external_receipt_check",
                details="外部操作未返回回执或状态凭证，转入待人工确认",
            )

        if status_code is not None and int(status_code) >= 400:
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="external_status_check",
                details=f"外部调用返回错误状态码: {status_code}",
            )

        return VerificationResult(
            success=True,
            needs_approval=False,
            verifier_type="external_receipt_check",
            details=f"外部操作回执校验正常 (Receipt: {receipt or status_code})",
        )

    @classmethod
    def _verify_research(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        if result.get('research_review_required') is True:
            return VerificationResult(False, True, 'research_evidence_review',
                '研究报告和方案已保存；尚未获得独立核验证据，需人工审查，不能标记为已证实突破。')
        min_words = task_info.get("min_words", 80)
        output = str(result.get("output") or result.get("summary") or "").strip()

        if len(output) < min_words:
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="research_substance_check",
                details=f"研究交付内容实质字数不足 ({len(output)}/{min_words} 字)",
            )

        # Check for empty generic evasion
        evasions = ["AI, ", "无法获取", "我不知道", "对不起，我无法", "作为AI，我不能"]
        if any(ev in output and len(output) < min_words * 2 for ev in evasions):
            return VerificationResult(
                success=False,
                needs_approval=False,
                verifier_type="research_evasion_check",
                details="模型输出包含拒答或无实质研究结论",
            )

        target_files = task_info.get("target_files") or result.get("artifacts") or []
        if target_files:
            file_res = cls._verify_file(task_info, result, workspace)
            if not file_res.success:
                return file_res

        # If no tangible artifacts and no explicit keywords were defined, require human approval
        if not target_files and not task_info.get("required_keywords"):
            return VerificationResult(
                success=False,
                needs_approval=True,
                verifier_type="research_subjective_review",
                details=f"研究分析已产出 ({len(output)} 字)，缺少客观产物文件核验，需人工确认",
            )

        return VerificationResult(
            success=True,
            needs_approval=False,
            verifier_type="research_substance_check",
            details=f"研究分析产出符合深度要求 ({len(output)} 字)",
        )

    @classmethod
    def _verify_subjective(
        cls,
        task_info: dict[str, Any],
        result: dict[str, Any],
        workspace: Path,
    ) -> VerificationResult:
        # Subjective tasks cannot be objectively validated -> must be marked for approval
        return VerificationResult(
            success=True,
            needs_approval=True,
            verifier_type="human_approval_required",
            details="此任务属于主观或无法客观校验类型，已标记为待用户确认验收",
        )
