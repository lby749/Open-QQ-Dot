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
# Official stable MSI checksums; pinned so a valid cached installer also works offline.
TAILSCALE_INSTALL_VERSION = '1.104.6'
TAILSCALE_INSTALL_SHA256 = {
    'amd64': '8445636f79e258f56e01d324e5562d4ebcdf898ab84c2e618ae05014b8ba90ab',
    'arm64': '2810b17d4edcb6feb0307d0029e67426d2af78f0e4b1092fbbe307f5875c82b7',
}


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
    return _funnel_url(config, port)


def _funnel_url(config, port):
    for foreground in (config.get('Foreground') or {}).values():
        url = _funnel_url(foreground, port)
        if url:
            return url
    for site, on in (config.get('AllowFunnel') or {}).items():
        proxy = (((config.get('Web') or {}).get(site) or {}).get('Handlers') or {}).get('/', {}).get('Proxy', '')
        if on and proxy.rstrip('/') in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            host, _, listen = site.rpartition(':')
            return f'https://{host}/' if listen == '443' else f'https://{site}/'
    return None


def _tailscale():
    import shutil
    from pathlib import Path
    return shutil.which('tailscale') or (TAILSCALE if Path(TAILSCALE).is_file() else None)


def _install_tailscale(progress):
    """Reuse the installed client/cache; execute only a checksum-verified official MSI."""
    import hashlib
    import os
    import platform
    import subprocess
    import house_settings
    from model_downloader import download_file
    if os.name != 'nt':
        raise RuntimeError('请先安装 Tailscale，再点击一键配置：https://tailscale.com/download')
    base = 'https://pkgs.tailscale.com/stable/'
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'amd64'
    name = f'tailscale-setup-{TAILSCALE_INSTALL_VERSION}-{arch}.msi'
    expected = TAILSCALE_INSTALL_SHA256[arch]
    target = house_settings.data_dir() / 'downloads' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    def verified():
        if not target.is_file():
            return False
        with target.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == expected
    if not verified():
        progress('正在下载 Tailscale 官方安装包…')
        try:
            download_file(base + name, target, resume=True,
                          progress_callback=lambda done, total: progress(
                              f'正在下载 Tailscale：{done / 1048576:.1f}'
                              + (f'/{total / 1048576:.1f} MB' if total else ' MB')))
        except OSError as exc:
            raise RuntimeError('Tailscale 安装包下载未完成，请检查网络后重试') from exc
        if not verified():
            target.unlink(missing_ok=True)
            raise RuntimeError('Tailscale 安装包校验失败，未安装')
    progress('正在安装 Tailscale；如系统请求管理员确认，请完成确认…')
    result = subprocess.run(['msiexec.exe', '/i', str(target), '/passive', '/norestart'], timeout=300,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode not in (0, 3010) or not _tailscale():
        raise RuntimeError(f'Tailscale 安装未完成（返回码 {result.returncode}）；请检查系统安装提示')
    return _tailscale()


def _tailscale_run(executable, *args, timeout=20):
    import subprocess
    return subprocess.run([executable, *args], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _set_funnel_enabled(enabled):
    import house_settings
    import os
    settings = house_settings.load()
    settings['tailscale_funnel_enabled'] = bool(enabled)
    house_settings.save(settings)
    if 'QQ_ALLOW_FUNNEL' in os.environ:
        os.environ['QQ_ALLOW_FUNNEL'] = '1' if enabled else '0'


def _funnel_reachable(url):
    """Probe without a friend token; a forwarding rule alone is not a working connection."""
    import urllib.request
    import urllib.error
    from tls_support import client_context
    try:
        with urllib.request.urlopen(url, timeout=8, context=client_context()) as response:
            text = response.read(4096).decode('utf-8', 'replace')
            return response.status == 200 and '<title>Open QQ Dot · 好友小屋</title>' in text
    except urllib.error.HTTPError as exc:
        import json
        text = exc.read(4096).decode('utf-8', 'replace')
        try:
            error = json.loads(text).get('error', '')
        except (ValueError, AttributeError):
            return False
        return exc.code == 403 and ('Tailscale Funnel 未启用' in error or 'Tailscale Funnel 默认关闭' in error)
    except (OSError, ValueError):
        return False


def configure_funnel(port=8790, progress=None):
    """One explicit desktop action; never reset another application's Serve configuration."""
    import json
    import subprocess
    progress = progress or (lambda text: None)
    executable = _tailscale() or _install_tailscale(progress)
    progress('正在检查 Tailscale 连接…')
    status = _tailscale_run(executable, 'status', '--json')
    state = json.loads(status.stdout or '{}')
    if not state.get('BackendState'):
        raise RuntimeError('Tailscale 服务未就绪；请先启动 Tailscale，再重试')
    if state.get('BackendState') != 'Running':
        from pathlib import Path
        app = Path(executable).with_name('tailscale-ipn.exe')
        if app.is_file():
            subprocess.Popen([str(app)], close_fds=True)
        return {'state': 'login', 'message': '已打开 Tailscale；请先连接或登录，再点一键配置。QQ 登录不会改变。'}
    serve = _tailscale_run(executable, 'serve', 'status', '--json')
    if serve.returncode:
        raise RuntimeError('无法读取 Tailscale 转发配置，未修改已有连接')
    config = json.loads(serve.stdout or '{}')
    url = _funnel_url(config, port)
    if not url:
        occupied = set((config.get('TCP') or {}).keys())
        occupied.update(site.rpartition(':')[2] for site in (config.get('Web') or {}))
        for foreground in (config.get('Foreground') or {}).values():
            occupied.update((foreground.get('TCP') or {}).keys())
            occupied.update(site.rpartition(':')[2] for site in (foreground.get('Web') or {}))
        listen = next((p for p in ('443', '8443', '10000') if p not in occupied), None)
        if not listen:
            raise RuntimeError('Tailscale 的 Funnel 端口均已被其他服务使用，未覆盖原配置')
        progress('正在配置 Tailscale Funnel…')
        try:
            result = _tailscale_run(executable, 'funnel', '--bg', '--yes', f'--https={listen}',
                                    f'http://127.0.0.1:{port}')
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            output = ''.join(part.decode('utf-8', 'replace') if isinstance(part, bytes) else part
                             for part in (exc.stdout or '', exc.stderr or ''))
        # The CLI may request account-owner authorization; opening this does not complete it.
        authorization = re.search(r'https://login\.tailscale\.com/[^\s<>\"\']+', output)
        if authorization:
            return {'state': 'authorize', 'authorization_url': authorization.group(),
                    'message': '请在打开的 Tailscale 页面启用 Funnel，完成后再点一键配置。'}
        check = _tailscale_run(executable, 'serve', 'status', '--json')
        url = _funnel_url(json.loads(check.stdout or '{}'), port) if not check.returncode else None
    if not url:
        raise RuntimeError('Tailscale Funnel 尚未建立；请检查网络连接和 Funnel 授权，再重试')
    progress('正在验证 Funnel 的 HTTPS 连接…')
    if not _funnel_reachable(url):
        return {'state': 'unreachable',
                'message': 'Funnel 转发已配置，但公网 HTTPS 尚未连通。二维码未切换，请检查 Tailscale 网络后重试。'}
    _set_funnel_enabled(True)
    return {'state': 'ready', 'public': url, 'message': 'Tailscale Funnel 已连接，配对二维码已更新。'}


def disable_funnel(port=8790):
    """Close the application gate first, and stop only a verified matching Funnel listener."""
    import json
    from urllib.parse import urlparse
    _set_funnel_enabled(False)
    executable = _tailscale()
    if executable:
        result = _tailscale_run(executable, 'serve', 'status', '--json')
        url = _funnel_url(json.loads(result.stdout or '{}'), port) if not result.returncode else None
        if url:
            listen = urlparse(url).port or 443
            result = _tailscale_run(executable, 'funnel', '--bg', '--yes', f'--https={listen}', '--set-path=/', 'off')
            if result.returncode:
                raise RuntimeError('应用已拒绝 Funnel 访问；Tailscale 转发尚未关闭，请在 Tailscale 中检查')
    return {'state': 'disabled', 'public': None, 'message': 'Funnel 已关闭，本机入口仍可使用。'}
