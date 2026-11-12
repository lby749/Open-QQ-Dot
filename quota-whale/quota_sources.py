""" Claude Code / Codex . , , . """
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CODEX_SESSIONS = Path(os.environ.get('CODEX_HOME') or HOME / '.codex') / 'sessions'
CLAUDE_PROJECTS = HOME / ".claude" / "projects"

# :  (, )
_claude_cache = {}


def codex_quota():
    """items rate_limits .  None . """
    if not CODEX_SESSIONS.exists():
        return None
    files = sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:30]:
        last = None
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' in line:
                    last = line
        if last is None:
            continue
        try:
            obj = json.loads(last)
        except ValueError:
            continue
        rl = (obj.get("payload") or {}).get("rate_limits") or {}
        windows = {}
        for key in ("primary", "secondary"):
            w = rl.get(key) or {}
            if w.get("window_minutes") and w.get("used_percent") is not None:
                windows[int(w["window_minutes"])] = w
        if 300 not in windows or 10080 not in windows:
            continue
        sampled = _parse_iso(obj.get('timestamp'))
        now = datetime.now(timezone.utc)
        return {'five_used': _window_usage(windows[300], sampled, now),
                'week_used': _window_usage(windows[10080], sampled, now), 'updated': sampled}
    return None


def _window_usage(window, sampled, now):
    """An expired snapshot is unknown, not evidence that the new window is exhausted."""
    if sampled is None or (now - sampled).total_seconds() >= int(window['window_minutes']) * 60:
        return None
    reset = window.get('resets_at')
    if reset is not None:
        try:
            if now.timestamp() >= float(reset):
                return None
        except (TypeError, ValueError):
            return None
    return float(window['used_percent'])


def claude_usage(now=None):
    """ Claude Code  token  () :  5 hours,  7 days. . """
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    five_ago = now - timedelta(hours=5)
    seen = {}  # message id -> (, token), items
    if CLAUDE_PROJECTS.exists():
        for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
            st = path.stat()
            if st.st_mtime < week_ago.timestamp():
                continue
            for t, key, tokens in _claude_records(path, st):
                if t >= week_ago:
                    seen[key] = (t, tokens)
    five_h = sum(tok for t, tok in seen.values() if t >= five_ago)
    week = sum(tok for _, tok in seen.values())
    return {"five_h": five_h, "week": week}


def _claude_records(path, st):
    hit = _claude_cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    records = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage")
            t = _parse_iso(obj.get("timestamp"))
            if not usage or t is None:
                continue
            tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
            records.append((t, msg.get("id") or obj.get("uuid"), tokens))
    _claude_cache[path] = (st.st_mtime, st.st_size, records)
    return records


def _parse_iso(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)
