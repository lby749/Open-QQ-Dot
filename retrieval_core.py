"""One retrieval scheduler for every module that retrieves (2026-10-26 user: unify the embedding / rerank / RAG
chain, pick among the app's five embedding and four rerank models, cache, batch, and keep stale vectors out).

What lives here, used by hybrid_search (chat memory, topics, stickers, persona examples), local_retrieval_engine
(task experience, tool discovery, task dedupe, failure diagnosis, task history) and agent_memory:

1. the model registry: the presets small_models already knows, with what each is good for, and which are on disk;
2. model choice (choose_embedding / choose_reranker): a friend's own retrieval_api setting or the house override
   (house.local.json retrieval_models) first, then by task, language, amount of text and free GPU memory. Only
   installed local models are chosen automatically; nothing is downloaded and no cloud API is used unless the user
   configured it. A vector collection keeps the model it was built with (its signature), so a query is always
   embedded by the collection's model — the choice for stored vectors changes only when the installed models do;
3. caches: query vectors and rerank scores in memory (the last chat lines are searched again on every reply, and
   the memory and topic libraries search the same lines), document vectors on disk keyed by model signature;
4. batched reranking of many (query, passage) pairs in one model call;
5. result hygiene shared by all callers: a relevance floor with a dynamic top-K, MMR-style removal of near-duplicates,
   and the identifier check used by task dedupe.

stats() reports cache hits, model calls and choices for the benchmark (tools/retrieval_benchmark.py) and traces."""
import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / 'data' / 'embedding_models'


# ---------- 1. registry ----------

@dataclass(frozen=True)
class Model:
    name: str
    kind: str  # 'embedding' | 'rerank'
    languages: tuple  # 'zh', 'en', 'multi'
    params_m: int
    quality: int  # 1 small .. 3 best
    speed: int  # 1 slow .. 3 fast
    directory: str  # under data/embedding_models
    dimension: int = 0
    pooling: str = 'cls'
    query_prefix: str = ''
    document_prefix: str = ''
    builtin: bool = False  # run by embedding_provider / reranker (frozen revision) instead of small_models

    @property
    def vram_mb(self):  # fp16 weights plus activations for a batch, measured for the two built-in models
        return int(self.params_m * 2 * 1.15) + 200

    def path(self):
        return MODELS_DIR / self.directory


def _builtin_dirs():
    from embedding_provider import REVISION as EMBED_REVISION
    from reranker import REVISION as RERANK_REVISION
    return 'bge-m3/' + EMBED_REVISION, 'bge-reranker-v2-m3/' + RERANK_REVISION


def registry():
    """{name: Model} of the five embedding and four rerank presets (small_models.PRESETS)."""
    embed_dir, rerank_dir = _builtin_dirs()
    zh_prefix = '为这个句子生成表示以用于检索相关文章：'
    models = [
        Model('BAAI/bge-m3', 'embedding', ('zh', 'en', 'multi'), 568, 3, 1, embed_dir, 1024, 'cls', builtin=True),
        Model('BAAI/bge-base-zh-v1.5', 'embedding', ('zh', 'en'), 102, 2, 2, 'bge-base-zh-v1.5', 768, 'cls', zh_prefix),
        Model('BAAI/bge-small-zh-v1.5', 'embedding', ('zh', 'en'), 24, 1, 3, 'bge-small-zh-v1.5', 512, 'cls', zh_prefix),
        Model('intfloat/multilingual-e5-small', 'embedding', ('zh', 'en', 'multi'), 118, 1, 3,
              'multilingual-e5-small', 384, 'mean', 'query: ', 'passage: '),
        Model('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', 'embedding', ('zh', 'en', 'multi'), 118, 1,
              3, 'paraphrase-multilingual-MiniLM-L12-v2', 384, 'mean'),
        Model('BAAI/bge-reranker-v2-m3', 'rerank', ('zh', 'en', 'multi'), 568, 3, 1, rerank_dir, builtin=True),
        Model('BAAI/bge-reranker-base', 'rerank', ('zh', 'en'), 278, 2, 2, 'bge-reranker-base'),
        Model('cross-encoder/mmarco-mMiniLMv2-L12-H384-v1', 'rerank', ('zh', 'en', 'multi'), 118, 1, 3,
              'mmarco-mMiniLMv2-L12-H384-v1'),
        Model('cross-encoder/ms-marco-MiniLM-L6-v2', 'rerank', ('en',), 22, 1, 3, 'ms-marco-MiniLM-L6-v2'),
    ]
    return {model.name: model for model in models}


