"""Bridge control and friend status texts shared by the desktop panels (no UI code)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from scheduled_care import DEFAULTS as CARE_DEFAULTS, configuration as care_configuration, validate as care_validate
from store import FriendStore
from scheduler import SLOT_GRACE, window_start

ROOT = Path(os.environ.get("QQ_BRIDGE_HOME", Path(__file__).resolve().parent))
REGISTRY = ROOT / "friends.json"
SCRIPT = ROOT / "bridge.ps1"
LOCAL_THINKING = {"Close () ": "off", "low (≤256 token) ": "low",
                  "medium (≤1024 token) ": "medium", "high (≤4096 token) ": "high"}


def invoke_bridge(action: str) -> tuple[int, str]:
    try:
        from packaged_bridge import bridge_process, invoke
        if os.name!='nt' or getattr(sys, "frozen", False) or os.environ.get("QQ_BRIDGE_LAUNCHER") or bridge_process():
            return invoke(action)
        script = str(SCRIPT).replace("'", "''")
        command = f"[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); & '{script}' {action}"
        with tempfile.TemporaryFile() as captured:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=ROOT, stdout=captured, stderr=subprocess.STDOUT, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            captured.seek(0)
            output = captured.read().decode("utf-8", errors="replace").strip()
        return result.returncode, output
    except Exception as exc:
        return 1, str(exc)


def load_profile(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_profile(path: Path, profile: dict):
    from source_state import profile_write
    with profile_write(path):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as file:
            json.dump(profile, file, ensure_ascii=False, indent=2)
            file.write("\n")
            temp_path = file.name
        from house_settings import replace_file
        try:
            replace_file(temp_path, path)
        finally:
            Path(temp_path).unlink(missing_ok=True)

def _set_proactive(profile, on):
    from main import default_profile, validate_proactive_settings
    schedule = {**default_profile(profile.get('qq_id', ''), '', True)['proactive'], **profile.get('proactive', {})}
    schedule['enabled'] = on
    validate_proactive_settings(schedule)
    profile['proactive'] = schedule


def _care_on(profile):
    return bool(care_configuration(profile)['enabled'])


def _set_care(profile, on):
    care = care_configuration(profile)
    care['enabled'] = on
    care_validate(care)
    profile['scheduled_care'] = care
    profile.setdefault('proactive', {})['time_aware'] = False


# 2026-10-11 user (Settings): every on/off switch of a friend, (key, title, note, read, write); each saves at once.
# Shared by the desktop panel's status page and the phone web page (house_web).
SWITCHES = (
    ('auto_reply', '自动回复', '收到消息时自动回复这位好友。',
     lambda p: p.get('auto_reply', True), lambda p, on: p.update(auto_reply=on)),
    ('proactive', '定时主动', '按设定的时段与间隔，由模型自行判断要不要主动发消息；详细设置在「主动聊天」页。',
     lambda p: p.get('proactive', {}).get('enabled', False), _set_proactive),
    ('care', '时段关心', '每个时段每天最多一次的关心消息；时段和条数在「主动聊天」页设置。', _care_on, _set_care),
    ('research', '话题知识库', '每天整理已记录的话题资料，只在正常回复时作为背景知识使用。',
     lambda p: p.get('daily_research_enabled', True), lambda p, on: p.update(daily_research_enabled=on)),
)


def set_switch(path: Path, key: str, on: bool) -> str:
    """Saves one switch into friend.json; returns its title."""
    _, title, _, _, change = next(item for item in SWITCHES if item[0] == key)
    profile = load_profile(path)
    change(profile, on)
    write_profile(path, profile)
    return title


# ---------- the proactive / care settings forms (desktop panel and phone page) ----------
# 2026-10-11 user (Settings): the validation used to live in the desktop panel's ProactivePage; the phone page edits
# the same settings, so the rules moved here. The desktop page reads its widgets into plain strings and calls these;
# the phone page sends the same strings over HTTP. Anything written here reaches friend.json unchanged in meaning.

PROACTIVE_NUMBERS = {'rounds_min': '每天轮次最少', 'rounds_max': '每天轮次最多',
                     'bubbles_min': '每轮条数最少', 'bubbles_max': '每轮条数最多'}
PROACTIVE_TIMES = {'window_start': 'Start', 'window_end': '结束时间'}
PAGE_LIMITS = {'unanswered_round_limit': ('连续未回复主动轮数', 1000),
               'unanswered_round_pause_minutes': ('Pauseminutes', 10080)}
CARE_FIELDS = ('start', 'end', 'count_min', 'count', 'instruction')


def proactive_values(profile: dict) -> dict:
    """The settings shown in the form: a complete proactive schedule for this friend, defaults filled in."""
    from main import default_profile
    qq_id = str(profile.get('qq_id', ''))
    return {**default_profile(qq_id, '', True)['proactive'], **profile.get('proactive', {})}


def read_number(values: dict, key: str, label: str, low: int, high: int) -> int:
    """One whole number from the form; the phone sends JSON numbers, the desktop panel sends text."""
    value = values.get(key)
    if isinstance(value, bool) or value is None:
        raise ValueError(f'{label}请填写{low}至{high}的整数。')
    text = str(value).strip()
    if not text.isdecimal() or not low <= int(text) <= high:
        raise ValueError(f'{label}请填写{low}至{high}的整数。')
    return int(text)


def read_clock(values: dict, key: str, label: str) -> str:
    """One hours:minutes from the form, accepting full-width colon and surrounding spaces."""
    text = re.sub(r'\s+', '', str(values.get(key) or '')).replace('：', ':')
    if not re.fullmatch(r'\d{1,2}:\d{2}', text):
        raise ValueError(label + 'hours:minutes,  09:00. ')
    try:
        return datetime.strptime(text, '%H:%M').strftime('%H:%M')
    except ValueError:
        raise ValueError(label + ', hours 00–23, minutes 00–59. ') from None


def read_settings(profile: dict, values: dict) -> dict:
    """The proactive schedule (window, rounds, bubbles, topic types) from form values; raises ValueError on bad input."""
    from main import validate_proactive_settings
    schedule = proactive_values(profile)
    for key, label in PROACTIVE_NUMBERS.items():
        schedule[key] = read_number(values, key, label, 1, 1000)
    for key, label in PROACTIVE_TIMES.items():
        schedule[key] = read_clock(values, key, label)
    raw = values.get('topic_types') or []
    if isinstance(raw, str):
        raw = re.split(r'[,，、]', raw)
    schedule['topic_types'] = [str(part).strip() for part in raw if str(part).strip()]
    validate_proactive_settings(schedule)
    return schedule


def read_reply_pause(values: dict) -> int:
    return read_number(values, 'reply_pause_minutes', '未回复暂停时间', 1, 10080)


def read_round_pause(values: dict) -> dict:
    return {key: read_number(values, key, label, 1, maximum) for key, (label, maximum) in PAGE_LIMITS.items()}


def followup_values(profile: dict) -> dict:
    """2026-10-13 user: how many follow-ups after an unanswered reply, and the mood after being ignored repeatedly."""
    mood = profile.get('ignored_mood') or {}
    return {'continuation_max': profile.get('continuation_max', 1),
            'ignored_mood_enabled': bool(mood.get('enabled', False)), 'ignored_mood_after': mood.get('after', 2),
            'agenda_min_gap_minutes': profile.get('agenda_min_gap_minutes', 30)}


def read_followups(profile: dict, values: dict) -> dict:
    """The follow-up fields from the form; a form that does not send them keeps the saved values."""
    values = {**followup_values(profile), **{key: value for key, value in values.items() if value is not None}}
    return {'continuation_max': read_number(values, 'continuation_max', '对方不回时最多续话次数', 0, 10),
            'agenda_min_gap_minutes': read_number(values, 'agenda_min_gap_minutes', '待办最少间隔（分钟）', 10, 720),
            'ignored_mood': {'enabled': read_flag(values.get('ignored_mood_enabled')),
                             'after': read_number(values, 'ignored_mood_after', '连续被无视几次后有情绪', 1, 20)}}


def save_ignored_mood(path: Path, values: dict) -> dict:
    """2026-10-13 user (Emotion Log): only the ignored-mood switch and count."""
    profile = load_profile(path)
    profile['ignored_mood'] = read_followups(profile, values)['ignored_mood']
    write_profile(path, profile)
    return profile['ignored_mood']


def read_flag(value) -> bool:
    """An on/off value from a checkbox (bool), a switch word or the phone's text form."""
    if isinstance(value, bool) or value is None:
        return bool(value)
    text = str(value).strip().lower()
    if text in ('', '0', 'off', 'false', 'no', '关', '否', '不'):
        return False
    return True


