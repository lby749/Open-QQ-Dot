"""Synchronous OneBot 11 client used by DotLink.

DotLink keeps its own connection settings under ``dotlink/local.json`` and never
reads a third-party client's token or login state.  The client only talks to the
loopback OneBot socket that DotLink started.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from websockets.sync.client import connect

DEFAULT_URL = "ws://127.0.0.1:3001"


def local_settings(home: Path | None = None) -> dict:
    root = Path(home or os.environ.get("QQ_BRIDGE_HOME", Path(__file__).resolve().parent))
    for name in ("dotlink/local.json", "local.json"):
        path = root / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            continue
    return {}


class QQClient:
    def __init__(self, url: str | None = None, token: str | None = None, *, timeout: float = 5.0):
        settings = local_settings()
        self.url = url or os.environ.get("DOTLINK_WS_URL") or settings.get("ws_url") or DEFAULT_URL
        self.token = token if token is not None else (
            os.environ.get("DOTLINK_ACCESS_TOKEN") or str(settings.get("access_token") or ""))
        parsed = urlsplit(self.url)
        if (parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or not parsed.port):
            raise ValueError("QQ 直连 WebSocket 只允许本机地址")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        self.ws = connect(self.url, additional_headers=headers, open_timeout=timeout,
                          ping_interval=20, ping_timeout=20)
        self.events: queue.Queue = queue.Queue()
        self.pending: dict = {}
        self.lock = threading.Lock()
        self.closed = False
        self.connected_at = time.time()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        failure = RuntimeError("QQ 直连 WebSocket 已断开")
        try:
            for raw in self.ws:
                data = json.loads(raw)
                echo = data.get("echo")
                if echo is not None:
                    with self.lock:
                        target = self.pending.get(str(echo))
                    if target:
                        target.put(data)
                elif data.get("post_type"):
                    self.events.put(data)
        except Exception as exc:
            failure = RuntimeError("QQ 直连 WebSocket 连接被关闭：Token 不匹配或登录态已失效") if "1005" in str(exc) else exc
        finally:
            self.closed = True
            self.events.put(failure)
            with self.lock:
                for target in self.pending.values():
                    target.put(failure)

    def call(self, action: str, params: dict | None = None, timeout: float = 10):
        if self.closed:
            raise RuntimeError("QQ 直连 WebSocket 已断开")
        echo = uuid.uuid4().hex
        answer: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[echo] = answer
        try:
            self.ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            try:
                result = answer.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"QQ 直连 {action} 响应超时；发送结果不确定") from None
            if isinstance(result, Exception):
                raise result
            if result.get("status") != "ok" or result.get("retcode") not in (0, None):
                raise RuntimeError(f"QQ 直连 {action} 失败：{result.get('message') or result.get('wording') or result.get('retcode')}")
            return result.get("data")
        finally:
            with self.lock:
                self.pending.pop(echo, None)

    def send_private_msg(self, qq, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("消息内容不能为空")
        return self.send_private_segments(qq, [{"type": "text", "data": {"text": text}}])

    def send_private_segments(self, qq, segments, timeout=10):
        qq = str(qq or "").strip()
        if not re.fullmatch(r"\d{5,12}", qq):
            raise ValueError("请先选择有效的 QQ 好友")
        if not isinstance(segments, list) or not segments:
            raise ValueError("消息内容不能为空")
        receipt = self.call("send_private_msg", {"user_id": int(qq), "message": segments}, timeout=timeout)
        if not isinstance(receipt, dict) or not receipt.get("message_id"):
            raise RuntimeError("QQ 未返回发送回执，结果尚未确认，请核对聊天记录，勿直接重复发送")
        return receipt

    def send_group_msg(self, group, text, timeout=10):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("消息内容不能为空")
        return self.send_group_segments(group, [{"type": "text", "data": {"text": text}}], timeout=timeout)

    def send_group_segments(self, group, segments, timeout=10):
        group = str(group or "").strip()
        if not re.fullmatch(r"\d{5,12}", group):
            raise ValueError("请先选择有效的 QQ 群")
        if not isinstance(segments, list) or not segments:
            raise ValueError("消息内容不能为空")
        receipt = self.call("send_group_msg", {"group_id": int(group), "message": segments}, timeout=timeout)
        if not isinstance(receipt, dict) or not receipt.get("message_id"):
            raise RuntimeError("QQ 未返回发送回执，结果尚未确认，请核对群聊，勿直接重复发送")
        return receipt

    def login_info(self) -> dict:
        return self.call("get_login_info") or {}

    def friend_list(self) -> list[dict]:
        value = self.call("get_friend_list") or []
        return [item for item in value if isinstance(item, dict)]

    def group_list(self) -> list[dict]:
        value = self.call("get_group_list") or []
        return [item for item in value if isinstance(item, dict)]

    def friend_history(self, qq, count=100, before=None):
        params = {"user_id": int(qq), "count": max(1, min(int(count), 500))}
        if before:
            params["message_seq"] = before
        value = self.call("get_friend_msg_history", params) or {}
        return value.get("messages") if isinstance(value, dict) else value

    def group_history(self, group, count=100, before=None):
        params = {"group_id": int(group), "count": max(1, min(int(count), 500))}
        if before:
            params["message_seq"] = before
        value = self.call("get_group_msg_history", params) or {}
        return value.get("messages") if isinstance(value, dict) else value

    def message(self, message_id):
        return self.call("get_msg", {"message_id": int(message_id)})

    def forward(self, message_id):
        return self.call("get_forward_msg", {"message_id": int(message_id)})

    def next_event(self, timeout=1):
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(event, Exception):
            raise event
        return event

    def reconnect(self, attempts=3, delay=1.5):
        self.close()
        last = None
        for _ in range(max(1, int(attempts))):
            try:
                self.__init__(url=self.url, token=self.token)
                return True
            except Exception as exc:
                last = exc
                time.sleep(delay)
        if last:
            raise last
        return False

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