_installed_cache = {'at': 0.0, 'value': None}


def installed(kind=None):
    """Names of the presets whose weights are on disk (checked at most every 60 s)."""
    now = time.monotonic()
    if _installed_cache['value'] is None or now - _installed_cache['at'] > 60:
        _installed_cache.update(at=now, value={name for name, model in registry().items()
                                               if (model.path() / 'config.json').is_file()})
    return sorted(name for name in _installed_cache['value'] if kind is None or registry()[name].kind == kind)


def local_value(model):
    """The small_models setting that runs a non-built-in preset."""
    value = {'enabled': True, 'mode': 'local', 'model_path': str(model.path()), 'model': model.name}
    if model.kind == 'embedding':
        value.update(dimension=model.dimension, pooling=model.pooling, query_prefix=model.query_prefix,
                     document_prefix=model.document_prefix)
    return value


# ---------- 2. choosing models ----------

# what each kind of retrieval needs; 'fast' ones run inside a QQ reply, 'quality' ones decide what an agent sees
TASKS = {'chat': 'fast', 'memory': 'balanced', 'topics': 'balanced', 'examples': 'fast', 'stickers': 'fast',
         'experience': 'quality', 'tools': 'quality', 'dedup': 'quality', 'failure': 'balanced',
         'history': 'balanced', 'remedies': 'balanced'}
FAST_WORK_CHARS = 20000  # above this much text a 'fast' task prefers a quicker installed reranker
VRAM_MARGIN_MB = 512


@dataclass(frozen=True)
class Choice:
    kind: str  # 'builtin' | 'preset' | 'configured' | 'none'
    model: str = ''
    reason: str = ''
    value: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def signature(self):
        if self.kind == 'builtin':
            return self.model + '@' + registry()[self.model].directory.rsplit('/', 1)[-1]
        if self.kind == 'preset':
            return self.model
        return self.kind + ':' + self.model


