"""Antigravity Codex Cockpit Server for Open QQ Dot.

High-performance, lightweight asynchronous HTTP + SSE telemetry server.
Serves the Antigravity Codex Web Cockpit and provides full management APIs:
- System & Engine Telemetry (Codex CLI, QQ 直连 WS, Memory, Latency)
- Zero-Backlog Queue Governor (Inspection, Instant Drain, Consolidation)
- Multi-Agent Mailbox & Codex Desk (Collaboration threads, Receipts)
- Group Assistant (Grok Bot settings, Fact-checks, Search status)
- Interactive Simulation Sandbox (Live test inbound QQ messages)
"""
from __future__ import annotations

import json
import base64
import hmac
import secrets
import shutil
import subprocess
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import house_status
from queue_governor import QueueGovernor
from ui_dashboard_data import system_snapshot, usage_snapshot

logger = logging.getLogger("cockpit_server")

PORT = 8791
_log_subscribers = []
_subscribers_lock = threading.Lock()
_recent_logs = []
_MAX_LOGS = 200
_COCKPIT_TOKEN = None


def cockpit_auth_file() -> Path:
    """Non-exportable local admin credential, separate from the published source tree."""
    import house_settings
    return Path(house_settings.data_dir()) / '.cockpit_admin_token'


def cockpit_token() -> str:
    """Create one per-installation token; do not put it in URLs or public assets."""
    global _COCKPIT_TOKEN
    if _COCKPIT_TOKEN is not None:
        return _COCKPIT_TOKEN
    supplied = os.environ.get('OPENQQ_COCKPIT_TOKEN', '').strip()
    if supplied:
        if len(supplied) < 32:
            raise ValueError('OPENQQ_COCKPIT_TOKEN must be at least 32 characters')
        _COCKPIT_TOKEN = supplied
        return supplied
    path = cockpit_auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _COCKPIT_TOKEN = path.read_text('utf-8').strip()
    if len(_COCKPIT_TOKEN) < 32:
        raise RuntimeError('Invalid cockpit token file')
    return _COCKPIT_TOKEN


def _local_hostname(host: str) -> bool:
    hostname = host.rsplit(':', 1)[0].lower().strip('[]')
    return hostname in ('localhost', '127.0.0.1', '::1')


def push_log_event(level: str, source: str, message: str, payload: dict | None = None):
    entry = {
        "timestamp": datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3],
        "level": level,
        "source": source,
        "message": message,
        "payload": payload or {},
    }
    with _subscribers_lock:
        _recent_logs.append(entry)
        if len(_recent_logs) > _MAX_LOGS:
            _recent_logs.pop(0)
        dead = []
        for q in _log_subscribers:
            try:
                q.put_nowait(entry)
            except Exception:
                dead.append(q)
        for q in dead:
            _log_subscribers.remove(q)


