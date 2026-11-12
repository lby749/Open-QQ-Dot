"""Friend-wide persisted Codex usage, including ephemeral CLI calls."""
import importlib.util
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path



def normalize_usage(usage, tier):
    """Canonical input includes cache reads/writes; old Claude CLI rows exclude both."""
    if not isinstance(usage, dict):
        return usage
    usage = dict(usage)
    if tier == 'claude' and not usage.get('input_tokens_include_cache'):
        usage['input_tokens'] = sum(int(usage.get(key, 0) or 0) for key in
            ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens'))
        usage['input_tokens_include_cache'] = True
    return usage


def record(directory, model, tier, usage):
    if not directory or not Path(directory).is_dir():
        return
    with closing(sqlite3.connect(Path(directory) / 'history.sqlite3', timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS usage_calls '
                   '(seq INTEGER PRIMARY KEY, created_at TEXT, model TEXT, tier TEXT, usage TEXT)')
        db.execute('INSERT INTO usage_calls(created_at,model,tier,usage) VALUES(?,?,?,?)',
                   (datetime.now().astimezone().isoformat(), model, tier, json.dumps(normalize_usage(usage, tier))))
        db.commit()


@lru_cache(maxsize=1)
def plugin():
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    scripts = list((home / 'plugins/cache/local-thread-usage/codex-thread-usage').glob(
        '*/skills/thread-usage/scripts/usage.py'))
    if not scripts:
        raise ValueError('尚未安装 Codex 用量核查插件。')
    source = max(scripts, key=lambda path: path.stat().st_mtime_ns)
    spec = importlib.util.spec_from_file_location('qq_usage_plugin', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=128)
def _quota_tail(path, modified, size):
    """Account quota is shared; inspecting it must not parse another friend's full conversation."""
    with path.open('rb') as stream:
        offset = max(0, size - 128 * 1024)
        stream.seek(offset)
        lines = stream.read(128 * 1024).splitlines()
    if offset:
        lines = lines[1:]  # the first line may start in the middle of a record
    for line in reversed(lines):
        if b'"token_count"' not in line:
            continue
        try:
            item = json.loads(line)
            payload = item.get('payload') or {}
            limits = payload.get('rate_limits') or {}
            if (item.get('type') == 'event_msg' and payload.get('type') == 'token_count'
                    and isinstance(limits, dict) and limits.get('limit_id', 'codex') == 'codex'
                    and isinstance(item.get('timestamp'), str)):
                return {'time': item['timestamp'], 'limits': limits}
        except (ValueError, AttributeError):
            continue
    return None


def summary(directory):
    try:
        reader = plugin()
    except Exception:
        reader = None
    directory = Path(directory).resolve()
    token_fields = reader.TOKEN_FIELDS if reader else ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_input_tokens')
    tokens = dict.fromkeys(token_fields, 0)
    costs, sessions, latest = [], 0, None
    if reader:
        try:
            paths = [path for root in (reader.HOME / 'sessions', reader.HOME / 'archived_sessions')
                     if root.exists() for path in root.rglob('*.jsonl')]
            seen = set()
            for path in sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True):
                try:
                    with path.open(encoding='utf-8') as stream:
                        meta = json.loads(stream.readline(16384)).get('payload', {})
                    if meta.get('id') in seen:
                        continue
                    if meta.get('id'):
                        seen.add(meta['id'])
                    owned = bool(meta.get('cwd')) and Path(meta['cwd']).resolve() == directory
                    stat = path.stat()
                    if not owned:
                        if stat.st_mtime >= time.time() - 86400:
                            event = _quota_tail(path, stat.st_mtime_ns, stat.st_size)
                            if event and (latest is None or event['time'] > latest['time']):
                                latest = event
                        continue
                    data = reader.read_log(path)
                except (OSError, ValueError):
                    continue
                events = data['events']
                if events and (latest is None or events[-1]['time'] > latest['time']):
                    latest = events[-1]
                if owned:
                    sessions += 1
                    for field in tokens:
                        tokens[field] += data['total'].get(field, 0)
                    costs.append(data['billing'])
        except Exception:
            pass
    rows = []
    if (directory / 'history.sqlite3').exists():
        with closing(sqlite3.connect(directory / 'history.sqlite3', timeout=10)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_calls'").fetchone()
            rows = db.execute('SELECT created_at,model,tier,usage FROM usage_calls ORDER BY seq').fetchall() if exists else []
    calls, missing = [], 0
    model_counts = {}
    for _, model, tier, body in rows:
        try:
            usage = normalize_usage(json.loads(body), tier) if body else None
        except Exception:
            usage = None
        if not isinstance(usage, dict):
            missing += 1
            continue
        try:
            normalized = {field: int(usage.get(field, 0) or 0) for field in tokens}
            if min(normalized.values(), default=0) < 0 or normalized.get('cached_input_tokens', 0) > normalized.get('input_tokens', 0):
                raise ValueError('inconsistent token counts')
        except (ValueError, TypeError):
            missing += 1
            continue
        for field in tokens:
            tokens[field] += normalized[field]
        calls.append((model, tier, normalized, normalized.get('input_tokens', 0)))
        model_counts[model] = model_counts.get(model, 0) + 1
    if calls and reader:
        try:
            costs.append(reader.billing_summary(calls))
        except Exception:
            pass
    unknown = missing + sum(cost.get('unknown_records', 0) for cost in costs)
    subtotal = sum(cost.get('known_subtotal_usd', 0) for cost in costs)
    windows = reader.window_points([latest], 300) if (reader and latest) else []
    account = windows[-1] if windows else None
    expired = account and isinstance(account.get('reset'), (int, float)) and account['reset'] <= time.time()
    return {'tokens': tokens, 'total_usd': subtotal if costs and not unknown and subtotal > 0 else None,
            'known_subtotal_usd': subtotal, 'unknown_records': unknown,
            'models': [model for cost in costs for model in cost.get('models', [])],
            'model_counts': model_counts,
            'sessions': sessions, 'temporary_calls': len(rows), 'recorded_since': rows[0][0] if rows else None,
            'account_remaining': 100 - account['used'] if account and not expired and 'used' in account else None,
            'account_reset': account.get('reset') if account else None,
            'observed_at': latest['time'] if latest else None}


def display(data):
    tokens = data['tokens']
    remaining = data['account_remaining']
    lines = [f"输入：{tokens['input_tokens']:,} token", f"输出：{tokens['output_tokens']:,} token",
             f"缓存命中：{tokens['cached_input_tokens']:,} token（已包含在输入中）",
             '缓存占比：' + (f"{tokens['cached_input_tokens'] / tokens['input_tokens']:.1%}" if tokens['input_tokens'] else '暂无输入'),
             f"缓存写入：{tokens['cache_write_input_tokens']:,} token",
             f"普通会话：{data['sessions']}；已记录临时调用：{data['temporary_calls']}", '',
             'API 参考费用（美元，不代表订阅扣款）',
             f"合计：${data['total_usd']:.6f}" if data['total_usd'] is not None else
             f"无法完整计价；已知部分：${data['known_subtotal_usd']:.6f}，未知记录：{data['unknown_records']}"]
    for model in data['models']:
        rates = model['rates']
        counts = model['tokens']
        lines.append(f"{model['model']} 输入 {counts['input_tokens']:,} / 输出 {counts['output_tokens']:,} / 缓存命中 {counts['cached_input_tokens']:,}；"
                     + ('费用' if model['complete'] else '已知部分费用') + f" ${model['total_usd']:.6f}")
        lines.append(f"{model['model']} / {model['tier']} / " +
                     ('输入、缓存命中、缓存写入、输出单价（$/百万token）：' + ', '.join(map(str, rates)) if rates else '暂无可核实单价'))
    if not data['models'] and data.get('model_counts'):
        lines.append('已记录各模型调用次数：')
        for model_name, count in data['model_counts'].items():
            lines.append(f"• {model_name}: {count} 次调用")
    if remaining is not None or data.get('account_reset') is not None:
        lines += ['', 'Codex 账号共享 5 小时额度：' + (f'剩余 {remaining:.1f}%' if remaining is not None else '等待有效快照'),
                  '额度重置时间：' + (datetime.fromtimestamp(data['account_reset']).astimezone().isoformat(timespec='minutes')
                                      if isinstance(data['account_reset'], (int, float)) else 'none'),
                  '快照时间：' + (data['observed_at'] or 'none'),
                  '额度属于整个账号，不能准确分摊到当前好友。']
    lines += ['',
              '临时调用记录起点：' + (data['recorded_since'] or '接入后首次调用开始记录'),
              '包含主动、续话、记忆、摘要、兴趣提取和资料储备。接入前未保存的临时调用无法补算。',
              '支持 Codex CLI、Claude Code、Antigravity 与各类 API 的本地用量统计；工具费用不在模型 token 参考费用中。']
    return '\n'.join(lines)


def cache_summary(directory, limit: int = 100) -> dict:
    """Actual provider-reported prompt-cache usage for one friend, not estimates.

    Older records and providers without usage are excluded, rather than being
    presented as misses. Totals include cache reads exactly once.
    """
    directory = Path(directory).resolve()
    if not 1 <= limit <= 10000:
        raise ValueError('limit must be 1..10000')
    path = directory / 'history.sqlite3'
    data = {'calls': 0, 'excluded': 0, 'input_tokens': 0,
            'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
            'by_model': {}}
    if not path.is_file():
        data['hit_rate'] = None
        return data
    with closing(sqlite3.connect(path, timeout=10)) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_calls'").fetchone()
        if table is None:
            data['hit_rate'] = None
            return data
        rows = db.execute('SELECT model,tier,usage FROM usage_calls WHERE tier=? ORDER BY seq DESC LIMIT ?',
                          ('api', limit)).fetchall()
    for model, tier, raw in rows:
        try:
            usage = json.loads(raw)
            if not isinstance(usage, dict) or usage.get('usage_source') != 'provider':
                raise ValueError('not verified provider usage')
            total = int(usage['input_tokens'])
            hits = int(usage['cached_input_tokens'])
            writes = int(usage.get('cache_write_input_tokens', 0))
            if not (0 <= hits <= total and 0 <= writes <= total and hits + writes <= total):
                raise ValueError('inconsistent provider usage')
        except (TypeError, ValueError, KeyError):
            data['excluded'] += 1
            continue
        data['calls'] += 1
        data['input_tokens'] += total
        data['cached_input_tokens'] += hits
        data['cache_write_input_tokens'] += writes
        bucket = data['by_model'].setdefault(model, {'calls': 0, 'input_tokens': 0, 'cached_input_tokens': 0})
        bucket['calls'] += 1
        bucket['input_tokens'] += total
        bucket['cached_input_tokens'] += hits
    data['hit_rate'] = (data['cached_input_tokens'] / data['input_tokens']) if data['input_tokens'] else None
    return data
