"""Prepare an agent source: open its app/page, install a known CLI, or guide config.

Only the OpenCode desktop installer is downloaded automatically (with a
release SHA-256 check).  For other tools we either start an already installed
local tool, install its official npm package when npm is available, or open its
official page.  Custom API/ACP/MCP sources can only be configured, not installed
by this module; they return a precise instruction instead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

# provider -> (command candidates, desktop app candidates, npm package, official page)
SPECS = {
    "dsh": (("dsh", "dsh.cmd"), (), "", ""),
    "claude": (("claude", "claude.cmd"), (), "@anthropic-ai/claude-code", "https://docs.anthropic.com/en/docs/claude-code"),
    "codex": (("codex", "codex.cmd"), (), "@openai/codex", "https://developers.openai.com/codex/cli/"),
    "antigravity": (("agy", "antigravity"), (), "", "https://antigravity.google"),
    "cursor": (("cursor-agent", "cursor"), (), "", "https://cursor.com/downloads"),
    "qwen_code": (("qwen", "qwen.cmd"), (), "@qwen-code/qwen-code", "https://github.com/QwenLM/qwen-code"),
    "zcode": (("zcode", "zcode.cmd"), (), "", ""),
    "kimi_code": (("kimi", "kimi.cmd"), (), "", "https://kimi.moonshot.cn"),
    "acp": ((), (), "", ""),
    "mcp": ((), (), "", ""),
    "custom": ((), (), "", ""),
}

WEB_URLS = {
    "webchat_deepseek": "https://chat.deepseek.com",
    "webchat_chatgpt": "https://chatgpt.com",
    "webchat_gemini": "https://gemini.google.com/app",
}

DESKTOP_PATHS = {
    "cursor": (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cursor" / "Cursor.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Cursor" / "Cursor.exe",
    ),
    "antigravity": (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity" / "Antigravity.exe",
    ),
    "dsh": (
        Path("D:/dsh/dsh.exe"),
        Path("D:/dsh/DeepSeek Harness.exe"),
    ),
    "zcode": (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ZCode" / "ZCode.exe",
    ),
}


def _which(names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _desktop(provider: str) -> str:
    for path in DESKTOP_PATHS.get(provider, ()):
        if path.is_file():
            return str(path)
    return ""


def _open_console(command: str) -> None:
    if os.name == "nt":
        suffix = Path(command).suffix.casefold()
        if suffix in (".cmd", ".bat"):
            # cmd.exe /k ""path"" mis-parses nested quotes; `call` handles .cmd paths with spaces.
            subprocess.Popen(["cmd.exe", "/k", f'call "{command}"'],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
        else:
            subprocess.Popen([command], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
    else:
        subprocess.Popen([command], close_fds=True)


def _npm_metadata(package: str) -> tuple[str, str]:
    """Exact version and registry integrity for an official npm package."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("未找到 npm；请先安装 Node.js，或手动安装该 CLI")
    result = subprocess.run([npm, "view", package, "version", "dist.integrity", "--json"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "无法读取 npm 包信息").strip()[:300])
    try:
        data = json.loads(result.stdout)
        version, integrity = data["version"], data["dist.integrity"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("npm 返回的版本或完整性信息无效") from exc
    if not isinstance(integrity, str) or not integrity.startswith(("sha512-", "sha384-")):
        raise RuntimeError("npm 包没有可验证的 registry integrity")
    return version, integrity


def _install_npm(package: str, version: str) -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("未找到 npm；请先安装 Node.js，或手动安装该 CLI")
    # npm verifies the tarball against the registry integrity for this exact version.
    result = subprocess.run([npm, "install", "-g", f"{package}@{version}"],
                            capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "npm 安装失败").strip()[:300])


def prepare(provider: str, progress=None) -> str:
    """Run the best available one-click action for a model source."""
    if provider == "opencode":
        import opencode_install
        return opencode_install.install_and_launch(progress)
    if provider in WEB_URLS:
        webbrowser.open(WEB_URLS[provider])
        return "已打开网页登录页；登录后回到这里点「检测接入」。"
    if provider in ("custom", "acp", "mcp"):
        return "这个来源不需要下载：请在下方卡片填写地址、协议、模型或 ACP 启动命令，保存后点「检测接入」。"
    commands, _, package, page = SPECS.get(provider, ((), (), "", ""))
    desktop = _desktop(provider)
    if desktop:
        subprocess.Popen([desktop], close_fds=True)
        return f"已打开本机 {provider} 应用；请在里面自行登录。"
    executable = _which(commands)
    if executable:
        _open_console(executable)
        return f"已打开 {provider} 的登录/交互终端；登录完成后回到这里点「检测接入」。"
    if package:
        if progress:
            progress(5, f"正在校验 {provider} 官方包完整性…")
        version, _integrity = _npm_metadata(package)
        if progress:
            progress(10, f"正在安装 {provider} {version}…")
        _install_npm(package, version)
        executable = _which(commands)
        if executable:
            _open_console(executable)
            return f"已安装并打开 {provider}；请在终端里完成登录，然后回来点「检测接入」。"
        raise RuntimeError(f"{package} 已安装，但没有找到命令 {commands[0]}")
    if page:
        webbrowser.open(page)
        return f"没有检测到本机 {provider}；已打开官方安装页，安装后回来点「检测接入」。"
    raise RuntimeError(f"没有检测到 {provider}，也没有可用的自动安装源；请先安装，或填写程序路径。")
