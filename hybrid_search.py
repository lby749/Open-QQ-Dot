"""Hybrid retrieval: BM25 + dense vectors, fused by RRF, reranked by a cross-encoder.

2026-10-10 user: label + pure-vector search matched short chat lines by sentence shape and missed same-topic ones.
Every library (persona-template examples, sticker cards, and the topic library once it is rebuilt) now searches
like this:

1. metadata — the caller passes the indices its metadata allows (kind, sensitivity, …) and may add a metadata
   ranking (e.g. items whose scene label matches the message) that is fused like the other two;
2. BM25 over character unigrams + bigrams (Chinese needs no tokenizer this way) and dense BGE-M3 similarity,
   each ranking the allowed items;
3. Reciprocal Rank Fusion (k=60) of the rankings, top POOL candidates;
4. bge-reranker-v2-m3 scores the candidates against the query; below `min_score` is left out (low);
5. context expansion is the caller's: it shows a hit with the turns around it.

Without the reranker (no CUDA, model missing) the fused order is used and `fallback_floor` (dense cosine) applies."""
import math
import re
import struct
from pathlib import Path
import threading
from collections import Counter

RRF_K = 60
POOL = 30  # fused candidates sent to the reranker
DEPTH = 100  # how deep each of the two rankings goes into the fusion
_WORD = re.compile(r'[a-z0-9]+|[㐀-鿿]+')
_trace = threading.local()  # searches since the last generation record (generation_log keeps them)


def drain_trace():
    """The searches recorded on this thread since the last call, then forget them."""
    items, _trace.items = getattr(_trace, 'items', []), []
    return items


def extend_trace(items):
    """Add searches another thread recorded (prompt_context.retrieved_blocks runs them side by side)."""
    _trace.items = (getattr(_trace, 'items', []) + list(items))[-20:]


def _record(label, query, pool, hits, dropped, reranked, texts):
    items = getattr(_trace, 'items', [])
    items.append({'label': label, 'query': query[:80], 'pool': pool, 'reranked': reranked, 'dropped': dropped,
                  'hits': [[texts[i][:60], None if s is None else round(s, 3)] for i, s in hits]})
    _trace.items = items[-20:]


def tokens(text):
    """Latin words, CJK characters and CJK character bigrams."""
    out = []
    for run in _WORD.findall(text.casefold()):
        if run[0].isascii():
            out.append(run)
        else:
            out += list(run) + [run[i:i + 2] for i in range(len(run) - 1)]
    from retrieval_core import diagnostic_terms
    return out + sorted(diagnostic_terms(text))


class BM25:
    def __init__(self, texts, k1=1.2, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokens(text)) for text in texts]
        self.lengths = [sum(doc.values()) for doc in self.docs]
        self.average = sum(self.lengths) / max(1, len(self.docs)) or 1
        frequency = Counter(term for doc in self.docs for term in doc)
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in frequency.items()}

    def scores(self, query, pool):
        """{index: score} for the pool items sharing at least one term with the query."""
        terms = set(tokens(query)) & set(self.idf)
        out = {}
        for i in pool:
            doc, total = self.docs[i], 0.0
            for term in terms:
                f = doc.get(term)
                if f:
                    total += self.idf[term] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.average))
            if total:
                out[i] = total
        return out


def rrf(*rankings, k=RRF_K):
    """Indices ordered by Reciprocal Rank Fusion of the rankings."""
    fused = Counter()
    for ranking in rankings:
        for rank, i in enumerate(ranking):
            fused[i] += 1 / (k + rank + 1)
    return [i for i, _ in fused.most_common()]


def fuse(query, texts, dense, pool, bm25=None, metadata_rank=None, size=POOL):
    """(fused indices best first, dense ranking, {index: BM25 score}) — step 3 of the module docstring."""
    dense_rank = sorted(pool, key=lambda i: -float(dense[i]))[:DEPTH] if any(dense[i] for i in pool) else []
    lexical = (bm25 or BM25(texts)).scores(query, pool)
    lexical_rank = sorted(lexical, key=lambda i: -lexical[i])[:DEPTH]
    allowed = set(pool)
    fused = rrf(dense_rank, lexical_rank, [i for i in metadata_rank or () if i in allowed][:DEPTH])[:size]
    return fused, dense_rank, lexical


