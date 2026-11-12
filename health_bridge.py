"""HealthBridge event and capability core.

No health data is fabricated.  A platform adapter must explicitly report that it
is available, the user must grant a scope, and only then can a normalized event be
ingested.  Desktop-only status shows "待手机上报" until the Android client sends a
real capability report.

Persisted data stays local (data/health_bridge.json) and contains only the
minimum normalized event fields.  Full raw records are never stored here.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BRANDS = {
    'health_connect': 'Android Health Connect',
    'xiaomi': '小米 / Redmi',
    'huawei': '华为',
    'honor': '荣耀',
    'samsung': '三星',
    'fitbit': 'Fitbit / Pixel Watch',
    'amazfit': 'Amazfit / Zepp',
    'garmin': 'Garmin',
    'oppo': 'OPPO / 一加 / realme',
    'vivo': 'vivo / iQOO',
    'apple': 'Apple Watch',
}
SCOPES = ('sleep', 'steps', 'workout', 'activity')
EVENT_TYPES = ('sleep_start', 'sleep_end', 'wake', 'steps_change', 'workout_end',
               'activity_change', 'sync_status')
MAX_EVENTS = 500


@dataclass(frozen=True)
class HealthEvent:
    adapter: str
    event_type: str
    occurred_at: str
    scope: str
    value: dict
    event_id: str = ''

    def normalized(self) -> dict:
        if self.adapter not in BRANDS:
            raise ValueError('unknown health adapter')
        if self.event_type not in EVENT_TYPES:
            raise ValueError('unsupported health event type')
        if self.scope not in SCOPES:
            raise ValueError('unsupported health scope')
        if not isinstance(self.value, dict):
            raise ValueError('health value must be an object')
        occurred = str(self.occurred_at or '').strip()
        if not occurred or len(occurred) > 64:
            raise ValueError('invalid occurred_at')
        event_id = str(self.event_id or '').strip()[:128]
        if not event_id:
            event_id = hashlib.sha256(
                f'{self.adapter}:{self.event_type}:{occurred}:{json.dumps(self.value, sort_keys=True)}'.encode()
            ).hexdigest()[:24]
        return {'adapter': self.adapter, 'event_type': self.event_type, 'occurred_at': occurred,
                'scope': self.scope, 'value': self.value, 'event_id': event_id}


class HealthBridge:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = self._read()

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text('utf-8'))
            return value if isinstance(value, dict) else {'adapters': {}, 'events': []}
        except (OSError, ValueError):
            return {'adapters': {}, 'events': []}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), 'utf-8')
        temp.replace(self.path)

    def report(self, adapter: str, *, supported: bool, note: str = '') -> dict:
        if adapter not in BRANDS:
            raise ValueError('unknown health adapter')
        with self.lock:
            current = self.data.setdefault('adapters', {}).setdefault(adapter, {})
            current.update({'supported': bool(supported), 'note': str(note)[:200],
                            'reported_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'authorized_scopes': current.get('authorized_scopes', [])})
            self._write()
            return dict(current)

    def authorize(self, adapter: str, scopes: tuple[str, ...], granted: bool = True) -> dict:
        clean = tuple(dict.fromkeys(s for s in scopes if s in SCOPES))
        if not granted:
            clean = ()
        with self.lock:
            item = self.data.setdefault('adapters', {}).setdefault(adapter, {'supported': False})
            if not item.get('supported'):
                raise ValueError('adapter is not supported; authorize after a real capability report')
            item['authorized_scopes'] = list(clean)
            self._write()
            return dict(item)

    def ingest(self, event: HealthEvent) -> dict:
        item = event.normalized()
        with self.lock:
            adapter = self.data.setdefault('adapters', {}).setdefault(item['adapter'], {'supported': False})
            if not adapter.get('supported'):
                raise ValueError('adapter is not supported')
            if item['scope'] not in adapter.get('authorized_scopes', []):
                raise ValueError('scope is not authorized')
            events = self.data.setdefault('events', [])
            if any(existing.get('event_id') == item['event_id'] for existing in events):
                return {'accepted': False, 'reason': 'duplicate'}
            events.append(item)
            self.data['events'] = events[-MAX_EVENTS:]
            self._write()
            return {'accepted': True, 'event_id': item['event_id']}

    def status(self) -> dict:
        with self.lock:
            adapters = []
            for key, label in BRANDS.items():
                item = self.data.get('adapters', {}).get(key)
                adapters.append({
                    'adapter': key, 'label': label,
                    'supported': bool(item and item.get('supported')),
                    'authorized_scopes': list(item.get('authorized_scopes', [])) if item else [],
                    'note': (item or {}).get('note', ''),
                })
            return {'adapters': adapters,
                    'event_count': len(self.data.get('events', [])),
                    'has_real_events': bool(self.data.get('events')),
                    'message': '桌面端尚未收到手机能力报告' if not self.data.get('adapters') else ''}

    def events(self, limit: int = 50) -> list[dict]:
        with self.lock:
            return [dict(event) for event in self.data.get('events', [])[-max(1, min(MAX_EVENTS, limit)):]]

    def purge(self, adapter: str) -> int:
        with self.lock:
            before = len(self.data.get('events', []))
            self.data['events'] = [e for e in self.data.get('events', []) if e.get('adapter') != adapter]
            if adapter in self.data.get('adapters', {}):
                self.data['adapters'][adapter]['authorized_scopes'] = []
            self._write()
            return before - len(self.data['events'])
