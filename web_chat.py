"""Ask the ChatGPT / Gemini windows the operator is already signed in to.

2026-10-26 user: 用已经登录好的浏览器，别再让人登录一次。
做法：Chrome 用「短路径的 user-data-dir + 调试端口」启动（Chrome 只拦默认路径的调试端口），
再用 Playwright 通过 CDP 复用这套登录状态；窗口照常可以自己用。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.error
import urllib.request

PORT = 9222
CHROME = os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe")
# Chrome 只拦“默认路径 + 调试端口”，用同一套登录的 8.3 短路径即可带端口启动，不必重新登录。
DEFAULT_PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

SOURCES = {
    "chatgpt": {"label": "网页对话 · ChatGPT", "url": "https://chatgpt.com/",
                "composers": ["#prompt-textarea", "div[contenteditable='true']", "textarea"],
                "replies": ['div[class*="DilResponseRoot"]', 'div[class*="prose"]'],
                "marker": ("ChatGPT said:", ("You said:", "ChatGPT can make mistakes")),
                "models": ["GPT-6.1 Sol", "GPT-6 Luna", "GPT-5.6 Luna"],
                "efforts": ["高", "中", "低"]},
    "gemini": {"label": "网页对话 · Gemini", "url": "https://gemini.google.com/app",
               "composers": ["rich-textarea div[contenteditable='true']", "div[contenteditable='true']"],
               "replies": ["div.presented-response-container", "div.response-container-content"],
               "marker": ("Gemini 说", ("你说", "Gemini 是一款")),
               "models": ["Flash", "Pro"],
               "efforts": ["高", "中", "低"]},
    "deepseek": {"label": "网页对话 · DeepSeek", "url": "https://chat.deepseek.com/",
                 "composers": ["textarea[placeholder*='Message DeepSeek']", "textarea", "div[contenteditable='true']"],
                 "replies": ['div.ds-markdown', 'div[class*="markdown"]'],
                 "marker": ("DeepSeek", ("深度思考", "内容由 AI 生成")),
                 "models": ["DeepSeek Chat", "DeepSeek Reasoner"],
                 "efforts": ["深度思考", "关闭"]},
}

# 站点上的模型 / 思考强度选择器：点开它，再点菜单里的对应文字。都是“尽量试一次”，失败不影响问答。
MODEL_SWITCHERS = {
    "chatgpt": ['button:has-text("ChatGPT")', 'button[aria-label*="模型"]', 'button[aria-haspopup="menu"]'],
    "gemini": ['button:has-text("Flash")', 'button:has-text("Pro")', 'button[aria-label*="模型"]'],
    "deepseek": ['div[class*="model"]', 'button:has-text("DeepSeek")'],
}


def models_for(source: str) -> list[str]:
    return list((SOURCES.get(source) or {}).get("models") or [])


def efforts_for(source: str) -> list[str]:
    return list((SOURCES.get(source) or {}).get("efforts") or [])


def _choose(page, source: str, wanted: str) -> bool:
    """Best-effort: open the site's model/effort menu and click the entry with this text."""
    wanted = (wanted or "").strip()
    if not wanted:
        return False
    for selector in MODEL_SWITCHERS.get(source, []):
        try:
            trigger = page.locator(selector).first
            trigger.wait_for(state="visible", timeout=3_000)
            trigger.click()
            time.sleep(0.6)
            option = page.get_by_role("menuitem", name=re.compile(re.escape(wanted), re.I)).first
            if not option.count():
                option = page.locator(f'[role="menuitem"]:has-text("{wanted}")').first
            if not option.count():
                option = page.get_by_text(wanted, exact=False).first
            option.click(timeout=4_000)
            time.sleep(0.6)
            return True
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            continue
    return False


class WebChatError(RuntimeError):
    """The page could not be driven; the text is shown to the operator."""


def _endpoint() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as reply:
            return b"Browser" in reply.read(4096)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _short_path(path: str) -> str:
    """8.3 form of a long Windows path; Chrome only refuses the debug port on the *default* path.

    Deriving it at runtime keeps this working on other people's machines instead of hardcoding
    one profile path.
    """
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 1024):
            return buffer.value or path
    except Exception:
        pass
    return path


def _debug_browser_processes():
    try:
        import psutil
        found = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(proc.info.get("cmdline") or [])
            if f"--remote-debugging-port={PORT}" in cmd:
                found.append((proc, cmd))
        return found
    except Exception:
        return []