def order_reranked(query, texts, pairs, task, *, recall_rankings=()):
    """Dialogue examples containing the complete long user turn retain lexical relevance.

    A question-answer reranker can otherwise prefer a different conversational topic.
    This changes ordering only; callers still apply their normal score/permission gates.
    """
    pairs = list(pairs)
    semantic_order = sorted(pairs, key=lambda pair: -pair[1])
    positions = {index: rank for rank, (index, _) in enumerate(semantic_order)}
    if task == 'examples':
        allowed = set(positions)
        recalls = [[index for index in ranking if index in allowed] for ranking in recall_rankings]
        recalls = [ranking for ranking in recalls if ranking]
        if len(recalls) >= 2:
            # Dialogue matching needs recall agreement as well as QA relevance. Fuse ranks,
            # not incompatible score scales; the caller still gates on the raw rerank score.
            positions = {index: rank for rank, index in enumerate(rrf(*recalls, [i for i, _ in semantic_order]))}
    literal = re.sub(r'\s+', '', query.casefold()) if task == 'examples' else ''
    def key(pair):
        index, score = pair
        exact = len(literal) >= 12 and literal in re.sub(r'\s+', '', texts[index].casefold())
        return not exact, positions[index]
    return sorted(pairs, key=key)


def search(query, texts, dense, *, pool=None, bm25=None, limit=5, min_score=None, fallback_floor=None,
           rerank_texts=None, metadata_rank=None, label='', keep=None, directory=None, task='memory', size=POOL):
    """[(index, rerank score or None)] of the texts best matching query, best first.

    dense: cosine similarity to the query per index (array or dict covering the pool; callers use their stored vectors);
    pool: indices the metadata filter allows (None = all); bm25: a prebuilt BM25 over texts (cached by callers);
    rerank_texts: what the reranker reads per index when it differs from texts (e.g. with context);
    metadata_rank: indices ordered by a metadata match (e.g. scene label probability), fused as a third ranking;
    label: names the library in the trace that the next generation record keeps (drain_trace);
    keep: keep(index, rerank score or None without the reranker) -> bool, used instead of min_score / fallback_floor
    when one number is not enough (long-term memory: the reranker misses one-step links such as  → )."""
    pool = list(range(len(texts))) if pool is None else list(pool)
    if not pool or not query.strip():
        return []
    fused, dense_rank, lexical = fuse(query, texts, dense, pool, bm25, metadata_rank, size)
    try:
        from retrieval_core import rerank
        scores = rerank(query[:1000], [(rerank_texts or texts)[i][:1000] for i in fused], directory, task)
    except Exception:  # persona_store.Unavailable or an import problem: keep the fused order
        kept = [(i, None) for i in fused if (keep(i, None) if keep else
                                             fallback_floor is None or float(dense[i]) >= fallback_floor or
                                             (not dense_rank and lexical.get(i, 0) > 0))]
        _record(label, query, len(pool), kept[:limit], len(fused) - len(kept), False, texts)
        return kept[:limit]
    ranked = order_reranked(query, rerank_texts or texts, zip(fused, scores), task,
        recall_rankings=(dense_rank, sorted(lexical, key=lambda i: -lexical[i]),
                         metadata_rank or ()))
    kept = [(i, s) for i, s in ranked if (keep(i, s) if keep else min_score is None or s >= min_score)]
    _record(label, query, len(pool), kept[:limit], len(ranked) - len(kept), True, texts)
    return kept[:limit]


