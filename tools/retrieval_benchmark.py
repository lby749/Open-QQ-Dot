"""Reproducible retrieval benchmark for every module that retrieves (2026-10-26).

    python tools/retrieval_benchmark.py --label before --mode real      # local BGE-M3 + bge-reranker-v2-m3
    python tools/retrieval_benchmark.py --label before --mode lexical   # local models switched off (degraded)
    python tools/retrieval_benchmark.py --label after --mode api --api-key-file KEY   # an API model for memory/topics
    python tools/retrieval_benchmark.py --label before --mode real --qa  # also the end-to-end answer check (Claude Haiku)

It builds the synthetic corpus of tools/retrieval_bench_data.py in a temporary QQ_BRIDGE_HOME (never the real data
folder), runs each module through its public API, and writes docs/retrieval_bench/<label>-<mode>.json with, per module:
Recall@K of the essential items, nDCG@K, MRR, precision of what was returned, noise on negative queries, duplicates,
stale/foreign leaks, P50/P95 latency (warm) and the first call (cold), embedding texts and rerank pairs that reached
a model, peak GPU memory and the characters that would enter the prompt. The same script measures before and after
because it only uses the modules' public functions and counts at the model boundary."""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import retrieval_bench_data as data  # noqa: E402


# ---------- counting at the model boundary ----------

COUNTS = {}


def _count(name, amount):
    COUNTS[name] = COUNTS.get(name, 0) + amount


def instrument():
    """Wrap the functions that reach an embedding or rerank model (before and after the refactor)."""
    import embedding_provider
    import reranker
    import retrieval_api
    import small_models
    submit = embedding_provider._EmbeddingWorker.submit

    def counted_submit(self, texts, priority, recheck):
        result = submit(self, texts, priority, recheck)
        _count('embed_calls', 1)
        _count('embed_texts', len(texts))
        return result
    embedding_provider._EmbeddingWorker.submit = counted_submit

    def wrap(module, name, calls, items, size):
        original = getattr(module, name, None)
        if original is None:
            return

        def counted(*args, **kwargs):
            result = original(*args, **kwargs)  # only calls that reached a model count
            _count(calls, 1)
            _count(items, size(args, kwargs))
            return result
        setattr(module, name, counted)
    wrap(reranker, 'score', 'rerank_calls', 'rerank_pairs', lambda a, k: len(a[1]))
    wrap(reranker, 'score_pairs', 'rerank_calls', 'rerank_pairs', lambda a, k: len(a[0]))
    wrap(small_models, 'embed', 'embed_calls', 'embed_texts', lambda a, k: len(a[0]))
    wrap(small_models, 'rerank', 'rerank_calls', 'rerank_pairs', lambda a, k: len(a[1]))
    request = retrieval_api.request

    def counted_request(directory, kind, payload):
        if kind == 'embedding':
            _count('api_embed_calls', 1)
            _count('embed_texts', len(payload.get('input', [])))
        else:
            _count('api_rerank_calls', 1)
            _count('rerank_pairs', len(payload.get('documents') or payload.get('passages') or []))
        return request(directory, kind, payload)
    retrieval_api.request = counted_request
    import claude_backend  # noqa: F401  (imported once here so the QA check's errors are not import errors)


def gpu_reset():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def gpu_peak_mb():
    try:
        import torch
        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / 2**20, 1)
    except Exception:
        pass
    return None


# ---------- metrics ----------

def canonical(groups):
    out = {}
    for group in groups:
        for member in group:
            out[member] = group[0]
    return out


