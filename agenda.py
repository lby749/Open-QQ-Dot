"""The model's own todo list for unsolicited messages.

2026-10-13 user (proactivedaysmodel, , ): instead of a timing check inside a
sending window plus fixed care slots, the model keeps a few todos of its own ("12:47 ask about lunch", "21:35 just
look what they are doing"). When one is due the program wakes it with a fresh look at the phone; it sends now or not
and rewrites the upcoming todos. The care slots are only suggestions in the prompt. The program keeps (user's
choice): at most rounds_max unsolicited rounds a day, todos at least min_gap_minutes apart, and contact todos filled in
up to rounds_min a day unless the model gives a reason (fill_minimum).
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_TODOS = 6  # 2026-10-13 user: 4 -> 6
HORIZON = timedelta(days=2)
JITTER_MINUTES = 9
EMPTY_RETRY = timedelta(hours=3)  # the model left no todo: look again then
FAILED_RETRY = timedelta(minutes=30)


def min_gap(profile):
    return timedelta(minutes=int(profile.get('agenda_min_gap_minutes', 30)))


def todos(store):
    try:
        items = json.loads(store.get('agenda') or '[]')
    except ValueError:
        return []
    return [item for item in items if isinstance(item, dict) and item.get('at')]


def due_todo(store, now):
    """The earliest todo that is due; a first wake when the list was never written."""
    items = todos(store)
    if not items:
        return None if store.get('agenda') is not None else {'at': now.isoformat(), 'kind': 'plan', 'note': '排一下接下来的待办'}
    first = min(items, key=lambda item: item['at'])
    return first if datetime.fromisoformat(first['at']) <= now else None


def sent_today(store, now):
    return store.db.execute("SELECT count(*) FROM metadata WHERE key LIKE ? AND value='sent'",
                            (f'slot:{now.date().isoformat()}:%',)).fetchone()[0]


def clean_todos(raw, now, gap, rng=random):
    """The model's todos as the program keeps them: readable, moved by up to JITTER_MINUTES, inside now+gap .. now+2
    days, at least `gap` apart, at most MAX_TODOS; when nothing is left, one look again EMPTY_RETRY later."""
    kept = []
    for item in raw if isinstance(raw, list) else []:
        try:
            at = datetime.fromisoformat(item['at'])
        except (TypeError, KeyError, ValueError):
            continue
        at = at if at.tzinfo else at.replace(tzinfo=now.tzinfo)
        # 2026-10-13 user (): the model still picks round times (16:40, 22:10)
        at += timedelta(minutes=rng.randint(-JITTER_MINUTES, JITTER_MINUTES), seconds=rng.randint(0, 59))
        if not now + gap <= at <= now + HORIZON:
            continue
        if any(abs(at - datetime.fromisoformat(other['at'])) < gap for other in kept):
            continue
        kind = item.get('kind') if item.get('kind') in ('contact', 'peek') else 'contact'
        kept.append({'at': at.isoformat(), 'kind': kind, 'note': str(item.get('note', ''))[:80]})
        if len(kept) >= MAX_TODOS:
            break
    if not kept:
        kept.append({'at': (now + EMPTY_RETRY).isoformat(), 'kind': 'peek', 'note': '没有待办，再看看'})
    return sorted(kept, key=lambda item: item['at'])


def fill_minimum(kept, now, gap, needed, rng=random):
    """2026-10-13 user (): below the daily minimum, with too few contact todos left today and no reason
    given, contact todos are added at random times in the rest of the day (still `gap` apart, at most MAX_TODOS)."""
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=15)
    today = sum(1 for item in kept if item['kind'] == 'contact' and datetime.fromisoformat(item['at']) < end)
    for _ in range(40):
        if today >= needed or len(kept) >= MAX_TODOS or end <= now + gap:
            break
        at = now + gap + timedelta(seconds=rng.randint(0, int((end - now - gap).total_seconds())))
        if all(abs(at - datetime.fromisoformat(other['at'])) >= gap for other in kept):
            kept.append({'at': at.isoformat(), 'kind': 'contact', 'note': '今天还没聊够，找个话题'})
            today += 1
    return sorted(kept, key=lambda item: item['at'])


def phone_look(profile, directory):
    """What the phone shows now, looked at by the program at every wake; '' when the phone is not connected."""
    import phone_client
    import phone_tools
    if not phone_client.online(profile, directory):
        return ''
    try:
        return phone_tools.call_tool(directory, 'phone_now', {}).get('result', '')
    except Exception:
        return ''


def care_ideas(profile):
    """The care slots as suggestions (no longer triggers)."""
    from scheduled_care import configuration
    care = configuration(profile)
    if not care['enabled']:
        return ''
    ideas = [f"{slot['start']}-{slot['end']} {slot['instruction']}" for slot in care['slots'].values() if slot['enabled']]
    return 'CARE_IDEAS(OPTIONAL; ONLY_IF_IT_FITS_TODAY; ALREADY_CHATTED_ABOUT_IT -> SKIP): ' + ' | '.join(ideas) if ideas else ''


def wake(store, profile, directory, now, generate):
    """When a todo is due: one judgment (send now or not, the upcoming todos). Returns the round key to send, or None."""
    due = due_todo(store, now)
    if due is None:
        return None
    retry = store.get('smart_retry_at')
    if retry and now < datetime.fromisoformat(retry):
        return None
    from main import (conversation_context, _runtime_context_for_mode, record_prompt, EMOTION_PROMPT, EMOTION_LABELS,
                      save_emotions, opener_query, proactive_task, proactive_bubbles)
    from persona_template import bubble_cap
    from prompt_context import recent_rounds
    from proactive_planner import update_activity_memory
    update_activity_memory(store, now)
    settings = profile['proactive']
    gap = min_gap(profile)
    cap, floor = int(settings['rounds_max']), int(settings['rounds_min'])
    sent = sent_today(store, now)
    can_send = sent < cap
    count = random.randint(int(settings['bubbles_min']), int(settings['bubbles_max']))
    query = due.get('note') or opener_query(store, profile, use_topic=False)
    round_lines, _, offered = proactive_task(store, profile, directory, count, query=query)
    context = _runtime_context_for_mode(store, profile, directory, conversation_context(store, profile, include_recent=False), query)
    phone = phone_look(profile, directory) if due.get('kind') == 'peek' else ''
    lines = [
        ' [OUR_TODOS] ',
        'GOAL: A_TODO_OF_OURS_IS_DUE -> SEND_NOW_OR_NOT; THEN_REWRITE_OUR_UPCOMING_TODOS',
        'TODO_KINDS: contact=MAYBE_MESSAGE_FRIEND(TOPIC|CARE); peek=JUST_LOOK_WHAT_FRIEND_IS_DOING_THEN_DECIDE',
        'CONTEXT: PERSONA RELATIONSHIP_STAGE NOW PHONE TIMED_CHAT MEMORY_ROUTINES',
        'RESPECT: CLASS|SLEEP|BUSY|DONT_DISTURB; LOCKED_LONG|LATE_NIGHT -> PROBABLY_ASLEEP; GAME|VIDEO -> TOPIC_OR_LATER; '
        'UNANSWERED -> FEWER_AND_LIGHTER; NOT_AFTER_UNANSWERED_QUESTION',
        f'TIMES: LIKE_A_REAL_PERSON (12:47 NOT 12:00); BY FRIEND_ROUTINE CHAT PHONE; {MAX_TODOS}_TODOS_MAX; '
        f'AT_LEAST_{int(gap.total_seconds() // 60)}_MIN_APART; WITHIN_2_DAYS; KEEP_OLD_ONES_YOU_STILL_WANT',
        'DAILY_MIN: rounds_min_today; SENT_BELOW_IT -> ENOUGH_contact_TODOS_BEFORE_THE_DAY_ENDS; '
        'NOT_PLANNING_ENOUGH -> min_exception=CONCRETE_REASON(ASLEEP|BUSY_ALL_DAY|SAID_DONT_DISTURB); ELSE ""',
        'PHONE: 只在需要判断对方此刻做什么时查一次；MORE -> app_history/phone_usage_today; NEVER_SAY_YOU_LOOKED',
        care_ideas(profile),
        '' if settings.get('enabled') else 'PROACTIVE_TOPICS: OFF -> CONTACT_ONLY_FOR_CARE_IDEAS',
        ' [OUTPUT] ',
        'OUTPUT_FORMAT: JSON {"send_now":false,"reason":"SHORT","topic":"CONCRETE|","messages":[],'
        '"todos":[{"at":"ISO_WITH_TZ","kind":"contact|peek","note":"SHORT"}],"min_exception":"REASON|"}',
        (f'MESSAGES: ONLY_IF send_now; THINGS_TO_SAY={count} SPLIT_LIKE_TA; ON_THE_CHOSEN_topic'
         if bubble_cap(profile, count) != count else f'MESSAGES: ONLY_IF send_now; LENGTH={count}; ON_THE_CHOSEN_topic'),
        *round_lines,
        ' [DATA] ',
        'DATA: ' + json.dumps({'now': now.isoformat(), 'due_todo': due,
                               'upcoming_todos': [item for item in todos(store) if item is not due and item != due],
                               'rounds_min_today': floor, 'sent_today': sent,
                               'rounds_left_today': max(0, cap - sent), 'can_send': can_send,
                               'unanswered_rounds': int(store.get('active_unanswered_rounds', '0') or 0),
                               'timed_messages': recent_rounds(store)}, ensure_ascii=False),
    ]
    if phone:
        lines.append('DATA_PHONE_NOW: ' + phone)
    prompt = '\n'.join(line for line in lines if line)
    if profile.get('emotion_policy', '').strip():
        prompt += EMOTION_PROMPT
    prompt = record_prompt(store, '自排待办', context, prompt)
    try:
        response, _ = generate(prompt, directory, ephemeral=True, text_only=True, profile=profile)
        decision = json.loads(response)
        if (not isinstance(decision, dict) or type(decision.get('send_now')) is not bool
                or not isinstance(decision.get('reason'), str)):
            raise ValueError('模型待办判断格式无效')
    except Exception:
        store.set('smart_retry_at', (now + FAILED_RETRY).isoformat())
        store.set('smart_status', 'failed, 30minutesRetry; Send')
        raise
    kept = clean_todos(decision.get('todos'), now, gap)
    exception = decision.get('min_exception')
    if not (isinstance(exception, str) and exception.strip()):
        kept = fill_minimum(kept, now, gap, floor - sent - (1 if decision['send_now'] and can_send else 0))
    store.set('agenda', json.dumps(kept, ensure_ascii=False))
    store.set('smart_retry_at', '')
    topic = decision.get('topic') if isinstance(decision.get('topic'), str) else ''
    store.set('smart_proactive', json.dumps({'send_now': decision['send_now'], 'reason': decision['reason'][:300],
                                             'topic': topic[:300], 'decided_at': now.isoformat()}, ensure_ascii=False))
    if profile.get('emotion_policy', '').strip() and decision.get('self') in EMOTION_LABELS:
        save_emotions(store, {**decision, 'user': 'uncertain'}, '自排待办')
    if not decision['send_now']:
        store.set('smart_status', '模型这次选择不发：' + decision['reason'][:60])
        return None
    if not can_send:
        store.set('smart_status', 'model, daysproactive')
        return None
    prepared = None
    try:
        prepared = proactive_bubbles(store, decision.get('messages'), offered, bubble_cap(profile, count), decision.get('sticker'), profile)
    except RuntimeError:
        prepared = None
    key = f'{now.date().isoformat()}:smart:{uuid.uuid4().hex}'
    store.set('smart_prepared', json.dumps({'key': key, 'messages': prepared}, ensure_ascii=False) if prepared else '')
    store.set('smart_status', '模型决定现在发：' + decision['reason'][:60])
    return key


def next_label(store, now):
    """The status line: the next todo, a retry, or waiting for the first plan."""
    retry = store.get('smart_retry_at')
    if retry and now < datetime.fromisoformat(retry):
        return 'Retry ' + datetime.fromisoformat(retry).strftime('%m-%d %H:%M')
    items = todos(store)
    if not items:
        return '等待模型排待办'
    first = items[0]
    kind = '只看看' if first['kind'] == 'peek' else '可能联系'
    return f"下一个待办 {datetime.fromisoformat(first['at']):%m-%d %H:%M}（{kind}：{first['note']}，届时可不发）"


def todo_lines(store):
    """Every upcoming todo, one line each, for the proactivedays page."""
    return [f"  {datetime.fromisoformat(item['at']):%m-%d %H:%M}  {'只看看' if item['kind'] == 'peek' else '可能联系'}  {item['note']}"
            for item in todos(store)]


# ==============================================================================
# P2 Extension: Event-Driven Autonomous Action Engine (Dots-style Proactivity)
# ==============================================================================
import hashlib
import os
import sqlite3
import threading
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class EventType(str, Enum):
    TIMER = "timer"                          # Scheduled time
    PERIODIC = "periodic"                    # Periodic / Cron
    FILE_CHANGE = "file_change"              # File modified / created
    TASK_COMPLETED = "task_completed"        # A dependent task finished
    TASK_FAILED = "task_failed"              # Task execution failed
    GOAL_STATE_CHANGE = "goal_state_change"  # Goal reached milestone or needs next stage
    GITHUB_EVENT = "github_event"            # New issue / commit / release
    EXTERNAL_STATE = "external_state"        # API returns target status
    CHECKPOINT_REACHED = "checkpoint_reached"# Long task reached milestone
    CONDITION_MET = "condition_met"          # Custom user expression


class ProactiveLevel(str, Enum):
    LEVEL_0_PASSIVE = "level_0_passive"          # Level 0: Completely passive, no action or reminder
    LEVEL_1_REMIND = "level_1_remind"            # Level 1: Remind only, never create/execute tasks
    LEVEL_2_LOW_RISK = "level_2_low_risk"        # Level 2: Low-risk tasks (read-only, inspection, analysis)
    LEVEL_3_AUTONOMOUS = "level_3_autonomous"    # Level 3: Fully autonomous within bounds and budget

    # Backward compatibility aliases
    CONSERVATIVE = "level_1_remind"
    BALANCED = "level_2_low_risk"
    AUTONOMOUS = "level_3_autonomous"


def parse_proactive_level(val: Any) -> ProactiveLevel:
    """Safely parse input to ProactiveLevel."""
    if isinstance(val, ProactiveLevel):
        return val
    s = str(val).lower().strip()
    if s in ("0", "level_0", "passive", "off", "level_0_passive"):
        return ProactiveLevel.LEVEL_0_PASSIVE
    if s in ("1", "level_1", "remind", "conservative", "level_1_remind"):
        return ProactiveLevel.LEVEL_1_REMIND
    if s in ("2", "level_2", "low_risk", "balanced", "level_2_low_risk"):
        return ProactiveLevel.LEVEL_2_LOW_RISK
    if s in ("3", "level_3", "autonomous", "full", "level_3_autonomous"):
        return ProactiveLevel.LEVEL_3_AUTONOMOUS
    return ProactiveLevel.LEVEL_2_LOW_RISK


class ProactiveActionEngine:
    """Event-driven autonomous proactive engine:

    Event -> Identity & Ownership Check -> Proactive Level & Capability Filter -> Decision -> TaskManager / Notification.
    Includes persistent deduplication, cooldowns, lifecycle management, and explicit owner_id binding.
    """

    _instance: ProactiveActionEngine | None = None
    _engine_lock = threading.Lock()

    def __init__(self, db_path: Path | None = None):
        self._db_lock = threading.RLock()
        if db_path is None:
            data_dir = __import__("house_settings").data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = data_dir / "proactive_state.sqlite3"
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._cooldowns: dict[str, float] = {}  # in-memory fast cache
        self._file_mtimes: dict[str, float] = {}
        self._listeners: list[dict[str, Any]] = []
        self._default_cooldown_sec = 300.0  # 5 minutes default anti-spam cooldown
        self._builtin_registered = False
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._db_lock, self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS proactive_events (
                    fingerprint TEXT PRIMARY KEY,
                    trigger_name TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    last_triggered_at REAL NOT NULL,
                    next_allowed_at REAL NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS proactive_triggers (
                    name TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    cooldown_sec REAL NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    min_level TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_triggered_at REAL NOT NULL DEFAULT 0.0,
                    last_result TEXT NOT NULL DEFAULT '',
                    config_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS proactive_checkpoints (
                    key TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT '',
                    last_processed_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_owner ON proactive_events(owner_id);
                CREATE INDEX IF NOT EXISTS idx_proactive_checkpoints ON proactive_checkpoints(entity_id);
                """
            )
            conn.commit()

            # Load persisted cooldowns into memory cache
            cur = conn.execute("SELECT fingerprint, next_allowed_at FROM proactive_events WHERE next_allowed_at > ?", (datetime.now().timestamp(),))
            for row in cur.fetchall():
                self._cooldowns[row["fingerprint"]] = float(row["next_allowed_at"])

    @classmethod
    def get_instance(cls, db_path: Path | None = None) -> ProactiveActionEngine:
        with cls._engine_lock:
            if cls._instance is None:
                cls._instance = cls(db_path)
            return cls._instance

    def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        try:
            with self._db_lock, self._get_connection() as conn:
                row = conn.execute("SELECT * FROM proactive_checkpoints WHERE key = ?", (key,)).fetchone()
                if row:
                    return {
                        "key": row["key"],
                        "entity_id": row["entity_id"],
                        "version": row["version"],
                        "status": row["status"],
                        "last_processed_at": float(row["last_processed_at"]),
                        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
                    }
        except Exception:
            pass
        return None

    def set_checkpoint(self, key: str, entity_id: str, version: int = 1, status: str = "", metadata: dict | None = None):
        try:
            with self._db_lock, self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO proactive_checkpoints (key, entity_id, version, status, last_processed_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        entity_id = excluded.entity_id,
                        version = excluded.version,
                        status = excluded.status,
                        last_processed_at = excluded.last_processed_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (key, entity_id, int(version), status, datetime.now().timestamp(), json.dumps(metadata or {}, ensure_ascii=False)),
                )
                conn.commit()
        except Exception as exc:
            logger.debug(f"set_checkpoint error: {exc}")

    def register_trigger(
        self,
        event_type: EventType,
        name: str,
        condition_fn: Callable[[], bool | dict[str, Any]],
        action_plan_fn: Callable[[dict[str, Any]], dict[str, Any]],
        cooldown_sec: float = 300.0,
        priority: int = 50,
        owner_id: str = "system:proactive",
        min_proactive_level: ProactiveLevel = ProactiveLevel.LEVEL_2_LOW_RISK,
        enabled: bool = True,
    ):
        """Register or update an event trigger with explicit owner_id, proactive level, and persistent metadata.

        Preserves user-configured enabled/disabled state across server restarts.
        """
        clean_owner = str(owner_id).strip() or "system:proactive"

        # Check existing user preference in DB so code re-registration NEVER overwrites user-disabled status
        persisted_enabled = None
        persisted_cooldown = None
        persisted_level = None
        try:
            with self._db_lock, self._get_connection() as conn:
                cur = conn.execute("SELECT enabled, cooldown_sec, min_level FROM proactive_triggers WHERE name = ?", (name,))
                row = cur.fetchone()
                if row:
                    persisted_enabled = bool(row["enabled"])
                    persisted_cooldown = float(row["cooldown_sec"])
                    persisted_level = row["min_level"]
        except Exception:
            pass

        actual_enabled = persisted_enabled if persisted_enabled is not None else enabled
        actual_cooldown = persisted_cooldown if persisted_cooldown is not None else cooldown_sec
        actual_min_level = parse_proactive_level(persisted_level) if persisted_level is not None else min_proactive_level

        with self._engine_lock:
            # Update memory registry with preserved actual_enabled
            self._listeners = [l for l in self._listeners if l["name"] != name]
            self._listeners.append({
                "type": event_type,
                "name": name,
                "check": condition_fn,
                "plan": action_plan_fn,
                "cooldown": actual_cooldown,
                "priority": priority,
                "owner_id": clean_owner,
                "min_level": actual_min_level,
                "enabled": actual_enabled,
            })

        # Persist trigger config to SQLite: retain user's existing enabled state
        try:
            with self._db_lock, self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO proactive_triggers (name, event_type, owner_id, cooldown_sec, priority, min_level, enabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        event_type = excluded.event_type,
                        owner_id = excluded.owner_id,
                        priority = excluded.priority,
                        enabled = proactive_triggers.enabled
                    """,
                    (name, event_type.value if isinstance(event_type, EventType) else str(event_type),
                     clean_owner, float(actual_cooldown), int(priority), actual_min_level.value, 1 if actual_enabled else 0),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Persist trigger error: {e}")

    def list_triggers(self) -> list[dict[str, Any]]:
        """Inspect all registered triggers, their enabled state, cooldowns, and last executions."""
        with self._engine_lock:
            listeners_map = {l["name"]: l for l in self._listeners}

        results = []
        try:
            with self._db_lock, self._get_connection() as conn:
                cur = conn.execute("SELECT * FROM proactive_triggers ORDER BY priority DESC, name ASC")
                db_rows = {r["name"]: dict(r) for r in cur.fetchall()}
        except Exception:
            db_rows = {}

        now_ts = datetime.now().timestamp()
        for name, tr in listeners_map.items():
            db_info = db_rows.get(name, {})
            results.append({
                "name": name,
                "event_type": tr["type"].value if isinstance(tr["type"], EventType) else str(tr["type"]),
                "owner_id": tr["owner_id"],
                "cooldown_sec": tr["cooldown"],
                "priority": tr["priority"],
                "min_level": tr["min_level"].value if isinstance(tr["min_level"], ProactiveLevel) else str(tr["min_level"]),
                "enabled": bool(db_info.get("enabled", tr.get("enabled", True))),
                "last_triggered_at": db_info.get("last_triggered_at", 0.0),
                "next_allowed_at": self._cooldowns.get(name, 0.0),
                "in_cooldown": now_ts < self._cooldowns.get(name, 0.0),
                "last_result": db_info.get("last_result", ""),
            })
        return results

    def get_trigger(self, name: str) -> dict[str, Any] | None:
        triggers = [t for t in self.list_triggers() if t["name"] == name]
        return triggers[0] if triggers else None

    def enable_trigger(self, name: str) -> bool:
        """Enable a registered trigger."""
        with self._engine_lock:
            for l in self._listeners:
                if l["name"] == name:
                    l["enabled"] = True
                    break
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("UPDATE proactive_triggers SET enabled = 1 WHERE name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0

    def disable_trigger(self, name: str) -> bool:
        """Disable a registered trigger."""
        with self._engine_lock:
            for l in self._listeners:
                if l["name"] == name:
                    l["enabled"] = False
                    break
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("UPDATE proactive_triggers SET enabled = 0 WHERE name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0

    def remove_trigger(self, name: str) -> bool:
        """Unregister and delete a trigger."""
        with self._engine_lock:
            self._listeners = [l for l in self._listeners if l["name"] != name]
        with self._db_lock, self._get_connection() as conn:
            cur = conn.execute("DELETE FROM proactive_triggers WHERE name = ?", (name,))
            conn.commit()
            return cur.rowcount > 0

    def register_file_watcher(
        self,
        target_path: Path,
        on_change_task_title: str,
        on_change_description: str,
        owner_id: str = "system:proactive",
        cooldown_sec: float = 180.0,
    ):
        """Monitor a specific file/dir and create a TaskManager task upon changes."""
        path_str = str(target_path.resolve())
        if target_path.exists():
            self._file_mtimes[path_str] = target_path.stat().st_mtime

        def _check_change():
            if not target_path.exists():
                return False
            mtime = target_path.stat().st_mtime
            old_mtime = self._file_mtimes.get(path_str, 0.0)
            if mtime > old_mtime:
                self._file_mtimes[path_str] = mtime
                return {"file": path_str, "mtime": mtime}
            return False

        def _make_plan(event_data):
            return {
                "title": f"文件变更响应: {on_change_task_title}",
                "description": f"检测到文件 {event_data.get('file')} 发生变动。\n{on_change_description}",
                "category": "config_check" if owner_id == 'system:config_watch' else "code",
                "owner_id": owner_id,
                "requires_write": False,
                "requires_shell": False,
            }

        self.register_trigger(
            event_type=EventType.FILE_CHANGE,
            name=f"file_watch:{target_path.name}",
            condition_fn=_check_change,
            action_plan_fn=_make_plan,
            cooldown_sec=cooldown_sec,
            owner_id=owner_id,
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

    def ensure_builtin_triggers(self, workspace: Path | None = None, task_manager: Any | None = None):
        """Idempotently register all core production triggers across the four required categories."""
        with self._engine_lock:
            if self._builtin_registered:
                return
            self._builtin_registered = True

        ws = workspace or Path.cwd()

        # 1.  (TIMER / PERIODIC)
        def _check_timer_health():
            from task_manager import TaskManager, TaskStatus
            mgr = task_manager or TaskManager.get_instance(workspace=ws)
            # Find running tasks that haven't updated heartbeat for a long time (>600s)
            with mgr._db_lock, mgr._get_connection() as db:
                stale_rows = db.execute(
                    "SELECT task_id, title FROM tasks WHERE status = 'RUNNING' AND last_heartbeat > 0 AND (? - last_heartbeat) > 600",
                    (datetime.now().timestamp(),)
                ).fetchall()
            if stale_rows:
                return {"stale_count": len(stale_rows), "stale_tasks": [r["task_id"] for r in stale_rows[:3]]}
            return False

        def _plan_timer_health(data):
            return {
                "title": "后台超时任务健康巡检",
                "description": f"检测到 {data.get('stale_count', 0)} 个后台任务心跳超限，启动轻量状态恢复评估",
                "category": "general",
                "owner_id": "system:health",
                "requires_shell": False,
                "requires_write": False,
            }

        self.register_trigger(
            event_type=EventType.PERIODIC,
            name="builtin:timer_health",
            condition_fn=_check_timer_health,
            action_plan_fn=_plan_timer_health,
            cooldown_sec=120.0,
            priority=70,
            owner_id="system:health",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # 2.  (GOAL_STATE_CHANGE) -
        def _check_goal_patrol():
            from task_manager import TaskManager
            mgr = task_manager or TaskManager.get_instance(workspace=ws)
            active_goals = mgr.list_goals(status="ACTIVE")
            waiting_goals = mgr.list_goals(status="WAITING_REVIEW")
            all_goals = active_goals + waiting_goals
            if not all_goals:
                return False

            now_ts = datetime.now().timestamp()
            changed_goals = []
            for g in all_goals:
                gid = g["goal_id"]
                ver = int(g.get("version", 1))
                status = str(g.get("status", ""))
                prog = float(g.get("progress", 0.0))
                completed_count = len([t for t in g.get("tasks", []) if t.get("status") == "COMPLETED"])
                unresolved_count = len(g.get("unresolved", []))

                cp = self.get_checkpoint(f"goal:{gid}")
                needs_patrol = False
                if cp is None:
                    # Note
                    needs_patrol = True
                else:
                    cp_meta = cp.get("metadata", {})
                    #  (, , , , ) (12h)
                    if (ver != cp.get("version") or
                        status != cp.get("status") or
                        abs(prog - float(cp_meta.get("progress", -1.0))) > 0.001 or
                        completed_count != cp_meta.get("completed_count", -1) or
                        unresolved_count != cp_meta.get("unresolved_count", -1)):
                        needs_patrol = True
                    elif now_ts - cp.get("last_processed_at", 0.0) > 43200.0:
                        needs_patrol = True

                if needs_patrol:
                    changed_goals.append({
                        "goal_id": gid,
                        "version": ver,
                        "status": status,
                        "progress": prog,
                        "completed_count": completed_count,
                        "unresolved_count": unresolved_count,
                    })

            if not changed_goals:
                # ,  AI
                return False

            primary = changed_goals[0]
            return {
                "active_goals_count": len(active_goals),
                "waiting_goals_count": len(waiting_goals),
                "changed_goals": changed_goals,
                "primary_goal_id": primary["goal_id"],
                "version": primary["version"],
                "status": primary["status"],
                "progress": primary["progress"],
                "checkpoint_type": "goal_patrol",
            }

        def _plan_goal_patrol(data):
            primary_id = data.get("primary_goal_id", "")
            return {
                "title": f"长期目标状态自动巡检: {primary_id[:20]}",
                "description": f"巡检长期目标（检测到实质变动，活跃 {data.get('active_goals_count', 0)}，待确认 {data.get('waiting_goals_count', 0)}），就绪度评估与自动续推",
                "category": "goal_patrol",
                "owner_id": "system:goal_patrol",
                "requires_shell": False,
                "requires_write": False,
            }

        self.register_trigger(
            event_type=EventType.GOAL_STATE_CHANGE,
            name="builtin:goal_patrol",
            condition_fn=_check_goal_patrol,
            action_plan_fn=_plan_goal_patrol,
            cooldown_sec=60.0,
            priority=65,
            owner_id="system:goal_patrol",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # 3.  (TASK_COMPLETED) - ,
        def _check_task_completed_audit():
            from task_manager import TaskManager, TaskStatus
            mgr = task_manager or TaskManager.get_instance(workspace=ws)
            with mgr._db_lock, mgr._get_connection() as db:
                rows = db.execute(
                    "SELECT task_id, title, category, owner_id FROM tasks WHERE status = 'COMPLETED' AND priority >= 70 ORDER BY updated_at ASC"
                ).fetchall()

            now_ts = datetime.now().timestamp()
            for r in rows:
                tid = r["task_id"]
                cp = self.get_checkpoint(f"audit_task:{tid}")
                if cp is None:
                    # high
                    return {
                        "task_id": tid,
                        "title": r["title"],
                        "owner_id": r["owner_id"],
                        "checkpoint_type": "task_audit",
                        "retry_count": 0,
                    }
                else:
                    cp_status = cp.get("status", "")
                    cp_meta = cp.get("metadata", {})
                    audit_task_id = cp_meta.get("audit_task_id")
                    retry_count = int(cp_meta.get("retry_count", 1))

                    # 1. success,
                    if cp_status in ("VERIFIED", "AUDITED"):
                        continue

                    # 2.
                    if audit_task_id:
                        audit_task = mgr.get_task(audit_task_id)
                        if audit_task:
                            # success
                            if audit_task.status == TaskStatus.COMPLETED:
                                self.set_checkpoint(
                                    f"audit_task:{tid}",
                                    tid,
                                    status="VERIFIED",
                                    metadata={
                                        "task_id": tid,
                                        "audit_task_id": audit_task_id,
                                        "verified_at": now_ts,
                                        "retry_count": retry_count,
                                    },
                                )
                                continue
                            # failedCancel
                            elif audit_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                                if retry_count < 3:
                                    # Retry,
                                    return {
                                        "task_id": tid,
                                        "title": r["title"],
                                        "owner_id": r["owner_id"],
                                        "checkpoint_type": "task_audit",
                                        "retry_count": retry_count,
                                    }
                                else:
                                    # Retry,  FAILED
                                    self.set_checkpoint(
                                        f"audit_task:{tid}",
                                        tid,
                                        status="FAILED",
                                        metadata={
                                            "task_id": tid,
                                            "audit_task_id": audit_task_id,
                                            "retry_count": retry_count,
                                            "error": audit_task.error or "审计任务重试已达上限",
                                        },
                                    )
                                    continue
                            # running, ok,
                            else:
                                continue
                        else:
                            # error, Retry
                            if retry_count < 3:
                                return {
                                    "task_id": tid,
                                    "title": r["title"],
                                    "owner_id": r["owner_id"],
                                    "checkpoint_type": "task_audit",
                                    "retry_count": retry_count,
                                }

            return False

        def _plan_task_completed_audit(data):
            return {
                "title": f"任务完成沉淀审计: {data.get('title', '')[:30]}",
                "description": f"对高优先级完成任务 {data.get('task_id')} 进行知识复盘与经验模式持久化",
                "category": "general",
                "owner_id": data.get("owner_id", "system:audit"),
                "requires_shell": False,
                "requires_write": False,
            }

        self.register_trigger(
            event_type=EventType.TASK_COMPLETED,
            name="builtin:task_completed_auditor",
            condition_fn=_check_task_completed_audit,
            action_plan_fn=_plan_task_completed_audit,
            cooldown_sec=120.0,
            priority=50,
            owner_id="system:audit",
            min_proactive_level=ProactiveLevel.LEVEL_2_LOW_RISK,
        )

        # 4.  (FILE_CHANGE)
        config_file = ws / "friends.json"
        if config_file.exists():
            self.register_file_watcher(
                target_path=config_file,
                on_change_task_title="配置变动巡检",
                on_change_description="好友配置已更新，执行系统权限重载与工作区校验",
                owner_id="system:config_watch",
                cooldown_sec=180.0,
            )

    register_builtin_triggers = ensure_builtin_triggers

    def poll_events_and_act(
        self,
        proactive_level: ProactiveLevel | str = ProactiveLevel.LEVEL_2_LOW_RISK,
        workspace: Path | None = None,
        notification_sink: Callable[[str, str], None] | None = None,
        task_manager: Any | None = None,
    ) -> list[str]:
        """Poll registered triggers, evaluate proactive decision, check capability authorization, and dispatch tasks."""
        level = parse_proactive_level(proactive_level)
        if level == ProactiveLevel.LEVEL_0_PASSIVE:
            # Level 0 is completely passive: neither notifications nor tasks are executed
            return []

        from task_manager import TaskManager
        manager = task_manager or TaskManager.get_instance(workspace=workspace)
        ws = workspace or getattr(manager, "workspace", Path.cwd())
        now_ts = datetime.now().timestamp()
        dispatched_task_ids = []

        with self._engine_lock:
            active_triggers = list(self._listeners)

        for tr in active_triggers:
            tr_name = tr["name"]
            cooldown_sec = tr["cooldown"]
            tr_owner = tr.get("owner_id", "system:proactive")
            is_enabled = tr.get("enabled", True)

            # Skip disabled trigger
            if not is_enabled:
                continue

            # 1. Cooldown check (memory cache + fallback to persistent state)
            next_allowed = self._cooldowns.get(tr_name, 0.0)
            if now_ts < next_allowed:
                continue

            # 2. Condition check with exception isolation
            try:
                event_data = tr["check"]()
            except Exception as e:
                logger.debug(f"Trigger {tr_name} check exception: {e}")
                continue

            if not event_data:
                continue

            event_payload = event_data if isinstance(event_data, dict) else {}

            # 3. Persistent Fingerprint deduplication across restarts
            fp = hashlib.md5(f"{tr_name}:{json.dumps(event_payload, sort_keys=True)}".encode()).hexdigest()
            if now_ts < self._cooldowns.get(fp, 0.0):
                continue

            # Update cooldowns in memory & DB
            next_time = now_ts + cooldown_sec
            self._cooldowns[tr_name] = next_time
            self._cooldowns[fp] = now_ts + cooldown_sec * 2

            try:
                with self._db_lock, self._get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO proactive_events (fingerprint, trigger_name, owner_id, last_triggered_at, next_allowed_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fingerprint) DO UPDATE SET
                            last_triggered_at = excluded.last_triggered_at,
                            next_allowed_at = excluded.next_allowed_at
                        """,
                        (fp, tr_name, tr_owner, now_ts, next_time, json.dumps(event_payload, ensure_ascii=False)),
                    )
            except Exception:
                pass

            # 4. Generate action plan safely
            try:
                plan = tr["plan"](event_payload)
            except Exception as plan_err:
                logger.debug(f"Trigger {tr_name} plan exception: {plan_err}")
                continue

            target_owner = str(plan.get("owner_id") or tr_owner).strip()
            task_title = plan.get("title", f"自主响应: {tr_name}")
            task_desc = plan.get("description", "事件驱动自动规划任务")
            task_category = plan.get("category", "general")

            # 5. Evaluate Capabilities & Real User Proactive Level
            # Read real user proactive_level and computer_tools permissions from friend config
            user_level = level
            commands_allowed = False
            tools_enabled = False
            try:
                friend_cfg_path = ws / "friends" / target_owner / "friend.json"
                if friend_cfg_path.is_file():
                    cfg_data = json.loads(friend_cfg_path.read_text("utf-8"))
                    if "proactive_level" in cfg_data:
                        user_level = parse_proactive_level(cfg_data["proactive_level"])
                    ct_info = cfg_data.get("computer_tools", {})
                    commands_allowed = bool(ct_info.get("commands", False))
                    tools_enabled = bool(ct_info.get("enabled", False))
                elif target_owner.startswith("system:"):
                    # System triggers default to controlled system tools
                    tools_enabled = True
                    commands_allowed = False
            except Exception:
                pass

            # Behavior A: Level 0 is PASSIVE -> totally silent, NO tasks, NO notifications!
            if user_level == ProactiveLevel.LEVEL_0_PASSIVE:
                continue

            # Behavior B: User proactive level must satisfy trigger's minimum level requirement
            tr_min_level = tr.get("min_level", ProactiveLevel.LEVEL_2_LOW_RISK)
            if isinstance(tr_min_level, str):
                tr_min_level = parse_proactive_level(tr_min_level)
            _level_weights = {
                ProactiveLevel.LEVEL_0_PASSIVE: 0,
                ProactiveLevel.LEVEL_1_REMIND: 1,
                ProactiveLevel.LEVEL_2_LOW_RISK: 2,
                ProactiveLevel.LEVEL_3_AUTONOMOUS: 3,
            }
            if _level_weights.get(user_level, 0) < _level_weights.get(tr_min_level, 2):
                continue

            requires_shell = bool(plan.get("requires_shell") or ("shell" in task_desc.lower()) or ("命令" in task_title))
            requires_write = bool(plan.get("requires_write") or task_category in ("high_risk", "system_config", "file_delete"))

            # Level 1: Remind only. Send notifications, DO NOT create or execute tasks
            if user_level == ProactiveLevel.LEVEL_1_REMIND:
                msg_content = f"触发源: {tr_name}\n归属: {target_owner}\n详情: {task_desc}"
                if notification_sink:
                    notification_sink(f"【主动提醒】 [Proactive Reminder] 触发事件: {task_title}", msg_content)
                try:
                    from notification_manager import NotificationManager
                    NotificationManager.get_instance().publish(
                        title=f"【主动提醒】 [Proactive Reminder] 触发事件: {task_title}",
                        content=msg_content,
                        category="proactive",
                        recipient_id=target_owner,
                        metadata={"trigger_name": tr_name, "proactive_level": user_level.value},
                    )
                except Exception:
                    pass
                continue

            # Level 2: Low-risk tasks only. Strictly enforce Capability boundaries
            if user_level == ProactiveLevel.LEVEL_2_LOW_RISK:
                # If plan requires shell commands but user has disabled commands, BLOCK autonomous run
                if requires_shell and not commands_allowed:
                    block_msg = f"当前主动等级为 Level 2 (低风险)，且用户 {target_owner} 未授权 Shell 命令执行权限 (Authorize Shell not permitted)，已拦截自主执行"
                    if notification_sink:
                        notification_sink(f"【权限拦截】自主任务跳过: {task_title}", block_msg)
                    try:
                        from notification_manager import NotificationManager
                        NotificationManager.get_instance().publish(
                            title=f"【权限拦截】自主任务跳过: {task_title}",
                            content=block_msg,
                            category="proactive",
                            recipient_id=target_owner,
                            metadata={"trigger_name": tr_name, "reason": "commands_disabled"},
                        )
                    except Exception:
                        pass
                    continue

                # If plan requires destructive writes but tools are not enabled, BLOCK autonomous run
                if requires_write and not tools_enabled:
                    block_msg = f"当前主动等级为 Level 2，用户 {target_owner} 未启用工具修改权限，已拦截高危写任务: {task_title}"
                    if notification_sink:
                        notification_sink(f"【权限拦截】高危自主任务跳过: {task_title}", block_msg)
                    try:
                        from notification_manager import NotificationManager
                        NotificationManager.get_instance().publish(
                            title=f"【权限拦截】高危自主任务跳过: {task_title}",
                            content=block_msg,
                            category="proactive",
                            recipient_id=target_owner,
                            metadata={"trigger_name": tr_name, "reason": "tools_disabled"},
                        )
                    except Exception:
                        pass
                    continue

            # 6. Dispatch task to TaskManager with EXPLICIT owner_id and idempotency key
            # 6.1 Special handling for goal_patrol: dispatch PER-GOAL tasks to avoid missing any changed goals
            if event_payload.get("checkpoint_type") == "goal_patrol" and event_payload.get("changed_goals"):
                changed_goals = event_payload.get("changed_goals", [])
                for cg in changed_goals:
                    cg_id = cg["goal_id"]
                    cg_title = f"长期目标状态自动巡检: {cg_id[:20]}"
                    cg_desc = f"巡检长期目标 {cg_id}（检测到实质变动，状态: {cg.get('status')}，进度: {cg.get('progress')}），就绪度评估与自动续推"
                    cg_fp = hashlib.md5(f"goal_patrol:{cg_id}:{cg.get('version', 1)}:{cg.get('status')}:{cg.get('progress')}".encode()).hexdigest()
                    try:
                        cg_tid = manager.create_task(
                            title=cg_title,
                            description=cg_desc,
                            category="goal_patrol",
                            priority=tr.get("priority", 65),
                            owner_id=target_owner,
                            initial_checkpoint={
                                "trigger_source": tr_name,
                                "goal_id": cg_id,
                                "owner_id": target_owner,
                                "idempotency_key": cg_fp,
                            },
                        )
                        dispatched_task_ids.append(cg_tid)
                        # success,  checkpoint!
                        self.set_checkpoint(
                            f"goal:{cg_id}",
                            cg_id,
                            version=cg.get("version", 1),
                            status=cg.get("status", ""),
                            metadata={
                                "progress": cg.get("progress", 0.0),
                                "completed_count": cg.get("completed_count", 0),
                                "unresolved_count": cg.get("unresolved_count", 0),
                                "patrol_task_id": cg_tid,
                            },
                        )
                    except Exception as cg_err:
                        logger.error(f"Failed to dispatch patrol task for goal {cg_id}: {cg_err}")
                continue

            # 6.2 Standard task dispatch for other triggers (including task_completed_audit)
            try:
                tid = manager.create_task(
                    title=task_title,
                    description=task_desc,
                    category=task_category,
                    priority=tr.get("priority", 50),
                    owner_id=target_owner,
                    initial_checkpoint={
                        "trigger_source": tr_name,
                        "owner_id": target_owner,
                        "event_data": event_payload,
                        "proactive_level": user_level.value,
                        "idempotency_key": fp,
                        "fingerprint": fp,
                    },
                )
                dispatched_task_ids.append(tid)

                # Update trigger record in database
                with self._db_lock, self._get_connection() as conn:
                    conn.execute(
                        "UPDATE proactive_triggers SET last_triggered_at = ?, last_result = ? WHERE name = ?",
                        (now_ts, f"Created task {tid}", tr_name),
                    )
                    conn.commit()

                # Update proactive checkpoints: task audit is set to PENDING (never premature AUDITED/VERIFIED!)
                if event_payload.get("checkpoint_type") == "task_audit":
                    audited_tid = event_payload.get("task_id", "")
                    if audited_tid:
                        prev_cp = self.get_checkpoint(f"audit_task:{audited_tid}")
                        prev_meta = prev_cp.get("metadata", {}) if prev_cp else {}
                        cur_retry = int(prev_meta.get("retry_count", 0)) + 1
                        self.set_checkpoint(
                            f"audit_task:{audited_tid}",
                            audited_tid,
                            status="PENDING",
                            metadata={
                                "task_id": audited_tid,
                                "audit_task_id": tid,
                                "retry_count": cur_retry,
                                "created_at": now_ts,
                            },
                        )

                # 7. Notify user of task creation via sink or durable NotificationManager
                notif_title = f"【自主事件响应】已为 {target_owner} 创建任务: {task_title}"
                notif_content = f"任务ID: {tid}\n触发来源: {tr_name}"
                if notification_sink:
                    notification_sink(notif_title, notif_content)
                try:
                    from notification_manager import NotificationManager
                    NotificationManager.get_instance().publish(
                        title=notif_title,
                        content=notif_content,
                        category="proactive",
                        recipient_id=target_owner,
                        metadata={"task_id": tid, "trigger_name": tr_name, "dedup_key": f"{tid}:created"},
                    )
                except Exception:
                    pass
            except Exception as task_err:
                logger.error(f"Failed to dispatch proactive task for {tr_name}: {task_err}")
                # Rollback cooldown so the event remains retryable upon task dispatch failure
                self._cooldowns.pop(fp, None)
                self._cooldowns.pop(tr_name, None)
                try:
                    with self._db_lock, self._get_connection() as conn:
                        conn.execute("DELETE FROM proactive_events WHERE fingerprint = ?", (fp,))
                        conn.commit()
                except Exception:
                    pass

        return dispatched_task_ids