def read_care_slot(key: str, slot: dict) -> dict:
    count_min = read_number(slot, 'count_min', '时段关心的最少条数', 1, 20)
    count = read_number(slot, 'count', '时段关心的最多条数', 1, 20)
    if count_min > count:
        raise ValueError('时段关心的最少条数不能大于最多条数。')
    return {'count': count, 'count_min': count_min, 'enabled': read_flag(slot.get('enabled')),
            'instruction': str(slot.get('instruction') or '').strip(),
            'start': read_clock(slot, 'start', '关心时段的开始'), 'end': read_clock(slot, 'end', '关心时段的结束')}


def read_care(profile: dict, slots: dict) -> dict:
    """The care slots from plain form values; whether care is on stays as set by the switch ()."""
    care = read_care_slots(slots, care_configuration(profile))
    return {**care, 'enabled': care_configuration(profile)['enabled']}


def read_care_slots(slots: dict, defaults: dict | None = None) -> dict:
    """Care slots from plain form values; every slot must be present (the form always shows all four)."""
    known = defaults or care_configuration({})
    care = {'enabled': known.get('enabled', True),
            'slots': {key: read_care_slot(key, {**known['slots'][key], **(slots.get(key) or {})})
                      for key in CARE_DEFAULTS}}
    care_validate(care)
    return care


