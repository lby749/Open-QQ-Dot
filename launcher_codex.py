"""Desktop Launcher for Open QQ Dot Antigravity Codex Cockpit.

Launches the background Cockpit server and seamlessly opens the UI in native frameless
Desktop App Mode via Microsoft Edge or Google Chrome (--app=http://127.0.0.1:8791).
"""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from cockpit_server import PORT


def is_port_open(host="127.0.0.1", port=PORT):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_server_if_needed():
    if is_port_open():
        from urllib.error import HTTPError
        from urllib.request import urlopen
        try:
            urlopen(f'http://127.0.0.1:{PORT}/', timeout=2).close()
        except HTTPError as error:
            if error.code == 401 and error.headers.get('X-OpenQQDot-Service') == 'cockpit':
                return
        raise RuntimeError(f'{PORT} 端口已被其他服务占用，不能将其当作管理工作台打开。')
    from cockpit_server import run_server
    t = threading.Thread(target=run_server, args=(PORT,), daemon=True, name="cockpit-server")
    t.start()
    for _ in range(30):
        if is_port_open():
            return
        time.sleep(0.1)
    raise RuntimeError(f'管理工作台未能启动，请检查 {PORT} 端口和本地配置。')


def find_browser_app_executable():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def main():
    start_server_if_needed()
    url = f"http://127.0.0.1:{PORT}"

    browser_bin = find_browser_app_executable()
    if browser_bin:
        # Native App Mode: no URL bar, custom window frame, hardware accelerated
        profile_dir = ROOT / "data" / "cockpit_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            browser_bin,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1280,820",
            "--window-position=120,80",
        ]
        try:
            subprocess.run(cmd)
            return
        except Exception:
            pass

    # Fallback to default browser
    webbrowser.open(url)
    print(f"Antigravity Codex Cockpit running at {url}. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
