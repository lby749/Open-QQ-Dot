"""Shared call preparation for the separate agent backend modules."""
from __future__ import annotations

from pathlib import Path


def prepare(profile, directory, backend, source, record_input=None):
    from agent_tool_adapters import require_native_permissions, validate_command
    import workspace_tools
    from task_runtime import current_execution

    require_native_permissions(profile)
    config = dict((profile.get('agent_adapters') or {}).get(backend) or {})
    command = config.get('command')
    if command is not None:
        validate_command(command)
    execution = current_execution.get()
    workspace = (execution.workspace() if execution else
                 (profile.get('computer_tools') or {}).get('project_path') or
                 workspace_tools.workspace(directory))
    Path(workspace).mkdir(parents=True, exist_ok=True)
    if record_input:
        record_input(source, {'provider': backend, 'input_scope': 'initial_user_message.text',
                              'tool_followups_recorded': False})
    return config, workspace, execution


def finish(execution, result):
    if execution:
        execution.check()
    return result['output'], result.get('session_id') or None