def care_slot_values(profile: dict) -> dict:
    """The care slots as the form shows them (with defaults filled in)."""
    return care_configuration(profile)['slots']


def format_care_slot(key: str, slot: dict) -> str:
    return '{}={}|{}|{}|{}|{}|{}'.format(key, '开' if slot['enabled'] else '关', slot['start'], slot['end'],
                                         slot['count_min'], slot['count'], slot['instruction'])


def care_slot_text(profile: dict) -> str:
    """The care slots as one editable line each (the phone page's text form):
    key=/|Start||items|items|"""
    return '\n'.join(format_care_slot(key, slot) for key, slot in care_slot_values(profile).items())


def care_slots_from_text(text: str) -> dict:
    """Parse the phone page's care text; the line/key shape is checked here, the values by read_care_slots."""
    slots = {}
    for number, line in enumerate(str(text or '').splitlines(), 1):
        if not line.strip():
            continue
        key, _, body = line.partition('=')
        key = key.strip()
        if key not in CARE_DEFAULTS:
            raise ValueError(f'{number}' + ', '.join(CARE_DEFAULTS))
        if key in slots:
            raise ValueError(f'第{number}行的时段「{key}」写了两遍。')
        parts = [part.strip() for part in body.split('|')]
        if len(parts) != 6:
            raise ValueError(f'第{number}行应写成：{key}=开|开始|结束|最少条数|最多条数|关心指令')
        slots[key] = {'enabled': read_flag(parts[0]), 'start': parts[1], 'end': parts[2],
                      'count_min': parts[3], 'count': parts[4], 'instruction': parts[5]}
    return read_care_slots(slots)


def page_settings(profile: dict) -> dict:
    """Everything theproactivedayspage form shows, in the shape both the panel and the phone page submit."""
    schedule = proactive_values(profile)
    return {**{key: schedule.get(key) for key in PROACTIVE_NUMBERS},
            **{key: schedule.get(key) for key in PROACTIVE_TIMES},
            'topic_types': ', '.join(schedule.get('topic_types', [])),
            'reply_pause_minutes': profile.get('reply_pause_minutes', 60),
            'unanswered_round_limit': profile.get('unanswered_round_limit', 2),
            'unanswered_round_pause_minutes': profile.get('unanswered_round_pause_minutes', 300),
            **followup_values(profile),
            'care': care_slot_values(profile), 'care_text': care_slot_text(profile)}


