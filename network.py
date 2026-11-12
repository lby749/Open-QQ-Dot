"""Tailscale, Radmin VPN, and LAN addresses used by the friend house and cross-PC connections.

Supports:
1. Local loopback (127.0.0.1 / localhost)
2. Tailscale virtual network (100.64.0.0/10)
3. Radmin VPN virtual LAN (26.0.0.0/8)
4. Standard LAN subnets (192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12)
"""
from __future__ import annotations

import ipaddress
import re

TAILSCALE_SUBNET = ipaddress.ip_network('100.64.0.0/10')
RADMIN_SUBNET = ipaddress.ip_network('26.0.0.0/8')
LAN_SUBNETS = (
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
)
NORMAL_SUBNET = TAILSCALE_SUBNET


def addresses() -> list[str]:
    """IPv4 addresses on the live Tailscale interface."""
    import psutil
    found = []
    for interface, entries in psutil.net_if_addrs().items():
        if 'tailscale' not in interface.casefold():
            continue
        for entry in entries:
            try:
                value = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if value.version == 4 and value in TAILSCALE_SUBNET:
                found.append(str(value))
    return found


def address() -> str | None:
    """This machine's live Tailscale address, or None while disconnected."""
    live = addresses()
    return live[0] if live else None


def radmin_address() -> str | None:
    """This machine's live Radmin VPN address (26.x.x.x), or None if not running."""
    import psutil
    for interface, entries in psutil.net_if_addrs().items():
        is_radmin = 'radmin' in interface.casefold()
        for entry in entries:
            try:
                value = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if value.version == 4 and (value in RADMIN_SUBNET or is_radmin):
                return str(value)
    return None


def lan_address() -> str | None:
    """This machine's primary LAN IPv4 address (e.g. 192.168.x.x), or None."""
    import psutil
    for interface, entries in psutil.net_if_addrs().items():
        if any(bad in interface.casefold() for bad in ('tailscale', 'radmin', 'sstap', 'flclash', 'pseudo', 'bluetooth')):
            continue
        for entry in entries:
            try:
                value = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if value.version == 4 and any(value in subnet for subnet in LAN_SUBNETS):
                return str(value)
    return None


def accepted(host: str) -> bool:
    """True for loopback and Tailscale virtual network."""
    if host == 'localhost':
        return True
    try:
        value = ipaddress.ip_address(host)
    except ValueError:
        return False
    if value.is_loopback:
        return True
    if value in TAILSCALE_SUBNET:
        return True
    return False


def usable_url(url: str) -> bool:
    """A phone MCP address this house is willing to call: http on virtual network, LAN or loopback."""
    match = re.match(r'http://([^/:]+)(?::(\d{1,5}))?/mcp$', url or '')
    if not match:
        return False
    host, port = match.group(1), match.group(2)
    if not host or (port is not None and not 1 <= int(port) <= 65535):
        return False
    return accepted(host)


TAILSCALE = r'C:\Program Files\Tailscale\tailscale.exe'


def public_url(port: int = 8790) -> str | None:
    """The https address `tailscale funnel 8790` publishes, or None."""
    import json
    import shutil
    import subprocess
    try:
        done = subprocess.run([shutil.which('tailscale') or TAILSCALE, 'serve', 'status', '--json'],
                              capture_output=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        config = json.loads(done.stdout or b'{}')
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for site, on in (config.get('AllowFunnel') or {}).items():
        proxy = (((config.get('Web') or {}).get(site) or {}).get('Handlers') or {}).get('/', {}).get('Proxy', '')
        if on and proxy.rstrip('/') in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            host, _, listen = site.rpartition(':')
            return f'https://{host}/' if listen == '443' else f'https://{site}/'
    return None
