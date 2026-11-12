"""OpenCode loopback HTTP transport (v2 API first, v1 fallback)."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from agent_tool_adapters import AgentAdapterError, _workspace

OPENCODE_DEFAULT_URL = 'http://127.0.0.1:4096'
def _loopback_url(url: str) -> str:
    """Accept only http://127.0.0.1:<port>, without credentials, path, query or fragment."""
    p = urlsplit(url.rstrip('/'))
    if (p.scheme != 'http' or p.hostname != '127.0.0.1' or p.username or p.password or
            p.path or p.query or p.fragment or not p.port or not (1 <= p.port <= 65535)):
        raise AgentAdapterError('OpenCode server must use http://127.0.0.1:<port>')
    return url.rstrip('/')


def _opencode_cli() -> str:
    """The installed OpenCode CLI: on PATH, or bundled with the desktop app."""
    found = shutil.which('opencode')
    if found:
        return found
    local = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / '@opencodedesktop' / 'resources'
    candidates = [local / 'opencode-cli.exe']
    candidates += sorted((Path(os.environ.get('APPDATA', '')) / 'ai.opencode.desktop' / 'cli').glob(
        '*/opencode-cli.exe'), reverse=True)
    return next((str(path) for path in candidates if path.is_file()), '')


def opencode_discovery(*, start_service=False) -> tuple[str, str]:
    """(url, password) of the locally running OpenCode server; either may be ''.

    2026-10-26 user: "open code 接入失败". The installed desktop build (2.0.32) serves the v2 API on a port
    it picks at random and guards it with Basic auth, so the documented `serve` on 4096 never answered. The
    live URL comes from `opencode service status` and the password from the service config; an explicit
    loopback URL and OPENCODE_SERVER_PASSWORD still win.
    """
    url = ''
    cli = _opencode_cli()
    if cli:
        try:
            done = subprocess.run([cli, 'service', 'status'], capture_output=True, text=True, timeout=6,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            lines = [line.strip() for line in (done.stdout or '').splitlines() if line.strip()]
            if start_service and lines and lines[-1] == 'stopped':
                started = subprocess.run([cli, 'service', 'start'], capture_output=True, text=True, timeout=20,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if started.returncode:
                    raise AgentAdapterError('OpenCode 本机服务未能启动，请先打开 OpenCode 检查服务状态')
                deadline = time.monotonic() + 10
                while True:
                    done = subprocess.run([cli, 'service', 'status'], capture_output=True, text=True, timeout=6,
                                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    lines = [line.strip() for line in (done.stdout or '').splitlines() if line.strip()]
                    candidate = next((line for line in reversed(lines) if line.startswith('http://')), '')
                    if candidate:
                        import socket
                        port = urlsplit(_loopback_url(candidate)).port
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                                break
                        except OSError:
                            pass
                    if time.monotonic() >= deadline:
                        raise AgentAdapterError('OpenCode 服务启动后尚未就绪，请先在桌面端检查服务状态')
                    time.sleep(0.3)
            url = next((line for line in reversed(lines) if line.startswith('http://')), '')
        except (OSError, subprocess.SubprocessError):
            url = ''
    password = os.environ.get('OPENCODE_SERVER_PASSWORD', '')
    if not password:
        try:
            config = json.loads((Path.home() / '.config' / 'opencode' / 'service.json').read_text('utf-8'))
            password = str(config.get('password') or '')
        except (OSError, ValueError, TypeError):
            password = ''
    return url, password


def _message_text(message: dict) -> str:
    """Assistant text from a v2 message: either a flat `text` or typed content parts."""
    if isinstance(message.get('text'), str) and message['text'].strip():
        return message['text']
    parts = message.get('content') or message.get('parts') or []
    texts = [part['text'] for part in parts if isinstance(part, dict) and part.get('type') == 'text'
             and isinstance(part.get('text'), str) and part['text'].strip()]
    return '\n'.join(texts)


def _opencode_model(value) -> tuple[str, str] | tuple[()]:
    """'provider/model' from the trusted config; empty means the server's own default."""
    if not value:
        return ()
    text = str(value).strip()
    provider, _, name = text.partition('/')
    if (not name or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', provider) or
            not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', name)):
        raise AgentAdapterError('OpenCode model must be provider/model, e.g. opencode/gemini-4-argon')
    return provider, name


def _opencode_variant(value) -> str:
    """Reasoning variant (thinking effort) reported by the model list; empty means the server default."""
    text = str(value or '').strip()
    if text and not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', text):
        raise AgentAdapterError('OpenCode 思考档位名称不合法')
    return text


def _opencode_error(error) -> str:
    """A readable reason first, then the provider's own words (2026-10-26: three different walls)."""
    if isinstance(error, dict):
        text = str(error.get('message') or error.get('type') or '未知错误')
    elif isinstance(error, str):
        text = error
    else:
        text = '未知错误'
    lowered = text.lower()
    if 'trains on request data' in lowered:
        hint = 'OpenCode 这个模型要求在 Privacy 设置里允许「会用请求数据训练的付费端点」，或换一个模型。'
    elif 'free usage limit' in lowered:
        hint = 'OpenCode 免费额度已用尽：需要订阅 OpenCode Go，或选一个已付费的模型。'
    elif 'insufficient account funds' in lowered:
        hint = 'OpenCode 账户余额不足：请充值或换一个可用模型。'
    else:
        hint = ''
    return (hint + ' 原始信息：' + text[:240]) if hint else text[:300]


class LocalOpenCode:
    """A loopback OpenCode server: v2 `/api/*` first, the v1 web API as a fallback.

    Never connects to a remote URL. A server started by hand is addressed by an explicit url and
    OPENCODE_SERVER_PASSWORD; the desktop build's background service is discovered automatically.
    """
    POLL_SECONDS = 1.5
    def __init__(self, url: str = '', *, username: str = 'opencode', password: str = '',
                 model: str = '', variant: str = ''):
        self.url = _loopback_url(url) if url else ''
        self.username = username
        self._password = password
        self.model = _opencode_model(model)
        self.variant = _opencode_variant(variant)
        self._base = ''
        self._resolved_password = ''
        self.api = 0

    # ---------- transport ----------
    def base_url(self) -> str:
        if not self._base:
            url, password = self.url, self._password
            if not url or not password:
                found_url, found_password = opencode_discovery()
                url = url or found_url
                password = password or found_password
            self._base = _loopback_url(url or OPENCODE_DEFAULT_URL)
            self._resolved_password = password
        return self._base

    def _call(self, path: str, data: dict | None = None, *, timeout: float = 30) -> object:
        base = self.base_url()  # resolves the port and password once
        headers = {'Accept': 'application/json'}
        if self._resolved_password:
            credentials = base64.b64encode(
                f'{self.username}:{self._resolved_password}'.encode()).decode()
            headers['Authorization'] = 'Basic ' + credentials
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        # Disable HTTP redirects: redirects could carry Basic credentials elsewhere.
        from urllib.request import HTTPRedirectHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = build_opener(ProxyHandler({}), NoRedirect())
        req = Request(base + path, data=payload, headers=headers,
                      method='POST' if payload is not None else 'GET')
        try:
            with opener.open(req, timeout=timeout) as response:
                if response.status not in (200, 201):
                    raise AgentAdapterError(f'OpenCode returned HTTP {response.status}')
                return json.loads(response.read(4_000_000))
        except (OSError, ValueError) as exc:
            raise AgentAdapterError(f'OpenCode request failed: {type(exc).__name__}') from exc

    def info(self) -> dict:
        """Identify the server: v2 answers /api/info with a version, v1 answers /global/health."""
        try:
            obj = self._call('/api/info', timeout=5)
        except AgentAdapterError:
            obj = None
        if isinstance(obj, dict) and obj.get('version'):
            self.api = 2
            return obj
        obj = self._call('/global/health', timeout=5)
        if not isinstance(obj, dict) or not obj.get('healthy'):
            raise AgentAdapterError('OpenCode server not healthy')
        self.api = 1
        return obj

    health = info  # older callers and the desktop probe used this name

    def server_workspace(self) -> str:
        if not self.api:
            self.info()
        obj = self._call('/api/location' if self.api == 2 else '/path', timeout=5)
        value = obj.get('directory') if isinstance(obj, dict) else None
        if not isinstance(value, str) or not value:
            raise AgentAdapterError('Cannot verify OpenCode server workspace')
        return value

    def list_models(self) -> list[dict]:
        """Models this account can use, each with its reasoning variants (the 'thinking effort' tiers)."""
        if not self.url and not self._base:
            url, password = opencode_discovery(start_service=True)
            if url:
                self._base = _loopback_url(url)
                self._resolved_password = self._password or password
        self.info()
        if self.api != 2:
            raise AgentAdapterError('OpenCode v1 服务不提供模型清单；请使用 OpenCode 2 或更新版本')
        obj = self._call('/api/model', timeout=15)
        items = obj if isinstance(obj, list) else (obj.get('data') if isinstance(obj, dict) else [])
        models = []
        for item in items or []:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            provider = str(item.get('providerID') or '')
            variants = [str(v['id']) for v in (item.get('variants') or [])
                        if isinstance(v, dict) and v.get('id')]
            models.append({'id': item['id'], 'provider': provider,
                           'name': str(item.get('name') or item['id']),
                           'variants': variants, 'enabled': item.get('enabled') is not False,
                           'context': (item.get('limit') or {}).get('context')})
        return models

    def run(self, instruction: str, *, workspace: str | Path, session_id: str = '', timeout: float = 600) -> dict:
        if not instruction.strip() or len(instruction) > 50000:
            raise AgentAdapterError('Invalid instruction')
        if session_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', session_id):
            raise AgentAdapterError('Invalid OpenCode session id')
        self.info()
        if self.api == 2:
            return self._run_v2(instruction, workspace, session_id, timeout)
        return self._run_v1(instruction, workspace, session_id, timeout)

    def _run_v1(self, instruction, workspace, session_id, timeout):
        # CRITICAL: OpenCode HTTP server is bound to its own project, not the
        # TaskManager workspace. Fail closed rather than executing for another
        # user/project in a healthy but unrelated server.
        if Path(self.server_workspace()).resolve() != _workspace(workspace):
            raise AgentAdapterError('OpenCode server belongs to a different workspace')
        if not session_id:
            sess = self._call('/session', {'title': 'Open QQ Dot agent task'}, timeout=10)
            if not isinstance(sess, dict) or not isinstance(sess.get('id'), str):
                raise AgentAdapterError('OpenCode session creation failed')
            session_id = sess['id']
        path = '/session/' + quote(session_id, safe='') + '/message'
        result = self._call(path, {'parts': [{'type': 'text', 'text': instruction}]}, timeout=timeout)
        if not isinstance(result, dict) or not isinstance(result.get('parts'), list):
            raise AgentAdapterError('OpenCode returned an unexpected message')
        text = '\n'.join(part['text'] for part in result['parts'] if isinstance(part, dict) and
                         part.get('type') == 'text' and isinstance(part.get('text'), str))
        if not text.strip():
            raise AgentAdapterError('OpenCode did not return a completed text response')
        return {'output': text, 'session_id': session_id, 'backend': 'opencode',
                'verified': False, 'transport': 'http', 'tokens_used': 0}

    def _run_v2(self, instruction, workspace, session_id, timeout):
        """v2: a session carries its own directory, so the task runs in the authorized workspace even when
        the desktop app is looking at another folder. The prompt answers immediately with the queued user
        message, so the result is read back from the session until it reports idle."""
        target = str(_workspace(workspace))
        if not session_id:
            payload = {'title': 'Open QQ Dot agent task', 'location': {'directory': target}}
            if self.model:
                payload['model'] = {'providerID': self.model[0], 'id': self.model[1]}
                if self.variant:
                    payload['model']['variant'] = self.variant
            created = self._call('/api/session', payload, timeout=20)
            data = created.get('data') if isinstance(created, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get('id'), str):
                raise AgentAdapterError('OpenCode session creation failed')
            session_id = data['id']
        sid = quote(session_id, safe='')
        self._call(f'/api/session/{sid}/prompt', {'text': instruction}, timeout=min(timeout, 120))
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            # 2026-10-26 user: a 你好 waited ~5.7 s, most of it the model; the fixed 1.5 s poll then added up to
            # another 1.5 s of pure waiting. Look often at first, back off once the answer is clearly slow.
            elapsed = time.monotonic() - started
            time.sleep(0.25 if elapsed < 4 else min(1.0, self.POLL_SECONDS))
            listing = self._call(f'/api/session/{sid}/message?limit=100&order=asc', timeout=15)
            items = listing.get('data') if isinstance(listing, dict) else None
            if not isinstance(items, list):
                continue
            idle = next((item for item in reversed(items)
                         if isinstance(item, dict) and item.get('type') == 'idle'), None)
            if idle is None:
                continue
            if idle.get('outcome') != 'succeeded' or idle.get('error'):
                raise AgentAdapterError('OpenCode 模型调用失败：' + _opencode_error(idle.get('error')))
            text = '\n'.join(_message_text(item) for item in items
                             if isinstance(item, dict) and item.get('type') == 'assistant').strip()
            if not text:
                raise AgentAdapterError('OpenCode did not return a completed text response')
            return {'output': text, 'session_id': session_id, 'backend': 'opencode',
                    'verified': False, 'transport': 'http', 'tokens_used': 0}



def run_cli(prompt: str, workspace, model: str = '', timeout: int = 600) -> str:
    """Fallback to the installed OpenCode CLI when the desktop server is geo-blocked.

    The CLI uses the same signed-in account but has its own regional routing.  It is
    only used after a server error explicitly says the model is unavailable here.
    """
    import subprocess
    cli = _opencode_cli()
    if not cli:
        raise AgentAdapterError('OpenCode CLI 不可用')
    command = [cli, 'run']
    if model:
        command += ['-m', model]
    command.append(prompt)
    done = subprocess.run(command, cwd=str(workspace), capture_output=True, text=True,
                          timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = (done.stdout or '').strip()
    lines = []
    for line in text.splitlines():
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not clean or clean.startswith('>'):
            continue
        lines.append(clean)
    if not lines:
        detail = (done.stderr or text or '').strip()[-500:]
        raise AgentAdapterError('OpenCode CLI 调用失败：' + (detail or f'exit {done.returncode}'))
    return '\n'.join(lines)