def save_page_settings(path: Path, values: dict) -> dict:
    """One save for the whole proactive page (schedule, pauses and care slots); returns what was saved."""
    profile = load_profile(path)
    previous = dict(profile.get('proactive', {}))
    schedule = read_settings(profile, values)
    care = read_care(profile, values.get('care') or {})
    reply_pause = read_reply_pause(values)
    rounds = read_round_pause(values)
    followups = read_followups(profile, values)
    raw_bubbles = values.get('reply_bubbles_max')
    if raw_bubbles is None or str(raw_bubbles).strip() == '':
        reply_bubbles = max(1, min(8, int(profile.get('reply_bubbles_max', 4) or 4)))
    else:
        reply_bubbles = read_number(values, 'reply_bubbles_max', '普通回复最多气泡数', 1, 8)
    profile['reply_bubbles_max'] = reply_bubbles
    profile['proactive'] = {**schedule, 'time_aware': False}
    profile['reply_pause_minutes'] = reply_pause
    profile.update(rounds, **followups)
    profile['scheduled_care'] = care
    write_profile(path, profile)
    return {'schedule': schedule, 'care': care, 'reply_pause_minutes': reply_pause, 'reply_bubbles_max': reply_bubbles, **rounds, **followups,
            'changed': any(previous.get(key) != schedule[key]
                           for key in ('rounds_min', 'rounds_max', 'window_start', 'window_end'))}


def preview_proactive(qq_id: str, profile: dict | None = None) -> list[str]:
    from main import make_round, validate_proactive_settings
    from app_server import SERVER
    import random

    friend_dir = ROOT / "friends" / qq_id
    if profile is None:
        profile = load_profile(friend_dir / "friend.json")
    settings = profile.get("proactive", {})
    validate_proactive_settings(settings)
    with tempfile.TemporaryDirectory(prefix="qq-preview-") as folder:
        source = FriendStore(friend_dir)
        preview = FriendStore(Path(folder))
        try:
            source.db.backup(preview.db)
            preview.set("thread_id", "")
            count = random.randint(int(settings["bubbles_min"]), int(settings["bubbles_max"]))
            return make_round(preview, profile, friend_dir, count)
        finally:
            preview.close()
            source.close()
            SERVER.close()


def delete_friend(qq: str, *, root: Path | None = None) -> str:
    """Remove one friend from the registry and delete their local folder.

    The directory is renamed first so a hidden/leftover folder can never keep a
    stale registry entry.  If Windows still holds a database handle, the rename
    or final delete reports a clear error instead of silently doing nothing.
    """
    qq = str(qq or "").strip()
    if not re.fullmatch(r"\d{5,12}", qq):
        raise ValueError("QQ 号无效")
    base = Path(root or ROOT)
    friends = (base / "friends").resolve()
    folder = (friends / qq).resolve()
    if folder.parent != friends:
        raise ValueError("好友目录越界，已拒绝删除")
    registry_path = base / "friends.json"
    try:
        registry = json.loads(registry_path.read_text("utf-8")) if registry_path.exists() else {"friends": {}}
    except (OSError, ValueError) as exc:
        raise RuntimeError("friends.json 读取失败：" + str(exc)) from None
    records = registry.setdefault("friends", {})
    if not isinstance(records, dict) or qq not in records:
        raise KeyError("好友不存在或已经删除")
    records.pop(qq, None)
    temporary = registry_path.with_name(registry_path.name + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, registry_path)
    if not folder.is_dir():
        return "已从好友列表移除；本地目录原本不存在"
    trash = base / "friends" / (".deleted-" + qq + "-" + str(int(__import__("time").time())))
    try:
        folder.rename(trash)
    except OSError as exc:
        return "已从好友列表移除；本地目录仍被占用，重启后可手动删除：" + str(exc)
    try:
        import shutil
        shutil.rmtree(trash)
        return "好友及其本地数据已删除"
    except OSError as exc:
        return "已从好友列表移除；本地目录暂留，重启后可清理：" + str(exc)


def recent_activity(directory, limit: int = 50, *, compact: bool = False) -> str:
    """The friend-scoped activity tail used by the Qt status page and its tests."""
    path = Path(directory) / 'activity.log'
    from collections import deque
    if not path.exists():
        return '这位好友暂无小动态'
    with path.open(encoding='utf-8', errors='replace') as stream:
        lines = [line.rstrip('\r\n') for line in deque(stream, maxlen=5000 if compact else max(1, int(limit)))]
    if not compact:
        return '\n'.join(reversed(lines)) or '这位好友暂无小动态'
    groups = []
    for line in lines:
        stamp, separator, body = line.partition('] ')
        stamp = stamp.lstrip('[') if separator else ''
        body = body if separator else line
        if groups and groups[-1]['body'] == body:
            groups[-1].update(last=stamp, line=line, count=groups[-1]['count']+1)
        else:
            groups.append(dict(body=body, first=stamp, last=stamp, line=line, count=1))
    rendered = []
    for group in reversed(groups[-max(1, int(limit)):]):
        text = group['line'].replace('database is locked', '数据库暂被占用').replace(
            'source snapshots require stable DELETE journals', '源资料数据库日志模式不兼容，需要检查会话存储')
        if group['count'] > 1:
            text += f"\n    连续出现 {group['count']} 次（首次：{group['first']}；最近：{group['last']}）"
        rendered.append(text)
    return '\n'.join(rendered) or '这位好友暂无小动态'


