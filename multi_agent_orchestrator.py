# -*- coding: utf-8 -*-
"""Multi-Agent Orchestrator: Autonomous Multi-Agent Planning, Execution, Verification & Recovery Engine.

Full Closed-Loop Architecture:
1. Planner: Decomposes complex goals into typed subtasks with explicit dependencies and file scopes.
2. Dispatcher: Dynamically assigns subtasks according to capabilities, quota health, and concurrency limits.
3. Executor: Executes tasks with unified permissions, isolated workspaces, and structured telemetry.
4. Verifier: Objectively verifies deliverables (file existence, python syntax, non-empty outputs) instead of blindly trusting assertions.
5. Recovery: Autonomous failover on quota exhaustion, capability refusal, or verification failure with bounded retries.
6. Reporter: Synthesizes final authoritative response with complete structured <think> trace.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
import os
import py_compile
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agent_errors import AgentError, AgentErrorType, classify_error, execute_with_retry

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent
QW_DIR = ROOT_DIR / "quota-whale"
if str(QW_DIR) not in sys.path:
    sys.path.insert(0, str(QW_DIR))

AGENT_SPECS = {
    'codex': {
        'name': 'Codex CLI',
        'provider': 'codex',
        'role_title': '系统实现与终端执行专家',
        'specialties': '代码编写与调试、工具与终端命令执行、系统级工程实现与文件落地',
    },
    'claude': {
        'name': 'Claude Code',
        'provider': 'claude',
        'role_title': '系统架构与安全审查专家',
        'specialties': '复杂逻辑架构设计、长上下文代码分析、重构设计、安全与边界审查',
    },
    'antigravity': {
        'name': 'Antigravity CLI',
        'provider': 'antigravity',
        'role_title': '深度推理与综合探索专家',
        'specialties': '深度推理思考、跨领域知识综合、网络检索与前沿方案探索',
    },
    'dsh': {
        'name': 'DeepSeek Harness',
        'provider': 'dsh',
        'role_title': '算法推导与逻辑验证专家',
        'specialties': '深度逻辑推导、数学证明、高性价比算法实现与代码逻辑分析',
    },
    'custom': {
        'name': '自定义 API',
        'provider': 'custom',
        'role_title': '领域专用与定制模型专家',
        'specialties': '快速高并发推理、特定微调模型与定制领域知识问答',
    },
}

from model_dispatcher import DEFAULT_MODEL_SPECS
from agent_tool_adapters import AGENT_BACKENDS, BUILTINS
for _backend in AGENT_BACKENDS:
    AGENT_SPECS[_backend] = {'name': BUILTINS[_backend].name, 'provider': _backend,
                           'role_title': '本机执行智能体', 'specialties': '代码、文件与终端任务；沿用工具自身模型配置'}
for _backend, _name, _specialty in (
        ('webchat_deepseek', '网页端 DeepSeek', '网页对话与快速分析'),
        ('webchat_chatgpt', '网页端 ChatGPT', '网页对话与通用推理'),
        ('webchat_gemini', '网页端 Gemini', '网页对话与长上下文分析')):
    AGENT_SPECS[_backend] = {'name': _name, 'provider': _backend,
                             'role_title': _specialty, 'specialties': '浏览器网页模型；不提供本机工具循环',
                             'capabilities': {'webchat', 'analysis'}}
for _provider, _metadata in AGENT_SPECS.items():
    _metadata['capabilities'] = DEFAULT_MODEL_SPECS[_provider].routing_tags

REFUSAL_KEYWORDS = (
    'ok',
    'i cannot search the web',
    "i don't have internet access",
    'i cannot browse',
    'i cannot execute system commands',
    'i cannot run terminal',
    'as an ai language model',
    '无法访问互联网',
    '无法联网',
    '无法查询最新',
    '无法获取实时',
    '不能访问网络',
    '无法执行命令',
    '不能运行终端',
    '没有权限修改文件',
    '不能直接在本地'
)


def get_agent_name(provider: str) -> str:
    p = 'custom' if provider in ('deepseek', 'api') else 'dsh' if provider == 'deepseek_harness' else provider
    return AGENT_SPECS.get(p, {}).get('name', provider.upper())


def check_agent_quota_critical(provider: str) -> tuple[bool, str]:
    """Check if an agent's quota is critical (remaining <= 5% or used >= 95%)."""
    p = 'custom' if provider in ('deepseek', 'api') else 'dsh' if provider == 'deepseek_harness' else provider
    if p == 'codex':
        try:
            import quota_sources
            q = quota_sources.codex_quota()
            if q and isinstance(q, dict):
                five_used = q.get('five_used')
                week_used = q.get('week_used')
                if five_used is not None and five_used >= 95.0:
                    return True, f"5小时额度已消耗 {five_used}%（剩余 ≤ 5%）"
                if week_used is not None and week_used >= 95.0:
                    return True, f"周额度已消耗 {week_used}%（剩余 ≤ 5%）"
        except Exception as exc:
            logger.debug(f"Codex quota check error: {exc}")
    return False, ""


def is_capability_refusal(text: str) -> tuple[bool, str]:
    """Detect if output indicates the agent cannot perform the requested action."""
    lower = text.lower()
    for kw in REFUSAL_KEYWORDS:
        if kw in lower:
            if '网' in kw or 'internet' in kw or 'web' in kw or 'search' in kw:
                return True, 'web_search'
            if '命令' in kw or '终端' in kw or 'terminal' in kw or 'command' in kw:
                return True, 'terminal'
            if '文件' in kw or 'file' in kw:
                return True, 'file_write'
            return True, 'specialized'
    return False, ""


def find_capable_agent(required_cap: str, candidates: list[str], exclude: str | None = None) -> str | None:
    """Select a capable, authorized and available model through the existing dispatcher."""
    from model_dispatcher import ModelDispatcher, TaskCategory
    dispatcher = ModelDispatcher.get_instance()
    eligible = [c for c in candidates if c != exclude and dispatcher.is_available(c)]
    capable = [c for c in eligible if required_cap in dispatcher.routing_capabilities(c)]
    if not (capable or eligible):
        return None
    return dispatcher.select_best_model(TaskCategory.GENERAL, capable or eligible)