class CockpitHandler(BaseHTTPRequestHandler):
    server_version = "AntigravityCodexCockpit/2.5"

    def log_message(self, format, *args):
        # Suppress noisy standard access logging
        pass

    def _require_auth(self, *, write: bool = False) -> bool:
        """Loopback binding is insufficient: reject unauthenticated tabs and CSRF."""
        if not _local_hostname(self.headers.get('Host', '')):
            self._send_json({'error': 'Invalid Host'}, 403)
            return False
        auth = self.headers.get('Authorization', '')
        supplied = ''
        if auth.startswith('Bearer '):
            supplied = auth[7:].strip()
        elif auth.startswith('Basic '):
            try:
                username, supplied = base64.b64decode(auth[6:], validate=True).decode('utf-8').split(':', 1)
                if username != 'admin':
                    supplied = ''
            except (ValueError, UnicodeError):
                supplied = ''
        if not supplied or not hmac.compare_digest(supplied, cockpit_token()):
            body = b'Authentication required. Username: admin; password: local cockpit token.'
            self.send_response(401)
            self.send_header('X-OpenQQDot-Service', 'cockpit')
            self.send_header('WWW-Authenticate', 'Basic realm="Open QQ Dot local cockpit", charset="UTF-8"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        origin = self.headers.get('Origin', '')
        if origin:
            parsed = urlparse(origin)
            host = self.headers.get('Host', '').lower()
            if parsed.scheme != 'http' or parsed.netloc.lower() != host or not _local_hostname(parsed.netloc):
                self._send_json({'error': 'Cross-origin request forbidden'}, 403)
                return False
        if write:
            media_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if media_type != 'application/json':
                self._send_json({'error': 'JSON content-type required'}, 415)
                return False
        return True

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # Browser cross-site requests cannot bootstrap CORS permissions.
        self.send_response(403)
        self.end_headers()

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/cockpit"):
            self._serve_ui()
            return

        if path == "/api/workspace":
            self._send_json(system_snapshot(__import__('house_settings').data_dir()))
            return

        if path == "/api/insights":
            self._send_json(usage_snapshot(__import__('house_settings').data_dir()))
            return

        if path == "/api/agent-adapters":
            # Read-only, authenticated installation/capability snapshot. No tokens,
            # arbitrary executable commands or session contents are returned.
            from agent_tool_adapters import available_adapters
            self._send_json({'adapters': available_adapters(),
                             'note': 'Installed does not mean authenticated or verified. '
                                     'External CLI execution requires explicit host permission.'})
            return

        if path == "/api/status":
            self._handle_status()
            return

        if path == "/api/queue":
            self._handle_queue()
            return

        if path == "/api/mailbox":
            self._handle_mailbox()
            return

        if path == "/api/friends":
            self._handle_friends()
            return

        if path == "/api/group_bot":
            self._handle_group_bot()
            return

        if path == "/api/logs":
            with _subscribers_lock:
                self._send_json({"logs": list(_recent_logs)})
            return

        if path == "/api/logs/stream":
            self._handle_sse_stream()
            return

        self.send_error(404, "Endpoint not found")

    def do_POST(self):
        if not self._require_auth(write=True):
            return
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({'error': 'Invalid Content-Length'}, 400)
            return
        if content_len < 0 or content_len > 64 * 1024:
            self._send_json({'error': 'Payload too large'}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(content_len).decode('utf-8')) if content_len else {}
            if not isinstance(payload, dict):
                raise ValueError('Expected JSON object')
        except (ValueError, UnicodeError):
            self._send_json({'error': 'Invalid JSON object'}, 400)
            return

        if path == "/api/queue/flush":
            self._handle_queue_flush(payload)
            return

        if path == "/api/simulate":
            self._handle_simulate(payload)
            return

        if path == "/api/bridge/toggle":
            self._handle_bridge_toggle(payload)
            return

        if path == "/api/friends/update":
            self._handle_friend_update(payload)
            return

        self.send_error(404, "Endpoint not found")

    def _serve_ui(self):
        ui_path = ROOT / "cockpit_ui.html"
        if not ui_path.exists():
            self.send_error(500, "cockpit_ui.html not found")
            return
        content = ui_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_status(self):
        governor = QueueGovernor.get_instance()
        reg = house_status.REGISTRY
        friends = {}
        if reg.is_file():
            try:
                friends = json.loads(reg.read_text("utf-8")).get("friends", {})
            except Exception:
                friends = {}

        queue_health = governor.inspect_queue_health(friends)

        # Check bridge running status
        bridge_alive = None
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            mutex = kernel32.OpenMutexW(0x00100000, False, "Local\\QQFriendHouseBridge")
            if mutex:
                kernel32.CloseHandle(mutex)
                bridge_alive = True
        except Exception:
            bridge_alive = None

        from house_settings import local_models_enabled
        codex_cli = shutil.which('codex')
        codex_version = None
        if codex_cli:
            try:
                probe = subprocess.run([codex_cli, '--version'], capture_output=True, text=True, timeout=3, check=False)
                if probe.returncode == 0:
                    codex_version = probe.stdout.strip()[:120] or None
            except (OSError, subprocess.TimeoutExpired):
                pass
        data = {
            "version": "2.5.0-antigravity",
            "bridge_running": bridge_alive,
            "queue_health": queue_health,
            "engine": {
                "codex_cli": {"installed": codex_cli is not None, "version": codex_version, "running": "not_probed"},
                "local_models_enabled": local_models_enabled(ROOT),
                "embedding_model": "not_probed",
                "reranker_model": "not_probed",
            },
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        self._send_json(data)

    def _handle_queue(self):
        reg = house_status.REGISTRY
        friends = {}
        if reg.is_file():
            try:
                friends = json.loads(reg.read_text("utf-8")).get("friends", {})
            except Exception:
                friends = {}
        health = QueueGovernor.get_instance().inspect_queue_health(friends)
        self._send_json(health)

    def _handle_queue_flush(self, payload: dict):
        reg = house_status.REGISTRY
        friends = {}
        if reg.is_file():
            try:
                friends = json.loads(reg.read_text("utf-8")).get("friends", {})
            except Exception:
                friends = {}
        mark_handled = bool(payload.get("mark_ancient_handled", False))
        if mark_handled:
            self._send_json({'success': False, 'error': 'Deleting unread messages is disabled'}, 400)
            return
        res = QueueGovernor.get_instance().flush_all(friends, mark_ancient_handled=False)
        push_log_event("SUCCESS", "QueueGovernor", f"一键疏通完成：清理并恢复了 {res.get('drained_count')} 项滞留消息！", res)
        self._send_json(res)

    def _handle_mailbox(self):
        """Read-only metadata; never return private message bodies or prompts."""
        snapshot = system_snapshot(__import__('house_settings').data_dir())
        # Both these endpoints intentionally share the same redacted data layer.
        self._send_json({'messages': snapshot['mail_items'], 'counts': snapshot['mail'],
                         'warnings': [w for w in snapshot['warnings'] if 'agent_mail' in w]})

    def _handle_friends(self):
        reg = house_status.REGISTRY
        friends = {}
        if reg.is_file():
            try:
                friends = json.loads(reg.read_text('utf-8')).get('friends', {})
            except (OSError, ValueError):
                pass
        result = []
        for q, rec in friends.items():
            # QQ IDs are strictly numeric. Registry entries are metadata, not paths.
            if not str(q).isdigit() or not isinstance(rec, dict):
                continue
            profile_path = house_status.ROOT / 'friends' / str(q) / 'friend.json'
            try:
                profile = house_status.load_profile(profile_path)
            except (OSError, ValueError):
                # Do not pretend the user has a functioning profile.
                continue
            result.append({
                'qq_id': str(q),
                'display_name': rec.get('display_name') or str(q),
                'bridge_enabled': bool(profile.get('bridge_enabled', True)),
                'auto_reply': bool(profile.get('auto_reply', True)),
                'proactive_enabled': bool(profile.get('proactive', {}).get('enabled', False)),
                'model_provider': profile.get('model_api', {}).get('provider', 'unknown'),
            })
        self._send_json({'friends': result})

    def _handle_group_bot(self):
        from group_bot import load_config, health
        cfg = load_config()
        checks = health(probe_search=False)
        self._send_json({
            "config": cfg,
            "health": [{"name": c[0], "ok": c[1], "detail": c[2]} for c in checks],
        })

    def _handle_simulate(self, payload: dict):
        """Read-only preview. Never impersonate QQ authentication or claim to queue work."""
        body = str(payload.get('body', '')).strip()
        if not body:
            self._send_json({'error': 'Empty message body'}, 400)
            return
        from queue_governor import QueueGovernor
        self._send_json({'success': True, 'mode': 'preview_only', 'executed': False, 'queued': False,
                         'fast_path': QueueGovernor.is_fast_command(body),
                         'message': 'Preview only; send instructions through an authenticated QQ session.'})

    def _handle_bridge_toggle(self, payload: dict):
        # The old code returned fake success without restarting a real process.
        self._send_json({'success': False, 'executed': False,
                         'error': 'Bridge control is not wired to a verified process manager'}, 501)

    def _handle_friend_update(self, payload: dict):
        qq_id = str(payload.get("qq_id", "")).strip()
        if not qq_id or not qq_id.isdigit():
            self._send_json({"error": "Invalid QQ id"}, 400)
            return
        reg = house_status.REGISTRY
        if not reg.is_file():
            self._send_json({"error": "No friends.json"}, 500)
            return
        records = json.loads(reg.read_text("utf-8")).get("friends", {})
        rec = records.get(qq_id)
        if not rec:
            self._send_json({"error": "Friend not found"}, 404)
            return

        profile_path = house_status.ROOT / 'friends' / qq_id / 'friend.json'
        if not profile_path.is_file():
            self._send_json({'error': 'Friend profile not found'}, 404)
            return
        profile = house_status.load_profile(profile_path)
        for k in ("bridge_enabled", "auto_reply"):
            if k in payload:
                profile[k] = bool(payload[k])
        if "proactive_enabled" in payload:
            profile.setdefault("proactive", {})["enabled"] = bool(payload["proactive_enabled"])
        house_status.write_profile(profile_path, profile)
        push_log_event("CONFIG", "FriendProfile", f"更新好友 {qq_id} 配置", payload)
        # Profiles may hold backend credentials; never return full configuration.
        self._send_json({"success": True, "qq_id": qq_id,
                         "bridge_enabled": bool(profile.get('bridge_enabled', True)),
                         "auto_reply": bool(profile.get('auto_reply', True))})

    def _handle_sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        q = queue.Queue()
        with _subscribers_lock:
            _log_subscribers.append(q)

        # Send initial recent logs
        try:
            with _subscribers_lock:
                for entry in _recent_logs[-30:]:
                    data = json.dumps(entry, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    entry = q.get(timeout=15.0)
                    data = json.dumps(entry, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive heartbeat
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _subscribers_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)


def run_server(port: int = PORT):
    # Fail closed if the admin secret is unavailable.
    cockpit_token()
    server = ThreadingHTTPServer(("127.0.0.1", port), CockpitHandler)
    logger.warning('Cockpit admin password is in %s (username: admin). Never publish this file.', cockpit_auth_file())
    push_log_event("STARTUP", "CockpitServer", f"Admin-authenticated cockpit listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run_server(p)