def effective_house_status(profile: dict, store: FriendStore, bridge_running: bool) -> str:
    now = datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    pause_until = store.refresh_reply_pause(profile.get('reply_pause_minutes', 60), now)
    wait_label = '等待好友回复，暂停至 ' + pause_until.strftime('%m-%d %H:%M') if pause_until else ''
    round_until = store.refresh_round_pause(profile.get('unanswered_round_limit', 2), profile.get('unanswered_round_pause_minutes', 300), now)
    if round_until:
        pause_until = max(pause_until, round_until) if pause_until else round_until
        wait_label = '未回复／轮数限制，暂停至 ' + pause_until.strftime('%m-%d %H:%M')
    settings = profile.get('proactive', {})
    blocker = '桥接未启动' if not bridge_running else ('好友已暂停' if not profile.get('bridge_enabled', True) else '')
    auto = '关闭' if not profile.get('auto_reply', True) else ('暂停（' + blocker + '）' if blocker else '开启')
    if blocker:
        proactive = '暂停（' + blocker + '）'
    elif not settings.get('enabled', False):
        proactive = '关闭'
    elif pause_until and not settings.get('smart_timing', True):
        proactive = wait_label
    elif store.get('paused_reason'):
        proactive = '暂停（' + store.get('paused_reason') + '）'
    elif settings.get('smart_timing', True):
        proactive = '开启（模型自己排待办）'
    else:
        eligible = store.get('eligible_after')
        if window_start(now, settings) is None:
            proactive = '等待发送时段'
        elif eligible and now < datetime.fromisoformat(eligible):
            proactive = '冷却至 ' + datetime.fromisoformat(eligible).strftime('%H:%M')
        else:
            proactive = '开启（模型判断时机）' if settings.get('smart_timing', True) else '开启（按计划发送）'
    from scheduled_care import configuration, bounds, DEFAULTS
    care = configuration(profile)
    smart = settings.get('smart_timing', True)
    care_state = '关闭' if not care['enabled'] else ('暂停（' + blocker + '）' if blocker else
                 '开启（模型自主规划）' if smart else
                 wait_label if pause_until else
                 '暂停（' + store.get('paused_reason') + '）' if store.get('paused_reason') else '开启（独立时段）')
    if round_until and not blocker and not smart:
        if settings.get('enabled'):
            proactive += ' · 暂停结束后重新判断'
        if care['enabled']:
            care_state += ' · 暂停结束后重新检查关心时段'
    if smart and (settings.get('enabled') or care['enabled']) and not blocker and not store.get('paused_reason'):
        import agenda
        proactive += ' · ' + agenda.next_label(store, now)
    elif settings.get('enabled') and not round_until and not smart:
        start = window_start(now, settings)
        if start:
            day = start.date().isoformat()
            raw = store.get('plan:' + day)
            eligible = datetime.fromisoformat(store.get('eligible_after')) if store.get('eligible_after') else now
            pending = [datetime.fromisoformat(value) for index, value in enumerate(json.loads(raw or '[]'))
                       if store.get(f'slot:{day}:{index}') is None
                       and max(now, eligible) <= datetime.fromisoformat(value) + SLOT_GRACE]
            next_label = ('下次计划 ' + min(pending).strftime('%m-%d %H:%M:%S') if pending else
                          '本时段无剩余计划' if raw else '等待后台生成计划')
        else:
            hour, minute = map(int, settings.get('window_start', '10:00').split(':'))
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
            next_label = '下一时段 ' + start.strftime('%m-%d %H:%M') + ('（再由模型判断）' if settings.get('smart_timing', True) else '（待生成计划）')
        proactive += ' · ' + next_label
    if care['enabled'] and not round_until and not smart:
        upcoming = []
        for name, slot in care['slots'].items():
            if not slot['enabled']:
                continue
            start, end = bounds(now, slot)
            key = f'{start.date().isoformat()}:{name}'
            state = store.get('care:' + key)
            plan = json.loads(store.get('care_plan:' + key) or '{}')
            at = (datetime.fromisoformat(plan['at']) if plan and plan.get('start') == start.isoformat()
                  and plan.get('end') == end.isoformat() else None)
            if not state and at and now < end and now <= at + SLOT_GRACE:
                upcoming.append((at, '下次计划 ' + at.strftime('%m-%d %H:%M:%S') + '（' + DEFAULTS[name][0] + '）'))
            else:
                if state or now >= end or (at and now > at + SLOT_GRACE):
                    start += timedelta(days=1)
                upcoming.append((max(start, now), '下次时段 ' + start.strftime('%m-%d %H:%M')
                                 + '（' + DEFAULTS[name][0] + '，待抽取）'))
        care_state += ' · ' + (min(upcoming)[1] if upcoming else '没有开启的关心时段')
    return f'自动回复：{auto}\n定时主动：{proactive}\n时段关心：{care_state}'


