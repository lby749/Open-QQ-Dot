""": Claude Code  Codex ,  App . 

ok: .venv\\Scripts\\python.exe quota-whale\\quota_whale.py
 () :  --check
"""
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

import quota_sources as qs

HERE = Path(__file__).resolve().parent
HTML = HERE / "whale.html"
SIZE = 250          #  () ,  .dshwv-root  250px
TICK_MS = 1000      #  /
QUOTA_S = 60        # Refresh,  REFRESH_MS

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
DWMWA_EXTENDED_FRAME_BOUNDS = 9  # ,  GetWindowRect
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

#  App  ( .exe) →
CARDS = [
    {"apps": {"claude"}, "key": "claude"},
    {"apps": {"codex"}, "key": "codex"},
]

state = {"version": 0, "codex": None, "claude": None}


def app_window_rect(app_names):
    """ (, ) ;  None. """
    pids = {
        p.info["pid"]
        for p in psutil.process_iter(["pid", "name"])
        if (p.info["name"] or "").lower().removesuffix(".exe") in app_names
    }
    best = None

    def visit(hwnd, _):
        nonlocal best
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        rect = wintypes.RECT()
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 400 or h < 300:  # ,
            return True
        if best is None or w * h > best[2] * best[3]:
            best = (rect.right, rect.bottom, w, h)
        return True

    user32.EnumWindows(EnumWindowsProc(visit), 0)
    return None if best is None else best[:2]


def refresh_forever():
    while True:
        try:
            state["codex"] = qs.codex_quota()
            state["claude"] = qs.claude_usage()
        except OSError:
            pass  # failed,
        state["version"] += 1
        time.sleep(QUOTA_S)


def card_text(key):
    now = datetime.now(timezone.utc)
    if key == "codex":
        q = state["codex"]
        if q is None:
            return "Codex 额度", "--", "无本地数据"
        left_5h = max(0.0, 100.0 - q["five_used"])
        left_week = max(0.0, 100.0 - q["week_used"])
        stale = q["updated"] is None or now - q["updated"] > timedelta(hours=1)
        hint = f"Weekly remaining:  {left_week:.0f}%" + (" · 快照旧" if stale else "")
        return "Codex 额度", f"5h {left_5h:.0f}%", hint
    u = state["claude"]
    if u is None:
        return "Claude 额度", "--", "无本地数据"
    return "Claude 额度", f"5h {qs.fmt_tokens(u['five_h'])}", f"Week:  {qs.fmt_tokens(u['week'])} · Est."


class Card:
    def __init__(self, key, apps):
        self.key = key
        self.apps = apps
        self.loaded = False
        self.shown_version = -1
        self.view = QWebEngineView()
        self.view.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput  # :  App
            | Qt.WindowDoesNotAcceptFocus
        )
        self.view.setAttribute(Qt.WA_TranslucentBackground)
        self.view.setAttribute(Qt.WA_ShowWithoutActivating)
        self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.view.setFixedSize(SIZE, SIZE)
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(HTML)))

    def _on_loaded(self, ok):
        self.loaded = bool(ok)
        self.shown_version = -1

    def tick(self):
        anchor = app_window_rect(self.apps)
        if anchor is None:
            self.view.hide()
        else:
            dpr = QGuiApplication.primaryScreen().devicePixelRatio()
            right, bottom = anchor[0] / dpr, anchor[1] / dpr
            self.view.move(int(right - SIZE), int(bottom - SIZE))
            if not self.view.isVisible():
                self.view.show()
        if self.loaded and self.shown_version != state["version"]:
            label, amount, hint = card_text(self.key)
            self.view.page().runJavaScript(
                f"setQuota({json.dumps(label)}, {json.dumps(amount)}, {json.dumps(hint)})"
            )
            self.shown_version = state["version"]


def check():
    """, , . """
    state["codex"] = qs.codex_quota()
    state["claude"] = qs.claude_usage()
    for card in CARDS:
        print(card["key"], card_text(card["key"]), "anchor:", app_window_rect(card["apps"]))


def main():
    if "--check" in sys.argv:
        check()
        return
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    threading.Thread(target=refresh_forever, daemon=True).start()
    cards = [Card(c["key"], c["apps"]) for c in CARDS]
    timer = QTimer()
    timer.timeout.connect(lambda: [card.tick() for card in cards])
    timer.start(TICK_MS)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