def score(returned, gold, k, groups=(), forbidden=()):
    """Metrics of one query. returned: ids best first; gold: {id: grade}."""
    canon = canonical(groups)
    c = lambda i: canon.get(i, i)
    gold_c = {}
    for item, grade in gold.items():
        gold_c[c(item)] = max(grade, gold_c.get(c(item), 0))
    seen, ranked, duplicates = set(), [], 0
    for item in returned[:k]:
        if c(item) in seen:
            duplicates += 1
            continue
        seen.add(c(item))
        ranked.append(c(item))
    leaks = sum(item in forbidden for item in returned)
    result = {'returned': len(returned[:k]), 'duplicates': duplicates, 'leaks': leaks}
    if not gold:
        result['noise'] = 1 if returned else 0
        return result
    essential = {item for item, grade in gold_c.items() if grade >= 2}
    import math
    gains = [(2 ** gold_c.get(item, 0) - 1) / math.log2(rank + 2) for rank, item in enumerate(ranked)]
    ideal = [(2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(sorted(gold_c.values(), reverse=True)[:k])]
    first = next((rank + 1 for rank, item in enumerate(ranked) if item in gold_c), None)
    result.update(recall=len(essential & set(ranked)) / len(essential) if essential else 1.0,
                  ndcg=sum(gains) / sum(ideal) if ideal else 0.0, mrr=1 / first if first else 0.0,
                  precision=(sum(item in gold_c for item in ranked) / len(ranked)) if ranked else None)
    return result


def summarize(rows, latencies, cold_ms, counts, extra=None):
    positives = [row for row in rows if 'recall' in row]
    negatives = [row for row in rows if 'noise' in row]
    precisions = [row['precision'] for row in positives if row['precision'] is not None]
    lat = sorted(latencies)
    pick = lambda q: round(lat[min(len(lat) - 1, int(len(lat) * q))], 1) if lat else None
    out = {
        'queries': len(rows), 'positives': len(positives), 'negatives': len(negatives),
        'recall': round(statistics.mean(row['recall'] for row in positives), 4) if positives else None,
        'ndcg': round(statistics.mean(row['ndcg'] for row in positives), 4) if positives else None,
        'mrr': round(statistics.mean(row['mrr'] for row in positives), 4) if positives else None,
        'precision': round(statistics.mean(precisions), 4) if precisions else None,
        'empty_on_positive': sum(row['returned'] == 0 for row in positives),
        'noise_rate': round(statistics.mean(row['noise'] for row in negatives), 4) if negatives else None,
        'duplicates': sum(row['duplicates'] for row in rows),
        'leaks': sum(row['leaks'] for row in rows),
        'returned_avg': round(statistics.mean(row['returned'] for row in rows), 2) if rows else 0,
        'p50_ms': pick(0.5), 'p95_ms': pick(0.95), 'cold_ms': round(cold_ms, 1) if cold_ms is not None else None,
        **{key: counts.get(key, 0) for key in ('embed_calls', 'embed_texts', 'rerank_calls', 'rerank_pairs',
                                               'api_embed_calls', 'api_rerank_calls')},
        'gpu_peak_mb': gpu_peak_mb(),
    }
    out.update(extra or {})
    return out


def timed_queries(queries, run, k, groups=(), forbidden=(), texts=None):
    """Runs every query once as warm-up of the first call (cold), then all of them timed; returns the summary."""
    COUNTS.clear()
    gpu_reset()
    started = time.perf_counter()
    run(queries[0][0])
    cold = (time.perf_counter() - started) * 1000
    COUNTS.clear()
    rows, latencies, chars, per_query = [], [], 0, []
    for query, gold in queries:
        started = time.perf_counter()
        returned = run(query)
        latencies.append((time.perf_counter() - started) * 1000)
        rows.append(score(returned, gold, k, groups, forbidden))
        chars += sum(len(texts[i]) for i in returned[:k]) if texts else 0
        per_query.append({'query': query, 'returned': returned[:k], 'gold': gold})
    summary = summarize(rows, latencies, cold, dict(COUNTS), {'context_chars': chars})
    summary['per_query'] = per_query
    return summary


# ---------- modules ----------

def friend_dir(home, qq):
    directory = home / 'friends' / qq
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / 'friend.json').is_file():
        (directory / 'friend.json').write_text('{}', encoding='utf-8')
    return directory


def set_recent(store, query):
    for body in data.MEMORY_RECENT + [query]:
        store.add_message('10086001', 'friend', body)


def bench_memory(home, api=None):
    import memory_management
    from store import FriendStore
    directory = friend_dir(home, '10086001')
    if api:
        (directory / 'friend.json').write_text(json.dumps({'retrieval_api': api}), encoding='utf-8')
    store = FriendStore(directory)
    ids, texts = {}, {}
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        for item, text, keywords, expires in data.MEMORY:
            ids[memory_management.insert(store, text, 'fact', keywords, ['1'], False, 'other',
                                         expires_at=expires)] = item
            texts[item] = text
        for item, text, keywords in data.MEMORY_SUPERSEDED:
            entry = memory_management.insert(store, text, 'fact', keywords, ['1'], False, 'other')
            ids[entry] = item
            memory_management.invalidate(store, entry, next(key for key, value in ids.items() if value == 'M03'))
        memory_management.sync(store)
    build = time.perf_counter()

    def run(query):
        set_recent(store, query)
        return [ids[row['id']] for row in memory_management.recall(directory, query)]
    run('预热')  # builds the vector cache of all entries: the index build
    build_ms = (time.perf_counter() - build) * 1000
    result = timed_queries(data.MEMORY_QUERIES, run, memory_management.RECALL_LIMIT, data.MEMORY_DUPLICATES,
                           data.MEMORY_STALE, texts)
    result['index_build_ms'] = round(build_ms, 1)
    result['_store'] = store
    return result


