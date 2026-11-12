"""Local, no-label ablation on existing per-speaker dialogue/vector libraries.

This measures known-item recovery, not human semantic relevance or persona selection.
Only hashes and ranks leave the private input directory; chat text is never exported.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, action='append', required=True)
    parser.add_argument('--models-root', type=Path, required=True)
    parser.add_argument('--queries', type=int, default=32)
    parser.add_argument('--seed', type=int, default=20261110)
    parser.add_argument('--exclude-results', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    excluded = {row['query_hash'] for row in json.loads(args.exclude_results.read_text('utf-8'))['rows']} if args.exclude_results else set()
    home = Path(tempfile.mkdtemp(prefix='openqq-private-retrieval-'))
    os.environ['QQ_BRIDGE_HOME'] = str(home)
    (home/'house.local.json').write_text('{"local_models_enabled":true}', 'utf-8')
    friend = home/'friends/benchmark'
    friend.mkdir(parents=True)
    def location(name):
        parent = args.models_root/name
        return str(next(p for p in [parent, *sorted(parent.glob('*'))] if (p/'config.json').is_file()))
    config = {'retrieval_api': {
        'embedding': {'enabled': True, 'mode': 'local', 'model': 'BAAI/bge-m3', 'model_path': location('bge-m3'),
                      'dimension': 1024, 'pooling': 'cls', 'device': 'auto'},
        'rerank': {'enabled': True, 'mode': 'local', 'model': 'BAAI/bge-reranker-v2-m3',
                   'model_path': location('bge-reranker-v2-m3'), 'device': 'auto'}}}
    (friend/'friend.json').write_text(json.dumps(config), 'utf-8')
    import numpy as np
    import torch
    import psutil
    import retrieval_core as core
    import hybrid_search as hs
    import persona_vectors as pv
    from embedding_provider import embed_unscoped
    torch.set_num_threads(4)
    started = time.perf_counter()
    plans, libraries, requests = [], [], []
    for number, folder in enumerate(args.library):
        raw = json.loads((folder/'labels.json').read_text('utf-8'))
        # Legacy model labels are deliberately discarded before indexing, filtering and query creation.
        items = [{key: item[key] for key in ('kind', 'context', 'user', 'target')} for item in raw['items']]
        texts = [pv.query_text(item) for item in items]
        with sqlite3.connect((folder/'vectors.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
            blobs = [row[0] for row in db.execute('SELECT vector FROM vectors ORDER BY i')]
        matrix = np.frombuffer(b''.join(blobs), np.float32).reshape(len(blobs), -1)
        if len(matrix) != len(items):
            raise ValueError('向量与对话条目数量不一致，未继续评测')
        indices = [i for i, text in enumerate(texts) if 12 <= len(text) <= 400][:8]
        current = embed_unscoped([texts[i] for i in indices])
        agreement = [core.cosine(matrix[i], vector) for i, vector in zip(indices, current)]
        reused = bool(agreement and min(agreement) >= .995 and matrix.shape[1] == 1024)
        if not reused:
            matrix = np.asarray(embed_unscoped(texts), np.float32)
        bm = hs.BM25(texts)
        pool = [i for i, item in enumerate(items) if item['kind'] == 'reply' and not pv.sensitive(item)]
        candidates = []
        seen = set()
        for i in pool:
            query = ''.join(items[i]['user'][-2:]).strip()[:500]
            if (len(query) >= 12 and core.informative(query) and query not in seen and
                    hashlib.sha256(query.encode()).hexdigest() not in excluded):
                candidates.append((i, query)); seen.add(query)
        selected = random.Random(args.seed).sample(candidates, min(args.queries, len(candidates)))
        vectors = embed_unscoped([query for _, query in selected])
        libraries.append({'library': number+1, 'items': len(items), 'reply_pool': len(pool), 'queries': len(selected),
            'legacy_vector_cache_reused': reused, 'compatibility_samples': len(indices),
            'sampled_vector_min_cosine': min(agreement) if agreement else None})
        for (gold, query), vector in zip(selected, vectors):
            begin = time.perf_counter()
            dense = matrix @ np.asarray(vector, np.float32)
            dr = sorted(pool, key=lambda i: -dense[i])
            dense_ms = (time.perf_counter()-begin)*1000
            begin = time.perf_counter()
            lex = bm.scores(query, pool)
            lr = sorted(lex, key=lambda i: -lex[i])
            fused, _, _ = hs.fuse(query, texts, dense, pool, bm, size=30)
            fuse_ms = (time.perf_counter()-begin)*1000
            union = list(dict.fromkeys(dr[:30]+fused))
            requests.append((query, [texts[i] for i in union]))
            plans.append(dict(library=number+1, gold=gold, query_hash=hashlib.sha256(query.encode()).hexdigest(),
                dr=dr, lr=lr, fused=fused, union=union, matrix=matrix, texts=texts, cosine=dense,
                pool=pool, bm25=bm,
                dense_rank_ms=dense_ms, lexical_fusion_ms=fuse_ms))
        print('LIBRARY_READY', number+1, 'QUERIES', len(selected), 'REUSE', reused, flush=True)
    begin = time.perf_counter()
    scores = core.rerank_groups(requests, friend, 'examples')
    rerank_seconds = time.perf_counter()-begin
    rows = []
    for plan, score in zip(plans, scores):
        byid = dict(zip(plan['union'], score))
        ranked = sorted(plan['fused'], key=lambda i: -byid[i])
        kept = [i for i in ranked if byid[i] >= pv.MIN_SCORE]
        production = [i for i, value in hs.order_reranked(requests[len(rows)][0], plan['texts'],
                      [(i, byid[i]) for i in plan['fused']], 'examples') if value >= pv.MIN_SCORE]
        actual = hs.search(requests[len(rows)][0], plan['texts'], plan['cosine'],
            pool=plan['pool'], bm25=plan['bm25'], limit=5, min_score=pv.MIN_SCORE,
            fallback_floor=pv.DENSE_FLOOR, directory=friend, task='examples')
        if [i for i, _ in actual] != production[:5]:
            raise RuntimeError('生产检索路径与分解结果不一致，未继续汇总')
        full = core.mmr_unique(production, {i: plan['matrix'][i] for i in production}, plan['texts'], .9)[:5]
        rankings = {'single_vector': plan['dr'][:5], 'bm25': plan['lr'][:5], 'vector_bm25_rrf': plan['fused'][:5],
            'vector_rerank': sorted(plan['dr'][:30], key=lambda i: -byid[i])[:5],
            'rrf_rerank': ranked[:5], 'rrf_rerank_threshold': kept[:5],
            'production_exact_preservation': production[:5], 'full_with_mmr': full}
        rows.append({key: plan[key] for key in ('library', 'gold', 'query_hash', 'dense_rank_ms', 'lexical_fusion_ms')} | {'rankings': rankings})
    summary = {name: {**{f'hit_at_{k}': statistics.mean(row['gold'] in row['rankings'][name][:k] for row in rows) for k in (1, 5)},
        'empty_rate': statistics.mean(not row['rankings'][name] for row in rows)} for name in rows[0]['rankings']}
    result = {'objective': 'known_item_recovery_within_bound_speaker_library', 'tag_generation': False,
        'old_labels_used': False, 'queries': len(rows), 'seed': args.seed,
        'production_search_executed': True,
        'excluded_query_hashes': len(excluded), 'libraries': libraries, 'summary': summary,
        'direct_local_embedding_texts': sum(lib['queries']+lib['compatibility_samples']+
            (0 if lib['legacy_vector_cache_reused'] else lib['items']) for lib in libraries),
        'timing': {'total_seconds': time.perf_counter()-started, 'real_rerank_batch_seconds': rerank_seconds,
            'dense_rank_p50_ms': statistics.median(row['dense_rank_ms'] for row in rows),
            'fusion_p50_ms': statistics.median(row['lexical_fusion_ms'] for row in rows)},
        'resources': {'process_rss_mb_at_end': psutil.Process().memory_info().rss/1024**2,
            'cuda_peak_allocated_mb': torch.cuda.max_memory_allocated()/1024**2 if torch.cuda.is_available() else None},
        'limitations': ['查询来自已入库的真实对话，测已知条目恢复；不是人工语义相关性准确率，也不是任务成功率。',
            '每位说话人单独索引，未使用旧标签、场景元数据或新大模型标签。',
            '旧向量兼容性只抽样核对；完整性不同则重建。计时包含公共模型初始化，排序耗时不包含公共嵌入。'],
        'rows': rows, 'model_stats': core.stats()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'summary': summary, 'timing': result['timing']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
