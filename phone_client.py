"""The computer's client for the friend-scoped phone tools, through an outbound relay or a legacy MCP endpoint.

The phone's server offers the friend-house tool set only while it is paired (McpServerService registers the phone
context tools and the screen tools, nothing that drives the phone). This client speaks just enough MCP to call those
tools, keeps the session, and never follows a redirect — a 302 must not carry the bearer token to another host.

Relay calls go only to the house's loopback API. Legacy direct endpoints must be on the shared virtual network
(network.py): the house never sends its token to the LAN or to a public address.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import network

TIMEOUT_SECONDS = 20
MAX_BYTES = 8 * 1024 * 1024
# 2026-10-12 user (model): the relay phone long-polls every 10 s; the house web process stamps each
# poll in the friend's folder so the bridge process (prompt) and the status page can tell whether it is online
SEEN_FILE = 'phone_last_seen'
ONLINE_SECONDS = 60


def mark_seen(directory: Path) -> None:
    path = Path(directory) / SEEN_FILE
    path.touch()


def last_seen(directory: Path) -> float | None:
    """Epoch seconds of the phone's last poll for this friend, None if it never connected."""
    try:
        return (Path(directory) / SEEN_FILE).stat().st_mtime
    except OSError:
        return None


def online(session: dict, directory: Path) -> bool:
    """A paired relay phone that polled within ONLINE_SECONDS (a legacy direct endpoint is not tracked: False)."""
    import time
    seen = last_seen(directory)
    return (session.get('phone_transport') == 'relay' and configured(session) and seen is not None
            and time.time() - seen < ONLINE_SECONDS)


class PhoneUnavailable(RuntimeError):
    """The computer cannot reach the phone right now; the caller reports it and the turn continues."""


class PhoneToolError(RuntimeError):
    """The phone answered, but the tool call failed (permissions, a private screen, a bad argument)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def address(session: dict) -> dict:
    """The phone endpoint stored for a friend: {'mcp_url', 'token'} (empty values when not paired yet)."""
    return {'mcp_url': str(session.get('phone_mcp_url') or ''), 'token': str(session.get('phone_mcp_token') or '')}


def usable(address_value: str) -> bool:
    """Only the phone's Tailscale address or loopback; the house never calls a public URL with a token."""
    return network.usable_url(address_value)


def configured(session: dict) -> bool:
    if session.get('phone_transport') == 'relay':
        return bool(session.get('phone_mcp_token') and session.get('phone_pc_token')
                    and session.get('phone_friend_id'))
    current = address(session)
    return bool(current['token']) and usable(current['mcp_url'])


class PhoneClient:
    """One MCP session per client; create it per tool call so a restarted phone server is picked up."""

    def __init__(self, mcp_url: str, token: str):
        if not usable(mcp_url) or not token:
            raise PhoneUnavailable('手机还没连上电脑小屋：在手机小屋点「连接手机 App」或扫描配对二维码')
        self.mcp_url = mcp_url
        self.token = token
        self.session = ''
        self.sequence = 0
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _call(self, method: str, params: dict, notify: bool = False) -> dict:
        self.sequence += 1
        payload: dict = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notify:
            payload['id'] = self.sequence
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer ' + self.token}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.mcp_url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                self.session = response.headers.get('Mcp-Session-Id', self.session)
                raw = response.read(MAX_BYTES)
        except urllib.error.HTTPError as exc:
            raise PhoneUnavailable(f'手机连接服务返回 {exc.code}；请确认手机小屋开着、两边都连着 Tailscale') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PhoneUnavailable('连不上手机：请确认手机小屋开着、MCP 服务在运行、两边都连着 Tailscale') from None
        if notify or not raw:
            return {}
        try:
            answer = json.loads(raw.decode('utf-8', errors='replace'))
        except ValueError:
            raise PhoneUnavailable('手机返回的内容不是 JSON-RPC') from None
        if 'error' in answer:
            raise PhoneToolError(str(answer['error'].get('message', answer['error']))[:200])
        return answer.get('result') or {}

    def _initialize(self):
        self._call('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                 'clientInfo': {'name': 'friend-house', 'version': '1'}})
        self._call('notifications/initialized', {}, notify=True)

    def _listed(self) -> list[dict]:
        return list(self._call('tools/list', {}).get('tools') or [])

    def tools(self) -> list[dict]:
        self._initialize()
        return self._listed()

    def call(self, name: str, arguments: dict | None = None) -> str:
        """Runs one tool and returns its text; raises PhoneToolError with the phone's own message.

        The phone prefixes every tool (android_<tool>, or android_<device>_<tool> when it has a device name), so
        our short name is matched to the one tool the phone lists as <prefix>_<name>; the model keeps the short name.
        """
        self._initialize()
        matches = [tool.get('name', '') for tool in self._listed()
                   if tool.get('name') == name or str(tool.get('name', '')).endswith('_' + name)]
        if len(matches) != 1:
            raise PhoneToolError(f'手机上没有唯一的工具 {name}（找到 {len(matches)} 个）')
        result = self._call('tools/call', {'name': matches[0], 'arguments': arguments or {}})
        parts = [item.get('text', '') for item in result.get('content', []) if item.get('type') == 'text']
        text = '\n'.join(part for part in parts if part).strip()
        if result.get('isError'):
            raise PhoneToolError(text[:300] or '手机上的工具执行失败')
        if not text:
            raise PhoneToolError('手机没有返回内容')
        return text


def client_for(session: dict) -> PhoneClient | RelayClient:
    """The client for one friend's stored phone endpoint."""
    if session.get('phone_transport') == 'relay':
        return RelayClient(session)
    current = address(session)
    return PhoneClient(current['mcp_url'], current['token'])


class RelayClient:
    """The phone receives requests over its outbound long poll; only the local house is contacted here."""

    def __init__(self, session):
        from urllib.parse import urlparse
        self.session = session
        url = str(session.get('phone_pc_url') or '')
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
            raise PhoneUnavailable('电脑手机连接地址无效，请重新配对')
        self.url = url.rstrip('/') + '/api/phone/query'

    def call(self, name, arguments=None):
        request = urllib.request.Request(
            self.url, data=json.dumps({'name': name, 'arguments': arguments or {}}).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + self.session['phone_pc_token'],
                     'X-Phone-Token': self.session['phone_mcp_token']})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
                answer = json.loads(response.read(MAX_BYTES))['result']
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            raise PhoneUnavailable('手机暂不可用：请确认手机小屋后台运行，FlClash 已连接电脑所在的 Tailscale 网络') from None
        text = '\n'.join(str(item.get('text', '')) for item in answer.get('content', []) if item.get('type') == 'text').strip()
        if answer.get('isError'):
            raise PhoneToolError(text[:300] or '手机上的工具执行失败')
        if not text:
            raise PhoneToolError('手机没有返回内容')
        return text


def trim_warning(text: str) -> str:
    """Drops the phone's untrusted-content banner: the model already sees that rule in its own prompt."""
    lines = text.splitlines()
    # 2026-10-13: the current app sends "CAUTION: The data below comes from an untrusted external source ..." instead
    while lines and (lines[0].startswith(('UNTRUSTED', 'CAUTION: The data below comes from an untrusted'))
                     or not lines[0].strip()):
        lines.pop(0)
    return '\n'.join(lines).strip()


def load_session(path: Path) -> dict:
    """The phone fields of a friend.json, without loading the whole profile for a one-line tool call."""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