def _without_duplicate_mission(subtask: str, mission: str) -> str:
    """Avoid retransmitting a complete user mission inside a delegated subtask.

    We only remove *exact* repeated content. Partial or similar instructions
    are not shortened because that might discard essential requirements.
    """
    subtask, mission = subtask.strip(), mission.strip()
    if mission and mission in subtask:
        return re.sub(r'(?m)^'+re.escape(mission)+r'(?=\n|$)', '[Primary mission is provided above]',subtask)
    return subtask


def build_claude_english_handover(
    source_agent: str,
    trigger_reason: str,
    user_task: str,
    context_or_subtask: str,
    task_id: str | None = None,
) -> str:
    """Construct a high-precision, natural English handover briefing for Claude Code."""
    src_name = get_agent_name(source_agent)
    tid_line = f"Subtask Identifier: {task_id}\n" if task_id else ""
    return (
        f"[MULTI-AGENT AUTONOMOUS HANDOVER BRIEF -> CLAUDE CODE]\n"
        f"Handover Origin: {src_name}\n"
        f"{tid_line}"
        f"Handover Reason: {trigger_reason}\n"
        f"Target Engine: Claude Code (Anthropic Claude Architecture & Analytical Engine)\n\n"
        f"=== PRIMARY MISSION & USER INSTRUCTION ===\n"
        f"{user_task.strip()}\n\n"
        f"=== PRECEDING CONTEXT & DELEGATED SUBTASK ===\n"
        f"{_without_duplicate_mission(context_or_subtask, user_task)}\n\n"
        f"=== DIRECTIVES FOR CLAUDE CODE ===\n"
        f"1. You are officially delegated by the multi-agent orchestration schedule to take over this assignment.\n"
        f"2. Apply superior architectural rigor, safety, and deep contextual reasoning.\n"
        f"3. Produce your output directly addressing the user's core intent with absolute clarity and precision.\n"
        f"4. Output the definitive substantive deliverable without repetitive handover meta-chatter."
    )


def take_dir_snapshot(directory: Path) -> dict[str, float]:
    """Capture mtimes of all existing files in directory to detect modifications."""
    snapshot = {}
    if not directory.is_dir():
        return snapshot
    try:
        for p in directory.rglob('*'):
            if p.is_file():
                try:
                    snapshot[str(p.resolve())] = p.stat().st_mtime
                except OSError:
                    pass
    except Exception:
        pass
    return snapshot


def detect_modified_files(directory: Path, before_snapshot: dict[str, float]) -> list[Path]:
    """Return all files newly created or modified since before_snapshot."""
    modified = []
    if not directory.is_dir():
        return modified
    try:
        for p in directory.rglob('*'):
            if p.is_file():
                resolved = str(p.resolve())
                try:
                    mtime = p.stat().st_mtime
                    if resolved not in before_snapshot or mtime > before_snapshot[resolved]:
                        modified.append(p)
                except OSError:
                    pass
    except Exception:
        pass
    return modified


def check_python_syntax(files: list[Path]) -> tuple[bool, str]:
    """Verify syntax of modified python files using py_compile."""
    for f in files:
        if f.suffix == '.py':
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as exc:
                return False, f"Python 语法错误 ({f.name}): {exc.msg}"
            except Exception as exc:
                return False, f"Python 编译检查失败 ({f.name}): {exc}"
    return True, "Python 语法校验全部通过"


def call_single_agent(
    provider: str,
    prompt: str,
    directory: Path,
    base_profile: dict | None = None,
    ephemeral: bool = True,
    image_path: Path | None = None,
    allow_empty: bool = False,
    web_search: bool = False,
    text_only: bool = False,
    record_input: Callable | None = None,
    local_ok: bool = True,
    raise_on_error: bool = False,
    thread_id: str | None = None,
    output_schema: Path | None = None,
    session_key: str = 'main',
    max_retries: int | None = None,
) -> tuple[str, str | None]:
    """Execute a single specific agent engine with structured error classification.

    Dispatch goes through main.codex_text with a single-provider profile, so workers get exactly the
    chat path's backend selection, signatures, quota handling and local fallback (one router, not two)."""
    import main

    p = 'custom' if provider in ('deepseek', 'api') else 'dsh' if provider == 'deepseek_harness' else provider
    agent_profile = copy.deepcopy(base_profile or {})
    api = agent_profile.setdefault('model_api', {})
    api['provider'] = p
    api['multi_agent_enabled'] = False
    api['multi_agent'] = {'enabled': False}

    def _invoke():
        sent,resume,commit,discard=prompt,thread_id,None,None
        if execution and not ephemeral and thread_id is None and p in ('codex','claude') and image_path is None:
            from prompt_context import task_session_prompt
            sent,resume,commit,discard=task_session_prompt(execution,p,session_key,prompt,agent_profile)
        try:
            text, new_thread = main.codex_text(sent, directory, resume, ephemeral=ephemeral, image_path=image_path,
                                           allow_empty=allow_empty, profile=agent_profile, web_search=web_search,
                                           text_only=text_only, output_schema=output_schema,
                                           record_input=record_input, local_ok=local_ok, cooperation_entry='')
        except Exception as error:
            from prompt_context import missing_session_error
            if not (resume and discard and missing_session_error(error)):
                raise
            discard()
            sent,resume,commit,discard=task_session_prompt(execution,p,session_key,prompt,agent_profile)
            text,new_thread=main.codex_text(sent,directory,None,ephemeral=ephemeral,image_path=image_path,
                allow_empty=allow_empty,profile=agent_profile,web_search=web_search,text_only=text_only,
                output_schema=output_schema,record_input=record_input,local_ok=local_ok,cooperation_entry='')
        if commit:
            commit(new_thread)
        return text or "", new_thread

    from model_dispatcher import ModelDispatcher
    from task_runtime import current_execution
    dispatcher = ModelDispatcher.get_instance()
    execution = current_execution.get()
    def gated():
        if execution:
            execution.check()
        if not dispatcher.is_available(p):
            raise AgentError(AgentErrorType.RATE_LIMIT, provider=p, message='后端冷却中，未发起调用')
        # Inside a task, wait for a concurrency slot in short slices so cancel/pause still stop us promptly.
        return dispatcher.execute_with_budget_and_gate(p, _invoke, timeout_sec=300,
                                                       should_stop=execution.check if execution else None)
    try:
        retries = (0 if execution else 1) if max_retries is None else max_retries
        return execute_with_retry(p, gated, max_retries=retries)
    except (InterruptedError, TimeoutError):
        raise
    except AgentError as err:
        if execution and err.error_type in (AgentErrorType.NETWORK_ERROR,AgentErrorType.TIMEOUT):
            raise InterruptedError('模型请求结果未确认，已暂停并保留检查点，不自动重复发送：'+err.message) from err
        logger.warning(f"Single agent {provider} call failed with AgentError: {err}")
        if raise_on_error:
            raise
        return f"【{get_agent_name(provider)} 执行异常 ({err.error_type.value})】：{err.message}", None
    except Exception as exc:
        err = classify_error(p, exc=exc)
        logger.warning(f"Single agent {provider} call failed: {exc} -> classified as {err.error_type.value}")
        if raise_on_error:
            raise err
        return f"【{get_agent_name(provider)} 执行异常 ({err.error_type.value})】：{err.message}", None


