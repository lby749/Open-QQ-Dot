"""Official non-interactive CLI transport (Cursor / Qwen)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_tool_adapters import AgentAdapterError, BUILTINS, _validate_program, _workspace
def _parse_output(stdout: str, backend: str) -> tuple[str, str, int]:
    """Extract actual provider result, fail closed on error envelopes."""
    raw = stdout.strip()
    if not raw:
        raise AgentAdapterError(f'{backend} exited without a result')
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # Some CLIs emit plain text in older versions. Do not mistake a crash for success.
        if raw.startswith('{') or raw.startswith('['):
            raise AgentAdapterError('Invalid JSON result from agent') from None
        return raw, '', 0
    if isinstance(obj, list):
        errors = [e for e in obj if isinstance(e, dict) and e.get('type') == 'result' and e.get('is_error')]
        if errors:
            raise AgentAdapterError('Qwen Code returned an error result')
        results = [e for e in obj if isinstance(e, dict) and e.get('type') == 'result']
        if not results:
            raise AgentAdapterError('Agent returned no final result event')
        obj = results[-1]
    if not isinstance(obj, dict) or obj.get('is_error') is True or obj.get('subtype') in ('error', 'failure'):
        raise AgentAdapterError('Agent returned an error envelope')
    text = obj.get('result') or obj.get('text') or obj.get('output')
    if not isinstance(text, str) or not text.strip():
        raise AgentAdapterError('Agent returned no final text')
    usage = obj.get('usage') or {}
    tokens = 0
    if isinstance(usage, dict):
        for key in ('total_tokens', 'totalTokens'):
            if type(usage.get(key)) is int:
                tokens = usage[key]
                break
        if not tokens:
            tokens = sum(usage.get(k, 0) for k in ('input_tokens', 'output_tokens') if type(usage.get(k)) is int)
    return text, str(obj.get('session_id') or obj.get('sessionId') or ''), tokens


def run_headless(backend: str, instruction: str, workspace: str | Path, *,
                 timeout: float = 600, allow_write: bool = False, command: list[str] | None = None,
                 model: str = '') -> dict:
    """Official non-interactive CLI, bounded execution, no shell, no YOLO defaults."""
    if backend not in ('cursor', 'qwen_code'):
        raise AgentAdapterError('Unsupported headless backend')
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 50000:
        raise AgentAdapterError('Invalid instruction')
    cwd = _workspace(workspace)
    if not 1 <= timeout <= 3600:
        raise AgentAdapterError('Invalid execution timeout')
    try:
        program = _validate_program(command[0] if command else BUILTINS[backend].executable)
    except AgentAdapterError:
        if backend != 'cursor' or command:
            raise
        program = _validate_program('cursor-agent')
    model_args = ['--model', model] if model else []
    if backend == 'cursor':
        # Cursor -p accepts the prompt as argv, and --force explicitly allows edits.
        # Never use --force unless TaskManager authorized code writes.
        argv = [program, *(command[1:] if command else []), *model_args,
                '-p', instruction, '--output-format', 'json']
        if allow_write:
            argv.append('--force')
        data = None
    else:
        # Qwen supports prompt via stdin; avoids exposing prompts in process argv.
        argv = [program, *(command[1:] if command else []), *model_args, '--output-format', 'json']
        data = instruction
    try:
        from task_runtime import run_process
        run = run_process(argv, input=data, cwd=str(cwd), capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AgentAdapterError(f'{backend} exceeded its time limit') from exc
    if run.returncode != 0:
        # stderr can contain secrets or the original prompt; do not forward it to QQ/logs.
        raise AgentAdapterError(f'{backend} exited with code {run.returncode}; inspect provider logs locally')
    output, session, tokens = _parse_output(run.stdout, backend)
    return {'output': output, 'session_id': session, 'tokens_used': tokens,
            'backend': backend, 'verified': False, 'transport': 'headless'}


OPENCODE_DEFAULT_URL = 'http://127.0.0.1:4096'


