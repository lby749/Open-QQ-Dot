"""DotLink desktop lifecycle: start, connect, quick-login and in-app QR.

DotLink is Open QQ Dot's own desktop-native QQ connector.  The old flow opened
a browser page and asked the user to scan a QR code there; this module owns the
whole local lifecycle instead:

* prepare the bundled runtime and its OneBot/WebUI configuration (no page, no
  manual token copying);
* start and supervise ``node.exe index.js`` with the workspace-local QQ data
  directory;
* authenticate to the loopback-only WebUI by minting the same credential the
  WebUI frontend uses;
* log in with a saved QQ quick-login account when one exists (passwordless,
  no QR at all), otherwise fetch the QR URL and render it inside the desktop
  window;
* report status for the connection page and for the bridge.

Only loopback addresses are used.  The WebUI token, OneBot token and QQ login
state never leave this machine and are never written into a capsule or export.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from pathlib import Path

WEBUI_PORT = 6099
ONEBOT_PORT = 3001
DEFAULT_WS = f"ws://127.0.0.1:{ONEBOT_PORT}"
START_TIMEOUT = 35.0
LOOPBACK = ("127.0.0.1", "localhost", "::1")
_LOGIN_BACKOFF: dict[tuple[str, int], float] = {}
_WEBUI_CREDENTIALS: dict[tuple[str, int, str], tuple[str, float]] = {}
_WEBUI_LOGIN_LOCK = threading.RLock()


class DotLinkError(RuntimeError):
    pass


def vendor_spec(home: Path) -> dict:
    """Runtime integration contract (env names, routes, token salt).

    Kept as data so the connector code itself does not hard-code a vendor name.
    """
    values = _read_json(Path(home) / "dotlink" / "vendor.json", None)
    if not isinstance(values, dict):
        values = _read_json(Path(__file__).with_name("dotlink_vendor_defaults.json"), {}) or {}
    return values


def _runtime_ready(directory: Path) -> bool:
    return (Path(directory) / "index.js").is_file() and (Path(directory) / "node.exe").is_file()


def _runtime_dir(home: Path) -> Path:
    home = Path(home)
    candidates = [
        Path(value) for value in (os.environ.get("OPENQQDOT_NAPCAT_RUNTIME"),
                                  os.environ.get("NAPCAT_RUNTIME")) if value
    ]
    for candidate in [*candidates, home / "qq-runtime", home / "runtime", home / "vendor" / "qq-runtime"]:
        if _runtime_ready(candidate):
            return candidate
    for candidate in sorted(home.glob("*/runtime")):
        if _runtime_ready(candidate):
            return candidate
    return home / "qq-runtime"


def find_existing_runtimes(home: Path) -> list[Path]:
    """Find an installed NapCat runtime without downloading or copying it.

    Only known install locations are checked, plus an explicit environment
    override.  The caller must ask the operator before adopting one.
    """
    home = Path(home)
    candidates: list[Path] = []
    for name in ("OPENQQDOT_NAPCAT_RUNTIME", "NAPCAT_RUNTIME"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value))
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    profile = os.environ.get("USERPROFILE")
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    for base in (local, roaming, profile):
        if base:
            candidates += [Path(base) / "NapCat", Path(base) / "Programs" / "NapCat"]
    for base in (program_files, program_files_x86):
        if base:
            candidates.append(Path(base) / "NapCat")
    candidates += [Path("C:/NapCat"), Path("D:/NapCat"), Path("C:/qq-runtime"), Path("D:/qq-runtime"),
                   Path("A:/qq-runtime"), Path("B:/qq-runtime")]
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
        except OSError:
            key = str(candidate).casefold()
        if key in seen or candidate == home / "qq-runtime":
            continue
        seen.add(key)
        if _runtime_ready(candidate):
            result.append(candidate)
    return result


def adopt_runtime(home: Path, source: Path) -> Path:
    """Use an existing immutable NapCat tree without copying or editing it."""
    home = Path(home)
    source = Path(source)
    if not _runtime_ready(source):
        raise DotLinkError("已有 NapCat 目录不完整（缺少 index.js 或 node.exe）")
    target = home / "qq-runtime"
    if not _runtime_ready(target):
        try:
            if target.exists():
                target.rmdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                                        capture_output=True, text=True, timeout=20,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0 and not _runtime_ready(target):
                    raise DotLinkError((result.stdout + result.stderr).strip() or "无法创建运行时链接")
            else:
                os.symlink(source, target, target_is_directory=True)
        except OSError as exc:
            raise DotLinkError(f"无法链接已有 NapCat：{exc}") from None
    if not _runtime_ready(target):
        raise DotLinkError("已有 NapCat 无法被当前安装目录使用")
    meta = home / "dotlink" / "runtime-source.json"
    _write_json(meta, {"adopted": True, "source": str(source), "copied": False,
                       "modified_vendor_files": False, "adopted_at": time.time(),
                       "copyright": "Copyright © 2024 Mlikiowa",
                       "license": "Limited Redistribution License (see licenses/third-party/NapCat-LICENSE)"})
    license_path = Path(__file__).with_name("licenses") / "third-party" / "NapCat-LICENSE"
    if license_path.is_file():
        target_license = home / "dotlink" / "NAPCAT-LICENSE.txt"
        target_license.write_text(license_path.read_text("utf-8") + "\n\nSource: " + str(source) + "\n",
                                  encoding="utf-8")
    return target


def bootstrap_runtime(home: Path) -> Path:
    """Expose the bundled runtime as ``qq-runtime`` without moving vendor files."""
    home = Path(home)
    target = home / "qq-runtime"
    if _runtime_ready(target):
        return target
    source = _runtime_dir(home)
    if not _runtime_ready(source) or source == target:
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.symlink(source, target, target_is_directory=True)
    except Exception:
        return source
    return target if _runtime_ready(target) else source


@dataclass
class Paths:
    home: Path

    @property
    def root(self) -> Path:
        return Path(self.home) / "dotlink"

    @property
    def runtime(self) -> Path:
        return bootstrap_runtime(self.home)

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def config(self) -> Path:
        return self.state / "config"

    @property
    def qq_data(self) -> Path:
        return self.root / "qq-data"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.state / "cache"

    @property
    def pid_file(self) -> Path:
        return self.root / "runtime.pid"

    @property
    def out_log(self) -> Path:
        return self.root / "runtime.out.log"

    @property
    def err_log(self) -> Path:
        return self.root / "runtime.err.log"

    @property
    def settings(self) -> Path:
        return self.root / "local.json"

    @property
    def onebot(self) -> Path:
        return self.config / "onebot11.json"

    @property
    def webui(self) -> Path:
        return self.config / "webui.json"

    @property
    def qr_png(self) -> Path:
        return self.cache / "qrcode.png"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _settings(paths: Paths) -> dict:
    return _read_json(paths.settings, {}) or {}


def _save_settings(paths: Paths, value: dict) -> None:
    _write_json(paths.settings, value)


def prepare(paths: Paths | Path, *, access_token: str = "", webui_token: str = "") -> dict:
    """Create/repair DotLink's own runtime configuration; never import old tokens or login state."""
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    vendor = vendor_spec(paths.home)
    _write_json(paths.home / "dotlink" / "vendor.json", vendor)
    bootstrap_runtime(paths.home)
    for folder in (paths.config, paths.qq_data, paths.cache, paths.logs):
        folder.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    # Compliance: never edit NapCat's index.js or bundled .mjs files.  DotLink
    # supplies its private state/data paths through the official process
    # environment and its own config files, leaving the vendor tree immutable.
    settings = _settings(paths)
    access_token = access_token or str(settings.get("access_token") or "") or secrets.token_hex(24)
    webui_token = webui_token or str(settings.get("webui_token") or "") or secrets.token_hex(16)
    # DotLink mints its own management token; an old client's webui.json is never read.
    port = int(settings.get("webui_port") or WEBUI_PORT)
    webui = {"host": "127.0.0.1", "port": port, "token": webui_token,
             "disableWebUI": False, "loginRate": 10, "autoLoginAccount": ""}
    _write_json(paths.webui, webui)
    accounts = _known_accounts(paths)
    onebot = _read_json(paths.onebot, {}) or {}
    onebot = _merge_onebot(onebot, access_token)
    _write_json(paths.onebot, onebot)
    # A per-account onebot11_<uin>.json wins over the template after the first login; keep every copy in sync.
    for path in list(paths.config.glob("onebot11_*.json")):
        if re.fullmatch(r"onebot11_\d{5,12}\.json", path.name):
            _write_json(path, _merge_onebot(_read_json(path, {}) or {}, access_token))
    settings.update({"ws_url": settings.get("ws_url") or DEFAULT_WS, "access_token": access_token,
                     "webui_token": webui_token, "webui_port": int(webui["port"]),
                     "jwt_secret": settings.get("jwt_secret") or secrets.token_hex(24),
                     "runtime_path": str(paths.runtime), "prepared_at": time.time()})
    _save_settings(paths, settings)
    return {"root": str(paths.root), "runtime": str(paths.runtime), "ws_url": settings["ws_url"],
            "webui_port": int(webui["port"]), "accounts": accounts, "warnings": warnings,
            "runtime_patched": False, "runtime_untouched": True,
            "onebot_config": str(paths.onebot), "webui_config": str(paths.webui)}


