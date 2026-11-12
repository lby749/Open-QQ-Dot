"""Create a small, redacted diagnostic bundle for support/debugging."""
from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
import time
import zipfile
from pathlib import Path

SECRET = re.compile(r"(?i)(token|password|secret|cookie|credential)(\s*[:=]\s*)['\"]?([^\s,;'\"]+)")
AUTH = re.compile(r"(?im)(authorization\s*[:=]\s*).+$")


def redact(text: str) -> str:
    text = AUTH.sub(lambda match: match.group(1) + "<redacted>", text or "")
    text = SECRET.sub(lambda match: match.group(1) + match.group(2) + "<redacted>", text)
    return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", text)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def report(home: Path) -> dict:
    home = Path(home)
    dotlink = home / "dotlink"
    return {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform.platform(),
        "python": sys.version,
        "ports": {"onebot_3001": _port_open(3001), "webui_6099": _port_open(6099)},
        "files": {
            "runtime_index": (home / "qq-runtime" / "index.js").is_file(),
            "dotlink_settings": (dotlink / "local.json").is_file(),
            "friends": (home / "friends.json").is_file(),
        },
        "logs": sorted(path.name for path in (home / "logs").glob("*.log"))[:20] if (home / "logs").is_dir() else [],
    }


def export_bundle(home: Path, target: Path) -> dict:
    """Write a zip with report.json plus the tail of known logs, tokens redacted."""
    home, target = Path(home), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    candidates = [home / "bridge.log", home / "bridge.err.log", home / "installer.log",
                  home / "installation.log", home / "dotlink" / "runtime.out.log",
                  home / "dotlink" / "runtime.err.log"]
    candidates += sorted((home / "logs").glob("*.log")) if (home / "logs").is_dir() else []
    included = []
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("report.json", json.dumps(report(home), ensure_ascii=False, indent=2))
        package.writestr("README.txt", "Open QQ Dot 诊断包；令牌、Authorization 和密码已脱敏。\n"
                                      "请先确认内容再发送给维护者。\n")
        for path in candidates:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                text = path.read_text("utf-8", errors="replace").splitlines()[-500:]
            except OSError:
                continue
            package.writestr("logs/" + path.name, redact("\n".join(text)))
            included.append(path.name)
    temporary.replace(target)
    return {"target": str(target), "logs": included, "bytes": target.stat().st_size}
