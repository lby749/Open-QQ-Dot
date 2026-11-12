"""Smoke-test the phone house inside a running MuMu/Android emulator.

The desktop app must be running (its house_web server listens on 127.0.0.1:8790).
This script uses adb's reverse port forwarding, opens the friend page, then taps
the bottom tabs and saves screenshots.  It is deliberately dependency-free.

Example:
  python tools/mobile_smoke.py --qq 1000000001
  Replace this placeholder with an explicitly authorized test account.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def find_adb(explicit: str = "") -> str:
    if explicit:
        return explicit
    candidates = [
        os.environ.get("ADB", ""),
        r"D:\Program Files\Netease\MuMu\nx_device\15.0\shell\adb.exe",
        r"D:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe",
        shutil.which("adb") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("找不到 adb，请用 --adb 指定 MuMu 的 adb.exe")


def run(adb: str, *args: str) -> str:
    result = subprocess.run([adb, *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
    if result.returncode:
        raise SystemExit(f"adb {' '.join(args)} 失败：{result.stderr or result.stdout}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="MuMu 手机端冒烟测试")
    parser.add_argument("--qq", required=True, help="好友 QQ 号")
    parser.add_argument("--token", default="", help="好友令牌；留空自动生成")
    parser.add_argument("--adb", default="", help="adb.exe 路径")
    parser.add_argument("--package", default="com.danielealbano.androidremotecontrolmcp.foss.debug")
    parser.add_argument("--activity", default="com.danielealbano.androidremotecontrolmcp.ui.FriendHouseActivity")
    parser.add_argument("--browser", action="store_true", help="改用系统浏览器打开网页版")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--out", default=str(ROOT / "exports" / "mobile-smoke"))
    args = parser.parse_args()
    adb = find_adb(args.adb)
    token = args.token
    if not token:
        import house_web
        token = house_web.friend_token(args.qq)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    run(adb, "reverse", f"tcp:{args.port}", f"tcp:{args.port}")
    page = f"http://127.0.0.1:{args.port}/?qq={args.qq}#t={token}"
    if args.browser:
        run(adb, "shell", f"am start -a android.intent.action.VIEW -d '{page}'")
        target_url = page
    else:
        from urllib.parse import quote
        target_url = (f"friendhouse://pair?pc={quote(f'http://127.0.0.1:{args.port}/', safe='')}"
                      f"&qq={args.qq}&t={quote(token, safe='')}")
        run(adb, "shell", f"am start -a android.intent.action.VIEW -d '{target_url}' "
                          f"-n {args.package}/{args.activity}")
    time.sleep(5)
    size = run(adb, "shell", "wm", "size")
    width, height = 1080, 1920
    if ":" in size:
        try:
            width, height = (int(part) for part in size.split(":")[-1].strip().split("x"))
        except ValueError:
            pass
    shots = []
    tabs = (("status", 0.09), ("console", 0.33), ("chat", 0.55), ("memory", 0.73), ("all", 0.91))
    for name, fraction in tabs:
        if name != "status":
            run(adb, "shell", "input", "tap", str(int(width * fraction)), str(int(height * 0.95)))
            time.sleep(1.5)
        target = out / f"mobile-{name}.png"
        with target.open("wb") as stream:
            process = subprocess.run([adb, "exec-out", "screencap", "-p"], stdout=stream, timeout=60)
        if process.returncode or target.stat().st_size < 1000:
            raise SystemExit(f"截图失败：{name}")
        shots.append(str(target))
    print(json.dumps({"ok": True, "url": target_url, "screenshots": shots}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