def _merge_onebot(onebot: dict, token: str) -> dict:
    network = onebot.get("network") if isinstance(onebot.get("network"), dict) else {}
    servers = [item for item in network.get("websocketServers") or [] if isinstance(item, dict)]
    if not servers:
        servers = [{"name": "friend-house", "enable": True, "host": "127.0.0.1", "port": ONEBOT_PORT,
                    "messagePostFormat": "array", "reportSelfMessage": False, "token": token,
                    "enableForcePushEvent": True, "debug": False, "heartInterval": 30000}]
    for server in servers:
        server.update({"enable": True, "host": "127.0.0.1", "port": int(server.get("port") or ONEBOT_PORT),
                       "messagePostFormat": server.get("messagePostFormat") or "array",
                       "token": token, "enableForcePushEvent": True})
    network.update({"websocketServers": servers, "httpServers": network.get("httpServers") or [],
                    "httpSseServers": network.get("httpSseServers") or [], "httpClients": network.get("httpClients") or [],
                    "websocketClients": network.get("websocketClients") or [], "plugins": network.get("plugins") or []})
    onebot["network"] = network
    onebot.setdefault("musicSignUrl", "")
    onebot.setdefault("enableLocalFile2Url", False)
    onebot.setdefault("parseMultMsg", True)
    return onebot