def bench_memory_qa(home, model='haiku'):
    """End-to-end: the retrieved memory decides whether a cheap model answers the friend's questions correctly."""
    import subprocess
    import claude_backend
    import memory_management
    from store import FriendStore
    directory = home / 'friends' / '10086001'
    store = FriendStore(directory)
    blocks, chars = [], 0
    for question, _ in data.MEMORY_QA:
        set_recent(store, question)
        block = memory_management.recall_block(directory, question)
        chars += len(block)
        blocks.append(block)
    store.close()
    items = '\n\n'.join(f'第{i + 1}题\n检索到的记忆：{block or "（无）"}\n问题：{question}'
                        for i, ((question, _), block) in enumerate(zip(data.MEMORY_QA, blocks)))
    prompt = ('你在替一位朋友回忆他自己的事。每题只根据该题“检索到的记忆”回答，记忆里没有就只答“不知道”，不要猜。'
              '只输出JSON数组，形如[{"i":1,"a":"答案"}]，不要别的文字。\n\n' + items)
    claude = claude_backend.cli_path()
    # the app's signed-in Claude home (QQ_BRIDGE_HOME points at the temporary benchmark folder, which has none)
    env = {**os.environ, 'CLAUDE_CONFIG_DIR': str(ROOT / 'data' / 'claude-home')}
    started = time.perf_counter()
    run = subprocess.run([claude, '-p', '--model', model, '--output-format', 'json', '--max-turns', '1'],
                         input=prompt, capture_output=True, text=True, encoding='utf-8', env=env, timeout=300,
                         cwd=tempfile.gettempdir(), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    elapsed = (time.perf_counter() - started) * 1000
    output = json.loads(run.stdout) if run.stdout.strip().startswith('{') else {}
    text = output.get('result', '')
    if '[' not in text:
        raise RuntimeError('QA answer was not a JSON list: ' + (run.stdout or run.stderr)[:400])
    answers = json.loads(text[text.index('['):text.rindex(']') + 1])
    by_index = {item['i']: str(item['a']) for item in answers}
    correct, details = 0, []
    for i, (question, words) in enumerate(data.MEMORY_QA, 1):
        answer = by_index.get(i, '')
        ok = any(word in answer for word in words) if words else ('不知道' in answer)
        correct += ok
        details.append({'question': question, 'answer': answer, 'ok': ok})
    usage = output.get('usage', {})
    return {'model': model, 'success_rate': round(correct / len(data.MEMORY_QA), 4), 'correct': correct,
            'total': len(data.MEMORY_QA), 'context_chars': chars, 'latency_ms': round(elapsed),
            'input_tokens': usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0)
            + usage.get('cache_creation_input_tokens', 0),
            'output_tokens': usage.get('output_tokens', 0), 'cost_usd': output.get('total_cost_usd'),
            'details': details}


def bench_topics(home, api=None):
    import sqlite3
    import topic_research
    from contextlib import closing
    from store import FriendStore
    directory = friend_dir(home, '10086002')
    if api:
        (directory / 'friend.json').write_text(json.dumps({'retrieval_api': api}), encoding='utf-8')
    texts = {}
    with closing(topic_research._open(directory)) as db:
        for item, kind, topic, title, summary, keywords, expires in data.TOPICS:
            db.execute('INSERT INTO items(id,kind,topic,title,url,summary,keywords,published,fetched_at,expires_at,active)'
                       ' VALUES(?,?,?,?,?,?,?,?,?,?,1)', (item, kind, topic, title, 'https://example.com/' + item, summary,
                                                          json.dumps(keywords, ensure_ascii=False), '2026-10-01',
                                                          '2026-10-20T10:00:00+08:00', expires))
            texts[item] = topic + title + summary
        db.commit()
    store = FriendStore(directory)
    build = time.perf_counter()

    def run(query):
        set_recent(store, query)
        return [row['id'] for row in topic_research.recall(directory, query)]
    run('预热')
    build_ms = (time.perf_counter() - build) * 1000
    result = timed_queries(data.TOPIC_QUERIES, run, topic_research.RECALL_LIMIT, (), data.TOPIC_STALE, texts)
    result['index_build_ms'] = round(build_ms, 1)
    store.close()
    del sqlite3
    return result