def cached_vectors(path, texts, directory=None):
    """{id: vector} for {id: text}, cached in the SQLite file `path`; new or edited texts are embedded, gone ids
    dropped (same layout as sticker_library.card_vectors).

    2026-10-26: one vector space per model signature (table space_vectors). A model switch embeds the texts once
    in the new space instead of dropping the cache, switching back finds the earlier vectors, and vectors of another
    model or dimension are never compared. KEEP_SPACES spaces are kept; the old single-space tables are migrated."""
    import sqlite3
    import time as clock
    from contextlib import closing
    from retrieval_api import signature, embed
    directory = directory or (Path(path).parent if (Path(path).parent / 'friend.json').is_file() else None)
    backend = signature(directory)
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS space_vectors(signature TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL,'
                   ' vector BLOB NOT NULL, PRIMARY KEY(signature, id))')
        db.execute('CREATE TABLE IF NOT EXISTS vector_spaces(signature TEXT PRIMARY KEY, used_at REAL NOT NULL)')
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'vectors', 'vector_backend'} <= names:  # the layout before 2026-10-26: one space, its signature beside it
            old = db.execute('SELECT signature FROM vector_backend').fetchone()
            if old:
                db.execute('INSERT OR IGNORE INTO space_vectors SELECT ?, id, text, vector FROM vectors', (old[0],))
                db.execute('INSERT OR IGNORE INTO vector_spaces VALUES(?, ?)', (old[0], clock.time() - 1))
            db.execute('DROP TABLE vectors')
            db.execute('DROP TABLE vector_backend')
        db.execute('INSERT INTO vector_spaces VALUES(?, ?) ON CONFLICT(signature) DO UPDATE SET used_at=excluded.used_at',
                   (backend, clock.time()))
        for (gone,) in db.execute('SELECT signature FROM vector_spaces ORDER BY used_at DESC').fetchall()[KEEP_SPACES:]:
            db.execute('DELETE FROM space_vectors WHERE signature=?', (gone,))
            db.execute('DELETE FROM vector_spaces WHERE signature=?', (gone,))
        stored = {key: (text, blob) for key, text, blob in
                  db.execute('SELECT id, text, vector FROM space_vectors WHERE signature=?', (backend,))}
        stale = [key for key, text in texts.items() if stored.get(key, ('',))[0] != text]
        if stale:
            for key, vector in zip(stale, embed([texts[key] for key in stale], directory)):
                stored[key] = (texts[key], struct.pack(f'{len(vector)}f', *vector))
                db.execute('INSERT OR REPLACE INTO space_vectors VALUES(?,?,?,?)', (backend, key, *stored[key]))
        db.executemany('DELETE FROM space_vectors WHERE signature=? AND id=?',
                       [(backend, key) for key in set(stored) - set(texts)])
        db.commit()
    return {key: struct.unpack(f'{len(stored[key][1]) // 4}f', stored[key][1]) for key in texts}


KEEP_SPACES = 2


# Gated recall for long-term memory and the topic library (2026-10-10/11). Sample memory entries: the reranker scored
# related lines 0.0–0.42 ( →  0.000), BGE-M3 cosine put short noise high too ( →
# badminton 0.66). Replayed real chat: each signal alone let through noise ( → medium, rerank 0.148
# on the shared word ;  → high, cosine 0.625). So a hit needs two of: rerank >= RECALL_MIN_SCORE,
# cosine >= RECALL_DENSE_FLOOR, a solid shared word (one of the item's keywords, or three characters in a row such as a
# a specific keyword); or rerank >= RECALL_SURE alone.
RECALL_MIN_SCORE = 0.05
RECALL_DENSE_FLOOR = 0.60
RECALL_SURE = 0.5
# 2026-10-26 (benchmark tools/retrieval_benchmark.py, real BGE-M3 + reranker): two narrow extra ways in.
# A keyword the entry was saved with plus moderate similarity: "周杰伦" and "考研" scored cosine .57/.59 and rerank
# .015/.038, which the gate above dropped, while no negative query carried an entry keyword. Another language:
# "National Day holiday" against the entry about 国庆 scored rerank .275 and cosine .49; negatives peaked at
# rerank .215 with cosine .42. Only across languages: within Chinese, "在干嘛呢" scored .28–.34 against entries
# that merely start with 在.
KEYWORD_RERANK, KEYWORD_DENSE = 0.01, 0.55
CROSS_RERANK, CROSS_DENSE = 0.25, 0.45
RECALL_POOL = 12  # fused candidates per line sent to the reranker (30 before)
DUP_COSINE = 0.9  # two hits this close are the same fact retold: the second is left out (MMR)