def _known_accounts(paths: Paths) -> list[str]:
    found = set()
    for path in paths.config.glob("onebot11_*.json"):
        match = re.fullmatch(r"onebot11_(\d{5,12})\.json", path.name)
        if match:
            found.add(match.group(1))
    for path in (paths.home / "friends.json",):
        try:
            registry = json.loads(path.read_text("utf-8"))
            for record in (registry.get("friends") or {}).values():
                if isinstance(record, dict) and re.fullmatch(r"\d{5,12}", str(record.get("self_id") or "")):
                    found.add(str(record["self_id"]))
        except (OSError, ValueError, AttributeError):
            pass
    return sorted(found)


# --------------------------------------------------------------------------- WebUI client

class WebUIClient:
    """Loopback WebUI client; mints the credential the shipped frontend uses."""

    def __init__(self, port: int = WEBUI_PORT, token: str = "", host: str = "127.0.0.1", timeout: float = 6.0):
        if host not in LOOPBACK:
            raise DotLinkError("WebUI 只允许本机地址")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise DotLinkError("WebUI 端口无效")
        self.host, self.port, self.token, self.timeout = host, port, str(token or ""), timeout
        self.credential = ""
        self.vendor = vendor_spec(Path(os.environ.get("QQ_BRIDGE_HOME", Path(__file__).resolve().parent)))

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _request(self, path: str, payload: dict | None = None, *, auth: bool = True, method: str = "POST") -> dict:
        data = json.dumps(payload or {}, ensure_ascii=False).encode() if method == "POST" else None
        request = urllib.request.Request(self._url(path), data=data, method=method,
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        if auth:
            if not self.credential:
                self.login()
            request.add_header("Authorization", f"Bearer {self.credential}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            if auth and exc.code in (401, 403):
                self._forget_credential()
            raise DotLinkError(f"QQ 直连管理接口 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DotLinkError(f"QQ 直连管理接口不可达：{exc}") from None
        if not isinstance(body, dict):
            raise DotLinkError("QQ 直连管理接口返回格式无效")
        if body.get("code") != 0:
            message = str(body.get("message") or "QQ 直连管理接口请求失败")
            if auth and (body.get("code") in (401, 403) or any(
                    term in message.lower() for term in ("unauthorized", "credential", "token is invalid"))):
                self._forget_credential()
            if "token is invalid" in message.lower():
                raise DotLinkError(
                    "管理令牌不匹配：另一个 NapCat/QQ 直连实例可能仍占用 6099 端口。"
                    "请先退出旧实例（或重启电脑）再启动本程序；不要同时运行两个 NapCat。")
            raise DotLinkError(message)
        return body.get("data")

    def _forget_credential(self):
        # A rejected command is never replayed; the next poll may authenticate.
        with _WEBUI_LOGIN_LOCK:
            key = getattr(self, '_auth_key', None)
            if _WEBUI_CREDENTIALS.get(key, (None,))[0] == self.credential:
                _WEBUI_CREDENTIALS.pop(key, None)
            self.credential = ""

    def login(self) -> str:
        with _WEBUI_LOGIN_LOCK:
            return self._login()

    def _login(self) -> str:
        if not self.token:
            raise DotLinkError("缺少 QQ 直连管理令牌")
        digest = hashlib.sha256((self.token + str(self.vendor.get("token_salt", ""))).encode()).hexdigest()
        self._auth_key = (self.host, self.port, digest)
        now = time.monotonic()
        for entry, (_, expires) in list(_WEBUI_CREDENTIALS.items()):
            if expires <= now:
                _WEBUI_CREDENTIALS.pop(entry, None)
        cached = _WEBUI_CREDENTIALS.get(self._auth_key)
        if cached:
            self.credential = cached[0]
            return self.credential
        key = (self.host, self.port)
        wait = _LOGIN_BACKOFF.get(key, 0.0) - time.monotonic()
        if wait > 0:
            raise DotLinkError(f"NapCat 登录尝试过于频繁，请等待约 {int(wait) + 1} 秒后再试；"
                               "不要同时运行多个 NapCat。")
        try:
            data = self._request("/api/auth/login", {"hash": digest}, auth=False)
        except DotLinkError as exc:
            if "rate limit" in str(exc).lower():
                _LOGIN_BACKOFF[key] = time.monotonic() + 60
                raise DotLinkError("NapCat 已触发登录频率限制（login rate limit）。"
                                   "请先完全退出旧的 NapCat/本程序，等待约 1 分钟，"
                                   "再只启动一个实例。") from None
            raise
        credential = data.get("Credential") if isinstance(data, dict) else ""
        if not credential:
            raise DotLinkError("QQ 直连管理登录失败（令牌不匹配）")
        _LOGIN_BACKOFF.pop(key, None)
        self.credential = credential
        if len(_WEBUI_CREDENTIALS) >= 32:
            _WEBUI_CREDENTIALS.pop(next(iter(_WEBUI_CREDENTIALS)))
        _WEBUI_CREDENTIALS[self._auth_key] = (credential, time.monotonic() + 300)
        return credential

    def version(self) -> str:
        route = str(self.vendor.get("version_route") or "")
        if not route:
            return ""
        data = self._request(route, method="GET")
        return str((data or {}).get("version") or "")

    def quick_accounts(self) -> list[dict]:
        data = self._request("/api/QQLogin/GetQuickLoginListNew")
        if not isinstance(data, list):
            data = self._request("/api/QQLogin/GetQuickLoginList") or []
        result = []
        for item in data:
            if isinstance(item, dict):
                uin = str(item.get("uin") or "")
                if uin:
                    result.append({"uin": uin, "nickname": str(item.get("nickName") or item.get("nick") or ""),
                                   "avatar": str(item.get("faceUrl") or item.get("avatarUrl") or "")})
            elif isinstance(item, str) and item:
                result.append({"uin": item, "nickname": "", "avatar": ""})
        return result

    def login_info(self) -> dict:
        data = self._request("/api/QQLogin/GetQQLoginInfo") or {}
        return data if isinstance(data, dict) else {}

    def status(self) -> dict:
        return self._request("/api/QQLogin/CheckLoginStatus") or {}

    def qr_url(self) -> str:
        data = self._request("/api/QQLogin/GetQQLoginQrcode") or {}
        return str(data.get("qrcode") or "") if isinstance(data, dict) else ""

    def refresh_qr(self) -> str:
        data = self._request("/api/QQLogin/RefreshQRcode") or {}
        return str(data.get("qrcodeurl") or "") if isinstance(data, dict) else ""

    def quick_login(self, uin: str) -> None:
        if not re.fullmatch(r"\d{5,12}", str(uin)):
            raise DotLinkError("QQ 号无效")
        self._request("/api/QQLogin/SetQuickLogin", {"uin": str(uin)})

    def password_login(self, uin: str, password: str) -> dict:
        """Password login (the runtime accepts an MD5, never the plain password)."""
        if not re.fullmatch(r"\d{5,12}", str(uin)):
            raise DotLinkError("QQ 号无效")
        if not password:
            raise DotLinkError("密码不能为空")
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        data = self._request("/api/QQLogin/PasswordLogin", {"uin": str(uin), "passwordMd5": digest})
        if isinstance(data, dict) and data.get("needCaptcha"):
            raise DotLinkError("QQ 要求图形验证码，请在浏览器打开：" + str(data.get("proofWaterUrl") or "QQ 安全验证页"))
        if isinstance(data, dict) and data.get("needNewDevice"):
            raise DotLinkError("QQ 要求新设备验证：" + str(data.get("jumpUrl") or "请在手机 QQ 确认"))
        return data if isinstance(data, dict) else {}

    def set_auto_account(self, uin: str) -> None:
        self._request("/api/QQLogin/SetQuickLoginQQ", {"uin": str(uin)})

    def restart(self) -> None:
        self._request("/api/QQLogin/RestartQQ 直连")

    def download(self, path: Path) -> bytes:
        request = urllib.request.Request(self._url("/api/File/download?path=" + urllib.parse.quote(str(path))),
                                         headers={"Authorization": f"Bearer {self.credential}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()


# --------------------------------------------------------------------------- lifecycle



NAPCAT_REPO = "https://github.com/NapNeko/NapCatQQ"
NAPCAT_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
NAPCAT_ASSET = "NapCat.Shell.Windows.Node.zip"


def download_official_runtime(home: Path, progress=None) -> dict:
    """Download the official NapCat Windows Node runtime, with license/source record."""
    import json as _json
    import tempfile
    import urllib.request
    home = Path(home)
    target = home / "qq-runtime"
    if runtime_ready(Paths(home)):
        return {"installed": True, "path": str(target), "already": True}
    import network_guard
    proxy = network_guard.proxy_url()
    opener = None
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    def open_url(url, timeout=120):
        request = urllib.request.Request(url, headers={"User-Agent": "OpenQQDot/1.0"})
        return (opener or urllib.request).open(request, timeout=timeout)
    if progress:
        progress("正在查询 NapCat 官方最新版本…")
    with open_url(NAPCAT_API, timeout=30) as response:
        release = _json.load(response)
    tag = str(release.get("tag_name") or "latest")
    asset = next((item for item in release.get("assets", [])
                  if item.get("name") == NAPCAT_ASSET), None)
    if not asset:
        raise DotLinkError("NapCat 官方最新版本没有 Windows Node 包，请检查官方发布页")
    url = str(asset.get("browser_download_url") or "")
    trusted_prefix = f"https://github.com/NapNeko/NapCatQQ/releases/download/{tag}/"
    if not url.startswith(trusted_prefix):
        raise DotLinkError("NapCat 下载地址不是官方 HTTPS release 地址，已停止")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise DotLinkError("NapCat 官方 release 未提供 SHA-256，无法校验完整性，已停止")
    expected_sha = digest.split(":", 1)[1].lower()
    temporary = Path(tempfile.mkdtemp(prefix="openqqdot-napcat-"))
    archive = temporary / NAPCAT_ASSET
    try:
        if progress:
            progress(f"正在从 NapCat 官方下载 {tag}…")
        with open_url(url, timeout=300) as response, archive.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    pct = int(done / total * 100) if total else 0
                    progress(f"下载 NapCat {tag}：{done // (1024 * 1024)} MB"
                             + (f" / {total // (1024 * 1024)} MB" if total else ""), pct)
        actual_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise DotLinkError("NapCat 运行时 SHA-256 校验失败，已删除下载文件")
        if progress:
            progress("SHA-256 校验通过，正在解压并配置 NapCat…")
        staging = home / ".qq-runtime-download"
        if staging.exists():
            import shutil
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                name = info.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or ":" in name:
                    raise DotLinkError("NapCat 包包含不安全路径")
                if info.is_dir():
                    continue
                dest = staging.joinpath(*path.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with package.open(info) as src, dest.open("wb") as out:
                    import shutil as _shutil
                    _shutil.copyfileobj(src, out)
        if target.exists():
            import shutil
            shutil.rmtree(target)
        staging.replace(target)
        license_path = Path(__file__).with_name("licenses") / "third-party" / "NapCat-LICENSE"
        if license_path.is_file():
            (target / "NAPCAT-LICENSE.txt").write_text(
                license_path.read_text("utf-8") + "\n\nSource: " + NAPCAT_REPO + "\n",
                encoding="utf-8")
        (target / "NAPCAT-SOURCE.json").write_text(_json.dumps({
            "project": "NapCatQQ", "source": NAPCAT_REPO, "release": tag,
            "asset": NAPCAT_ASSET, "url": url, "sha256": actual_sha,
            "downloaded_at": time.time(),
            "copyright": "Copyright © 2024 Mlikiowa",
            "license": "Limited Redistribution License (see NAPCAT-LICENSE.txt)",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if progress:
            progress("NapCat 已下载并配置完成")
        return {"installed": True, "path": str(target), "release": tag, "source": NAPCAT_REPO}
    finally:
        import shutil
        shutil.rmtree(temporary, ignore_errors=True)


def runtime_ready(paths: Paths) -> bool:
    return (paths.runtime / "index.js").is_file() and (paths.runtime / "node.exe").is_file()


_SUBST_CACHE: dict[str, str] = {}


def ascii_root(home: Path) -> Path:
    """QQ's native loader is happiest on a short ASCII path; map one when needed.

    The old PowerShell launcher used ``subst`` for the same reason.  A mapping is
    reused across launches and removed only by the user/OS, so no data moves.
    """
    home = Path(home).resolve()
    if os.name != "nt":
        return home
    key = str(home).casefold()
    if key in _SUBST_CACHE:
        return Path(_SUBST_CACHE[key])
    text = str(home)
    if all(ord(char) < 128 for char in text) and " " not in text:
        _SUBST_CACHE[key] = text
        return home
    for letter in "NOPQRSTUVWXYZ":
        drive = Path(letter + ":/")
        try:
            if drive.is_dir() and os.path.samefile(drive, home):
                _SUBST_CACHE[key] = f"{letter}:\\"
                return Path(f"{letter}:\\")
        except OSError:
            continue
    import string
    for letter in string.ascii_uppercase:
        drive = f"{letter}:"
        if not os.path.exists(drive + "\\"):
            try:
                result = subprocess.run(["subst", drive, str(home)], capture_output=True, timeout=10)
                if result.returncode == 0:
                    _SUBST_CACHE[key] = drive + "\\"
                    return Path(drive + "\\")
            except Exception:
                pass
            break
    _SUBST_CACHE[key] = text
    return home


def pid(paths: Paths) -> int:
    try:
        value = int(paths.pid_file.read_text("utf-8").strip())
        return value if value > 0 else 0
    except (OSError, ValueError):
        return 0


def process_alive(value: int) -> bool:
    if not value:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def discover_port(paths: Paths) -> int:
    """The WebUI walks to the next port if 6099 is busy; prefer the log's own URL."""
    try:
        tail = paths.out_log.read_text("utf-8", errors="replace")[-200000:]
    except OSError:
        tail = ""
    matches = re.findall(r"WebUi User Panel Url:\s*http://127\.0\.0\.1:(\d+)/webui", tail)
    for value in reversed(matches):
        if port_open(int(value)):
            return int(value)
    port = int((_read_json(paths.webui, {}) or {}).get("port") or WEBUI_PORT)
    # Never attach to an arbitrary open port in this range: another NapCat
    # instance may be running with a different WebUI token.  Only trust our
    # own runtime PID or the URL written by our own runtime log.
    if process_alive(pid(paths)) and port_open(port):
        return port
    return 0


def webui_client(paths: Paths) -> WebUIClient:
    settings = _settings(paths)
    port = discover_port(paths) or int(settings.get("webui_port") or WEBUI_PORT)
    token = str(settings.get("webui_token") or "")
    if not token:
        token = str((_read_json(paths.webui, {}) or {}).get("token") or "")
    client = WebUIClient(port, token)
    client.login()
    return client


def start(paths: Paths | Path, *, account: str = "", wait: bool = True) -> dict:
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    summary = prepare(paths)
    if not runtime_ready(paths):
        raise DotLinkError("QQ 直连运行时不完整（需要内置 runtime/index.js 和 node.exe）")
    if process_alive(pid(paths)) and (port_open(ONEBOT_PORT) or port_open(int(summary["webui_port"]))):
        return {**summary, "started": False, "pid": pid(paths)}
    settings = _settings(paths)
    vendor = vendor_spec(paths.home)
    mapped = ascii_root(paths.home)
    mapped_runtime = Path(mapped) / "qq-runtime"
    chosen_runtime = mapped_runtime if runtime_ready(Paths(Path(mapped))) else paths.runtime
    env = dict(os.environ)
    env[vendor.get("workdir_env", "DOTLINK_WORKDIR")] = str(Path(mapped) / "dotlink" / "state")
    env[vendor.get("data_dir_env", "DOTLINK_DATA_DIR")] = str(Path(mapped) / "dotlink" / "qq-data")
    env[vendor.get("pipe_env", "DOTLINK_DISABLE_PIPE")] = "1"
    env[vendor.get("webui_token_env", "DOTLINK_WEBUI_TOKEN")] = str(settings["webui_token"])
    env[vendor.get("jwt_env", "DOTLINK_JWT_SECRET")] = str(settings["jwt_secret"])
    env[vendor.get("webui_port_env", "DOTLINK_WEBUI_PORT")] = str(settings.get("webui_port") or WEBUI_PORT)
    env["PYTHONUTF8"] = "1"
    if account:
        env[vendor.get("quick_account_env", "DOTLINK_QUICK_ACCOUNT")] = str(account)
    arguments = [str(chosen_runtime / "node.exe"), "index.js"]
    if account:
        arguments += ["-q", str(account)]
    paths.out_log.parent.mkdir(parents=True, exist_ok=True)
    out = paths.out_log.open("a", encoding="utf-8", errors="replace")
    err = paths.err_log.open("a", encoding="utf-8", errors="replace")
    out.write(f"\n===== DotLink start {time.strftime('%Y-%m-%d %H:%M:%S')} account={account or '-'} =====\n")
    out.flush()
    try:
        process = subprocess.Popen(arguments, cwd=str(chosen_runtime), env=env, stdout=out, stderr=err,
                                   stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        out.close()
        err.close()
        raise DotLinkError(f"无法启动 QQ 直连运行时：{exc}") from None
    finally:
        try:
            out.close()
            err.close()
        except Exception:
            pass
    paths.pid_file.write_text(str(process.pid), encoding="utf-8")
    deadline = time.monotonic() + (START_TIMEOUT if wait else 0)
    port = 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DotLinkError(_startup_failure(paths))
        port = discover_port(paths)
        if port:
            break
        time.sleep(0.4)
    if wait and not port:
        raise DotLinkError("QQ 直连管理端口未在限定时间内启动，请查看运行日志")
    return {**summary, "started": True, "pid": process.pid, "webui_port": port or int(summary["webui_port"])}


def _startup_failure(paths: Paths) -> str:
    try:
        tail = paths.out_log.read_text("utf-8", errors="replace")[-1500:]
    except OSError:
        tail = ""
    return "QQ 直连运行时已退出。" + (("最近日志：" + tail[-600:]) if tail else "")


def _listening_node_pids(ports=(6099, 6100, 6101, 6102, 6103, 6104, 6105, 6106, 6107, 6108, 3001)) -> set[int]:
    """Find node.exe processes listening on the DotLink/NapCat ports.

    Used only by the explicit tray "Exit & Stop Backend" action, where the
    owner asked for every local NapCat instance to close.  The command line is
    checked so an unrelated service that happens to use port 3001 is not killed.
    """
    try:
        import psutil
    except Exception:
        return set()
    found: set[int] = set()
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        if int(connection.laddr.port) not in ports or not connection.pid:
            continue
        try:
            process = psutil.Process(connection.pid)
            name = (process.name() or "").lower()
            command = " ".join(process.cmdline()).lower()
        except (psutil.Error, OSError):
            continue
        if name == "node.exe" and ("qq-runtime" in command or "napcat" in command or "index.js" in command):
            found.add(int(connection.pid))
    return found


def stop(paths: Paths | Path, *, all_instances: bool = False) -> dict:
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    value = pid(paths)
    stopped = False
    targets = {value} if value and process_alive(value) else set()
    if all_instances and os.name == "nt":
        targets |= _listening_node_pids()
    for target in targets:
        if not process_alive(target):
            continue
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.kill(target, 15)
            except OSError:
                pass
        stopped = True
    paths.pid_file.unlink(missing_ok=True)
    return {"stopped": stopped, "pid": value, "stopped_pids": sorted(targets)}


def ensure_started(paths: Paths | Path, *, account: str = "") -> str:
    """Launcher entry: start silently and return an error string (empty on success)."""
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    try:
        prepare(paths)
        if port_open(ONEBOT_PORT):
            return ""
        start(paths, account=account, wait=True)
        return ""
    except Exception as exc:
        return str(exc)


def diagnose(paths: Paths | Path) -> dict:
    """Read-only connection checklist; safe to call while the runtime is running."""
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    checks = []

    def add(name, ok, detail, fix=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "fix": fix})

    ready = runtime_ready(paths)
    add("运行时文件", ready, str(paths.runtime) if ready else "缺少 qq-runtime/index.js 或 node.exe",
        "重新安装或点击“启动 / 修复直连”")
    token = str(_settings(paths).get("webui_token") or "")
    add("管理令牌", bool(token), "已生成" if token else "dotlink/local.json 中没有令牌",
        "点击“启动 / 修复直连”重新生成")
    add("OneBot 配置", paths.onebot.is_file(), str(paths.onebot) if paths.onebot.is_file() else "缺少 onebot11.json",
        "点击“启动 / 修复直连”")
    port = discover_port(paths)
    add("管理端口", bool(port), f"127.0.0.1:{port}" if port else "未监听",
        "点击“启动 / 修复直连”")
    login = {}
    accounts = []
    if port:
        try:
            client = WebUIClient(port, token)
            client.login()
            accounts = client.quick_accounts()
            login = client.status()
            add("管理接口鉴权", True, "登录成功")
        except Exception as exc:
            add("管理接口鉴权", False, str(exc)[:300], "检查管理令牌或重启运行时")
    add("QQ 登录", bool(login.get("isLogin")), f"已登录 {login.get('uin') or ''}".strip() if login.get("isLogin")
        else (login.get("loginError") or "未登录"), "使用快速登录或扫码登录")
    add("OneBot 端口", port_open(ONEBOT_PORT), f"127.0.0.1:{ONEBOT_PORT}" if port_open(ONEBOT_PORT) else "未监听",
        "登录后点击“连接并启动桥接”")
    add("快速登录记录", bool(accounts), f"{len(accounts)} 个账号" if accounts else "没有，需要扫码一次")
    return {"ok": all(item["ok"] for item in checks), "checks": checks,
            "login": login, "accounts": accounts, "timestamp": time.time()}


def state(paths: Paths | Path, *, include_logs: bool = True) -> dict:
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    value = pid(paths)
    port = discover_port(paths)
    running = process_alive(value) or bool(port)
    result = {"running": running, "pid": value, "webui_port": port, "onebot_listening": port_open(ONEBOT_PORT),
              "runtime_ready": runtime_ready(paths), "login": {}, "accounts": [], "qr": {}, "error": ""}
    if include_logs:
        try:
            lines = paths.out_log.read_text("utf-8", errors="replace").splitlines()
            result["logs"] = "\n".join(lines[-60:])
        except OSError:
            result["logs"] = ""
    if not port:
        result["error"] = "QQ 直连管理端口未运行" if not running else "正在等待管理端口"
        return result
    try:
        client = WebUIClient(port, str(_settings(paths).get("webui_token") or ""))
        client.login()
        result["accounts"] = client.quick_accounts()
        status = client.status()
        info = client.login_info() if status.get("isLogin") else {}
        # NapCat can report isLogin=true while the QR is still pending and the
        # identity endpoint has no uin/nick yet.  Treat that as "waiting", not
        # as a logged-in account named "()".
        logged_in = bool(status.get("isLogin")) and bool(info.get("uin") or info.get("nick"))
        result["login"] = {"is_login": logged_in, "phase": status.get("loginPhase", ""),
                           "online": bool(status.get("coreReady")), "uin": str(info.get("uin") or ""),
                           "nickname": str(info.get("nick") or ""), "error": str(status.get("loginError") or ""),
                           "qr_url": str(status.get("qrcodeurl") or "")}
        result["onebot_listening"] = port_open(ONEBOT_PORT)
        if result["login"]["is_login"]:
            result["qr"] = {}
        else:
            url = result["login"].get("qr_url") or ""
            if url:
                result["qr"] = {"url": url, "png": str(paths.qr_png) if paths.qr_png.is_file() else ""}
    except Exception as exc:
        result["error"] = str(exc)
    return result


def begin_login(paths: Paths | Path, *, account: str = "", prefer_quick: bool = True) -> dict:
    """Prefer a saved quick-login account; otherwise return an in-app QR image."""
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    start(paths, account=account)
    client = webui_client(paths)
    accounts = client.quick_accounts()
    if account:
        if not any(item["uin"] == str(account) for item in accounts):
            raise DotLinkError("本机没有这个 QQ 号的快速登录记录；请改用扫码登录一次")
        client.quick_login(account)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.status()
            if status.get("isLogin"):
                return {**state(paths, include_logs=False), "mode": "quick", "qr": {}}
            if status.get("loginError"):
                break
            time.sleep(1.0)
        # fall through to QR so the user is never stranded on a failed quick login
    elif accounts and prefer_quick:
        try:
            client.quick_login(accounts[0]["uin"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = client.status()
                if status.get("isLogin"):
                    return {**state(paths, include_logs=False), "mode": "quick", "qr": {}}
                if status.get("loginError"):
                    break
                time.sleep(1.0)
        except DotLinkError:
            pass
    return {"mode": "qr", "qr": qr_image(paths, client), "accounts": accounts}


def password_login(paths: Paths | Path, uin: str, password: str) -> dict:
    """Stop the current runtime and log in with a QQ number + password.

    The password is only hashed in this process; it is never written to disk.
    A captcha/new-device challenge is surfaced as DotLinkError with the URL.
    """
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    stop(paths)
    start(paths, wait=True)
    client = webui_client(paths)
    client.password_login(uin, password)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        status = client.status()
        if status.get("isLogin"):
            return {**state(paths, include_logs=False), "mode": "password", "qr": {}}
        if status.get("loginError"):
            raise DotLinkError(str(status.get("loginError")))
        time.sleep(1.0)
    raise DotLinkError("密码登录已提交，但 QQ 尚未确认；请稍后刷新状态，或改用扫码登录")


def refresh_qr_image(paths: Paths | Path) -> dict:
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    client = webui_client(paths)
    client.refresh_qr()
    return qr_image(paths, client)


def qr_image(paths: Paths, client: WebUIClient | None = None) -> dict:
    """Fetch/refresh the QR URL and render it locally. Falls back to the runtime cached PNG."""
    client = client or webui_client(paths)
    status = client.status()
    if status.get("isLogin"):
        return {}
    url = str(status.get("qrcodeurl") or "")
    if not url:
        try:
            url = client.qr_url()
        except DotLinkError:
            url = ""
    if not url:
        try:
            url = client.refresh_qr()
        except DotLinkError:
            url = ""
    if not url:
        cached = _read_cached_qr(paths)
        if cached:
            return cached
        raise DotLinkError("暂时拿不到 QQ 登录二维码，请查看运行日志")
    paths.qr_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode
        image = qrcode.make(url)
        image.save(paths.qr_png)
    except Exception:
        cached = _read_cached_qr(paths)
        if cached:
            return {**cached, "url": url}
        return {"url": url, "png": "", "data_url": ""}
    data = paths.qr_png.read_bytes()
    return {"url": url, "png": str(paths.qr_png),
            "data_url": "data:image/png;base64," + base64.b64encode(data).decode()}


def _read_cached_qr(paths: Paths) -> dict:
    for candidate in (paths.qr_png, paths.cache / "qrcode.png"):
        if candidate.is_file() and candidate.stat().st_size > 100:
            data = candidate.read_bytes()
            return {"url": "", "png": str(candidate),
                    "data_url": "data:image/png;base64," + base64.b64encode(data).decode()}
    return {}


def poll_login(paths: Paths | Path) -> dict:
    paths = paths if isinstance(paths, Paths) else Paths(Path(paths))
    try:
        client = webui_client(paths)
    except Exception as exc:
        return {**state(paths, include_logs=False), "error": str(exc)}
    try:
        status = client.status()
        info = client.login_info() if status.get("isLogin") else {}
        return {"is_login": bool(status.get("isLogin")), "phase": status.get("loginPhase", ""),
                "online": bool(status.get("coreReady")), "uin": str(info.get("uin") or ""),
                "nickname": str(info.get("nick") or ""), "error": str(status.get("loginError") or ""),
                "onebot_listening": port_open(ONEBOT_PORT)}
    except Exception as exc:
        return {"is_login": False, "phase": "", "online": False, "error": str(exc),
                "onebot_listening": port_open(ONEBOT_PORT)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Open QQ Dot 桌面直连 QQ（DotLink，无需网页扫码）")
    parser.add_argument("action", choices=("status", "prepare", "start", "stop", "login", "qr", "poll"))
    parser.add_argument("--home", default=os.environ.get("QQ_BRIDGE_HOME", str(Path(__file__).resolve().parent)))
    parser.add_argument("--account", default="")
    args = parser.parse_args(argv)
    paths = Paths(Path(args.home).resolve())
    try:
        if args.action == "status":
            value = state(paths)
        elif args.action == "prepare":
            value = prepare(paths)
        elif args.action == "start":
            value = start(paths, account=args.account)
        elif args.action == "stop":
            value = stop(paths)
        elif args.action == "login":
            value = begin_login(paths, account=args.account)
        elif args.action == "qr":
            value = qr_image(paths)
        else:
            value = poll_login(paths)
        print(json.dumps(value, ensure_ascii=False, indent=1))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