def _debug_browser_uses_proxy() -> bool:
    return any("--proxy-server=" in cmd for _, cmd in _debug_browser_processes())


def _stop_debug_browser():
    import psutil
    for proc, _ in _debug_browser_processes():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    for _ in range(20):
        if not _endpoint():
            return
        time.sleep(0.25)


def ensure_browser(profile: str = "Default", headless: bool = True, source: str = "") -> None:
    """Start Chrome once with the debug port on the operator's own profile.

    ``headless=True`` is the default: the console calls the web chat silently and
    must not steal focus or open a visible window.  If a debug-enabled Chrome is
    already running, it is reused as-is.
    """
    if source:
        import network_guard
        network_guard.require(source)
    if _endpoint():
        if source:
            import network_guard
            if network_guard.is_overseas(source) and not _debug_browser_uses_proxy():
                # The existing debug Chrome has no proxy; restart only that debug instance
                # so an overseas call never goes out on the real IP.
                _stop_debug_browser()
            else:
                return
    if not os.path.isfile(CHROME):
        raise WebChatError("没有找到 Chrome。")
    args = [CHROME, f"--user-data-dir={_short_path(DEFAULT_PROFILE)}",
            f"--profile-directory={profile}",
            f"--remote-debugging-port={PORT}", "--no-first-run", "--no-default-browser-check",
            "--restore-last-session"]
    if source:
        import network_guard
        args += network_guard.chrome_proxy_args(source)
    if headless:
        args[1:1] = ["--headless=new", "--disable-gpu", "--hide-scrollbars"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        if _endpoint():
            return
    raise WebChatError("浏览器没有带着调试端口起来。")


def _page(context, url: str):
    host = url.split("//", 1)[-1].split("/", 1)[0]
    for page in context.pages:
        if host in (page.url or ""):
            return page
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    return page


def _composer(page, source: str):
    for selector in SOURCES[source]["composers"]:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=6_000)
            return locator
        except Exception:
            continue
    raise WebChatError("没找到输入框：这个账号可能还没登录。")


def _last_reply(page, source: str) -> str:
    for selector in SOURCES[source]["replies"]:
        items = page.locator(selector)
        try:
            count = items.count()
        except Exception:
            continue
        if not count:
            continue
        text = (items.nth(count - 1).inner_text() or "").strip()
        if text:
            return text
    return ""


def _answer_text(page, source: str, prompt: str) -> str:
    """Structured selector first; when the site changes its DOM, fall back to the page text.

    2026-10-26: ChatGPT dropped data-message-author-role, so the selector path returned "" while the
    answer was plainly on screen. The text after our own echoed prompt is the answer either way.
    """
    text = _last_reply(page, source)
    if text:
        return text
    try:
        body = page.inner_text("body")
    except Exception:
        return ""
    index = body.rfind(prompt)
    if index < 0:
        return ""
    tail = body[index + len(prompt):].strip()
    # 页面正文后面接着输入框和站点页脚，截掉它们。
    for marker in ("与 ChatGPT 对话", "ChatGPT 可能会出错", "Ask anything", "Gemini 是一款",
                   "输入内容", "Send message", "语音输入", "请升级"):
        cut = tail.find(marker)
        if cut > 0:
            tail = tail[:cut].strip()
    return tail


def _answers(page, source: str) -> tuple[int, str]:
    """How many answers the page shows, and the newest one.

    2026-10-26: 说话人标记（You said / ChatGPT said）只在部分轮次出现，靠不住；
    结构化容器在两个站点都稳定，所以先数容器，再用正文标记兜底。
    """
    for selector in SOURCES[source]["replies"]:
        items = page.locator(selector)
        try:
            count = items.count()
        except Exception:
            continue
        if not count:
            continue
        newest = ""
        for index in range(count - 1, -1, -1):
            try:
                value = (items.nth(index).inner_text() or "").strip()
            except Exception:
                continue
            if value:
                newest = value
                break
        if newest:
            return count, _clean(newest)
    return 0, ""


CHROME_LINES = {"Flash", "高", "中", "低", "Pro", "思考", "Thinking", "Share", "编辑", "复制"}


def _clean(value: str) -> str:
    """Drop the trailing model chip / button labels the page leaves under the answer."""
    lines = [line.rstrip() for line in (value or "").splitlines()]
    while lines and (not lines[-1].strip() or lines[-1].strip() in CHROME_LINES):
        lines.pop()
    return "\n".join(lines).strip()


