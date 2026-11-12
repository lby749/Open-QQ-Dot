"""Evidence-based reply-time observations (the timing judgment itself is now the model's own todos, agenda.py)."""
from collections import defaultdict
from datetime import datetime, timedelta

from source_state import snapshot as source_snapshot, require_snapshot


def update_activity_memory(store, now):
    source_token = source_snapshot(store.path.parent)
    rows = [dict(row) for row in store.db.execute(
        "SELECT qq_id,created_at FROM messages WHERE direction='friend' AND seq>? ORDER BY seq DESC LIMIT 3000",
        (int(store.get('activity_memory_reset_seq', '0')),))]
    days = set()
    periods = defaultdict(set)
    sources = defaultdict(list)
    for row in rows:
        at = datetime.fromisoformat(row['created_at']).astimezone(now.tzinfo)
        if not now - timedelta(days=28) <= at <= now:
            continue
        day = at.date().isoformat()
        days.add(day)
        period = at.hour // 2 * 2
        periods[period].add(day)
        sources[period].append(row['qq_id'])
    if len(days) < 5:
        return
    reliable = sorted((period for period in periods if len(periods[period]) >= 3
                       and len(periods[period]) / len(days) >= 0.6), key=lambda period: -len(periods[period]))[:2]
    if not reliable:
        return
    from memory_management import entries, insert, invalidate, snapshot, sync
    text = '[回复时段观察] ' + '; '.join(
        f'近28天有来信的{len(days)}天中，{len(periods[period])}天在{period:02d}:00–{period + 2:02d}:00收到来信'
        for period in reliable) + f'（本机时区{now:%z}，截至{now:%Y-%m-%d}；仅是活跃线索，不代表真实上线或固定作息）'
    old = next((row for row in entries(store) if row['active'] and row['text'].startswith('[回复时段观察] ')), None)
    if old and (old['locked'] or old['text'] == text):
        return
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        require_snapshot(store.path.parent, source_token)
        snapshot(store, '更新有证据的回复时段观察')
        new_id = insert(store, text, 'observation', ['回复时间', '活跃时段', '作息'],
                        list(dict.fromkeys(source for period in reliable for source in sources[period])), False, '作息')
        if old:
            invalidate(store, old['id'], new_id)
        sync(store)

