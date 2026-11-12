"""Read-only Codex quota guard for the local annotation pipeline."""
import json
import os
from pathlib import Path
import time

from app_server import CodexAppServer


class QuotaGuard:
    def __init__(self, root, *, reader=None, clock=time.time):
        self.root = Path(root)
        self.reader = reader
        self.clock = clock
        self.server = None
        self.checked_at = None
        self.snapshot = None
        self.error = None

    def _read(self):
        if self.reader:
            return self.reader()
        if self.server is None:
            self.server = CodexAppServer(4096)
        self.server.start()
        # Documented protocol; no generation, credential changes or reset calls.
        return self.server._request('account/rateLimits/read', {}, time.monotonic() + 20)

    def _state(self):
        return json.loads((self.root / 'docs/quota_state.json').read_text('utf-8-sig'))

    def _save(self, state):
        target = self.root / 'docs/quota_state.json'
        temp = target.with_name(target.name + f'.{os.getpid()}.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        os.replace(temp, target)

    def allowed(self):
        try:
            now = self.clock()
            state = self._state()
            if self.checked_at is None or now - self.checked_at >= 60:
                raw = self._read()
                bucket = (raw.get('rateLimitsByLimitId') or {}).get('codex', raw.get('rateLimits'))
                windows = {x['windowDurationMins']: x for x in
                           (bucket.get('primary'), bucket.get('secondary')) if isinstance(x, dict)}
                primary, weekly = windows[300], windows[10080]
                for window in (primary, weekly):
                    if (type(window.get('usedPercent')) not in (float, int) or
                            not 0 <= window['usedPercent'] <= 100 or
                            type(window.get('resetsAt')) not in (float, int)):
                        raise ValueError('quota window incomplete')
                if type(raw.get('ordinaryUsageAllowed')) is not bool:
                    raise ValueError('ordinaryUsageAllowed unknown')
                self.snapshot = {'primary': primary, 'weekly': weekly,
                                 'ordinaryUsageAllowed': raw['ordinaryUsageAllowed']}
                self.checked_at = now
                self.error = None
            primary, weekly = self.snapshot['primary'], self.snapshot['weekly']
            status = ('PAUSED_WEEKLY' if weekly['usedPercent'] >= 98 else
                      'PAUSED_QUOTA' if primary['usedPercent'] >= 95 else None)
            # Re-read before mutation so the single-card record and root changes survive.
            state = self._state()
            if status:
                if state.get('status') != status:
                    window = weekly if status == 'PAUSED_WEEKLY' else primary
                    state.update(status=status, paused_resets_at=window['resetsAt'],
                                 observed_primary_used_percent=primary['usedPercent'],
                                 observed_weekly_used_percent=weekly['usedPercent'],
                                 pause_reason='local pipeline live quota threshold')
                    self._save(state)
                return False
            if not self.snapshot['ordinaryUsageAllowed']:
                return False
            if state.get('status') in ('PAUSED_QUOTA', 'PAUSED_WEEKLY'):
                reset = state.get('paused_resets_at')
                window = weekly if state['status'] == 'PAUSED_WEEKLY' else primary
                previous = state.get('observed_weekly_used_percent' if
                                     state['status'] == 'PAUSED_WEEKLY' else 'observed_primary_used_percent')
                if (type(reset) not in (int, float) or now < reset or
                        window['resetsAt'] <= reset or type(previous) not in (int, float) or
                        window['usedPercent'] >= previous):
                    return False
                state.update(status='RUNNING', paused_resets_at=None, pause_reason=None,
                             observed_primary_used_percent=primary['usedPercent'],
                             observed_weekly_used_percent=weekly['usedPercent'])
                self._save(state)
            return state.get('status') == 'RUNNING'
        except Exception as exc:
            self.error = type(exc).__name__
            self.checked_at = None
            return False

    def close(self):
        if self.server:
            self.server.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