def language(text):
    """'zh' (mostly CJK), 'en' (mostly Latin letters) or 'multi'."""
    cjk = len(re.findall(r'[㐀-鿿]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    if cjk >= max(2, latin / 3):
        return 'zh' if latin <= cjk else 'multi'
    return 'en' if latin else 'multi'


def overrides():
    """house.local.json retrieval_models: {'embedding': 'auto' | name, 'rerank': 'auto' | 'off' | name}."""
    try:
        from house_settings import load
        value = load().get('retrieval_models') or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _free_vram_mb():
    """Free CUDA memory, only when torch is already loaded (never imported just to look)."""
    import sys
    torch = sys.modules.get('torch')
    try:
        if torch is not None and torch.cuda.is_available():
            free, _ = torch.cuda.mem_get_info()
            return free / 2**20
    except Exception:
        pass
    return None


def _loaded(name):
    """Whether a built-in model already sits in memory (then choosing it costs no new GPU memory)."""
    try:
        if name == 'BAAI/bge-reranker-v2-m3':
            import reranker
            return reranker._state.get('model') is not None
        if name == 'BAAI/bge-m3':
            import embedding_provider
            worker = embedding_provider._WORKER
            return worker is not None and worker.backend is not None
    except Exception:
        pass
    return False


def _pick(kind, task, text_sample, work_chars, override):
    models = registry()
    have = [name for name in installed(kind) if models[name].kind == kind]
    if override and override != 'auto':
        if override == 'off':
            return Choice('none', reason='house override: off')
        if override in have:
            model = models[override]
            return Choice('builtin' if model.builtin else 'preset', override, 'house override',
                          {} if model.builtin else local_value(model))
        # an override naming a missing model falls back to the automatic choice, said so in the reason
    lang = language(text_sample) if text_sample else 'multi'
    usable = [name for name in have if lang in models[name].languages or
              lang == 'multi' and ('multi' in models[name].languages or
                                   {'zh', 'en'} <= set(models[name].languages))]
    if not usable:
        usable = [name for name in have if 'multi' in models[name].languages]
    if not usable:
        return Choice('none', reason='no local model installed' if not have else 'no installed model supports query language')
    need = TASKS.get(task, 'balanced')
    if need == 'fast' and work_chars > FAST_WORK_CHARS:
        order = sorted(usable, key=lambda name: (-models[name].speed, -models[name].quality))
        reason = f'{task}: {work_chars} characters, fastest installed'
    else:
        order = sorted(usable, key=lambda name: (-models[name].quality, -models[name].speed))
        reason = f'{task}: best installed for {lang}'
    free = _free_vram_mb()
    if free is not None and kind == 'rerank':
        fits = [name for name in order if _loaded(name) or models[name].vram_mb + VRAM_MARGIN_MB <= free]
        if fits and fits[0] != order[0]:
            order, reason = fits, reason + f'; {int(free)} MB GPU free, smaller model'
        elif not fits and len(order) > 1:  # nothing fits: the smallest is the least likely to fall back to CPU
            order = sorted(order, key=lambda name: models[name].vram_mb)
            reason += f'; {int(free)} MB GPU free, none fits, smallest'
    name = order[0]
    model = models[name]
    if override and override != 'auto':
        reason += f'; override {override} is not installed'
    return Choice('builtin' if model.builtin else 'preset', name, reason, {} if model.builtin else local_value(model))


def choose_reranker(task='memory', query='', passages=(), directory=None):
    """The reranker for this search. A friend's own retrieval_api rerank setting always wins (it is the user's)."""
    from house_settings import local_models_enabled
    from retrieval_api import config
    configured = config(directory, 'rerank')
    if configured.get('enabled'):
        return Choice('configured', configured.get('model', ''), 'friend retrieval_api setting', configured)
    if not local_models_enabled():  # the built-in reranker refuses by itself (and says why)
        return Choice('builtin', 'BAAI/bge-reranker-v2-m3', 'local models switched off')
    sample = query + ' ' + ' '.join(passage[:200] for passage in list(passages)[:8])
    work = sum(len(query) + len(passage) for passage in passages)
    choice = _pick('rerank', task, sample, work, overrides().get('rerank'))
    if choice.kind == 'none' and choice.reason == 'no local model installed':
        return Choice('builtin', 'BAAI/bge-reranker-v2-m3', 'no reranker installed (it reports the missing files)')
    return choice


def choose_embedding(directory=None):
    """The embedding model for stored vectors (one per collection, so not per query): the friend's setting, the
    house override, else the best installed multilingual model (chat mixes Chinese and English)."""
    from house_settings import local_models_enabled
    from retrieval_api import config
    configured = config(directory, 'embedding')
    if configured.get('enabled'):
        return Choice('configured', configured.get('model', ''), 'friend retrieval_api setting', configured)
    if not local_models_enabled():
        return Choice('builtin', 'BAAI/bge-m3', 'local models switched off (calls will be refused)')
    choice = _pick('embedding', 'memory', '', 0, overrides().get('embedding'))
    if choice.kind == 'none':  # nothing installed: the built-in path reports the missing model itself
        return Choice('builtin', 'BAAI/bge-m3', 'no embedding model installed')
    return choice


# ---------- 3. caches ----------

class LRU:
    def __init__(self, size):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                self.hits += 1
                return self.data[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)

    def clear(self):
        with self.lock:
            self.data.clear()
            self.hits = self.misses = 0


QUERY_VECTORS = LRU(2048)
RERANK_SCORES = LRU(16384)
_STATS = {'embed_model_calls': 0, 'embed_model_texts': 0, 'rerank_model_calls': 0, 'rerank_model_pairs': 0,
          'rerank_skipped': 0, 'doc_cache_hits': 0, 'doc_cache_misses': 0, 'choices': {}}
_stats_lock = threading.Lock()


def note(key, amount=1):
    with _stats_lock:
        _STATS[key] = _STATS.get(key, 0) + amount


def note_choice(kind, choice):
    with _stats_lock:
        _STATS['choices'][kind] = {'model': choice.model or choice.kind, 'kind': choice.kind, 'reason': choice.reason}


def stats():
    with _stats_lock:
        out = json.loads(json.dumps(_STATS))
    out.update(query_cache_hits=QUERY_VECTORS.hits, query_cache_misses=QUERY_VECTORS.misses,
               rerank_cache_hits=RERANK_SCORES.hits, rerank_cache_misses=RERANK_SCORES.misses)
    return out


def reset_stats():
    QUERY_VECTORS.clear()
    RERANK_SCORES.clear()
    with _stats_lock:
        for key in list(_STATS):
            _STATS[key] = {} if key == 'choices' else 0


def digest(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


def pack(vector):
    return struct.pack(f'{len(vector)}f', *vector)


def unpack(blob):
    return struct.unpack(f'{len(blob) // 4}f', blob)


def validate_vectors(vectors, count):
    """Validate the whole model batch before caching any part of it."""
    if len(vectors) != count:
        raise ValueError('向量模型返回条数不一致')
    dimensions = {len(vector) for vector in vectors}
    if count and (len(dimensions) != 1 or not 1 <= next(iter(dimensions)) <= 65536):
        raise ValueError('向量模型返回维度不一致或无效')
    if any(not math.isfinite(float(value)) for vector in vectors for value in vector):
        raise ValueError('向量模型返回非有限数值')
    return vectors


class DocVectors:
    """Document vectors on disk keyed by (model signature, text digest): a vector from another model or another
    dimension is never reused, and switching back to an earlier model finds its vectors again. Rows not used for
    KEEP_DAYS are dropped when the file grows past MAX_ROWS, followed by oldest-row eviction to enforce a hard bound."""
    MAX_ROWS = 50000
    KEEP_DAYS = 60

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS doc_vectors(signature TEXT NOT NULL, digest TEXT NOT NULL, '
                   'dimension INTEGER NOT NULL, vector BLOB NOT NULL, used_at REAL NOT NULL, '
                   'PRIMARY KEY(signature, digest))')
        return db

    def get(self, texts, signature, embed):
        """Vectors for texts (one batch call for the missing ones); embed(missing_texts) -> vectors."""
        keys = [digest(text) for text in texts]
        with self.lock, closing(self._open()) as db:
            found = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                for key, dimension, blob in db.execute(
                        'SELECT digest, dimension, vector FROM doc_vectors WHERE signature=? AND digest IN (%s)' % ','.join('?' * len(chunk)),
                        [signature, *chunk]):
                    if 1 <= dimension <= 65536 and len(blob) == dimension * 4:
                        vector = unpack(blob)
                        if all(math.isfinite(value) for value in vector):
                            found[key] = vector
            if len({len(vector) for vector in found.values()}) > 1:
                found.clear()  # A corrupt legacy cache must not mix dimensions under one signature.
            missing = [i for i, key in enumerate(keys) if key not in found]
            note('doc_cache_hits', len(keys) - len(missing))
            note('doc_cache_misses', len(missing))
            now = time.time()
            if missing:
                unique = list(dict.fromkeys(keys[i] for i in missing))
                by_key = dict(zip(keys, texts))
                vectors = validate_vectors(embed([by_key[key] for key in unique]), len(unique))
                validate_vectors(list(found.values()) + list(vectors), len(found) + len(vectors))
                rows = []
                for key, vector in zip(unique, vectors):
                    found[key] = tuple(vector)
                    rows.append((signature, key, len(vector), pack(vector), now))
                db.executemany('INSERT OR REPLACE INTO doc_vectors VALUES(?,?,?,?,?)', rows)
            missing_keys = {keys[i] for i in missing}
            db.executemany('UPDATE doc_vectors SET used_at=? WHERE signature=? AND digest=?',
                           [(now, signature, key) for key in set(keys) - missing_keys])
            if db.execute('SELECT COUNT(*) FROM doc_vectors').fetchone()[0] > self.MAX_ROWS:
                db.execute('DELETE FROM doc_vectors WHERE used_at < ?', (now - self.KEEP_DAYS * 86400,))
                excess = db.execute('SELECT COUNT(*) FROM doc_vectors').fetchone()[0] - self.MAX_ROWS
                if excess > 0:
                    db.execute('DELETE FROM doc_vectors WHERE rowid IN (SELECT rowid FROM doc_vectors ORDER BY used_at LIMIT ?)', (excess,))
            db.commit()
        return [found[key] for key in keys]


_doc_caches = {}


def doc_cache(directory=None):
    """The document-vector cache of a friend folder, or the house one (data/retrieval_cache.sqlite3)."""
    if directory is not None:
        path = Path(directory) / 'retrieval_cache.sqlite3'
    else:
        from house_settings import data_dir
        path = data_dir() / 'retrieval_cache.sqlite3'
    key = str(path.resolve())
    if key not in _doc_caches:
        _doc_caches[key] = DocVectors(path)
    return _doc_caches[key]


# ---------- 4. embedding and rerank entry points ----------

def embed_queries(texts, directory=None):
    """Query vectors (cached in memory by model signature and text)."""
    import retrieval_api
    return retrieval_api.embed(list(texts), directory, is_query=True)


def embed_documents(texts, directory=None, cache=True):
    """Document vectors through the on-disk cache of that folder (or the house cache)."""
    import retrieval_api
    texts = list(texts)
    if not texts:
        return []
    if not cache:
        return retrieval_api.embed(texts, directory, is_query=False)
    signature = retrieval_api.signature(directory)
    return doc_cache(directory).get(texts, signature,
                                    lambda missing: retrieval_api.embed(missing, directory, is_query=False))


def rerank_groups(groups, directory=None, task='memory'):
    """[[score per passage] per group] for groups = [(query, [passages])]: cached pairs are not scored again and all
    the others go to the chosen reranker in one batch. Raises persona_store.Unavailable without a reranker."""
    import retrieval_api
    passages_all = [passage for _, passages in groups for passage in passages]
    choice = choose_reranker(task, groups[0][0] if groups else '', passages_all, directory)
    note_choice('rerank', choice)
    if choice.kind == 'none':
        from persona_store import Unavailable
        raise Unavailable('no reranker: ' + choice.reason)
    import reranker
    import small_models
    # the scoring functions are part of the key, so a replaced (test) reranker never sees another one's scores
    signature = (choice.signature + (':' + digest(json.dumps(choice.value, sort_keys=True))
                                     if choice.kind == 'configured' else ''),
                 retrieval_api.cache_scope(directory, 'rerank'),
                 id(reranker.score), id(small_models.rerank), id(retrieval_api.request))
    results, todo = [], []
    for g, (query, passages) in enumerate(groups):
        row = []
        for p, passage in enumerate(passages):
            score = RERANK_SCORES.get((signature, query, passage))
            row.append(score)
            if score is None:
                todo.append((g, p, query, passage))
        results.append(row)
    if todo:
        unique = list(dict.fromkeys((query, passage) for _, _, query, passage in todo))
        scores = retrieval_api.rerank_pairs(unique, directory, choice)
        note('rerank_model_calls')
        note('rerank_model_pairs', len(unique))
        by_pair = dict(zip(unique, scores))
        for g, p, query, passage in todo:
            results[g][p] = by_pair[(query, passage)]
            RERANK_SCORES.put((signature, query, passage), by_pair[(query, passage)])
    return results


def rerank(query, passages, directory=None, task='memory'):
    return rerank_groups([(query, list(passages))], directory, task)[0] if passages else []


# ---------- 5. result hygiene ----------

def cosine(a, b):
    if a is None or b is None or len(a) == 0 or len(b) == 0 or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def mmr_unique(order, vectors, texts=None, threshold=0.9):
    """order (best first) without the later of two near-duplicates: cosine >= threshold between their stored
    vectors, or (without vectors) the same text once punctuation and spaces are removed."""
    kept, seen_text = [], set()
    for item in order:
        if texts is not None:
            plain = re.sub(r'[\W_]+', '', texts[item]).casefold()
            if plain in seen_text:
                continue
            seen_text.add(plain)
        vector = vectors.get(item) if vectors else None
        if vector is not None and any(cosine(vector, vectors[other]) >= threshold for other in kept
                                      if vectors.get(other) is not None):
            continue
        kept.append(item)
    return kept


def dynamic_cut(scored, floor, limit, ratio=0.0):
    """[(item, score)] best first, kept while score >= floor and >= ratio * the best score, at most limit."""
    ranked = sorted(scored, key=lambda pair: -pair[1])
    if not ranked:
        return []
    bar = max(floor, ratio * ranked[0][1])
    return [(item, score) for item, score in ranked if score >= bar][:limit]


_ANCHOR = re.compile(r'[A-Za-z_][\w.\-]*\.[A-Za-z0-9]+|[A-Za-z][A-Za-z0-9_\-]{2,}|\d{2,}')
_ANCHOR_STOP = {'the', 'and', 'for', 'with', 'into', 'from', 'this', 'that'}


def anchors(text):
    """Identifiers a task is about: file names, product / library names, numbers ('calc.py', 'MySQL', 'Top250')."""
    return ({match.casefold() for match in _ANCHOR.findall(text)} - _ANCHOR_STOP) | set(
        re.findall(r'(?:\d+|[零一二三四五六七八九十百]+)[年月日号]|(?:周|星期)[一二三四五六日天]', text))


def same_task_subject(left, right):
    """Related tasks need compatible targets before a relevance model may propose deduplication."""
    for positive, negative in (('启用|开启|enable', '禁用|关闭|disable'), ('添加|新增|创建|create|add', '删除|移除|delete|remove')):
        if (re.search(positive, left, re.I) and re.search(negative, right, re.I) or
                re.search(negative, left, re.I) and re.search(positive, right, re.I)):
            return False
    action = r'修复|报错|错误|提交|页面|生成|保存|整理|抓取|爬取|翻译|备份|新增|添加|实现|完成|进行|每周|单元测试|文件|为|把|成|页|写|爬'
    def terms(text):
        text = re.sub(action, '', text)
        return {run[i:i+2] for run in re.findall(r'[㐀-鿿]+', text) for i in range(len(run)-1)}
    a, b = terms(left), terms(right)
    return not (a and b and not a & b)


_DIAGNOSTICS = {
    'cuda_oom': re.compile(r'(?:CUDA|GPU)[^\n]{0,40}(?:out[ _-]+of[ _-]+memory|\boom\b)|显存(?:不足|不够|耗尽|溢出)', re.I),
    'http429': re.compile(r'(?<!\d)429(?!\d)|rate[ _-]*limit|too many requests|限流|请求过多', re.I),
    'http401': re.compile(r'(?<!\d)401(?!\d)|unauthorized|未经授权', re.I),
    'http403': re.compile(r'(?<!\d)403(?!\d)|forbidden|禁止访问', re.I),
    'sqlitebusy': re.compile(r'database is locked|sqlite_busy|数据库[被写]*锁', re.I),
    'modulemissing': re.compile(r'modulenotfounderror|no module named|找不到模块', re.I),
    'certificateverify': re.compile(r'certificate verify failed|证书验证失败|证书校验失败', re.I),
}


def diagnostic_terms(text):
    """Canonical protocol/error names, shared by lexical recall and evidence gating across languages."""
    return {name for name, pattern in _DIAGNOSTICS.items() if pattern.search(text)}


def lexical_scores(query, texts):
    from hybrid_search import BM25
    return BM25(texts).scores(query, range(len(texts)))


def evidence_cut(query, texts, scored, lexical, dense, floor, limit):
    """A cross-encoder probability is one relevance signal, not a veto of exact diagnostic evidence."""
    signals = diagnostic_terms(query)
    technical = set(re.findall(r'[a-z][a-z0-9_.-]{2,}', query.casefold())) - {
        'the', 'and', 'how', 'what', 'why', 'for', 'with', 'from', 'this', 'that', 'error', 'failed', 'api', 'please'}
    accepted = []
    for index, score in sorted(scored, key=lambda pair: -pair[1]):
        exact_diagnostic = signals & diagnostic_terms(texts[index])
        identifier = technical & set(re.findall(r'[a-z][a-z0-9_.-]{2,}', texts[index].casefold()))
        supported = bool(identifier and lexical.get(index, 0) > 0 and dense[index] >= 0.55 and score >= floor / 4)
        if score >= floor or exact_diagnostic or supported:
            accepted.append((index, score))
    return accepted[:limit]


def anchors_conflict(a, b):
    """Two task texts name different things (calc.py vs utils.py, MySQL vs PostgreSQL): not the same task."""
    left, right = anchors(a), anchors(b)
    return bool(left and right and left != right and not (left <= right or right <= left))


_SMALL_TALK = ('哈哈', '嗯嗯', '嗯', '好的', '好吧', '在吗', '在不在', '晚安', '早安', '午安', '谢谢', '收到', '知道了', '可以',
               '行吧', '是的', '对的', '没事', '啊', '哦', '呀', '吧', '呢', '啦')
_SMALL_TALK_EN = {'ok', 'okay', 'hello', 'hi', 'hey', 'thanks', 'thank', 'you', 'yes', 'no', 'yeah', 'lol', 'good', 'night'}


def informative(text):
    """Whether a line carries something to search for: '好的', '在吗', '哈哈哈哈', 'OK', 'hello' do not (they never
    matched a memory in the benchmark but cost a full search with reranking each)."""
    rest = re.sub(r'[\W_]+', '', text.casefold())
    for phrase in _SMALL_TALK:
        rest = rest.replace(phrase, '')
    cjk = len(re.findall(r'[㐀-鿿]', rest))
    words = [word for word in re.findall(r'[a-z]+', text.casefold()) if word not in _SMALL_TALK_EN]
    return cjk >= 1 or len(words) >= 2 or any(len(word) >= 4 for word in words) or bool(re.search(r'\d{2,}', rest))


# ---------- 6. one call for a plain collection of texts ----------

def strong_overlap(query, text, identifier_parts=1):
    """They share a real word: two CJK characters in a row, or a Latin word of four letters or more. When every
    shared word is a piece of a snake_case identifier in the query, `identifier_parts` of them are needed:
    'syntax_error_alice_private' and 'database_lock_bob_private' share only 'private'."""
    import hybrid_search
    words = {t for t in hybrid_search.tokens(query) if (len(t) >= 2 and not t.isascii()) or (t.isascii() and len(t) >= 4)}
    shared = words & set(hybrid_search.tokens(text))
    pieces = {t for name in re.findall(r'[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+', query) for t in hybrid_search.tokens(name)}
    if shared and shared <= pieces:
        return len(shared) >= min(identifier_parts, len(words & pieces))
    return bool(shared)


def rank_texts(query, texts, *, limit=5, task='memory', floor=0.1, pool=10, directory=None, cache_path=None,
               dup_cosine=0.9, semantic=True, fallback_floor=0.55):
    """[(index, score)] of the texts relevant to query, best first — the whole pipeline in one call for collections
    without their own index (agent fix memory, the cross-agent memory hub): small talk returns nothing; BM25 and
    dense vectors (cached on disk by model signature) are fused by RRF; the reranker reads only the `pool` best and
    a hit needs `floor`; near-duplicates are dropped. Without a reranker: retain dense recall above
    `fallback_floor`; without vectors require a shared real word (two pieces of an identifier);
    without an embedder: BM25 alone."""
    import hybrid_search
    if not texts or not informative(query):
        return []
    bm25 = hybrid_search.BM25(texts)
    lexical = bm25.scores(query, range(len(texts)))
    lexical_rank = sorted(lexical, key=lambda i: -lexical[i])
    if not semantic:
        return [(i, lexical[i]) for i in lexical_rank if strong_overlap(query, texts[i], identifier_parts=2)][:limit]
    vectors, dense_rank = None, []
    dense = [0.0] * len(texts)
    try:
        import retrieval_api
        q_vec = embed_queries([query[:1000]], directory)[0]
        signature = retrieval_api.signature(directory)
        cache = DocVectors(cache_path) if cache_path else doc_cache(directory)
        vectors = cache.get([text[:1000] for text in texts], signature,
                            lambda missing: retrieval_api.embed(missing, directory, is_query=False))
        dense = [cosine(q_vec, vector) for vector in vectors]
        dense_rank = sorted(range(len(texts)), key=lambda i: -dense[i])[:hybrid_search.DEPTH]
    except Exception:
        vectors = None
    def keep(index, score):
        if score is not None:
            return score >= floor
        if dense_rank:
            return dense[index] >= fallback_floor
        return strong_overlap(query, texts[index], identifier_parts=2)
    hits = hybrid_search.search(query, texts, dense, bm25=bm25, limit=pool, size=pool, keep=keep,
                                directory=directory, task=task)
    kept = [(i, score if score is not None else dense[i] if dense_rank else lexical[i])
            for i, score in hits]
    order = mmr_unique([i for i, _ in kept], {i: vectors[i] for i, _ in kept} if vectors else None, texts, dup_cosine)
    by_index = dict(kept)
    return [(i, by_index[i]) for i in order[:limit]]