def engine_for(home):
    import local_retrieval_engine as lre
    return lre.LocalHybridSearchEngine(home / 'data' / 'retrieval_engine.sqlite3')


def bench_experiences(home):
    import local_retrieval_engine as lre
    engine = engine_for(home)
    store = lre.TaskExperienceStore(engine)
    texts, ids = {}, {}
    started = time.perf_counter()
    for item, owner, shared, problem, action, cause, tools, result in data.EXPERIENCES:
        status = (lre.ExperienceResultStatus.FAILED_CONFIRMED if result == 'failed'
                  else lre.ExperienceResultStatus.EXECUTION_FINISHED)
        experience = store.record_experience(task_id='task_' + item, owner_id=owner, project_id='', agent_provider='codex',
                                             model_name='', tools_used=tools, problem_statement=problem,
                                             environment_conditions={}, action_taken=action, result_status=status,
                                             root_cause_or_condition=cause, is_shared=bool(shared))
        ids[experience.experience_id] = item
        texts[item] = problem + action + cause
    build_ms = (time.perf_counter() - started) * 1000

    def run(query):
        return [ids[row['experience_id']] for row in store.search_experiences(query, owner_id='alice', limit=5)]
    result = timed_queries(data.EXPERIENCE_QUERIES, run, 5, data.EXPERIENCE_DUPLICATES, data.EXPERIENCE_FOREIGN, texts)
    result['index_build_ms'] = round(build_ms, 1)
    return result


def bench_capabilities(home):
    import local_retrieval_engine as lre
    engine = engine_for(home)
    service = lre.CapabilityDiscoveryService(engine)
    texts = {}
    for item, name, provider, kind, description, tasks in data.CAPABILITIES:
        service.register_capability(lre.CapabilitySpec(
            capability_id=item, name=name, provider=provider, type=lre.CapabilityType(kind), description=description,
            parameters_schema={}, applicable_tasks=tasks, status=lre.CapabilityStatus.AVAILABLE,
            required_permission_level='read'))
        texts[item] = name + description + ' '.join(tasks)

    def run(query):
        return [spec.capability_id for spec in service.match_capabilities(query, owner_id='alice', limit=4)]
    return timed_queries(data.CAPABILITY_QUERIES, run, 4, (), (), texts)


def tasks_db(home):
    import sqlite3
    from contextlib import closing
    path = home / 'data' / 'tasks.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT,'
                   ' result TEXT, error TEXT, artifacts TEXT, verification_info TEXT, checkpoint TEXT, category TEXT,'
                   ' owner_id TEXT, created_at TEXT)')
        for n, (item, title, description, status) in enumerate(data.TASKS):
            db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (item, title, description, status, '', '', '[]', '{}', '{}', 'general', 'alice',
                        f'2026-10-{10 + n:02d}T10:00:00'))
        db.commit()
    return path


