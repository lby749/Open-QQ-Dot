"""One-click OpenCode desktop download/launch.

OpenCode is an optional third-party agent.  We only download the official
desktop installer from its GitHub release, verify the release's SHA-256 digest,
install it silently, then open the app so the operator can sign in themselves.
No account token, cookie or password is read or stored here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from tls_support import client_context

RELEASE_API = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
# Used only when the GitHub API is rate-limited; digest is from the official
# v1.19.4 asset.  A newer API response always wins.
FALLBACK_URL = "https://github.com/anomalyco/opencode/releases/download/v1.19.4/opencode-desktop-win-x64.exe"
FALLBACK_SHA256 = "d83dc2df16070bee6a08c7ec51dfd9d94b28859bce2d0e65662fab47715d4e09"
USER_AGENT = "OpenQQDot/1.0 OpenCode-installer"


class OpenCodeInstallError(RuntimeError):
    pass


def desktop_candidates() -> list[Path]:
    """Known Electron install locations for the Windows desktop app."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    return [
        local / "Programs" / "ai.opencode.desktop" / "OpenCode.exe",
        local / "Programs" / "@opencodedesktop" / "OpenCode.exe",
        local / "Programs" / "opencode" / "OpenCode.exe",
    ]


def desktop_executable() -> Path | None:
    return next((path for path in desktop_candidates() if path.is_file()), None)


def _release_info() -> tuple[str, str, str]:
    """(installer url, sha256, version) from the latest official release."""
    req = urllib.request.Request(RELEASE_API, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20, context=client_context()) as response:
            data = json.loads(response.read(2_000_000))
        for asset in data.get("assets") or []:
            if asset.get("name") != "opencode-desktop-win-x64.exe":
                continue
            url = str(asset.get("browser_download_url") or "")
            digest = str(asset.get("digest") or "")
            match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
            if url.startswith("https://github.com/") and match:
                return url, match.group(1).lower(), str(data.get("tag_name") or "")
    except Exception:
        pass
    return FALLBACK_URL, FALLBACK_SHA256, "v1.19.4"


def download_installer(target: Path, progress=None) -> tuple[Path, str]:
    """Download and hash-check the official installer; returns (path, version)."""
    url, expected, version = _release_info()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=90, context=client_context()) as response, temporary.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(int(done * 100 / total) if total else 0,
                             f"正在下载 OpenCode {version}：{done / 1048576:.1f}"
                             + (f"/{total / 1048576:.1f} MB" if total else " MB"))
        if digest.hexdigest().lower() != expected.lower():
            raise OpenCodeInstallError("OpenCode 安装包校验失败，已停止安装")
        temporary.replace(target)
        return target, version
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def install_and_launch(progress=None) -> str:
    """Open the existing app, or install the verified official app and open it."""
    existing = desktop_executable()
    if existing is not None:
        subprocess.Popen([str(existing)], close_fds=True)
        return f"已打开 OpenCode：{existing}。请在应用里自行登录，然后回来点「检测接入」。"

    installer, version = download_installer(
        Path(tempfile.gettempdir()) / "OpenQQDot" / "opencode-desktop-win-x64.exe", progress)
    if progress:
        progress(100, "正在安装 OpenCode…")
    done = subprocess.run([str(installer), "/S"], timeout=600,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise OpenCodeInstallError(f"OpenCode 安装器返回 {done.returncode}")
    exe = desktop_executable()
    if exe is None:
        # A per-user NSIS install can take a moment to publish OpenCode.exe.
        import time
        for _ in range(20):
            time.sleep(0.5)
            exe = desktop_executable()
            if exe is not None:
                break
    if exe is None:
        raise OpenCodeInstallError("OpenCode 已安装，但没有找到 OpenCode.exe；请手动打开后登录。")
    subprocess.Popen([str(exe)], close_fds=True)
    return f"OpenCode {version} 已安装并打开。请在应用里自行登录，然后回来点「检测接入」。"
