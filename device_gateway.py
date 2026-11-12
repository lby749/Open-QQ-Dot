"""Device pairing and scoped credentials for the vNext DeviceGateway.

The transport (TLS/WSS) is deliberately not implemented here yet.  This module
provides the security primitive the transport will use:

  * short-lived, one-use pairing codes;
  * per-device random bearer tokens;
  * only SHA-256 token hashes persisted at rest;
  * explicit scopes and revocation.

No token is ever logged or returned by listing functions.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import hmac
import json
import secrets
import threading
import time
import sqlite3
from pathlib import Path

PAIRING_TTL = 300
DEVICE_TTL = 86400 * 30
DEFAULT_SCOPES = ('chat:read', 'tasks:read', 'agents:read')
VALID_SCOPES = set(DEFAULT_SCOPES) | {'chat:write', 'voice:stream', 'health:read', 'collab:read', 'collab:write',
                                    'settings:read', 'settings:write'}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class DeviceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    @contextmanager
    def _locked(self):
        # One SQLite transaction also serializes JSON credential updates across GUI/server processes.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            db = sqlite3.connect(self.path.with_suffix('.pairing.sqlite3'), timeout=10)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS codes(digest TEXT PRIMARY KEY, expires REAL, scopes TEXT, owners TEXT)')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            finally:
                db.close()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text('utf-8'))
            return value if isinstance(value, dict) else {'devices': []}
        except (OSError, ValueError):
            return {'devices': []}

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
        temporary.replace(self.path)

    def create_code(self, ttl: int = PAIRING_TTL, *, scopes=DEFAULT_SCOPES, owners=()) -> str:
        now = time.time()
        clean = tuple(dict.fromkeys(scope for scope in scopes if scope in VALID_SCOPES))
        if not clean or len(owners) > 100 or any(not str(owner).isdigit() for owner in owners):
            raise ValueError('配对权限或好友范围无效')
        if set(clean) & {'collab:write', 'settings:write'} and not owners:
            raise ValueError('协作或设置写入权限必须指定好友范围')
        with self._locked() as db:
            db.execute('DELETE FROM codes WHERE expires<=?', (now,))
            code = ''.join(secrets.choice('0123456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(8))
            db.execute('INSERT INTO codes VALUES(?,?,?,?)', (_hash(code), now + max(30, min(900, int(ttl))),
                        json.dumps(clean), json.dumps([str(owner) for owner in owners])))
            return code

    def pair(self, code: str, name: str, scopes: tuple[str, ...] | None = None) -> dict:
        name = str(name or '').strip()[:80]
        if not name:
            raise ValueError('设备名称不能为空')
        now = time.time()
        with self._locked() as db:
            digest = _hash(str(code or '').strip().upper())
            entry = db.execute('SELECT expires,scopes,owners FROM codes WHERE digest=?', (digest,)).fetchone()
            if entry is None or entry[0] < now:
                raise ValueError('配对码无效或已过期')
            granted = json.loads(entry[1])
            clean = tuple(dict.fromkeys(scope for scope in (scopes if scopes is not None else granted) if scope in granted))
            if not clean:
                raise ValueError('设备权限不能为空或超出配对码授权')
            db.execute('DELETE FROM codes WHERE digest=?', (digest,))
            token = secrets.token_urlsafe(32)
            device = {
                'id': 'dev_' + secrets.token_hex(8),
                'name': name,
                'token_hash': _hash(token),
                'scopes': list(clean),
                'owners': json.loads(entry[2]),
                'created_at': now,
                'last_seen': 0,
                'expires_at': now + DEVICE_TTL,
            }
            data = self._read()
            devices = data.setdefault('devices', [])
            devices.append(device)
            self._write(data)
            return {'device_id': device['id'], 'token': token, 'scopes': list(clean)}

    def authenticate(self, token: str) -> dict | None:
        if not token:
            return None
        digest = _hash(str(token))
        now = time.time()
        with self._locked():
            data = self._read()
            for device in data.get('devices', []):
                if hmac.compare_digest(str(device.get('token_hash', '')), digest):
                    expires = float(device.get('expires_at', 0) or 0)
                    if expires and expires < now:
                        return None
                    previous = float(device.get('last_seen', 0) or 0)
                    device['last_seen'] = now
                    if now - previous > 60:
                        self._write(data)
                    return {key: device.get(key) for key in ('id', 'name', 'scopes', 'owners', 'created_at', 'last_seen', 'expires_at')}
        return None

    def refresh(self, token: str) -> dict | None:
        """Rotate a device token and extend its lifetime; old token stops working."""
        if not token:
            return None
        digest = _hash(str(token))
        now = time.time()
        with self._locked():
            data = self._read()
            for device in data.get('devices', []):
                if not hmac.compare_digest(str(device.get('token_hash', '')), digest):
                    continue
                expires = float(device.get('expires_at', 0) or 0)
                if expires and expires < now:
                    return None
                fresh = secrets.token_urlsafe(32)
                device['token_hash'] = _hash(fresh)
                device['expires_at'] = now + DEVICE_TTL
                device['last_seen'] = now
                self._write(data)
                return {'device_id': device['id'], 'token': fresh,
                        'scopes': list(device.get('scopes', [])),
                        'expires_at': device['expires_at']}
        return None

    def list_devices(self) -> list[dict]:
        with self._locked():
            return [{key: device.get(key) for key in ('id', 'name', 'scopes', 'owners', 'created_at', 'last_seen', 'expires_at')}
                    for device in self._read().get('devices', [])]

    def revoke(self, device_id: str) -> bool:
        with self._locked():
            data = self._read()
            before = len(data.get('devices', []))
            data['devices'] = [d for d in data.get('devices', []) if d.get('id') != device_id]
            if len(data['devices']) == before:
                return False
            self._write(data)
            return True
