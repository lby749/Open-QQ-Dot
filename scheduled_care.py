"""Per-friend care slots. 2026-10-13 user: they are only suggestions for the model's own todos (agenda.care_ideas);
bounds/ready remain for care rounds recorded before that."""
import json
import random
from datetime import datetime, timedelta

DEFAULTS = {'morning': ('早安', ('06:00', '10:00'), '简短自然地问好，结合聊天，不假装知道他刚起床。'),
            'lunch': ('午饭', ('11:00', '14:00'), '可轻松聊午饭、吃了什么或吃了没；已聊过就不重复问。'),
            'evening': ('一天近况', ('21:00', '22:00'), '可关心今天过得怎么样；只有语境适合才问有没有不开心的事。'),
            'night': ('晚安', ('23:00', '02:00'), '自然收尾，适合时道晚安，不催对方睡觉或要求回应。')}


def configuration(profile):
    settings = profile.get('scheduled_care') or {}
    slots = settings.get('slots') or {}
    result = {'enabled': settings.get('enabled', profile.get('proactive', {}).get('time_aware', False)), 'slots': {}}
    for key, default in DEFAULTS.items():
        previous = slots.get(key, {})
        start, end = default[1]
        if previous.get('time') and 'start' not in previous:
            point = datetime.strptime(previous['time'], '%H:%M')
            start = (point - timedelta(minutes=30)).strftime('%H:%M')
            end = (point + timedelta(minutes=30)).strftime('%H:%M')
        result['slots'][key] = {'enabled': previous.get('enabled', True),
                               'start': previous.get('start', start), 'end': previous.get('end', end),
                               'count': previous.get('count', 1),  # the most bubbles
                               'count_min': previous.get('count_min', 1),
                               'instruction': previous.get('instruction', default[2])}
    return result


def validate(settings):
    for slot in settings['slots'].values():
        if type(slot['count']) is not int or slot['count'] < 1:
            raise ValueError('每个关心时段的发送条数须为正整数。')
        if type(slot['count_min']) is not int or not 1 <= slot['count_min'] <= slot['count']:
            raise ValueError('每个关心时段的最少条数须为正整数，且不大于最多条数。')
        datetime.strptime(slot['start'], '%H:%M')
        datetime.strptime(slot['end'], '%H:%M')
        if slot['start'] == slot['end']:
            raise ValueError('关心时段的开始和结束不能相同。')


def bounds(now, slot):
    start_time = datetime.strptime(slot['start'], '%H:%M').time()
    end_time = datetime.strptime(slot['end'], '%H:%M').time()
    start = now.replace(hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0)
    end = now.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
        if now.time().replace(tzinfo=None) < end_time:
            start -= timedelta(days=1)
            end -= timedelta(days=1)
    return start, end


def ready(store, key, now, slot):
    raw = store.get('care_plan:' + key[5:])
    if not raw:
        return False
    plan = json.loads(raw)
    start, end = bounds(now, slot)
    at = datetime.fromisoformat(plan['at'])
    return (plan['start'] == start.isoformat() and plan['end'] == end.isoformat()
            and at <= now <= at + timedelta(minutes=10) and now < end)