def bench_dedup(home):
    import local_retrieval_engine as lre
    engine = engine_for(home)
    tasks_db(home)
    detector = lre.TaskDeduplicatorAndFailureDetector(engine)
    COUNTS.clear()
    gpu_reset()
    started = time.perf_counter()
    detector.check_duplicate_task('预热任务', '预热', owner_id='alice')
    cold = (time.perf_counter() - started) * 1000
    COUNTS.clear()
    tp = fp = fn = tn = wrong = 0
    latencies, per_query = [], []
    for title, description, expected in data.DEDUP_PROBES:
        started = time.perf_counter()
        is_dup, reason, info = detector.check_duplicate_task(title, description, owner_id='alice')
        latencies.append((time.perf_counter() - started) * 1000)
        found = info['task_id'] if is_dup and info else None
        per_query.append({'query': title, 'found': found, 'expected': expected})
        if expected and found == expected:
            tp += 1
        elif expected and found:
            wrong += 1
        elif expected:
            fn += 1
        elif found:
            fp += 1
        else:
            tn += 1
    lat = sorted(latencies)
    precision = tp / (tp + fp + wrong) if tp + fp + wrong else 1.0
    recall = tp / (tp + fn + wrong) if tp + fn + wrong else 1.0
    return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'wrong_task': wrong, 'precision': round(precision, 4),
            'recall': round(recall, 4), 'f1': round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0,
            'p50_ms': round(lat[len(lat) // 2], 1), 'p95_ms': round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
            'cold_ms': round(cold, 1), **{key: COUNTS.get(key, 0) for key in ('embed_calls', 'embed_texts', 'rerank_calls',
                                                                              'rerank_pairs')},
            'gpu_peak_mb': gpu_peak_mb(), 'per_query': per_query}


def bench_history(home):
    import local_retrieval_engine as lre
    engine = engine_for(home)
    path = tasks_db(home)
    store = lre.TaskExperienceStore(engine, tasks_db_path=path)
    texts = {item: title + description for item, title, description, _ in data.TASKS}

    def run(query):
        return [row['task_id'] for row in store.search_historical_tasks(query, 'alice', limit=3)]
    return timed_queries(data.HISTORY_QUERIES, run, 3, (), (), texts)


def bench_failures(home):
    import local_retrieval_engine as lre
    engine = engine_for(home)
    detector = lre.TaskDeduplicatorAndFailureDetector(engine)
    by_solution = {}
    for item, error, solution in data.FAILURES:
        detector.record_validated_failure_resolution(error, 'general', solution, owner_id='alice')
        by_solution[solution] = item

    def run(query):
        found, _, solution = detector.diagnose_failure_and_suggest(query, 'general', 'alice')
        return [by_solution[solution]] if found and solution in by_solution else []
    return timed_queries(data.FAILURE_QUERIES, run, 1)


def bench_remedies(home):
    from agent_memory import AgentMemoryStore
    store = AgentMemoryStore(home / 'data' / 'agent_memory.sqlite3')
    for item, category, action, cause, solution in data.REMEDIES:
        store.record_failure_experience(category, action, cause, solution, owner_id='alice')

    def run(query):
        found = []
        for content in store.retrieve_experience_remedies(query, owner_id='alice', limit=3):
            found += [item for item, _, _, _, solution in data.REMEDIES if solution in content]
        return found
    return timed_queries(data.REMEDY_QUERIES, run, 3)


def llm_chat(prompt, model, key, max_tokens=700):
    """One chat call to an OpenAI-compatible API (NVIDIA build): (answer, prompt tokens, completion tokens, ms)."""
    import urllib.error
    import urllib.request
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0,
                       'max_tokens': max_tokens}).encode()
    for attempt in range(6):
        started = time.perf_counter()
        try:
            request = urllib.request.Request('https://integrate.api.nvidia.com/v1/chat/completions', body,
                                             {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            usage = result.get('usage') or {}
            return ((result['choices'][0]['message'].get('content') or '').strip(), usage.get('prompt_tokens', 0),
                    usage.get('completion_tokens', 0), (time.perf_counter() - started) * 1000)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 5:
                raise
        except (TimeoutError, OSError):
            if attempt == 5:
                raise
        time.sleep(2 * (attempt + 1))


DONT_KNOW = ('不知道', '不清楚', '没有提到', '没有相关', '无法回答', "don't know", 'not know', 'unknown')


NEGATIONS = ('不要', '切勿', '别', '不得', '勿', '不应', '避免', '禁止', '而不是', 'not ', 'never', 'avoid', "don't", 'instead of')


def judged(answer, words, forbidden=()):
    """A forbidden phrase counts only when it is advice, not when the answer warns against it ("切勿关闭证书验证")."""
    low = answer.lower()
    for word in forbidden:
        start = low.find(word.lower())
        while start >= 0:
            if not any(neg in low[max(0, start - 12):start] for neg in NEGATIONS):
                return False
            start = low.find(word.lower(), start + 1)
    if not words:
        return any(word in low for word in DONT_KNOW)
    return any(word.lower() in low for word in words)


def bench_e2e(home, model, key, repeats=1):
    """End-to-end: an LLM does the real job using only what retrieval returned; success rate and prompt tokens."""
    from concurrent.futures import ThreadPoolExecutor
    import local_retrieval_engine as lre
    import memory_management
    import topic_research
    from store import FriendStore
    jobs = []  # (suite, prompt, words, forbidden)
    memory_dir, topic_dir = home / 'friends' / '10086001', home / 'friends' / '10086002'
    for directory, suite, questions, fetch, intro in (
            (memory_dir, 'memory_qa', [(q, w, []) for q, w in data.MEMORY_QA], memory_management.recall_block,
             '你在替一位朋友回忆他自己的事。只根据下面检索到的记忆回答问题，记忆里没有就只回答“不知道”，不要猜。'),
            (topic_dir, 'topic_qa', data.TOPIC_QA, topic_research.recall_block,
             '下面是话题资料库检索到的资料。只根据这些资料回答，资料里没有就只回答“不知道”，不要猜。')):
        store = FriendStore(directory)
        for question, words, forbidden in questions:
            set_recent(store, question)
            block = fetch(directory, question)
            jobs.append((suite, f'{intro}\n检索到的内容：\n{block or "（无）"}\n\n问题：{question}\n只输出简短答案。',
                         words, forbidden))
        store.close()
    engine = engine_for(home)
    experiences = lre.TaskExperienceStore(engine)
    for problem, words, forbidden in data.EXPERIENCE_TASKS:
        lines = []
        for item in experiences.search_experiences(problem, owner_id='alice', limit=5, include_failed_warnings=True):
            tag = '【踩坑：失败做法，不要照做】' if item.get('is_pitfall_warning') else '【经验】'
            lines.append(f"- {tag} 问题：{item['problem_statement']}；做法：{item['action_taken']}；"
                         f"原因：{item['root_cause_or_condition']}")
        jobs.append(('experience_fix', '你是编程助手。下面是团队以往的经验记录（可能不相关；标注踩坑的是失败做法，不要照做）：\n'
                     + ('\n'.join(lines) or '（无）') + f'\n\n问题：{problem}\n用一两句话给出修复建议。', words, forbidden))
    service = lre.CapabilityDiscoveryService(engine)
    for task, names in data.TOOL_TASKS:
        tools = service.match_capabilities(task, owner_id='alice', limit=4)
        listing = '\n'.join(f'- {spec.name}: {spec.description}' for spec in tools) or '（无）'
        jobs.append(('tool_choice', f'从下面的可用工具里选一个最适合完成任务的，只输出工具名；都不合适就输出 none。\n'
                     f'可用工具：\n{listing}\n\n任务：{task}', names, []))

    def one(job):
        suite, prompt, words, forbidden = job
        answer, prompt_tokens, completion_tokens, ms = llm_chat(prompt, model, key)
        if suite == 'tool_choice':
            chosen = answer.strip().strip('`').split()[0].lower() if answer.strip() else ''
            ok = chosen in [name.lower() for name in words]
        else:
            ok = judged(answer, words, forbidden)
        return {'suite': suite, 'ok': ok, 'answer': answer[:200], 'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens, 'ms': round(ms)}
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(repeats):
            rows += list(pool.map(one, jobs))
    out = {'model': model, 'repeats': repeats}
    for suite in dict.fromkeys(row['suite'] for row in rows):
        part = [row for row in rows if row['suite'] == suite]
        out[suite] = {'tasks': len(part), 'success_rate': round(sum(row['ok'] for row in part) / len(part), 4),
                      'prompt_tokens_avg': round(statistics.mean(row['prompt_tokens'] for row in part), 1),
                      'completion_tokens_avg': round(statistics.mean(row['completion_tokens'] for row in part), 1),
                      'p50_ms': round(statistics.median(row['ms'] for row in part))}
    out['overall_success'] = round(sum(row['ok'] for row in rows) / len(rows), 4)
    out['details'] = rows
    return out


def rescore(e2e):
    """Judge stored answers again (the answers are kept; only the judgement changes), by task order."""
    tasks = ([('memory_qa', w, []) for _, w in data.MEMORY_QA] + [('topic_qa', w, f) for _, w, f in data.TOPIC_QA] +
             [('experience_fix', w, f) for _, w, f in data.EXPERIENCE_TASKS] + [('tool_choice', n, []) for _, n in data.TOOL_TASKS])
    for i, row in enumerate(e2e['details']):
        suite, words, forbidden = tasks[i % len(tasks)]
        if suite != 'tool_choice':
            row['ok'] = judged(row['answer'], words, forbidden)
    for suite in ('memory_qa', 'topic_qa', 'experience_fix', 'tool_choice'):
        part = [row for row in e2e['details'] if row['suite'] == suite]
        e2e[suite]['success_rate'] = round(sum(row['ok'] for row in part) / len(part), 4)
    e2e['overall_success'] = round(sum(row['ok'] for row in e2e['details']) / len(e2e['details']), 4)
    return e2e


MODULES = {'memory': bench_memory, 'topics': bench_topics, 'experiences': bench_experiences,
           'capabilities': bench_capabilities, 'dedup': bench_dedup, 'history': bench_history,
           'failures': bench_failures, 'remedies': bench_remedies}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--mode', choices=('real', 'lexical', 'api'), default='real')
    parser.add_argument('--modules', default=','.join(MODULES))
    parser.add_argument('--qa', action='store_true')
    parser.add_argument('--e2e', action='store_true', help='LLM end-to-end tasks (needs --llm-key-file)')
    parser.add_argument('--llm-model', default='openai/gpt-oss-20b')
    parser.add_argument('--llm-key-file')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--heldout', action='store_true', help='the held-out query sets (same corpora)')
    parser.add_argument('--api-key-file')
    parser.add_argument('--api-embed-model', default='nvidia/nemotron-3-embed-1b')
    parser.add_argument('--api-rerank-model', default='nvidia/llama-nemotron-rerank-vl-1b-v2')
    parser.add_argument('--out', default=str(ROOT / 'docs' / 'retrieval_bench'))
    args = parser.parse_args()
    if args.heldout:  # same corpora, the queries written after tuning
        for name in ('MEMORY_QUERIES', 'TOPIC_QUERIES', 'EXPERIENCE_QUERIES', 'CAPABILITY_QUERIES'):
            setattr(data, name, getattr(data, name + '_HELDOUT'))
        data.DEDUP_PROBES = data.DEDUP_PROBES_HELDOUT
        args.label += '-heldout'
    home = Path(tempfile.mkdtemp(prefix='retrieval-bench-'))
    os.environ['QQ_BRIDGE_HOME'] = str(home)
    (home / 'data').mkdir()
    (home / 'house.local.json').write_text(json.dumps({'local_models_enabled': args.mode != 'lexical'}), 'utf-8')
    instrument()
    api = None
    if args.mode == 'api':
        api = {'embedding': {'enabled': True, 'mode': 'api', 'base_url': 'https://integrate.api.nvidia.com/v1',
                             'model': args.api_embed_model, 'dimension': 2048},
               'rerank': {'enabled': True, 'mode': 'api', 'base_url': 'https://ai.api.nvidia.com/v1/retrieval/'
                          + args.api_rerank_model.replace('.', '_'), 'model': args.api_rerank_model}}
        key = Path(args.api_key_file).read_text('utf-8').strip()
        import api_backend
        for qq in ('10086001', '10086002'):
            directory = friend_dir(home, qq)
            for kind in ('embedding', 'rerank'):
                api_backend.save_key(directory, key, kind)
    results = {'label': args.label, 'mode': args.mode, 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'modules': {}}
    for name in args.modules.split(','):
        print('==', name, flush=True)
        function = MODULES[name]
        result = function(home, api) if name in ('memory', 'topics') else function(home)
        store = result.pop('_store', None)
        if args.qa and name == 'memory':
            if store is not None:
                store.close()
                store = None
            result['qa'] = bench_memory_qa(home)
        if store is not None:
            store.close()
        results['modules'][name] = result
        print(json.dumps({key: value for key, value in result.items() if key not in ('per_query', 'qa')},
                         ensure_ascii=False), flush=True)
        if 'qa' in result:
            print('qa', json.dumps({key: value for key, value in result['qa'].items() if key != 'details'},
                                   ensure_ascii=False), flush=True)
    if args.e2e:
        print('== e2e', args.llm_model, flush=True)
        key = Path(args.llm_key_file).read_text('utf-8').strip()
        results['e2e'] = bench_e2e(home, args.llm_model, key, args.repeats)
        print(json.dumps({k: v for k, v in results['e2e'].items() if k != 'details'}, ensure_ascii=False), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{args.label}-{args.mode}.json').write_text(json.dumps(results, ensure_ascii=False, indent=1), 'utf-8')
    print('written', out / f'{args.label}-{args.mode}.json')


if __name__ == '__main__':
    main()