def _send(page, composer, prompt: str, silent: bool = True) -> None:
    """Fill the composer, confirm it really holds the text, then submit.

    2026-10-26: a click that landed while another window had focus left the composer empty and the
    whole call timed out; plain typing also misses <textarea> and rich editors. Try three ways to
    put the text in (fill / keyboard.insert_text / type) and read the composer back each time.
    """
    strategies = (
        lambda: composer.fill(prompt, timeout=5_000),
        lambda: (composer.click(timeout=5_000), page.keyboard.insert_text(prompt)),
        lambda: (composer.click(timeout=5_000), page.keyboard.type(prompt, delay=8)),
    )
    for attempt in range(4):
        if not silent:
            # 2026-10-26 user: 默认静默——后台标签页也能填字/回车（走 CDP，不需要窗口在前台）。
            try:
                page.bring_to_front()
                page.evaluate("() => window.focus()")
            except Exception:
                pass
        try:
            composer.click(timeout=8_000)
        except Exception:
            pass
        strategy = strategies[attempt % len(strategies)]
        try:
            strategy()
        except Exception:
            pass
        time.sleep(0.5)
        held = ""
        try:
            tag = composer.evaluate("e => e.tagName")
            if tag in ("TEXTAREA", "INPUT"):
                held = (composer.input_value() or "").strip()
            else:
                held = (composer.inner_text() or "").strip()
        except Exception:
            try:
                held = (composer.input_value() or "").strip()
            except Exception:
                held = ""
        if prompt.strip()[:40] and prompt.strip()[:40] in held:
            page.keyboard.press("Enter")
            time.sleep(0.8)
            return
        time.sleep(0.8)
    raise WebChatError("网页输入框没吃进文字（窗口可能被挡住，或该站点改了结构）。")


def ask(source: str, prompt: str, timeout: float = 180.0, silent: bool = True,
        model: str = "", effort: str = "") -> str:
    source = (source or "").strip().lower()
    if source not in SOURCES:
        raise WebChatError(f"不认识的来源：{source}")
    prompt = (prompt or "").strip()
    if not prompt:
        raise WebChatError("没有要发送的内容。")
    ensure_browser(source=source)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        context = browser.contexts[0]
        page = _page(context, SOURCES[source]["url"])
        if not silent:
            page.bring_to_front()
        # 2026-10-26 user: 网页端也能调模型和思考强度，控制台里选完这里照着点一次；点不到就算了，不影响问答。
        if model:
            _choose(page, source, model)
        if effort:
            _choose(page, source, effort)
        composer = _composer(page, source)
        before_count, before_text = _answers(page, source)
        _send(page, composer, prompt, silent=silent)
        deadline = time.monotonic() + timeout
        answer, stable = "", 0.0
        while time.monotonic() < deadline:
            time.sleep(1.2)
            count, newest = _answers(page, source)
            fresh = newest if count > before_count and newest else ""
            if fresh and fresh != answer:
                answer, stable = fresh, time.monotonic()
            elif fresh and stable and time.monotonic() - stable > 2.0:
                return fresh
        if answer:
            return answer
        raise WebChatError("页面没有给出回复。")


BODY_MARKERS = {
    "chatgpt": ("ChatGPT said:", ("You said:", "ChatGPT can make mistakes", "编辑")),
    "gemini": ("Gemini 说", ("你说", "Gemini 是一款", "Gemini said")),
}


def status() -> dict:
    """Which web chats are usable right now."""
    if not _endpoint():
        return {"browser": False, "chatgpt": False, "gemini": False}
    from playwright.sync_api import sync_playwright

    result = {"browser": True, "chatgpt": False, "gemini": False}
    with sync_playwright() as driver:
        browser = driver.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        context = browser.contexts[0]
        for source in ("chatgpt", "gemini"):
            try:
                page = _page(context, SOURCES[source]["url"])
                page.goto(SOURCES[source]["url"], wait_until="domcontentloaded", timeout=45_000)
                time.sleep(3)
                result[source] = _composer(page, source) is not None
            except Exception:
                result[source] = False
    return result


if __name__ == "__main__":
    import sys

    print(ask(sys.argv[1] if len(sys.argv) > 1 else "gemini",
              sys.argv[2] if len(sys.argv) > 2 else "只回复两个字：收到"))
