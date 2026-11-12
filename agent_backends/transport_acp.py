"""ACP / MCP stdio JSON-RPC transport."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from agent_tool_adapters import AgentAdapterError, _validate_program, _workspace
class StdioJsonRpc:
    """Minimal *client* transport for stdio ACP and MCP, with fail-closed reverse RPC.

    Persistent for the lifetime of this object. Never executes arbitrary reverse-RPC
    filesystem/terminal/approval requests; such operations require a separate
    authenticated human approval flow and sandbox outside this transport.
    """
    def __init__(self, command: list[str], workspace: str | Path, *, timeout: float = 30):
        if not isinstance(command, (tuple, list)) or not command or len(command) > 16 or any(
                not isinstance(a, str) or not a or '\0' in a or '\n' in a for a in command):
            raise AgentAdapterError('Invalid stdio command')
        self.command = [_validate_program(command[0]), *command[1:]]
        self.workspace = _workspace(workspace)
        self.timeout = timeout
        self.proc = None
        self.queue: queue.Queue = queue.Queue(maxsize=1000)
        self.events: list[dict] = []
        self.serial = 0
        self._pending = {}

    def __enter__(self):
        self.proc = subprocess.Popen(self.command, cwd=str(self.workspace), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1)
        try:
            from task_runtime import register_process
            register_process(self.proc)
        except Exception:
            self.proc.kill()
            self.proc.wait()
            raise
        def reader():
            try:
                for line in self.proc.stdout:
                    if len(line) > 2_000_000:
                        continue
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict):
                            self.queue.put(item, timeout=1)
                    except (json.JSONDecodeError, queue.Full):
                        continue
            finally:
                try:
                    self.queue.put({'_eof': True}, timeout=1)
                except queue.Full:
                    pass
        threading.Thread(target=reader, daemon=True).start()
        return self

    def __exit__(self, *_):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            for handle in (self.proc.stdin, self.proc.stdout):
                if handle:
                    handle.close()
            from task_runtime import current_execution
            execution = current_execution.get()
            if execution:
                execution.manager.unregister_process(execution.task_id, self.proc.pid)

    def _send(self, message: dict):
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise AgentAdapterError('Agent stdio subprocess unavailable')
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> object:
        self.serial += 1
        rid = self.serial
        self._send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params or {}})
        limit = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < limit:
            from task_runtime import current_execution
            execution = current_execution.get()
            if execution:
                execution.check()
            if rid in self._pending:
                packet = self._pending.pop(rid)
            else:
                try:
                    packet = self.queue.get(timeout=min(0.25, max(0.001, limit - time.monotonic())))
                except queue.Empty:
                    continue
            if packet.get('_eof'):
                raise AgentAdapterError(f'{method}: subprocess closed unexpectedly')
            if packet.get('id') is not None and packet.get('method'):
                # Never auto-approve reverse permissions or grant host file access.
                self._send({'jsonrpc': '2.0', 'id': packet['id'],
                            'error': {'code': -32601, 'message': 'Host permission not granted'}})
                continue
            if packet.get('id') == rid:
                if 'error' in packet:
                    # Avoid echoing arbitrary remote error details / credentials.
                    raise AgentAdapterError(f'{method} rejected by agent')
                return packet.get('result')
            if packet.get('id') is not None:
                self._pending[packet['id']] = packet
            elif len(self.events) < 200:
                self.events.append(packet)
        raise AgentAdapterError(f'{method}: response timeout')


def run_acp(command: list[str], instruction: str, workspace: str | Path, *,
            timeout: float = 600, protocol_version: int = 1, session_id: str = '') -> dict:
    if not 1 <= timeout <= 3600 or protocol_version not in (1, 2):
        raise AgentAdapterError('Invalid ACP options')
    if not instruction.strip() or len(instruction) > 50000:
        raise AgentAdapterError('Invalid instruction')
    with StdioJsonRpc(command, workspace, timeout=20) as rpc:
        initialization = ({'protocolVersion': 2, 'capabilities': {},
                           'info': {'name': 'Open QQ Dot', 'title': 'Open QQ Dot', 'version': '1'}}
                          if protocol_version == 2 else
                          {'protocolVersion': 1, 'clientCapabilities': {}})
        init = rpc.request('initialize', initialization)
        if not isinstance(init, dict):
            raise AgentAdapterError('ACP initialize returned no capabilities')
        if session_id:
            # Prefer lightweight resume. Fail closed if the agent doesn't support it.
            sess = rpc.request('session/resume', {'sessionId': session_id, 'cwd': str(_workspace(workspace)),
                                                  'mcpServers': []})
            sid = session_id
        else:
            sess = rpc.request('session/new', {'cwd': str(_workspace(workspace)), 'mcpServers': []})
            if not isinstance(sess, dict) or not isinstance(sess.get('sessionId'), str):
                raise AgentAdapterError('ACP session/new did not provide sessionId')
            sid = sess['sessionId']
        response = rpc.request('session/prompt', {'sessionId': sid,
                                                'prompt': [{'type': 'text', 'text': instruction}]}, timeout=timeout)
        if not isinstance(response, dict) or 'stopReason' not in response:
            raise AgentAdapterError('ACP prompt did not complete normally')
        if response['stopReason'] not in ('end_turn', 'endTurn', 'completed'):
            raise AgentAdapterError('ACP prompt stopped without normal completion')
        chunks = []
        for event in rpc.events:
            params = event.get('params') or {}
            if event.get('method') != 'session/update' or params.get('sessionId') != sid:
                continue
            update = params.get('update') or {}
            if update.get('sessionUpdate') == 'agent_message_chunk':
                content = update.get('content') or {}
                if content.get('type') == 'text' and isinstance(content.get('text'), str):
                    chunks.append(content['text'])
        output = ''.join(chunks).strip()
        if not output:
            # Some transports only signal completion, no final text; don't fabricate one.
            raise AgentAdapterError('ACP finished but did not stream an agent text response')
        return {'output': output, 'session_id': sid, 'backend': 'acp',
                'transport': 'acp', 'verified': False, 'tokens_used': 0}


