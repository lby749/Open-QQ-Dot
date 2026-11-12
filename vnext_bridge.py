"""Authenticated, owner-scoped bridge for desktop and paired mobile clients.

The Qt application remains the live production UI.  This bridge exposes a small,
stable, JSON-only view of the existing Python core so a Tauri/React client can be
built without reading SQLite or user profiles directly.

Security:
  * binds to 127.0.0.1 only;
  * nonempty administrator Bearer or scoped paired-device credentials;
  * read-only SQLite connections;
  * no chat bodies, memories, API keys, or credentials in responses.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_BODY = 4_000_000

ROOT = Path(__file__).resolve().parent
HOST = '127.0.0.1'
PORT = 8791
TOKEN = os.environ.get('QQDOT_VNEXT_TOKEN', '')
ALLOWED_ORIGINS = {'http://localhost:5173', 'http://127.0.0.1:5173', 'tauri://localhost', 'http://tauri.localhost'}


def _root() -> Path:
    return Path(os.environ.get('QQDOT_HOME') or ROOT).resolve()


def _safe_json(path: Path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return default


def _db(path: Path):
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _count(db: Path, table: str, owners=None) -> int:
    if owners is not None and not owners:
        return 0
    con = _db(db)
    if con is None:
        return 0
    try:
        where = ' WHERE owner_id IN (' + ','.join('?' for _ in owners) + ')' if owners is not None else ''
        return int(con.execute(f'SELECT count(*) FROM "{table}"' + where, tuple(owners or ())).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def friends(root: Path, owners=None) -> list[dict]:
    result = []
    base = root / 'friends'
    if not base.is_dir():
        return result
    for directory in sorted(base.iterdir(), key=lambda p: p.name):
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        if owners is not None and directory.name not in owners:
            continue
        profile = _safe_json(directory / 'friend.json', {})
        basic = profile.get('basic_info') or {}
        api = profile.get('model_api') or {}
        result.append({
            'qq': directory.name,
            'name': str(basic.get('name') or basic.get('nickname') or directory.name)[:80],
            'provider': str(api.get('provider') or 'unknown')[:40],
            'model': str(api.get('model') or '')[:120],
            'voice': str((profile.get('voice_reply') or {}).get('mode') or 'off')[:20],
        })
    return result


def agents() -> list[dict]:
    try:
        from agent_tool_adapters import available_adapters
        return available_adapters()
    except Exception:
        return []


_STORE = {}


def device_store(root: Path):
    from device_gateway import DeviceStore
    key = str(root.resolve())
    if key not in _STORE:
        _STORE[key] = DeviceStore(root / 'data' / 'vnext_devices.json')
    return _STORE[key]


def devices(root: Path) -> list[dict]:
    return device_store(root).list_devices()


def health_status(root: Path) -> dict:
    from health_bridge import HealthBridge
    return HealthBridge(root / 'data' / 'health_bridge.json').status()


def recent_tasks(root: Path, limit: int = 12, owners=None) -> list[dict]:
    if owners is not None and not owners:
        return []
    con = _db(root / 'data' / 'tasks.sqlite3')
    if con is None:
        return []
    try:
        where = ' WHERE owner_id IN (' + ','.join('?' for _ in owners) + ')' if owners is not None else ''
        rows = con.execute(
            'SELECT task_id AS id,title,status,created_at,updated_at FROM tasks' + where + ' ORDER BY rowid DESC LIMIT ?',
            (*tuple(owners or ()), max(1, min(50, limit)))).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def overview(root: Path, owners=None, scopes=None) -> dict:
    if owners is not None:
        scopes = set(scopes or ())
        return {'service': 'open-qq-dot-vnext-bridge', 'version': 1, 'read_only': True,
                'friends': len(friends(root, owners)) if 'chat:read' in scopes else None,
                'agents': len(agents()) if 'agents:read' in scopes else None,
                'tasks': _count(root / 'data/tasks.sqlite3', 'tasks', owners) if 'tasks:read' in scopes else None,
                'mail_messages': None, 'memories': None,
                'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    friend_rows = friends(root)
    return {
        'service': 'open-qq-dot-vnext-bridge',
        'version': 1,
        'read_only': True,
        'root': str(root),
        'friends': len(friend_rows),
        'agents': len(agents()),
        'tasks': _count(root / 'data' / 'tasks.sqlite3', 'tasks'),
        'mail_messages': _count(root / 'data' / 'agent_mail.sqlite3', 'messages'),
        'memories': _count(root / 'data' / 'agent_memory.sqlite3', 'memories'),
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }


def route(path: str, root: Path, query: str = '', *, experience_owner: str = '', experience_admin: bool = False,
          allowed_owners=None, scopes=None) -> tuple[int, dict | list]:
    if path == '/api/voice/invites':
        import voice_invites
        return 200, {'items': voice_invites.pending(root)}
    if path == '/api/health':
        return 200, {'ok': True, 'service': 'vnext-bridge', 'time': time.time()}
    if path == '/api/overview':
        return 200, overview(root, allowed_owners, scopes)
    if path == '/api/friends':
        return 200, {'items': friends(root, allowed_owners)}
    if path == '/api/agents':
        return 200, {'items': agents()}
    if path == '/api/tasks':
        return 200, {'items': recent_tasks(root, owners=allowed_owners)}
    if path == '/api/health-bridge':
        return 200, health_status(root)
    if path == '/api/stickers':
        import sticker_service
        qq = parse_qs(query).get('qq', [''])[0]
        try:
            return 200, sticker_service.snapshot(root, qq)
        except ValueError as exc:
            return 400, {'error': str(exc)}
    if path in ('/api/experiences', '/api/experiences/search'):
        from experience_service import snapshot, search
        values = parse_qs(query)
        q = str((values.get('q') or [''])[0])[:200]
        requested = str((values.get('owner') or [''])[0])
        if requested and not experience_admin and requested != experience_owner:
            return 403, {'error': '未授权读取此好友的经验'}
        owner = requested if experience_admin else experience_owner
        try:
            if path == '/api/experiences':
                return 200, snapshot(root, owner=owner, admin=experience_admin)
            return 200, {'items': search(root, q, owner, admin=experience_admin)}
        except PermissionError as exc:
            return 403, {'error': str(exc)}
        
    if path == '/api/accounts/route':
        import account_service
        qq = parse_qs(query).get('qq', [''])[0]
        row = account_service.route(root, qq)
        return 200, {'qq': qq, 'account': row, 'bound': row is not None}
    if path == '/api/accounts':
        import account_service
        return 200, {'items': account_service.list_accounts(root),
                     'discovered': account_service.discover(root)}
    if path == '/api/providers/route':
        import provider_router
        qq = parse_qs(query).get('qq', [''])[0]
        try:
            chosen = provider_router.route(root, qq)
        except ValueError as exc:
            return 400, {'error': str(exc)}
        return 200, {'route': asdict(chosen)}
    if path == '/api/providers':
        from provider_router import provider_summary
        return 200, provider_summary(root)
    if path == '/api/devices':
        return 200, {'items': devices(root)}
    if path == '/api/capabilities':
        return 200, {'features': {
            'desktop_shell': 'tauri-react', 'bridge': 'http-json', 'read_only': True,
            'android': 'not-yet-migrated', 'experience_engine': 'read-only-projection',
            'device_gateway': 'pairing-core-only', 'health_bridge': 'event-core-only'}}
    return 404, {'error': 'not_found'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'OpenQQDotVNext/1'

    def log_message(self, fmt, *args):
        # Keep the bridge quiet; the desktop shell shows health itself.
        return

    def _send(self, status: int, payload):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        origin = self.headers.get('Origin', '')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.end_headers()
        self.wfile.write(data)

    def _bearer(self) -> str:
        value = self.headers.get('Authorization', '')
        return value[7:] if value.startswith('Bearer ') else ''

    def _authorized(self) -> bool:
        token = self._bearer()
        if TOKEN and hmac.compare_digest(token.encode('utf-8'), TOKEN.encode('utf-8')):
            return True
        return device_store(_root()).authenticate(token) is not None

    def _pairing_admin(self) -> bool:
        return bool(TOKEN and hmac.compare_digest(self._bearer().encode('utf-8'), TOKEN.encode('utf-8')))

    def _origin_allowed(self) -> bool:
        return not self.headers.get('Origin') or self.headers.get('Origin') in ALLOWED_ORIGINS

    def do_OPTIONS(self):
        origin = self.headers.get('Origin', '')
        self.send_response(204)
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.end_headers()

    def do_GET(self):
        if not self._origin_allowed():
            self._send(403, {'error': '此网页来源未获授权'})
            return
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path == '/api/health':
            self._send(200, route(path, _root())[1])
            return
        admin = self._pairing_admin()
        device = None if admin else device_store(_root()).authenticate(self._bearer())
        if not admin and not device:
            self._send(401, {'error': 'unauthorized'})
            return
        experience = {}
        if not admin:
            scopes = set(device.get('scopes') or [])
            owners = tuple(device.get('owners') or [])
            required = {'/api/friends': 'chat:read', '/api/tasks': 'tasks:read', '/api/agents': 'agents:read',
                        '/api/stickers': 'chat:read', '/api/experiences': 'tasks:read',
                        '/api/experiences/search': 'tasks:read',
                        '/api/voice/invites': 'voice:stream'}
            permitted = ((path == '/api/overview' and bool(scopes & {'chat:read', 'tasks:read', 'agents:read'}))
                         or path == '/api/capabilities' or required.get(path) in scopes)
            if not permitted:
                self._send(403, {'error': '此设备未获该接口权限'})
                return
            if path == '/api/stickers' and (parse_qs(urlparse(self.path).query).get('qq') or [''])[0] not in owners:
                self._send(403, {'error': '此设备未获该好友权限'})
                return
            if path == '/api/voice/invites':
                import voice_invites
                rows = voice_invites.pending(_root(), device['id'])
                self._send(200, {'items': [row for row in rows if row.get('qq') in owners]})
                return
            experience.update(allowed_owners=owners, scopes=scopes)
        if path in ('/api/experiences', '/api/experiences/search'):
            owner = (parse_qs(urlparse(self.path).query).get('owner') or [''])[0]
            if not admin and (not device or 'tasks:read' not in (device.get('scopes') or []) or owner not in (device.get('owners') or [])):
                self._send(403, {'error': '此设备未获该好友的任务经验读取权限'})
                return
            experience.update(experience_owner=owner, experience_admin=admin)
        try:
            status, payload = route(path, _root(), urlparse(self.path).query, **experience)
        except Exception as exc:
            status, payload = 500, {'error': type(exc).__name__}
        self._send(status, payload)

    def do_POST(self):
        if not self._origin_allowed():
            self._send(403, {'error': '此网页来源未获授权'})
            return
        path = urlparse(self.path).path.rstrip('/') or '/'
        try:
            length = int(self.headers.get('Content-Length', '0') or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            self._send(400, {'error': '请求长度无效'})
            return
        if length > MAX_BODY:
            self._send(413, {'error': 'body_too_large'})
            return
        try:
            body = json.loads(self.rfile.read(length) or b'{}') if length else {}
            if not isinstance(body, dict):
                raise ValueError('body must be an object')
        except (ValueError, UnicodeDecodeError):
            self._send(400, {'error': 'invalid_json'})
            return
        root = _root()
        if path not in ('/api/collaboration', '/api/settings', '/api/device/pair', '/api/device/refresh') and not self._pairing_admin():
            device = device_store(root).authenticate(self._bearer())
            if not device:
                self._send(401, {'error': '需要有效的设备或管理员令牌'})
                return
            scopes = set(device.get('scopes') or [])
            owners = device.get('owners') or []
            required = {'/api/voice/transcribe': 'voice:stream', '/api/voice/synthesize': 'voice:stream',
                        '/api/voice/call': 'voice:stream', '/api/voice/invites/respond': 'voice:stream'}
            if required.get(path) not in scopes or (path == '/api/voice/call' and str(body.get('qq') or '').strip() not in owners):
                self._send(403, {'error': '此设备未获该操作或好友权限'})
                return
            if path == '/api/voice/invites/respond':
                import voice_invites
                allowed = [row for row in voice_invites.pending(root, device['id']) if row.get('qq') in owners]
                if not any(row.get('id') == str(body.get('id') or '') for row in allowed):
                    self._send(403, {'error': '此设备未获该邀请权限'})
                    return
        if path == '/api/settings':
            owner, operation = body.get('owner', ''), body.get('action', '')
            token = self._bearer()
            device = device_store(root).authenticate(token) if token else None
            admin = bool(TOKEN and token == TOKEN)
            if not admin and not device:
                self._send(401, {'error': '需要有效的设备令牌'})
                return
            scope = 'settings:read' if operation in ('owners', 'get') else 'settings:write'
            if not admin and scope not in (device.get('scopes') or []):
                self._send(403, {'error': '此设备未获好友设置权限；请在桌面明确授权后重新配对，原有配对保持可用'})
                return
            if operation == 'owners':
                rows = friends(root)
                if not admin:
                    rows = [row for row in rows if row['qq'] in (device.get('owners') or [])]
                self._send(200, {'items': rows})
                return
            if not admin and owner not in (device.get('owners') or []):
                self._send(403, {'error': '此设备未获当前好友的设置权限'})
                return
            import mobile_settings
            try:
                if operation == 'get':
                    result = mobile_settings.snapshot(root, owner)
                elif operation == 'save':
                    result = mobile_settings.update(root, owner, body)
                else:
                    raise ValueError('未知设置操作')
            except mobile_settings.SettingsConflict as error:
                self._send(409, {'error': str(error)})
                return
            except PermissionError:
                self._send(403, {'error': '好友配置归属或路径不匹配'})
                return
            except (ValueError, FileNotFoundError):
                self._send(400, {'error': '设置无效，请核对模型、思考强度、回复长度和好友范围'})
                return
            except Exception:
                self._send(503, {'error': '当前设置暂不可用，未确认保存，请重新读取核对'})
                return
            self._send(200, result)
            return
        if path == '/api/collaboration':
            owner = body.get('owner', '')
            operation = body.get('action', '')
            scope = 'collab:read' if operation in ('list', 'get') else 'collab:write'
            token = self._bearer()
            device = device_store(root).authenticate(token) if token else None
            admin = bool(TOKEN and token == TOKEN)
            if operation == 'owners':
                if not admin and (not device or 'collab:read' not in (device.get('scopes') or [])):
                    self._send(403, {'error': '请在桌面为指定好友生成带协作权限的配对码，再在手机配对'})
                    return
                rows = friends(root)
                if not admin:
                    rows = [row for row in rows if row['qq'] in (device.get('owners') or [])]
                self._send(200, {'items': rows})
                return
            if not admin and (not device or scope not in (device.get('scopes') or []) or owner not in (device.get('owners') or [])):
                self._send(403, {'error': '此设备未获当前好友的协作权限，请在桌面为该好友生成带协作权限的配对码'})
                return
            try:
                import mobile_collaboration
                result = mobile_collaboration.action(root, owner, body)
            except (ValueError, PermissionError, FileNotFoundError) as error:
                self._send(400, {'error': str(error)})
                return
            except Exception:
                self._send(500, {'error': '协作服务未完成请求，请在桌面检查任务；不要重复提交未确认的操作'})
                return
            self._send(200, result)
            return
        if path == '/api/voice/transcribe':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            import base64
            import tempfile
            from pathlib import Path as _Path
            raw = str(body.get('wav_base64') or '')
            try:
                audio = base64.b64decode(raw, validate=True)
            except Exception:
                self._send(400, {'error': 'invalid_base64'})
                return
            if not audio or len(audio) > 2_500_000:
                self._send(413, {'error': 'audio_too_large'})
                return
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as handle:
                handle.write(audio)
                name = handle.name
            try:
                import voice_input
                text, mood = voice_input.transcribe_local(_Path(name), root=root)
            except Exception as exc:
                self._send(503, {'error': f'ASR unavailable: {type(exc).__name__}'})
                return
            finally:
                try:
                    _Path(name).unlink()
                except OSError:
                    pass
            self._send(200, {'text': text, 'mood': mood})
            return
        if path == '/api/voice/invites':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            import voice_invites
            if True:
                if not self._pairing_admin():
                    self._send(403, {'error': 'admin_required'})
                    return
                try:
                    row = voice_invites.create(root, str(body.get('qq') or ''),
                                               str(body.get('device_id') or ''))
                except ValueError as exc:
                    self._send(400, {'error': str(exc)})
                    return
                self._send(200, row)
                return
            rows = voice_invites.pending(root, str(body.get('device_id') or ''))
            self._send(200, {'items': rows})
            return
        if path == '/api/voice/invites/respond':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            import voice_invites
            try:
                row = voice_invites.respond(root, str(body.get('id') or ''), bool(body.get('accept')))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, row)
            return
        if path == '/api/voice/call':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            import base64, uuid
            qq = str(body.get('qq') or '').strip()
            text = str(body.get('text') or '').strip()
            if not qq.isdigit() or not (root / 'friends' / qq / 'friend.json').is_file():
                self._send(400, {'error': 'friend_not_found'})
                return
            if not text:
                self._send(400, {'error': 'empty_text'})
                return
            from store import FriendStore
            import main
            directory = root / 'friends' / qq
            profile = json.loads((directory / 'friend.json').read_text('utf-8'))
            store = FriendStore(directory)
            try:
                incoming_id = 'voice-' + uuid.uuid4().hex
                store.add_message(incoming_id, 'friend', text)
                reply = main.make_reply(store, profile, directory)
                if isinstance(reply, (list, tuple)):
                    reply = chr(10).join(str(item) for item in reply if str(item).strip())
                if not isinstance(reply, str) or not reply.strip():
                    raise RuntimeError('empty_reply')
                store.add_message('voice-reply-' + uuid.uuid4().hex, 'me', reply)
            except Exception as exc:
                store.close()
                self._send(503, {'error': f'reply unavailable: {type(exc).__name__}'})
                return
            finally:
                try:
                    store.close()
                except Exception:
                    pass
            wav = ''
            try:
                import voice_tts
                part = voice_tts.record_segment(reply[:300], profile, root=root)
                wav = base64.b64encode(base64.b64decode(part['data']['file'][9:])).decode('ascii')
            except Exception:
                wav = ''
            self._send(200, {'user_text': text, 'reply': reply, 'wav_base64': wav})
            return
        if path == '/api/voice/synthesize':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            import base64
            text = str(body.get('text') or '').strip()[:300]
            if not text:
                self._send(400, {'error': 'empty_text'})
                return
            try:
                import voice_tts
                profile = {'voice_reply': {'mode': 'always', 'tier': body.get('tier') or 'small',
                                           'speaker': body.get('speaker') or 0, 'speed': 1.0,
                                           'atmosphere': body.get('atmosphere') or {'preset': 'dry'}}}
                part = voice_tts.record_segment(text, profile, root=root)
                payload = base64.b64decode(part['data']['file'][9:])
            except Exception as exc:
                self._send(503, {'error': f'TTS unavailable: {type(exc).__name__}'})
                return
            self._send(200, {'wav_base64': base64.b64encode(payload).decode('ascii'),
                             'sample_rate': 24000, 'text': text})
            return
        if path == '/api/experiences/candidate':
            if not (TOKEN and self._bearer() == TOKEN):
                self._send(403, {'error': 'admin_required'})
                return
            import experience_service
            try:
                row = experience_service.record_candidate(root, body.get('problem'), body.get('action'), body.get('cause', ''), owner=body.get('owner') or 'system:admin')
            except (ValueError, PermissionError) as exc:
                self._send(400, {'error': str(exc)})
                return
            except Exception as exc:
                self._send(503, {'error': f'experience unavailable: {type(exc).__name__}'})
                return
            self._send(200, row)
            return
        if path == '/api/accounts/add':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            import account_service
            api_key = str(body.pop('api_key', '') or '')
            try:
                row = account_service.add_with_key(root, body, api_key) if api_key else account_service.add(root, body)
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, row)
            return
        if path == '/api/accounts/bind':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            import account_service
            try:
                row = account_service.bind(root, str(body.get('qq') or ''), str(body.get('account_id') or ''))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, row)
            return
        if path == '/api/accounts/chat':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            import account_service
            try:
                result = account_service.chat(root, str(body.get('id') or ''), str(body.get('prompt') or ''))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            except RuntimeError as exc:
                self._send(502, {'error': str(exc)})
                return
            self._send(200, result)
            return
        if path == '/api/accounts/test':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            import account_service
            try:
                result = account_service.probe(root, str(body.get('id') or ''))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, result)
            return
        if path == '/api/accounts/remove':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            import account_service
            if not account_service.remove(root, str(body.get('id') or '')):
                self._send(404, {'error': 'account_not_found'})
                return
            self._send(200, {'removed': True})
            return
        if path == '/api/device/refresh':
            token = self._bearer()
            result = device_store(root).refresh(token) if token else None
            if result is None:
                self._send(401, {'error': 'invalid_or_expired_token'})
                return
            self._send(200, result)
            return
        if path == '/api/device/revoke':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            if not device_store(root).revoke(str(body.get('device_id') or '')):
                self._send(404, {'error': 'device_not_found'})
                return
            self._send(200, {'revoked': True})
            return
        if path == '/api/health/report':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            from health_bridge import HealthBridge
            try:
                result = HealthBridge(root / 'data' / 'health_bridge.json').report(
                    str(body.get('adapter') or ''), supported=bool(body.get('supported')),
                    note=str(body.get('note') or ''))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, result)
            return
        if path == '/api/health/authorize':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            from health_bridge import HealthBridge
            scopes = body.get('scopes') or []
            if not isinstance(scopes, list):
                self._send(400, {'error': 'scopes must be a list'})
                return
            try:
                result = HealthBridge(root / 'data' / 'health_bridge.json').authorize(
                    str(body.get('adapter') or ''), tuple(str(x) for x in scopes))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, result)
            return
        if path == '/api/health/events':
            if not self._authorized():
                self._send(401, {'error': 'unauthorized'})
                return
            from health_bridge import HealthBridge, HealthEvent
            try:
                event = HealthEvent(adapter=str(body.get('adapter') or ''),
                                    event_type=str(body.get('event_type') or ''),
                                    occurred_at=str(body.get('occurred_at') or ''),
                                    scope=str(body.get('scope') or ''),
                                    value=body.get('value') if isinstance(body.get('value'), dict) else {},
                                    event_id=str(body.get('event_id') or ''))
                result = HealthBridge(root / 'data' / 'health_bridge.json').ingest(event)
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, result)
            return
        if path == '/api/device/pairing-code':
            if not self._pairing_admin():
                self._send(403, {'error': 'admin_required'})
                return
            code = device_store(root).create_code()
            self._send(200, {'code': code, 'expires_in': 300})
            return
        if path == '/api/device/pair':
            try:
                result = device_store(root).pair(str(body.get('code') or ''), str(body.get('name') or ''))
            except ValueError as exc:
                self._send(400, {'error': str(exc)})
                return
            self._send(200, result)
            return
        self._send(404, {'error': 'not_found'})


def serve(port: int = PORT, token: str = '', tls: bool = False) -> None:
    global TOKEN
    TOKEN = token or TOKEN
    server = ThreadingHTTPServer((HOST, port), Handler)
    scheme = 'http'
    if tls:
        import ssl
        import tls_support
        cert_path, key_path = tls_support.ensure_certificate(_root() / 'data' / 'vnext_tls')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = 'https'
    print(json.dumps({'bridge': f'{scheme}://{HOST}:{port}', 'read_only': True,
                      'auth': bool(TOKEN), 'tls': tls}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--token', default='')
    parser.add_argument('--root', default='')
    parser.add_argument('--tls', action='store_true', help='serve HTTPS with a local self-signed certificate')
    args = parser.parse_args()
    if args.root:
        os.environ['QQDOT_HOME'] = str(Path(args.root).resolve())
    serve(args.port, args.token, args.tls)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