def detect_and_break_dependency_cycles(subtasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Detect circular dependencies in subtask DAG and break backward cycles safely."""
    adj = {st['id']: list(st.get('depends_on', [])) for st in subtasks}
    visited = {}
    cycle_detected = False

    def _dfs_cycle(node: str, path: set):
        visited[node] = 1
        path.add(node)
        for dep in adj.get(node, []):
            if dep in path:
                return True
            if dep in adj and visited.get(dep, 0) == 0:
                if _dfs_cycle(dep, path):
                    return True
        path.remove(node)
        visited[node] = 2
        return False

    for st_id in adj:
        if visited.get(st_id, 0) == 0:
            if _dfs_cycle(st_id, set()):
                cycle_detected = True
                break

    if cycle_detected:
        for st in subtasks:
            st['depends_on'] = [d for d in st.get('depends_on', []) if d < st['id']]

    return subtasks, cycle_detected


def is_subtask_blocked(subtask: dict[str, Any], completed_tasks: dict[str, dict[str, Any]]) -> tuple[bool, str | None]:
    """Check if a subtask is blocked due to any upstream failed or blocked dependency."""
    for dep_id in subtask.get('depends_on', []):
        if dep_id in completed_tasks:
            dep_res = completed_tasks[dep_id]
            if not dep_res.get('success', False) or dep_res.get('status') == 'BLOCKED':
                return True, dep_id
    return False, None


# ==============================================================================
# Full Multi-Agent Orchestration Loop (Planner, Dispatcher, Executor, Verifier, Recovery, Reporter)
# ==============================================================================

def orchestrate(
    prompt: str,
    directory: Path,
    thread_id: str | None = None,
    ephemeral: bool = False,
    image_path: Path | None = None,
    allow_empty: bool = False,
    profile: dict | None = None,
    web_search: bool = False,
    text_only: bool = False,
    output_schema: Path | None = None,
    record_input: Callable | None = None,
    local_ok: bool = True,
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    """Execute multi-agent collaboration with autonomous quota handover, planning, and verification."""
    from task_runtime import current_execution
    execution=current_execution.get()
    operation_directory=execution.workspace() if execution else directory
    saved_checkpoint=execution.manager.get_task(execution.task_id).checkpoint if execution else {}
    api = (profile or {}).get("model_api") or {}
    multi_cfg = api.get("multi_agent") or {}

    # 1. Resolve Handover Priority Order
    owner_id = "system"
    if execution:
        task_obj = execution.manager.get_task(execution.task_id)
        if task_obj and task_obj.owner_id:
            owner_id = task_obj.owner_id
    if owner_id == "system" and profile:
        owner_id = str(profile.get("qq_id") or profile.get("owner_id") or "system")
    handover_order = (
        multi_cfg.get("handover_order")
        or api.get("handover_order")
        or multi_cfg.get("active_providers")
        or api.get("active_providers")
        or [api.get("provider", "codex")]
    )

    normalized_order = []
    for prov in handover_order:
        n = 'custom' if prov in ('deepseek', 'api') else 'dsh' if prov == 'deepseek_harness' else prov
        if n not in normalized_order and n in AGENT_SPECS:
            normalized_order.append(n)

    if not normalized_order:
        normalized_order = ['codex']

    handover_traces: list[str] = []

    # 2. Check Quota Thresholds ( <= 5%) & Select Active Master
    master_provider = None
    for candidate in normalized_order:
        is_crit, crit_reason = check_agent_quota_critical(candidate)
        if is_crit:
            c_name = get_agent_name(candidate)
            handover_traces.append(f"⚠️ 智能体 [{c_name}] 额度预警（{crit_reason}），触发自动交接策略")
            continue
        master_provider = candidate
        break

    if not master_provider:
        raise AgentError(AgentErrorType.QUOTA_EXHAUSTED, message='所有已授权后端额度不足；等待用户恢复，不切换账号')

    collaborators = [p for p in normalized_order if p != master_provider]
    master_name = get_agent_name(master_provider)

    if len(normalized_order) > 1 and master_provider != normalized_order[0]:
        first_name = get_agent_name(normalized_order[0])
        handover_traces.append(f"↪️ 已自动交接至编排序列中的就绪智能体：[{master_name}]（接替 [{first_name}] 担任主控调度）")

    # If only 1 provider configured, run directly
    if not collaborators:
        # Inside a task an agent failure must fail the task, not come back as reply text.
        res, extra = call_single_agent(master_provider, prompt, directory, profile,
                                       ephemeral=ephemeral, image_path=image_path,
                                       allow_empty=allow_empty, web_search=web_search,
                                       text_only=text_only, record_input=record_input,
                                       local_ok=local_ok, raise_on_error=bool(execution),
                                       thread_id=thread_id, output_schema=output_schema)
        if handover_traces:
            th_block = "<think>\n" + "\n".join(handover_traces) + "\n</think>\n\n"
            return th_block + res, extra
        return res, extra

    collaborators_names = [get_agent_name(p) for p in collaborators]
    if on_step:
        on_step(f"👑 主智能体 [{master_name}] 开始分析任务，协同团队成员：{', '.join(collaborators_names)}…")

    # ================= Stage 1: Planner () =================
    mem_store = None
    try:
        from agent_memory import AgentMemoryStore
        from task_manager import learnable_work
        if execution and learnable_work(execution.manager.get_task(execution.task_id)):
            mem_store = AgentMemoryStore.get_instance(execution.manager.db_path.parent / 'agent_memory.sqlite3')
    except Exception:
        pass
    # 2026-10-26: what other agents, sessions and projects learned (memory_hub: verified fixes and experiences,
    # pitfalls, preferences), ranked for this task, each with its kind and source; only what owner_id may see
    try:
        import memory_hub
        past_knowledge = [] if execution else memory_hub.recall(prompt, owner_id, limit=3,
                                           kinds=('verified_fix', 'verified_experience', 'pitfall', 'preference', 'tool_note'))
    except Exception:
        past_knowledge = []

    remedy_block = ""
    if past_knowledge:
        remedy_block = "\n【历史工程排错与避坑经验 (AgentMemory)】：\n" + memory_hub.render(past_knowledge) + "\n"

    from agent_model_selection import options as model_choices, validate as validate_model_choice, apply as selected_profile
    routing_catalog = model_choices(profile or {}, normalized_order)
    agents_manifest = []
    for p in collaborators:
        spec = AGENT_SPECS.get(p, {})
        agents_manifest.append(f"- 智能体标识: `{p}`\n  名称: {spec.get('name')}\n  专长: {spec.get('specialties')}\n  能力标签: {list(spec.get('capabilities', set()))}")
    manifest_str = "\n".join(agents_manifest)

    decompose_prompt = (
        f"你是多智能体协同网络的主规划智能体（Planner / Master Agent: {master_name}），负责解析全局目标、拆解具备依赖关系的专业子任务。\n"
        f"你拥有以下协同智能体团队成员可供分派任务：\n{manifest_str}\n\n"
        f"每个成员可选的模型与思考强度（配置/缓存，不代表已登录或有额度）：\n{json.dumps(routing_catalog, ensure_ascii=False)}\n"
        f"当前用户提出的需求或指令如下：\n"
        f"【用户任务】\n{prompt}\n"
        f"{remedy_block}\n"
        f"【规划与拆解准则】：\n"
        f"1. 分析核心目标与依赖关系。\n"
        f"2. 若任务简单（问候、简明概念），请设 need_collaboration 为 false 并在 direct_reply 中直接回答。\n"
        f"3. 若任务复杂，请设 need_collaboration 为 true，拆解出 1 至 {len(collaborators)} 个专业子任务。\n"
        f"4. 明确区分任务类型 task_type ('analysis' 为只读分析，'execution' 为代码或文件落地执行)。\n"
        f"5. 若涉及文件写入，在 target_files 中列出涉及的相对路径；若多个子任务涉及同一文件，必须使用 depends_on 设置依赖顺序，严禁并行冲突修改！\n\n"
        f"6. 按子任务难度选择该后端目录内的 model 与 effort；不支持思考强度时留空，不必让所有成员都参与。\n"
        f"请严格按以下 JSON 格式输出：\n"
        f"```json\n"
        f'{{\n'
        f'  "need_collaboration": true,\n'
        f'  "analysis": "核心目标与技术难点分析",\n'
        f'  "direct_reply": "",\n'
        f'  "subtasks": [\n'
        f'    {{\n'
        f'      "id": "task_1",\n'
        f'      "title": "子任务简短标题",\n'
        f'      "agent": "{collaborators[0]}",\n'
        f'      "role": "该智能体承担的具体角色",\n'
        f'      "model": "该后端目录内的模型ID",\n'
        f'      "effort": "该模型支持的思考档位或空字符串",\n'
        f'      "task_type": "analysis",\n'
        f'      "required_capability": "architecture",\n'
        f'      "target_files": [],\n'
        f'      "depends_on": [],\n'
        f'      "subtask_prompt": "具体需要执行的指令"\n'
        f'    }}\n'
        f'  ]\n'
        f'}}\n'
        f"```"
    )

    if saved_checkpoint.get('orchestration_plan'):
        plan_res=json.dumps({'need_collaboration':True,'subtasks':saved_checkpoint['orchestration_plan']},ensure_ascii=False)
    else:
        try:
            plan_res, _ = call_single_agent(master_provider, decompose_prompt, directory, profile,
                                            ephemeral=True, allow_empty=True, web_search=False, raise_on_error=True)
        except AgentError as plan_err:
            # Master failed to plan: fail over to the next collaborator, or surface the failure.
            if not collaborators:
                raise
            handover_traces.append(f"⚠️ 主智能体 [{master_name}] 规划阶段异常（{plan_err.error_type.value}），触发容灾交接")
            next_master = collaborators[0]
            handover_traces.append(f"↪️ 提升 [{get_agent_name(next_master)}] 为新主智能体接管规划")
            master_provider = next_master
            master_name = get_agent_name(master_provider)
            collaborators = [p for p in normalized_order if p != master_provider]
            plan_res, _ = call_single_agent(master_provider, decompose_prompt, directory, profile,
                                            ephemeral=True, allow_empty=True, raise_on_error=True)

    # Parse JSON plan
    plan_data = None
    try:
        json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', plan_res)
        if json_match:
            plan_data = json.loads(json_match.group(1))
        else:
            json_match = re.search(r'(\{[\s\S]*"need_collaboration"[\s\S]*\})', plan_res)
            if json_match:
                plan_data = json.loads(json_match.group(1))
    except Exception as exc:
        logger.warning(f"Failed to parse decomposition JSON: {exc}")

    need_collab = True
    subtasks = []
    analysis_text = "综合评估用户需求，启动多智能体闭环协同。"
    direct_reply = ""

    if plan_data and isinstance(plan_data, dict):
        need_collab = bool(plan_data.get('need_collaboration', True))
        analysis_text = plan_data.get('analysis') or analysis_text
        direct_reply = plan_data.get('direct_reply') or ""
        subtasks = plan_data.get('subtasks') or []
    else:
        # Fallback default decomposition
        subtasks = [
            {
                "id": f"task_{idx+1}",
                "title": f"{AGENT_SPECS.get(p, {}).get('name')} 专项推演",
                "agent": p,
                "role": AGENT_SPECS.get(p, {}).get('role_title', '专项专家'),
                "task_type": "analysis",
                "required_capability": list(AGENT_SPECS.get(p, {}).get('capabilities', {'fast_code'}))[0],
                "target_files": [],
                "depends_on": [],
                "subtask_prompt": f"请针对用户指令【{prompt}】提供深入的分析方案与解答。",
            }
            for idx, p in enumerate(collaborators)
        ]

    if not need_collab and direct_reply.strip():
        thought_header = "<think>\n👥 跨智能体协作执行记录\n"
        if handover_traces:
            thought_header += "\n".join(f"  {h}" for h in handover_traces) + "\n"
        thought_header += (
            f"👑 主智能体 [{master_name}]：\n"
            f"  └─ 任务评估：{analysis_text}\n"
            f"  └─ 决策：该任务无需多智能体拆解，由主智能体独立高效交付。\n"
            f"</think>\n\n"
        )
        return thought_header + direct_reply.strip(), None

    # Sanitize and resolve file collision dependencies
    file_writers: dict[str, str] = {}
    valid_subtasks: list[dict[str, Any]] = []
    for idx, st in enumerate(subtasks):
        st_id = st.get('id') or f"task_{idx+1}"
        if any(item['id']==st_id for item in valid_subtasks):
            raise ValueError('规划包含重复子任务ID，拒绝覆盖已有子任务')
        target_agent = st.get('agent', '')
        if target_agent in ('deepseek', 'api'):
            target_agent = 'custom'
        elif target_agent == 'deepseek_harness':
            target_agent = 'dsh'
        if target_agent not in normalized_order or target_agent == master_provider:
            target_agent = collaborators[0]
        chosen_model, chosen_effort = validate_model_choice(routing_catalog, target_agent,
            st.get('model') or '', st.get('effort') or '')

        target_files = [str(f).replace('\\', '/').strip() for f in st.get('target_files', []) if f]
        depends_on = list(st.get('depends_on', []))

        # Enforce no concurrent edits to the same file
        for tf in target_files:
            from tool_registry import validate_path_in_bounds
            computer=(profile or {}).get('computer_tools') or {}
            tf=str(validate_path_in_bounds(operation_directory / tf, operation_directory, computer)).casefold()
            if tf in file_writers and file_writers[tf] != st_id:
                prev_id = file_writers[tf]
                if prev_id not in depends_on:
                    depends_on.append(prev_id)
            file_writers[tf] = st_id

        valid_subtasks.append({
            "id": st_id,
            "title": st.get('title') or f"子任务 {idx+1}",
            "agent": target_agent,
            "model": chosen_model,
            "effort": chosen_effort,
            "role": st.get('role', '专项专家'),
            "task_type": st.get('task_type', 'analysis'),
            "required_capability": st.get('required_capability', 'code_implementation'),
            "target_files": target_files,
            "depends_on": depends_on,
            "subtask_prompt": st.get('subtask_prompt', prompt),
        })

    # ================= Stage 2, 3, 4, 5: Dispatcher, Executor, Verifier & Recovery =================
    if on_step:
        on_step(f"⚡ Dispatcher: 调度 {len(valid_subtasks)} 个具备依赖约束的子任务…")

    completed_tasks: dict[str, dict[str, Any]] = {
        sid: result for sid,result in saved_checkpoint.get('subtask_results',{}).items()
        if result.get('verified') and result.get('success') and sid in {st['id'] for st in valid_subtasks}}
    pending_tasks = {st['id']: st for st in valid_subtasks if st['id'] not in completed_tasks}
    if execution:
        with execution.manager._db_lock:
            checkpoint=execution.manager.get_task(execution.task_id).checkpoint
            checkpoint['orchestration_plan']=valid_subtasks
            execution.manager.save_checkpoint(execution.task_id,checkpoint)

    from task_runtime import current_execution
    execution = current_execution.get()
    MAX_VERIFY_RETRIES = min(2, execution.manager.get_task(execution.task_id).max_retries) if execution else 2
    operation_directory = execution.workspace() if execution else directory

    # Supervisor mode (Mode B): inside a task, the dispatch is recorded as one mailbox thread.
    collab = None
    if execution:
        from agent_messaging import Collaboration, render_peer_findings
        collab = Collaboration.open(owner_id, execution.task_id, saved_checkpoint.get('collaboration_thread', ''))

    def _remember_thread():
        if not (collab and collab.thread_id and execution):
            return
        with execution.manager._db_lock:
            checkpoint = execution.manager.get_task(execution.task_id).checkpoint
            if checkpoint.get('collaboration_thread') != collab.thread_id:
                checkpoint['collaboration_thread'] = collab.thread_id
                execution.manager.save_checkpoint(execution.task_id, checkpoint)

    def _relative(path: Path) -> str:
        try:
            return path.resolve().relative_to(Path(operation_directory).resolve()).as_posix()
        except ValueError:
            return path.name

    def _execute_single_subtask(subtask_info: dict[str, Any]) -> dict[str, Any]:
        st_id = subtask_info['id']
        target = subtask_info['agent']
        role = subtask_info.get('role', '专项专家')
        st_prompt = subtask_info.get('subtask_prompt', prompt)
        task_type = subtask_info.get('task_type', 'analysis')
        target_files = subtask_info.get('target_files', [])
        name = get_agent_name(target)
        spec = AGENT_SPECS.get(target, {})

        # Contextual injection from completed dependency tasks
        dep_context_parts = []
        for dep_id in subtask_info.get('depends_on', []):
            if dep_id in completed_tasks:
                dep_res = completed_tasks[dep_id]
                dep_context_parts.append(f"【上游前置任务成果 ({dep_res['name']} - {dep_res['role']})】:\n{dep_res['output'][:800]}")
        dep_context_str = "\n\n".join(dep_context_parts)
        peer_block = render_peer_findings(collab.peer_findings(
            exclude=set(subtask_info.get('depends_on', [])) | {st_id})) if collab else ''
        handoff_id, handed_to = '', None

        # Snapshot workspace directory to detect modified files
        before_snap = take_dir_snapshot(operation_directory)

        attempt = 0
        current_prompt = st_prompt
        last_out = ""
        last_error = None
        verified = False
        verify_note = ""
        modified_files: list[Path] = []

        while attempt <= MAX_VERIFY_RETRIES:
            attempt += 1
            if collab and target != handed_to:
                if handoff_id:  # failover: the previous worker's attempt stays in the thread as a finding
                    collab.result(handoff_id, handed_to, str(last_error) if last_error else last_out,
                                  verified=False, note=f'handed over to {target}')
                handoff_id = collab.handoff(st_id, subtask_info.get('title', st_id), target, st_prompt)
                handed_to = target
                _remember_thread()
            is_claude = (target == 'claude')
            if is_claude:
                full_worker_prompt = build_claude_english_handover(
                    source_agent=master_provider,
                    trigger_reason=f"Multi-Agent Collaborative Task Assignment: {role}",
                    user_task=prompt,
                    context_or_subtask=(dep_context_str + "\n\n" if dep_context_str else "")
                    + (peer_block + "\n\n" if peer_block else "") + st_prompt,
                    task_id=st_id,
                )
            else:
                full_worker_prompt = (
                    f"【多智能体协同专项任务 ({st_id})】\n"
                    f"你是协同智能体【{name}】（角色：{role}），你的专长为：{spec.get('specialties')}。\n"
                    f"任务类型：{task_type}（{'落地写文件/执行' if task_type == 'execution' else '纯文本研究分析'}）。\n"
                    + (f"【涉及文件】：{', '.join(target_files)}\n" if target_files else "")
                    + (f"\n{dep_context_str}\n\n" if dep_context_str else "")
                    + (f"\n{peer_block}\n\n" if peer_block else "")
                    + f"【专项任务内容】：\n{st_prompt}\n\n"
                    + (f"【原始全局用户任务背景】：\n{prompt}\n\n"
                       if prompt.strip() and prompt.strip() not in st_prompt else '')
                    + "【输出要求】：请直接给出高质量专业交付成果。若为代码实现，请完整编写并落盘。"
                )
            if current_prompt!=st_prompt:
                full_worker_prompt+='\n'+current_prompt[len(st_prompt):]

            try:
                worker_profile = selected_profile(profile or {}, target,
                    subtask_info.get('model', '') if target == subtask_info['agent'] else '',
                    subtask_info.get('effort', '') if target == subtask_info['agent'] else '')
                out, _ = call_single_agent(target, full_worker_prompt, directory, worker_profile, ephemeral=False,
                                           raise_on_error=True,session_key='subtask:'+st_id)
                last_out = out
                last_error = None
            except (InterruptedError, TimeoutError):
                raise
            except AgentError as err:
                last_error = err
                # Stage 5 Recovery A: Quota exhausted / Rate limit -> Failover handover
                if err.is_quota_or_rate_limit():
                    fail_msg = f"智能体 [{name}] 额度不足或频控限制 ({err.error_type.value})"
                    alt_agent = find_capable_agent(subtask_info.get('required_capability', 'code_implementation'), normalized_order, exclude=target)
                    if alt_agent and alt_agent != target:
                        alt_name = get_agent_name(alt_agent)
                        handover_traces.append(f"⚠️ {fail_msg}，子任务 ({st_id}) 自动交接至 [{alt_name}] 重新执行")
                        target = alt_agent
                        name = alt_name
                        spec = AGENT_SPECS.get(target, {})
                        continue
                last_out = f"（执行异常：{err}）"
                break
            except Exception as exc:
                last_error = exc
                last_out = f"（执行异常：{exc}）"
                break

            # Stage 5 Recovery B: Capability refusal
            refusal_detected, needed_cap = is_capability_refusal(last_out)
            if refusal_detected:
                cap_name = '网络检索' if needed_cap == 'web_search' else '系统终端与文件操作' if needed_cap in ('terminal', 'file_write') else '专项计算'
                handover_traces.append(f"🔄 能力自省：智能体 [{name}] 缺乏 [{cap_name}] 能力，正在调度接替智能体…")
                alt_agent = find_capable_agent(needed_cap, normalized_order, exclude=target)
                if alt_agent and alt_agent != target:
                    alt_name = get_agent_name(alt_agent)
                    handover_traces.append(f"↪️ 子任务 ({st_id}) 转交由具备该能力的智能体 [{alt_name}] 执行")
                    target = alt_agent
                    name = alt_name
                    spec = AGENT_SPECS.get(target, {})
                    continue

            # Stage 4: Verifier (ok, refusesuccess)
            modified_files = detect_modified_files(operation_directory, before_snap)
            if task_type == 'execution':
                # Check target files existence
                missing_files = []
                for tf in target_files:
                    target_path = operation_directory / tf
                    if not target_path.exists() or target_path.stat().st_size == 0:
                        missing_files.append(tf)

                if not modified_files and not target_files:
                    missing_files.append('没有目标文件或文件变动证据')
                if missing_files:
                    verified = False
                    verify_note = f"预期落地文件未在工作区找到或为空：{', '.join(missing_files)}"
                else:
                    # Check python syntax on touched files
                    syntax_ok, syntax_msg = check_python_syntax(modified_files)
                    if not syntax_ok:
                        verified = False
                        verify_note = syntax_msg
                    else:
                        verified = True
                        verify_note = f"落地文件验证成功（{len(modified_files)} 个文件更新，语法通过）"
            else:
                # Analysis task: verify substantive deliverable
                if len(last_out.strip()) >= 20 and not last_out.startswith(" ["):
                    verified = True
                    verify_note = f"研究分析交付验证通过（{len(last_out)} 字）"
                else:
                    verified = False
                    verify_note = "交付内容过短或无实质产出"

            if verified or attempt > MAX_VERIFY_RETRIES:
                break

            # If not verified and retries remain, feed feedback back to the agent
            handover_traces.append(f"🔍 子任务 ({st_id}) 验证未通过（{verify_note}），触发反馈修复（重试 {attempt}/{MAX_VERIFY_RETRIES}）")
            current_prompt = (
                f"{st_prompt}\n\n"
                f"【前置验证失败反馈 - 请立即修正】：\n"
                f"上次执行产出未能通过系统验证：{verify_note}。\n"
                f"请检查实现逻辑，确保完整输出并修正上述问题。"
            )

        if attempt > 1 and verified and mem_store:
            try:
                from task_runtime import current_execution
                _curr_exec = current_execution.get()
                _tid = _curr_exec.task_id if _curr_exec else ""
                _gid = ""
                if _curr_exec and _curr_exec.manager:
                    _t = _curr_exec.manager.get_task(_tid)
                    if _t and _t.checkpoint:
                        _gid = _t.checkpoint.get('goal_id', '')
                mem_store.record_failure_experience(
                    task_category=task_type,
                    failed_action=subtask_info.get('title', st_id),
                    root_cause=verify_note,
                    solution=last_out[:300],
                    owner_id=owner_id,
                    task_id=_tid,
                    goal_id=_gid,
                    project_id=_t.checkpoint.get('workspace_id', 'default'),
                )
            except Exception:
                pass

        if collab:
            collab.result(handoff_id, target, last_out, verified=verified and last_error is None,
                          note=verify_note or str(last_error or ''),
                          artifacts=[_relative(f) for f in modified_files])

        return {
            'id': st_id,
            'agent': target,
            'model': subtask_info.get('model', '') if target == subtask_info['agent'] else '',
            'effort': subtask_info.get('effort', '') if target == subtask_info['agent'] else '',
            'name': name,
            'role': role,
            'prompt': st_prompt,
            'output': last_out,
            'modified_files': [f.name for f in modified_files],
            'verified': verified,
            'verify_note': verify_note,
            'is_claude': (target == 'claude'),
            'success': (last_error is None and verified),
        }

    # Cycle detection for subtask dependencies
    valid_subtasks, cycle_detected = detect_and_break_dependency_cycles(valid_subtasks)
    if cycle_detected:
        handover_traces.append("⚠️ 检测到子任务间存在循环依赖环，自动重置循环边以保证拓扑执行顺序")
        logger.warning("Cycle detected in subtasks; cyclic edges safely broken.")

    # Dependency-aware concurrent dispatch loop
    while pending_tasks:
        # 1. First, check for tasks whose prerequisites failed -> mark as BLOCKED immediately
        blocked_found = False
        for st_id, st in list(pending_tasks.items()):
            prereq_blocked, failing_dep = is_subtask_blocked(st, completed_tasks)
            if prereq_blocked:
                completed_tasks[st_id] = {
                    'id': st_id,
                    'agent': st.get('agent', 'unknown'),
                    'name': get_agent_name(st.get('agent', '')),
                    'role': st.get('role', '专项专家'),
                    'prompt': st.get('subtask_prompt', ''),
                    'output': f"【BLOCKED 阻断中止】：前置依赖任务 [{failing_dep}] 执行失败或未通过验收，依据数据完整性与安全隔离策略拒绝继续执行本子任务。",
                    'modified_files': [],
                    'verified': False,
                    'verify_note': f"Prerequisite task [{failing_dep}] ",
                    'status': 'BLOCKED',
                    'is_claude': (st.get('agent') == 'claude'),
                    'success': False,
                }
                del pending_tasks[st_id]
                blocked_found = True

        if blocked_found:
            continue

        # 2. Find all tasks whose dependencies are completely and successfully satisfied
        ready_tasks = [
            st for st in pending_tasks.values()
            if all(dep_id in completed_tasks and completed_tasks[dep_id].get('success', False) for dep_id in st.get('depends_on', []))
        ]

        if not ready_tasks:
            # If no tasks are ready but pending remain, break deadlock on the first pending task
            ready_tasks = list(pending_tasks.values())[:1]

        # Dispatch ready tasks in parallel
        from contextvars import copy_context
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ready_tasks), 4)) as executor:
            future_to_id = {executor.submit(copy_context().run, _execute_single_subtask, st): st['id'] for st in ready_tasks}
            for future in concurrent.futures.as_completed(future_to_id):
                st_id = future_to_id[future]
                try:
                    res = future.result()
                    completed_tasks[st_id] = res
                    if execution:
                        execution.check()
                        if res.get('verified') and res.get('success'):
                            with execution.manager._db_lock:
                                checkpoint = execution.manager.get_task(execution.task_id).checkpoint
                                completed = checkpoint.setdefault('completed_steps', [])
                                if st_id not in completed:
                                    completed.append(st_id)
                                checkpoint.setdefault('subtask_results', {})[st_id] = res
                                execution.manager.save_checkpoint(execution.task_id, checkpoint)
                except Exception as exc:
                    logger.error(f"Task {st_id} execution failed: {exc}")
                    completed_tasks[st_id] = {
                        'id': st_id, 'agent': 'unknown', 'name': '智能体', 'role': '专家',
                        'prompt': '', 'output': f"（执行异常：{exc}）", 'modified_files': [],
                        'verified': False, 'verify_note': str(exc), 'is_claude': False, 'success': False
                    }
                del pending_tasks[st_id]

    results = list(completed_tasks.values())

    # ================= Stage 6: Reporter () =================
    if on_step:
        on_step(f"👑 主智能体 [{master_name}] 正在汇总审查各方成果并统筹最终答复…")

    collaborators_reports = []
    thoughts_summary = []
    for r in results:
        w_name = r['name']
        w_role = r['role']
        w_out = r['output']
        v_status = "✅ 验收通过" if r.get('verified') else f"⚠️ 验收提示: {r.get('verify_note')}"
        mod_info = f" [修改文件: {', '.join(r['modified_files'])}]" if r.get('modified_files') else ""
        tag = " [英文协议对接]" if r.get('is_claude') else ""
        collaborators_reports.append(
            f"### 协同智能体成果：{w_name}（{w_role}）{tag}\n"
            f"**分配任务**：{_without_duplicate_mission(r['prompt'],prompt)}\n"
            f"**验证状态**：{v_status}{mod_info}\n\n"
            f"**成果输出**：\n{w_out}\n"
        )
        thoughts_summary.append(
            f"  ├─ ⚡ 协同智能体 [{w_name}]（{w_role}）{tag}：完成专项任务（产出 {len(w_out)} 字，{v_status}{mod_info}）"
        )

    all_reports_str = "\n\n".join(collaborators_reports)

    # Master synthesis prompt
    if master_provider == 'claude':
        synthesize_prompt = (
            f"[MULTI-AGENT MASTER SYNTHESIS -> CLAUDE CODE]\n"
            f"Original User Request:\n{prompt}\n\n"
            f"Collaborative Agents Deliverables & Verification:\n{all_reports_str}\n\n"
            f"Directive:\n"
            f"As Master Agent, thoroughly synthesize, verify, and consolidate all findings.\n"
            f"Deliver the final, comprehensive, and authoritative response fulfilling the user's intent in natural Simplified Chinese.\n"
            f"CRITICAL RULE: Directly output the final answer body. Do not include meta-process chatter."
        )
    else:
        synthesize_prompt = (
            f"你是多智能体协同网络的主智能体（Master Agent: {master_name}）。\n"
            f"针对用户的以下原始任务需求：\n"
            f"【原始任务】\n{prompt}\n\n"
            f"你此前分配的各专业协同智能体已全部完成专项输出，成果与验证结果如下：\n"
            f"{all_reports_str}\n\n"
            f"【主智能体统筹裁决与交付指令（至关重要）】：\n"
            f"1. 充分整合、提炼与审查各协同智能体的专业成果，去除冗余，修正分歧，融会贯通为一份完整、权威、高水准的最终方案或答复。\n"
            f"2. 确保回答逻辑严密、结构清晰，直接解决用户的原始需求。\n"
            f"3. 【输出铁律】：直接输出向用户交付的最终答复正文！绝对严禁复读分配过程、严禁输出“根据协同智能体汇报”、“作为主智能体”等任何前置废话与草稿标记！"
        )

    try:
        final_reply, _ = call_single_agent(master_provider, synthesize_prompt, directory, profile, ephemeral=True, raise_on_error=True)
    except AgentError as err:
        fail_msg = f"主智能体 [{master_name}] 在最终统筹汇总时遇到异常 ({err.error_type.value})"
        final_reply = ""
        for alt in normalized_order:
            if alt != master_provider:
                alt_name = get_agent_name(alt)
                handover_traces.append(f"⚠️ {fail_msg}，统筹交付自动顺位交接至智能体 [{alt_name}]")
                try:
                    alt_reply, _ = call_single_agent(alt, synthesize_prompt, directory, profile, ephemeral=True, raise_on_error=True)
                    if alt_reply:
                        final_reply = alt_reply
                        master_provider = alt
                        master_name = alt_name
                        break
                except Exception:
                    continue

    if not final_reply:
        if execution:
            raise AgentError(AgentErrorType.TOOL_ERROR, provider=master_provider,
                             message='Synthesis returned no final reply from any agent; task not completed')
        final_reply = "（主智能体统筹完成，但未返回最终正文）"

    if collab:
        verified_count = sum(1 for r in results if r.get('verified') and r.get('success'))
        collab.note(f'Synthesis by {master_provider}: {verified_count}/{len(results)} subtasks verified; '
                    f'final reply {len(final_reply.strip())} chars.', list(collab._workers))
        if collab.thread_id:
            handover_traces.append(f"📬 协作线程 {collab.thread_id}（{collab.supervisor}）")

    # Assemble complete thought trace block
    thought_lines = [
        "<think>",
        "👥 跨多智能体协作与自主执行闭环记录",
    ]
    if handover_traces:
        for h in handover_traces:
            thought_lines.append(f"  {h}")
    thought_lines.extend([
        f"👑 主智能体 [{master_name}] 任务规划与调度：",
        f"  ├─ 任务评估：{analysis_text}",
        f"  └─ 调度方案：成功分派 {len(results)} 个具备依赖与文件锁隔离的协同子任务",
    ])
    thought_lines.extend(thoughts_summary)
    thought_lines.append(f"👑 主智能体 [{master_name}]：统筹裁决、验收确认并交付最终答复")
    thought_lines.append("</think>\n\n")

    full_output = "\n".join(thought_lines) + final_reply.strip()
    return full_output, None
