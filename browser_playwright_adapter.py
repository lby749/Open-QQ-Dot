"""Actual browser operations isolated to a dedicated thread per user/session.

Unlike HTML parsing and synthetic images, actions run against a live Chromium
page. This is an optional dependency: absent browser binaries are reported as
failures, NEVER converted to simulated successes. Production must also enforce
an OS-level egress policy: URL/DNS checks alone cannot prevent DNS rebinding.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import threading
from typing import Any


class PlaywrightBrowserAdapter:
    def __init__(self, session):
        self.session = session
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"qq-browser-{session.owner_id}")
        self._playwright = None
        self._context = None
        self._page = None
        self._closed = False
        self._mutex = threading.RLock()

    def _route(self, route):
        try:
            if route.request.url != "about:blank":
                self.session._validate_target_url(route.request.url, is_redirect=True)
            route.continue_()
        except (PermissionError, ValueError):
            route.abort("blockedbyclient")

    def _ensure(self):
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            profile = self.session.session_dir / "chromium_profile"
            binary = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
            options = {"executable_path": binary} if binary else {}
            if not binary and os.name == 'nt':
                edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
                if edge.is_file():
                    options = {'channel': 'msedge'}
            if self.session.proxy_server:
                options['proxy'] = {'server': self.session.proxy_server, 'bypass': 'localhost,127.0.0.1,[::1]'}
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=True, accept_downloads=True,
                service_workers="block", permissions=[], **options,
            )
            self._context.route("**/*", self._route)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except BaseException:
            self._playwright.stop()
            self._playwright = None
            raise

    def _snapshot(self) -> dict[str, Any]:
        self.session._validate_target_url(self._page.url)
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "html": self._page.content(),
            "content": self._page.locator("body").inner_text(timeout=4000)[:12000],
        }

    def _execute(self, action: str, options: dict[str, Any]) -> dict[str, Any]:
        self._ensure()
        page = self._page
        if action == "navigate":
            url = self.session._validate_target_url(options["url"])
            response = page.goto(url, timeout=int(options.get("timeout_sec", 15)*1000), wait_until="domcontentloaded")
            if response is not None and response.status >= 400:
                return {"status": "failed", "http_status": response.status, "error": f"HTTP {response.status}"}
            return {"status": "success", "http_status": response.status if response else None, **self._snapshot()}
        if action == "click":
            loc = page.locator(options["selector"])
            before = page.content()
            is_submit = loc.evaluate("el => Boolean(el.form && ['submit', 'image'].includes(el.type))")
            navigations = []
            def on_request(request):
                if request.is_navigation_request() and request.frame == page.main_frame:
                    navigations.append(request.url)
            page.on('request', on_request)
            try:
                loc.click(timeout=int(options.get("timeout_sec", 10)*1000))
            finally:
                page.remove_listener('request', on_request)
            snapshot = self._snapshot()
            return {"status": "clicked", "selector": options["selector"],
                    "dom_mutated": snapshot['html'] != before,
                    "form_submitted": is_submit and bool(navigations), **snapshot}
        if action == "input":
            loc = page.locator(options["selector"])
            if loc.evaluate('node => node.tagName') == 'SELECT':
                loc.select_option(options.get('text',''),timeout=5000)
            else:
                loc.fill(options.get("text", ""), timeout=5000)
            return {"status": "filled", "selector": options["selector"],
                    "dom_value_updated": loc.input_value() == options.get('text', ''), **self._snapshot()}
        if action == "wait":
            loc = page.locator(options["selector"])
            loc.wait_for(state="visible" if options.get("visible", True) else "attached", timeout=int(options.get("timeout_sec", 5)*1000))
            return {"status": "success", "selector": options["selector"], "visible": loc.is_visible(), **self._snapshot()}
        if action == "read":
            selector = options.get("selector")
            if selector:
                loc = page.locator(selector)
                content = loc.inner_text(timeout=5000) if not (loc.count() and loc.first.evaluate("el => 'value' in el")) else loc.first.input_value()
            else:
                content = page.locator("body").inner_text(timeout=5000)
            snap = self._snapshot()
            return {**snap, "status": "success", "content": content[:int(options.get("max_chars", 4000))]}
        if action == "scroll":
            amount = int(options.get("pixels", 600)) * (-1 if options.get("direction") == "up" else 1)
            page.mouse.wheel(0, amount)
            return {"status": "scrolled", "scroll_y": page.evaluate("window.scrollY"), **self._snapshot()}
        if action == "screenshot":
            dest = Path(options["path"])
            if dest.suffix.lower() != ".png":
                raise ValueError("Real browser screenshot only supports PNG; synthetic SVG not supported")
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(dest), full_page=True)
            return {"status": "screenshot_saved", "path": str(dest), "engine": "playwright_chromium"}
        if action == "upload":
            page.locator(options["selector"]).set_input_files(str(options["path"]), timeout=5000)
            return {"status": "uploaded", "selector": options["selector"], **self._snapshot()}
        raise ValueError(f"Unsupported real browser action: {action}")

    def run(self, action: str, **options) -> dict[str, Any]:
        with self._mutex:
            if self._closed:
                return {"status": "failed", "error": "Browser session is closed"}
            try:
                return self.executor.submit(self._execute, action, options).result(timeout=max(15, float(options.get('timeout_sec', 15))+15))
            except Exception as exc:
                if action == 'wait' and type(exc).__name__ == 'TimeoutError':
                    return {"status": "timeout", "error": str(exc)}
                return {"status": "failed", "error": f"Real browser {action} failed: {type(exc).__name__}: {exc}"}

    def _shutdown(self):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
        self._context = None
        self._playwright = None
        self._page = None

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            self.executor.submit(self._shutdown).result(timeout=15)
            self.executor.shutdown(wait=True)
