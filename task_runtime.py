"""Execution-local cancellation, process ownership and call accounting glue."""
from contextvars import ContextVar
import json
from pathlib import Path
import subprocess
import time

current_execution = ContextVar('open_qq_dot_execution', default=None)
call_reservation = ContextVar('open_qq_dot_call_reservation', default=0)


class Execution:
    def __init__(self, manager, task_id, run_id, event):
        self.manager, self.task_id, self.run_id, self.event = manager, task_id, run_id, event
    @property
    def execution_id(self) -> str:
        return f"exec_{self.task_id}_{self.run_id}"

    @property
    def approval_scope(self) -> str:
        return "task"

    @property
    def budget(self) -> dict:
        task = self.manager.get_task(self.task_id) if self.manager else None
        if not task:
            return {"token_budget": 0, "max_calls": 100, "cost_budget": 0.0}
        return {
            "token_budget": getattr(task, "token_budget", 0),
            "max_calls": getattr(task, "max_calls", 100),
            "cost_budget": getattr(task, "cost_budget", 0.0),
            "consumed_tokens": getattr(task, "consumed_tokens", 0),
            "call_count": getattr(task, "call_count", 0),
        }

    @property
    def owner_id(self) -> str:
        task = self.manager.get_task(self.task_id)
        return task.owner_id if task else ""

    @property
    def cancel_event(self):
        return self.event

    def effective_permissions(self) -> dict:
        """Fetch and unify current task effective permissions across tools and adapters."""
        task = self.manager.get_task(self.task_id)
        if not task:
            return {"enabled": False, "commands": False, "full_computer_access": False, "read_only": True}
        owner = task.owner_id
        if owner.startswith("system:"):
            return {"enabled": True, "commands": False, "full_computer_access": False, "read_only": False, "is_system": True}
        friend_dir = self.friend_directory()
        cfg_file = friend_dir / "friend.json"
        if cfg_file.is_file():
            try:
                data = json.loads(cfg_file.read_text("utf-8"))
                ct = data.get("computer_tools", {})
                return {
                    "enabled": bool(ct.get("enabled", False)),
                    "commands": bool(ct.get("commands", False)),
                    "full_computer_access": bool(ct.get("full_computer_access", False)),
                    "read_only": bool(ct.get("read_only", False) or ct.get('mode') == 'read_only'),
                    "is_admin": __import__("trusted_admin_policy").is_admin_user(owner),
                }
            except Exception:
                pass
        return {"enabled": False, "commands": False, "full_computer_access": False, "read_only": True}

    def check(self):
        task = self.manager.get_task(self.task_id)
        if self.event.is_set() or self.manager._stop_event.is_set() or not task or task.run_id != self.run_id or task.status not in ('RUNNING', 'VERIFYING'):
            raise InterruptedError('Task is stopped or run lease expired; external call returned and will not continue')
        if not self.manager.goal_allows_task(self.task_id):
            self.wait('所属目标已暂停、停止或达到截止时间')
        if task.max_duration_seconds > 0 and task.elapsed_seconds + max(0, time.time()-task.run_started_at) >= task.max_duration_seconds:
            raise TimeoutError('任务执行时间预算耗尽')
        if task.token_budget > 0 and task.consumed_tokens + task.estimated_tokens >= task.token_budget:
            self.wait('Token 预算耗尽（真实用量与估算用量分别记录）')

    def wait(self, reason):
        from task_manager import TaskStatus
        self.manager.update_task_status(self.task_id, TaskStatus.WAITING_RESOURCE, error=reason, run_id=self.run_id)
        self.event.set()
        self.manager.add_log(self.task_id, 'Budget', reason+'；检查点保留')
        raise InterruptedError(reason)

    def before_call(self, prompt):
        self.check()
        with self.manager._db_lock, self.manager._get_connection() as db:
            task = self.manager.get_task(self.task_id)
            if task.cost_budget > 0:
                self.wait('当前后端没有可靠货币计价；不能承诺 cost_budget 硬上限，请配置可计价适配器或调整预算')
            if task.call_count >= task.max_calls:
                self.wait('模型调用次数预算耗尽')
            estimate = max(1, len(str(prompt).encode('utf-8'))//3)
            if task.token_budget and task.consumed_tokens+task.estimated_tokens+estimate >= task.token_budget:
                self.wait('下一次调用的输入估算将耗尽 Token 预算；未发起请求')
            call_reservation.set(estimate)
            db.execute('UPDATE tasks SET call_count=call_count+1,estimated_tokens=estimated_tokens+? WHERE task_id=? AND run_id=?',
                       (estimate, self.task_id, self.run_id))

    def after_call(self, output, usage=None):
        reserved = call_reservation.get()
        call_reservation.set(0)
        actual = usage_tokens(usage)
        with self.manager._db_lock, self.manager._get_connection() as db:
            if actual is not None:
                db.execute('UPDATE tasks SET estimated_tokens=max(0,estimated_tokens-?),consumed_tokens=consumed_tokens+? WHERE task_id=? AND run_id=?',
                           (reserved,actual,self.task_id,self.run_id))
            else:
                db.execute('UPDATE tasks SET estimated_tokens=estimated_tokens+? WHERE task_id=? AND run_id=?',
                           (max(0,len(str(output).encode('utf-8'))//3),self.task_id,self.run_id))
        self.check()

    def friend_directory(self):
        task = self.manager.get_task(self.task_id)
        registry = self.manager.workspace / 'friends.json'
        if task.owner_id.startswith('system:') or task.owner_id == 'default':
            return self.manager.workspace
        if registry.is_file():
            record = json.loads(registry.read_text('utf-8')).get('friends', {}).get(task.owner_id)
            if record and 'directory' in record:
                return Path(record['directory']).resolve()
        # Sanitize owner_id to prevent directory traversal
        import re
        raw_owner = str(task.owner_id).strip()
        if not raw_owner or '..' in raw_owner or '/' in raw_owner or '\\' in raw_owner:
            raise PermissionError(f"Invalid task owner identifier, path traversal forbidden: {raw_owner}")
        safe_owner = re.sub(r'[^a-zA-Z0-9_\-\:]', '_', raw_owner)

        # Fallback: check convention directory or isolated user workspace
        base_friends = (self.manager.workspace / 'friends').resolve()
        user_dir = (base_friends / safe_owner).resolve()
        if not str(user_dir).startswith(str(base_friends)):
            raise PermissionError(f"Task owner workspace directory boundary exceeded: {user_dir}")
        if user_dir.exists():
            return user_dir
        # Fallback for standalone/test environments without explicit friend binding
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def selected_workspace(self):
        """The project folder chosen for this task (workbench / task center), or None for the friend's default."""
        task = self.manager.get_task(self.task_id)
        if task and task.checkpoint.get('input_source') == 'workbench':
            workspace_id = task.checkpoint.get('workspace_id', 'default')
            if workspace_id != 'default':
                from session_manager import list_workspaces
                item = next((item for item in list_workspaces(task.owner_id) if item['id'] == workspace_id), None)
                if item is None or not Path(item['path']).is_dir():
                    raise PermissionError('任务所属项目目录已经失效')
                return Path(item['path']).resolve()
        return None

    def workspace(self):
        from workspace_tools import workspace
        selected = self.selected_workspace()
        if selected:
            return selected
        directory = self.friend_directory()
        return workspace(directory) if (directory / 'friend.json').is_file() else directory


# Task categories that work on files, commands, data and pages. Their model calls get the work tools only: the
# phone, chat-log and memory tools of a friend chat were 7 of the 13 tool definitions (about half of a 4.7k-char
# code-task prompt) and were sent again on every tool round.
WORK_CATEGORIES = frozenset({'code', 'file', 'data', 'browser', 'research'})
CHAT_ONLY_TOOLS = frozenset({'search_friend_memory', 'get_today_chat'})


def work_task():
    """True inside a work-category task, or in a tool server its launcher marked with QQ_TASK_KIND=work."""
    import os
    if os.environ.get('QQ_TASK_KIND') == 'work':
        return True
    execution = current_execution.get()
    task = execution.manager.get_task(execution.task_id) if execution and execution.manager else None
    return getattr(task, 'category', None) in WORK_CATEGORIES


# The shared study library's tools (about 1.9k characters of definitions) are shown to the model only in tasks made
# from a study template; QQ chat replies and other tasks would carry them unused. The desktop and phone study pages
# still call them directly.
STUDY_TOOLS = frozenset({'study_read', 'study_write'})


def study_task():
    """True inside a task from a study_* template (goal stages inherit it), or a tool server marked QQ_STUDY_TASK=1."""
    import os
    if os.environ.get('QQ_STUDY_TASK') == '1':
        return True
    execution = current_execution.get()
    task = execution.manager.get_task(execution.task_id) if execution and execution.manager else None
    return str((getattr(task, 'checkpoint', None) or {}).get('template', '')).startswith('study_')


def register_process(process):
    execution = current_execution.get()
    if execution:
        try:
            execution.check()
            execution.manager.register_process(execution.task_id, process.pid)
        except BaseException:
            # This freshly created Popen is ours even if cancellation won registration.
            process.kill()
            process.wait()
            raise


def run_process(*args, **kwargs):
    """subprocess.run equivalent for task-owned CLI calls; preserve normal call path."""
    # The app has no console, so on Windows every console child (agy, a test command through cmd) would open
    # its own window on the owner's desktop.
    kwargs.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    execution = current_execution.get()
    input_data = kwargs.pop('input', None)
    timeout = kwargs.pop('timeout', None)
    check = kwargs.pop('check', False)
    if kwargs.pop('capture_output', False):
        if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:
            raise ValueError('stdout and stderr arguments may not be used with capture_output')
        kwargs['stdout'] = kwargs['stderr'] = subprocess.PIPE
    if input_data is not None:
        if kwargs.get('stdin') is not None:
            raise ValueError('stdin and input arguments may not both be used')
        kwargs['stdin'] = subprocess.PIPE
    if execution:
        execution.check()
    with subprocess.Popen(*args, **kwargs) as process:
        register_process(process)
        started = time.monotonic()
        try:
            while True:
                if execution:
                    execution.check()
                    execution.manager.refresh_processes(execution.task_id)
                try:
                    stdout, stderr = process.communicate(input_data, timeout=.2 if execution else timeout)
                    break
                except subprocess.TimeoutExpired:
                    input_data = None
                    if not execution:
                        raise
                    if timeout is not None and time.monotonic()-started >= timeout:
                        raise subprocess.TimeoutExpired(args[0], timeout)
            if execution:
                execution.check()
            result = subprocess.CompletedProcess(args[0], process.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        finally:
            if process.poll() is None:
                if execution:
                    with execution.manager._active_lock:
                        execution.manager._terminate_owned_processes(execution.task_id)
                else:
                    # A .cmd launcher can leave its native agent holding the output pipes.
                    import psutil
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        children = []
                    for child in reversed(children):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    process.kill()
                    process.communicate()
            if execution:
                execution.manager.unregister_process(execution.task_id, process.pid)


def usage_tokens(usage):
    if not isinstance(usage, dict) or not usage:
        return None
    try:
        if 'total_tokens' in usage:
            return max(0,int(usage['total_tokens']))
        if not any(k in usage for k in ('input_tokens','prompt_tokens','output_tokens','completion_tokens')):
            return None
        return sum(max(0,int(usage.get(k,0))) for k in (
            'input_tokens' if 'input_tokens' in usage else 'prompt_tokens',
            'output_tokens' if 'output_tokens' in usage else 'completion_tokens',
            'cache_read_input_tokens','cache_creation_input_tokens'))
    except (TypeError,ValueError):
        return None


def reported_usage(stream):
    usage = None
    for line in stream.splitlines():
        try:
            event=json.loads(line)
        except (ValueError,TypeError):
            continue
        if event.get('type') in ('result','turn.completed') or event.get('event')=='result':
            body=event.get('result') if isinstance(event.get('result'),dict) else event
            if isinstance(body.get('usage'),dict):
                usage=body['usage']
    return usage
