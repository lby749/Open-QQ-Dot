"""Proxy guard for overseas model services.

Local models and domestic endpoints stay direct.  Overseas services (Codex,
Claude, Antigravity, ChatGPT/Gemini web) must use the operator's proxy: if no
proxy is configured, they fail loudly instead of silently leaking the real IP.
"""
from __future__ import annotations

import os
import socket
import urllib.request

OVERSEAS = {
    'codex', 'claude', 'antigravity', 'chatgpt', 'gemini',
    'webchat_chatgpt', 'webchat_gemini',
}
PROXY_ENV = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY',
             'https_proxy', 'http_proxy', 'all_proxy')


def _registry_proxy() -> str:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        with key:
            enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            if not enabled:
                return ''
            server, _ = winreg.QueryValueEx(key, 'ProxyServer')
            server = str(server or '').strip()
            if not server:
                return ''
            # "http=host:port;https=host:port" -> prefer https, else first.
            for part in server.split(';'):
                if '=' in part:
                    scheme, value = part.split('=', 1)
                    if scheme.casefold() in ('https', 'http'):
                        server = value
                        break
            if '://' not in server:
                server = 'http://' + server
            return server
    except Exception:
        return ''


def proxy_url() -> str:
    for key in PROXY_ENV:
        value = os.environ.get(key)
        if value:
            value = value.strip()
            return value if '://' in value else 'http://' + value
    value = _registry_proxy()
    if value:
        return value
    # Common local proxy ports; only used when the port is actually listening.
    for port in (7890, 7897, 10808, 10809):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return f'http://127.0.0.1:{port}'
        except OSError:
            continue
    return ''


def is_overseas(provider: str) -> bool:
    return str(provider or '').casefold() in OVERSEAS


def require(provider: str) -> str:
    if not is_overseas(provider):
        return ''
    proxy = proxy_url()
    if not proxy:
        raise RuntimeError(
            f'{provider} 属于境外服务，但系统没有检测到代理。'
            '为避免真实 IP 泄露，已停止调用；请先打开代理再试。')
    return proxy


def env_for(provider: str, base: dict | None = None) -> dict:
    env = dict(base or os.environ)
    proxy = require(provider)
    if proxy:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy'):
            env[key] = proxy
        env['NO_PROXY'] = '127.0.0.1,localhost,::1'
        env['no_proxy'] = env['NO_PROXY']
    return env


def chrome_proxy_args(provider: str) -> list[str]:
    proxy = require(provider)
    if not proxy:
        return []
    return [f'--proxy-server={proxy}', '--proxy-bypass-list=localhost;127.0.0.1;::1']


def should_proxy_url(url: str) -> bool:
    """External HTTP(S) URLs use the proxy; loopback/LAN does not."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").casefold()
    except Exception:
        return True
    if not host or host in ("localhost", "::1"):
        return False
    if host.endswith(".cn") or host in ("deepseek.com", "api.deepseek.com") or any(
            host.endswith(suffix) for suffix in ("aliyuncs.com", "bigmodel.cn", "moonshot.cn",
                                                 "baichuan.com", "minimax.com", "hunyuan.tencent.com")):
        return False
    if host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return False
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return False
        except Exception:
            pass
    return True
