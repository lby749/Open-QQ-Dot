"""Pure per-friend proactive scheduling rules."""
from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta


SLOT_GRACE = timedelta(minutes=10)


def window_start(now: datetime, settings: dict | None = None) -> datetime | None:
    settings = settings or {}
    start_time = time.fromisoformat(settings.get("window_start", "10:00"))
    end_time = time.fromisoformat(settings.get("window_end", "05:00"))
    local_time = now.timetz().replace(tzinfo=None)
    crosses_midnight = end_time <= start_time
    if crosses_midnight and local_time >= start_time:
        day = now.date()
    elif crosses_midnight and local_time < end_time:
        day = now.date() - timedelta(days=1)
    elif not crosses_midnight and start_time <= local_time < end_time:
        day = now.date()
    else:
        return None
    return datetime.combine(day, start_time, tzinfo=now.tzinfo)


def window_duration(settings: dict) -> timedelta:
    start_time = time.fromisoformat(settings.get("window_start", "10:00"))
    end_time = time.fromisoformat(settings.get("window_end", "05:00"))
    start = datetime.combine(datetime.min.date(), start_time)
    end = datetime.combine(datetime.min.date(), end_time)
    if end <= start:
        end += timedelta(days=1)
    return end - start


def make_plan(start: datetime, settings: dict, rng=random, avoid_minutes: set[str] | None = None) -> list[str]:
    low = int(settings.get("rounds_min", 6))
    high = int(settings.get("rounds_max", 10))
    count = rng.randint(low, high)
    span = window_duration(settings)
    spacing = span / (count + 1)
    jitter = min(timedelta(minutes=20), spacing * 0.18)
    result = []
    used_minutes = set(avoid_minutes or ())
    for index in range(count):
        center = start + spacing * (index + 1)
        offset = rng.uniform(-jitter.total_seconds(), jitter.total_seconds())
        slot = center + timedelta(seconds=offset)
        while slot.strftime("%H:%M") in used_minutes:
            slot += timedelta(minutes=1)
        used_minutes.add(slot.strftime("%H:%M"))
        result.append(slot.isoformat())
    return result


def due_slot(store, now: datetime, settings: dict, rng=random) -> tuple[str, datetime] | None:
    if (
        not settings.get("enabled", False)
        or store.get("proactive_paused", "0") == "1"
        or store.get("reply_waiting", "0") == "1"
        or store.get("paused_reason", "")
    ):
        return None
    start = window_start(now, settings)
    if start is None:
        return None
    plan_key = f"plan:{start.date().isoformat()}"
    raw = store.get(plan_key)
    if raw is None:
        previous = store.get(f"plan:{(start.date() - timedelta(days=1)).isoformat()}")
        avoid_minutes = {
            datetime.fromisoformat(slot).strftime("%H:%M") for slot in json.loads(previous)
        } if previous else set()
        slots = make_plan(start, settings, rng, avoid_minutes)
        store.set(plan_key, json.dumps(slots))
    else:
        slots = json.loads(raw)
    eligible_raw = store.get("eligible_after")
    eligible = datetime.fromisoformat(eligible_raw) if eligible_raw else None
    for index, slot_raw in enumerate(slots):
        slot = datetime.fromisoformat(slot_raw)
        slot_key = f"{start.date().isoformat()}:{index}"
        if store.get(f"slot:{slot_key}") is not None:
            continue
        if now > slot + SLOT_GRACE:
            store.set(f"slot:{slot_key}", "missed")
            continue
        if now >= slot and (eligible is None or now >= eligible):
            return slot_key, slot
        return None
    return None


def defer_after_activity(store, seconds: int):
    store.set("eligible_after", (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat())


def automatic_cooldown_range(store, settings, now):
    start = window_start(now, settings)
    count = (int(settings.get('rounds_min', 6)) + int(settings.get('rounds_max', 10))) / 2
    if start:
        day = start.date().isoformat()
        raw = store.get('plan:' + day)
        if raw:
            count = sum(store.get(f'slot:{day}:{index}') != 'rescheduled'
                        for index, _ in enumerate(json.loads(raw)))
    # Leave room for random scheduling jitter and the time spent sending bubbles.
    interval = window_duration(settings).total_seconds() / (count + 1)
    return max(1, round(interval * 0.4)), max(1, round(interval * 0.6))


def automatic_cooldown(store, settings, now):
    low, high = automatic_cooldown_range(store, settings, now)
    draw = float(store.get('cooldown_draw', '0.5'))
    return round(low + (high - low) * draw)


def defer_automatically(store, settings, now=None):
    now = now or datetime.now().astimezone()
    store.set('cooldown_activity_at', now.isoformat())
    store.set('cooldown_kind', 'automatic')
    store.set('cooldown_draw', str(random.random()))
    store.set('eligible_after', (now + timedelta(seconds=automatic_cooldown(store, settings, now))).isoformat())


def sync_automatic_cooldown(store, settings, now):
    raw = store.get('eligible_after')
    if not raw or store.get('cooldown_kind') == 'care':
        return
    activity = store.get('cooldown_activity_at')
    if not activity:
        rows = store.recent_messages(1)
        if not rows:
            return
        activity = rows[-1]['created_at']
        store.set('cooldown_activity_at', activity)
        store.set('cooldown_kind', 'automatic')
    if store.get('cooldown_draw') is None:
        store.set('cooldown_draw', str(random.random()))
    eligible = (datetime.fromisoformat(activity) + timedelta(seconds=automatic_cooldown(store, settings, now))).isoformat()
    if eligible != raw:
        store.set('eligible_after', eligible)


def replan_remaining(store, now: datetime, settings: dict, rng=random) -> int:
    start = window_start(now, settings)
    if start is None:
        start = datetime.combine(now.date(), time.fromisoformat(settings['window_start']), tzinfo=now.tzinfo)
    end = start + window_duration(settings)
    eligible = store.get('eligible_after')
    earliest = max(now + timedelta(minutes=1), start,
                   datetime.fromisoformat(eligible) if eligible else start)
    if earliest >= end:
        raise ValueError('今天的新时段或冷却之后已无可用时间；明天按新设置生成计划。')
    day = start.date().isoformat()
    plan_key = 'plan:' + day
    slots = json.loads(store.get(plan_key) or '[]')
    attempted = 0
    updates = {}
    for row in store.db.execute("SELECT key,value FROM metadata WHERE key LIKE 'plan:%'"):
        for index, slot_raw in enumerate(json.loads(row['value'])):
            key = f"slot:{row['key'][5:]}:{index}"
            state = store.get(key)
            slot = datetime.fromisoformat(slot_raw)
            if state in ('sent', 'failed', 'cancelled', 'in_progress') and (
                    row['key'] == plan_key or now - timedelta(days=1) <= slot <= now):
                attempted += 1
    for index in range(len(slots)):
        key = f'slot:{day}:{index}'
        if store.get(key) is None:
            updates[key] = 'rescheduled'
    count = max(0, rng.randint(int(settings['rounds_min']), int(settings['rounds_max'])) - attempted)
    spacing = (end - earliest) / (count + 1)
    slots.extend((earliest + spacing * (index + 1)).isoformat() for index in range(count))
    updates[plan_key] = json.dumps(slots)
    # Keep slot indices and save the replacement atomically while the bridge owns scheduling.
    with store.db:
        for key, value in updates.items():
            store.db.execute('INSERT INTO metadata(key,value) VALUES(?,?) '
                             'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    return count


def should_cancel_round(store, generation_at_start: int) -> bool:
    return int(store.get("inbound_generation", "0") or 0) != generation_at_start