def gated_recall(queries, items, vectors, label, limit, directory=None, task='memory'):
    """[index into items] best first: items = [(id, text, keywords)] that two of three signals tie to one of the query
    lines. Each line is searched on its own (joined, an unrelated line halved the match of the one that mattered,
    0.104 → 0.043). vectors({id: text}) -> {id: vector}; without the embedder only the reranker can let a hit in.

    2026-10-26: lines with nothing to search for ("好的", "在吗", "OK") are skipped; the lines' candidates are
    reranked in one batch through retrieval_core (cached, so the same line against the same entry is scored once);
    a second hit that retells the first (cosine >= DUP_COSINE) is left out."""
    import retrieval_core
    texts = [text + ' ' + ' '.join(keywords) for _, text, keywords in items]
    solid_words = [[k for k in keywords if len(k) > 1] for _, _, keywords in items]
    lines = [line for line in queries if retrieval_core.informative(line)]
    retrieval_core.note('rerank_skipped', len(queries) - len(lines))
    if not lines or not items:
        return []
    bm25 = BM25(texts)
    try:
        from retrieval_api import embed
        stored = vectors({key: text for key, text, _ in items})
        targets = embed([line[:500] for line in lines], directory, is_query=True)
    except Exception:
        stored, targets = None, [None] * len(lines)
    plans = []
    for line, target in zip(lines, targets):
        dense = [sum(a * b for a, b in zip(stored[key], target)) for key, _, _ in items] if stored else [0.0] * len(items)
        fused, _, lexical = fuse(line, texts, dense, list(range(len(items))), bm25, size=RECALL_POOL)
        plans.append((line, dense, fused, lexical))
    try:
        scored = retrieval_core.rerank_groups([(line[:1000], [texts[i][:1000] for i in fused])
                                               for line, _, fused, _ in plans if fused], directory, task)
        scored_iter = iter(scored)
        scored = [next(scored_iter) if fused else [] for _, _, fused, _ in plans]
    except Exception:  # persona_store.Unavailable or an import problem: the signals without the reranker
        scored = [[None] * len(fused) for _, _, fused, _ in plans]
    best = {}
    for (line, dense, fused, lexical), scores in zip(plans, scored):
        runs = [part[i:i + 3] for part in re.findall('[㐀-鿿]{3,}', line) for i in range(len(part) - 2)]

        def keep(i, score):
            if score is not None and score >= RECALL_SURE:
                return True
            solid = any(k in line for k in solid_words[i]) or any(run in items[i][1] for run in runs)
            if score is None and not stored:
                return solid and bool(lexical.get(i, 0))
            if (score is not None and score >= RECALL_MIN_SCORE) + (dense[i] >= RECALL_DENSE_FLOOR) + solid >= 2:
                return True
            if score is None:
                return False
            return (solid and score >= KEYWORD_RERANK and dense[i] >= KEYWORD_DENSE or
                    score >= CROSS_RERANK and dense[i] >= CROSS_DENSE and
                    retrieval_core.language(line) != retrieval_core.language(items[i][1]))
        kept = [(i, s) for i, s in sorted(zip(fused, scores), key=lambda pair: -(pair[1] or 0)) if keep(i, s)]
        _record(label, line, len(items), kept[:limit], len(fused) - len(kept),
                bool(scores) and scores[0] is not None, texts)
        for i, score in kept[:limit]:
            best[i] = max(best.get(i, -1.0), -1.0 if score is None else score)
    order = sorted(best, key=lambda i: -best[i])
    unique = retrieval_core.mmr_unique(order, {i: stored[items[i][0]] for i in order} if stored else None,
                                       [item[1] for item in items], DUP_COSINE)
    return unique[:limit]
