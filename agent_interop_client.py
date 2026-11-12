"""Dependency-free HTTP client usable by *any* supported agent/application.

This is a transport adapter, not a new model executor. The trusted host provides
an expiring bearer token for the agent identity registered in the mailbox.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


class InteropClientError(RuntimeError):
    pass


class AgentInteropClient:
    def __init__(self, url: str, token: str, *, timeout: float = 15.0):
        url = url.rstrip('/')
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Local agent gateway requires http://127.0.0.1:<port>')
        if not token or not token.startswith('v2.'):
            raise ValueError('Provide an expiring v2 bearer token from the trusted host')
        self.url, self.token, self.timeout = url, token, timeout
        self._opener = build_opener(ProxyHandler({}))  # never send localhost tokens through a proxy

    def _request(self, path: str, payload: dict | None = None) -> dict:
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json'}
        data = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = Request(self.url + path, data=data, headers=headers,
                          method='POST' if data is not None else 'GET')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(512).decode('utf-8', errors='replace')
            raise InteropClientError(f'Gateway returned HTTP {exc.code}: {detail}') from None

    def tools(self) -> list[dict]:
        return self._request('/v1/tools')['tools']

    def me(self) -> dict:
        return self._request('/v1/me')

    def call(self, name: str, **arguments) -> dict:
        return self._request('/v1/call', {'name': name, 'arguments': arguments})['result']

    def list_agents(self, capability: str = '') -> list[dict]:
        return self.call('list_agents', capability=capability)['agents']

    def send(self, recipients: list[str], body: str, **kwargs) -> dict:
        return self.call('send_message', to=recipients, body=body, **kwargs)

    def inbox(self, limit: int = 10, peek: bool = False) -> list[dict]:
        return self.call('fetch_inbox', limit=limit, peek=peek)['untrusted_messages']

    def wait(self, timeout_seconds: float = 20.0, limit: int = 10) -> list[dict]:
        return self.call('wait_for_inbox', timeout_seconds=timeout_seconds,
                         limit=limit)['untrusted_messages']

    def handoff(self, recipients: list[str], task_id: str, objective: str,
                acceptance_criteria: list[str], **kwargs) -> dict:
        return self.call('send_handoff', to=recipients, task_id=task_id,
                         objective=objective, acceptance_criteria=acceptance_criteria, **kwargs)

    def reply(self, message_id: str, body: str, **kwargs) -> dict:
        return self.call('reply_message', message_id=message_id, body=body, **kwargs)