def proactive_debug_text(qq_id: str, bridge_running: bool) -> str:
    profile = load_profile(ROOT / "friends" / qq_id / "friend.json")
    settings = profile.get("proactive", {})
    now = datetime.now().astimezone()
    store = FriendStore(ROOT / "friends" / qq_id)
    try:
        try:
            active_start = window_start(now, settings)
        except (ValueError, TypeError):
            active_start = None
        plan_day = active_start.date().isoformat() if active_start else None
        if not plan_day:
            row = store.db.execute(
                "SELECT key FROM metadata WHERE key LIKE 'plan:%' ORDER BY key DESC LIMIT 1"
            ).fetchone()
            plan_day = row["key"][5:] if row else None
        raw = store.get(f"plan:{plan_day}") if plan_day else None
        slots = json.loads(raw) if raw else []
        smart = settings.get('smart_timing', True)
        if smart:
            slots = []
        eligible_raw = store.get("eligible_after")
        eligible = datetime.fromisoformat(eligible_raw) if eligible_raw else None
        remaining = []
        lines = [f"当前时间  {now:%Y-%m-%d %H:%M:%S}"]
        end_label = "次日 " if settings.get("window_end", "") <= settings.get("window_start", "") else ""
        lines.append(f"发送时段  {settings.get('window_start', '?')} – {end_label}{settings.get('window_end', '?')}")
        lines.append(
            f"每日 {settings.get('rounds_min', '?')}–{settings.get('rounds_max', '?')} 轮，"
            f"每轮 {settings.get('bubbles_min', '?')}–{settings.get('bubbles_max', '?')} 条"
        )
        if smart:
            # 2026-10-13 user: proactive chat and care are the model's own todos (agenda.py)
            import agenda
            lines.append(f"主动时机  模型自己排待办（最多{agenda.MAX_TODOS}个，至少相隔"
                         f"{profile.get('agenda_min_gap_minutes', 30)}分钟；每天 {settings.get('rounds_min', '?')}–{settings.get('rounds_max', '?')} 轮）；"
                         "到点先看一眼手机再决定发不发；发送时段和未回复暂停不再限制它")
            decision = json.loads(store.get('smart_proactive', '{}'))
            lines.append('最近判断  ' + store.get('smart_status', '尚未判断'))
            if decision:
                lines.append('判断理由  ' + decision.get('reason', ''))
            lines.append('待办清单  ' + ('' if agenda.todos(store) else '还没有（等模型第一次排）'))
            lines.extend(agenda.todo_lines(store))
        from scheduler import automatic_cooldown, automatic_cooldown_range
        minimum, maximum = automatic_cooldown_range(store, settings, now)
        picked = (f"；本次抽取约 {int(automatic_cooldown(store, settings, now) / 60 + 0.5)} 分钟"
                  if store.get('cooldown_kind') == 'automatic' and store.get('cooldown_draw') is not None else '')
        lines.append(f"自动冷却范围  约 {int(minimum / 60 + 0.5)}–{int(maximum / 60 + 0.5)} 分钟"
                     f"（每次随机抽取并保存{picked}）")
        pause_until = store.refresh_reply_pause(profile.get('reply_pause_minutes', 60), now)
        lines.append(f"未回复暂停  {profile.get('reply_pause_minutes', 60)} 分钟" +
                     ('，至 ' + pause_until.strftime('%m-%d %H:%M') if pause_until else '，当前未等待'))
        round_until = store.refresh_round_pause(profile.get('unanswered_round_limit', 2), profile.get('unanswered_round_pause_minutes', 300), now)
        lines.append(f"连续未回复主动  {store.get('active_unanswered_rounds', '0')} / {profile.get('unanswered_round_limit', 2)} 轮，"
                     f"达到后暂停 {profile.get('unanswered_round_pause_minutes', 300)} 分钟" +
                     ('，至 ' + round_until.strftime('%m-%d %H:%M') if round_until else ''))
        if round_until:
            pause_until = max(pause_until, round_until) if pause_until else round_until
        # 2026-10-10 user (): proactive / continuation / care share one random 20–60 min gap (main.unsolicited_wait)
        gap = json.loads(store.get('unsolicited_gap') or '{}')
        gap_until = (datetime.fromisoformat(gap['until']) if gap.get('until')
                     and gap.get('generation') == store.get('inbound_generation', '0') else None)
        gap_until = gap_until if gap_until and gap_until > now else None
        lines.append('主动间隔  主动聊天、续话、时段关心共用：发出后随机 20–60 分钟，对方来信即解除'
                     + ('，至 ' + gap_until.strftime('%m-%d %H:%M') if gap_until else '，当前无间隔'))
        # 6stage: one heartbeat per friend; the continuation judgment is shown here too
        lines.append('心跳顺序  每位好友每轮最多做一次判断：续话 > 自排待办' if smart else
                     '心跳顺序  每位好友每轮最多做一次判断：续话 > 主动轮次')
        continuation = json.loads(store.get('chat_continuation') or '{}')
        state = continuation.get('state')
        label = {'pending': '等待判断', 'waiting': '已判断，等下次', 'ended': '这次续话已结束', 'processing': '发送中',
                 'failed': '上次判断失败', 'cancelled': '已取消'}.get(state, '没有进行中的续话')
        if state in ('pending', 'waiting') and continuation.get('next_check_at'):
            label += ' · 下次判断 ' + datetime.fromisoformat(continuation['next_check_at']).strftime('%m-%d %H:%M')
        if continuation.get('action'):
            label += f" · 上次：{continuation['action']}（{continuation.get('reason', '')[:60]}）"
        limit = int(profile.get('continuation_max', 1))
        sent_count = int(continuation.get('sent_count', 0))
        label += f" · 已续 {sent_count}/{limit} 次"
        if limit <= 0:
            label += "（已关闭续话）"
        lines.append('续话判断  ' + label)
        from scheduled_care import configuration, DEFAULTS
        care = configuration(profile)
        lines.append("时段关心  " + ("开启（只作为模型排待办的参考）" if care["enabled"] else "关闭"))
        for key, slot in care["slots"].items():
            if slot["enabled"] and not smart:
                from scheduled_care import bounds
                start, _ = bounds(now, slot)
                day = start.date().isoformat()
                state = store.get(f"care:{day}:{key}", "等待时段")
                plan = json.loads(store.get(f"care_plan:{day}:{key}") or '{}')
                picked = datetime.fromisoformat(plan['at']).strftime('%m-%d %H:%M:%S') if plan else '尚未抽取'
                label = {"sent": "已发送", "skipped": "模型决定跳过", "missed": "已错过", "failed": "失败", "uncertain": "发送结果未确认", "cancelled": "已取消", "in_progress": "处理中", "sending": "发送中"}.get(state, state)
                lines.append(f"  {DEFAULTS[key][0]}  {slot['start']}–{slot['end']}  抽取：{picked}  {label}（{slot['count']}条）")
        lines.append(f"独立测试已完成  {store.get('proactive_test_rounds', '0')} 轮")
        test_raw = store.get("proactive_test")
        if test_raw:
            test = json.loads(test_raw)
            test_state = {"pending": "等待桥接处理", "running": "发送中", "sent": "已完成",
                          "blocked": "未执行", "failed": "失败", "cancelled": "已取消",
                          "missed": "时段结束", "interrupted": "已中断"}.get(test.get("state"), test.get("state", "未知"))
            lines.append(f"最近测试  {test_state} · {test.get('detail', '')}")
        if eligible:
            lines.append(f"冷却至  {eligible:%m-%d %H:%M:%S}")
        for index, slot_raw in enumerate(slots):
            slot = datetime.fromisoformat(slot_raw)
            state = store.get(f"slot:{plan_day}:{index}")
            if state is None:
                remaining.append(slot)
            label = {"sent": "已完成", "missed": "已错过", "failed": "失败",
                     "cancelled": "已取消", "in_progress": "发送中", "rescheduled": "已替换"}.get(state, "待发送")
            if state is None and now > slot + SLOT_GRACE:
                label = "已过宽限，待桥接标记"
            lines.append(f"  {index + 1:02d}  {slot:%m-%d %H:%M}    {label}")
        if not bridge_running:
            reason = "桥接未启动"
        elif not profile.get("bridge_enabled", True):
            reason = "好友已暂停"
        elif not settings.get("enabled") and not (smart and care["enabled"]):
            reason = "主动功能已关闭"
        elif store.get("bridge_error"):
            reason = store.get("bridge_error")
        elif store.get("proactive_config_error"):
            reason = store.get("proactive_config_error")
        elif store.get("paused_reason"):
            reason = store.get("paused_reason")
        elif smart:
            reason = store.get('smart_status', '等待模型排待办') + ' · ' + agenda.next_label(store, now)
        elif pause_until:
            reason = '等待好友回复，暂停至 ' + pause_until.strftime('%m-%d %H:%M')
        elif store.get("paused_reason"):
            reason = store.get("paused_reason")
        elif active_start is None:
            reason = "当前不在发送时段"
        elif gap_until:
            reason = f"等待主动间隔结束（{gap_until:%H:%M}）"
        elif eligible and now < eligible:
            reason = f"等待冷却结束（{eligible:%H:%M}）"
        elif smart:
            reason = store.get('smart_status', '等待模型判断是否适合联系')
        elif not slots:
            reason = "等待桥接生成本时段计划"
        elif not remaining:
            reason = "本时段没有剩余轮次"
        elif eligible and now < eligible:
            reason = f"等待冷却结束（{eligible:%H:%M}）"
        else:
            reason = f"下一轮计划 {remaining[0]:%m-%d %H:%M}"
        lines.insert(1, "当前状态  " + reason)
        replan = json.loads(store.get('replan_request') or '{}')
        if replan:
            lines.append('最近重排  ' + ('等待桥接处理' if replan.get('state') == 'pending' else replan.get('detail', '')))
        section = 6 + bool(test_raw) + bool(eligible)
        lines.insert(section, "")
        lines.insert(section + 1, '主动时机由模型安排，不使用固定轮次' if smart else f"计划轮次  {plan_day or '尚未生成'}")
        if not slots:
            if not smart:
                lines.append("  尚无已生成的轮次")
    finally:
        store.close()
    log_path = ROOT / "friends" / qq_id / "activity.log"
    recent = []
    if log_path.exists():
        recent = [line for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                  if "proactive" in line][-12:]
    lines.extend(["", "主动消息记录", *(recent or ["  暂无记录"])])
    return "\n".join(lines)


def friend_mode_values(qq_id, directory, running):
    """File and database reads behind the status card (~0.15 s); safe off the UI thread."""
    profile = load_profile(directory / "friend.json")
    store = FriendStore(directory)
    try:
        error = store.get("bridge_error", "")
        summary = effective_house_status(profile, store, running)
        rows = store.reply_problems()
    finally:
        store.close()
    paused = not profile.get("bridge_enabled", True)
    state = "paused" if paused else "等待启动" if not running else "needs attention" if error else "running"
    # 2026-10-26: an unset window printed ?–次日 ? on the phone page; show the effective default instead.
    proactive = profile.get('proactive', {}) or {}
    start = proactive.get('window_start') or '10:00'
    end = proactive.get('window_end') or '22:00'
    end_label = '次日 ' if end <= start else ''
    return {"qq": qq_id, "state": state, "paused": paused, "rows": rows,
            "summary": summary + f'\n已保存时段：{start}–{end_label}{end}'}
