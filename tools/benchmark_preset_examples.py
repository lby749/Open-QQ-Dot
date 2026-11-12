"""Real local-model ablation on the public manually bound preset examples, without LLM labels.

Measures source-turn recovery and a small fixed paraphrase set, not task success or general relevance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PARAPHRASES = {
    'p0001': '外面雨太大了，到家发现鞋袜全湿透了。',
    'p0036': '买回来的午饭汤洒进了外包装里，挺郁闷的。',
    'p0181': '早饭在锅里烧焦了，现在屋子里都是糊味。',
    'p0217': '你推荐的那款橘子汽水软糖终于被我找到了。',
    'p0252': '早晨那只橘色猫又守在卖面包的店门外。',
    'p0397': '葱油饼煎过头发黑了，灶台那边还在冒烟。',
    'p0433': '旧物交换活动选错了报名时间，网页不让修改。',
    'p0468': '薄荷叶子软趴趴的，我本来想立刻多浇点水。',
    'p0613': '那株薄荷挪到室内以后还是没精神。',
    'p0649': '饼干做好忘了从公共厨房带走，像在给邻居送吃的。',
    'p0684': '出了门才想起午餐盒忘在家里的冷藏室了。',
    'p0829': '烤盘边上烧黑的一圈，洗了好久也刷不掉。',
    'p0865': '在会上发言忽然忘词，结束后越想越在意。',
    'p0900': '小区那只橘猫又坐在便利店入口，跟等下班似的。',
    'p1045': '姐姐，我养的薄荷叶子全垂下去了。',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-root', type=Path, required=True, help='Shared application root containing data/embedding_models')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261111)
    parser.add_argument('--tiers', nargs='+', choices=('small', 'base', 'large'), default=['small', 'base', 'large'])
    args = parser.parse_args()
    home = Path(tempfile.mkdtemp(prefix='qqdot-preset-ablation-'))
    os.environ['QQ_BRIDGE_HOME'] = str(home)
    (home / 'house.local.json').write_text('{"local_models_enabled":true}', 'utf-8')
    friend = home / 'friends' / 'synthetic-benchmark'
    friend.mkdir(parents=True)
    import psutil
    import torch
    import hybrid_search as hs
    import model_downloader as md
    import quick_setup_examples as qs
    import retrieval_api as api
    import retrieval_core as core
    import small_models
    torch.set_num_threads(4)
    bank = qs._bank()
    rng = random.Random(args.seed)
    plans = []
    for key in rng.sample(sorted(bank), 30):
        items = bank[key]
        index = rng.choice([i for i, item in enumerate(items) if len(item['user']) >= 12])
        plans.append((key, 'known_turn', index, items[index]['user']))
    for key, items in sorted(bank.items()):
        identifier = items[0]['source_id'].split(':')[0]
        if identifier in PARAPHRASES:
            plans.append((key, 'fixed_paraphrase', 0, PARAPHRASES[identifier]))
    results = {'tag_generation': False, 'manual_preset_routing': True, 'combinations': len(bank),
        'seed': args.seed, 'modes': {}, 'limitations': [
            '已知消息恢复和15条固定人工改写不能当作开放域语义准确率或实际任务成功率。',
            '每条查询只搜索手动选中的12个回复示例，不跨好友或跨1080种组合挑选人设。',
            '使用真实本地模型和生产rank_texts；权重共享，配置与缓存写入隔离临时目录。',
            '单次进程结束内存和GPU峰值不等于持续运行资源曲线。']}
    for label, tier in [('低占用', 'small'), ('中占用', 'base'), ('高占用', 'large')]:
        if tier not in args.tiers:
            continue
        if not all(md.is_installed(kind, tier, args.models_root) for kind in ('embedding', 'rerank')):
            results['modes'][label] = {'status': 'not_run', 'reason': '对应模型未完整安装'}
            continue
        config = {}
        for kind in ('embedding', 'rerank'):
            item = md.MODEL_CATALOG[kind][tier]
            config[kind] = {'enabled': True, 'mode': 'local', 'model': item['name'],
                            'model_path': str(args.models_root / item['target_dir'])}
            if kind == 'embedding':
                config[kind].update({k: item.get(k, '') for k in
                                    ('dimension', 'pooling', 'query_prefix', 'document_prefix')})
        (friend / 'friend.json').write_text(json.dumps({'retrieval_api': config}), 'utf-8')
        core.reset_stats()
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        began = time.perf_counter()
        rows = []
        for key, kind, gold, query in plans:
            items = bank[key]
            texts = [qs.retrieval_text(item) for item in items]
            start = time.perf_counter()
            vectors = core.doc_cache(friend).get(texts, api.signature(friend), lambda t: api.embed(t, friend))
            q_vec = api.embed([query], friend, True)[0]
            dense = [core.cosine(q_vec, vector) for vector in vectors]
            embedding_ms = (time.perf_counter() - start) * 1000
            dr = sorted(range(len(texts)), key=lambda i: -dense[i])
            bm = hs.BM25(texts)
            lexical = bm.scores(query, range(len(texts)))
            lr = sorted(lexical, key=lambda i: -lexical[i])
            fused, _, _ = hs.fuse(query, texts, dense, range(len(texts)), bm, size=10)
            start = time.perf_counter()
            scores = core.rerank(query, texts, friend, 'examples')
            rerank_ms = (time.perf_counter() - start) * 1000
            reranked = sorted(fused, key=lambda i: -scores[i])
            threshold = [i for i in reranked if scores[i] >= .1]
            exact = [i for i, score in hs.order_reranked(query, texts, [(i, scores[i]) for i in fused], 'examples') if score >= .1]
            style_order = [i for i, _score in hs.order_reranked(query, texts, [(i, scores[i]) for i in fused], 'examples')]
            consensus = [i for i, _score in hs.order_reranked(query, texts, [(i, scores[i]) for i in fused],
                'examples', recall_rankings=(dr, lr))]
            full = core.mmr_unique(consensus, dict(enumerate(vectors)), texts, .9)[:5]
            start = time.perf_counter()
            actual = core.rank_texts(query, texts, task='examples', floor=0, limit=5, directory=friend)
            production_ms = (time.perf_counter() - start) * 1000
            if [i for i, _ in actual] != full:
                raise RuntimeError('分解路径与真实生产检索结果不一致')
            rows.append({'preset': key, 'query_kind': kind, 'gold': gold,
                'query_hash': hashlib.sha256(query.encode()).hexdigest(),
                'embedding_ms': embedding_ms, 'rerank_ms': rerank_ms, 'production_warm_ms': production_ms,
                'rankings': {'single_vector': dr[:5], 'bm25': lr[:5], 'vector_bm25_rrf': fused[:5],
                    'vector_rerank': sorted(dr[:10], key=lambda i: -scores[i])[:5],
                    'rrf_rerank': reranked[:5], 'rrf_rerank_threshold': threshold[:5],
                    'exact_turn_preservation': exact[:5], 'bounded_style_references': style_order[:5],
                    'rerank_recall_consensus': consensus[:5], 'full_mmr': full}})
        summary = {}
        for kind in ('known_turn', 'fixed_paraphrase'):
            subset = [row for row in rows if row['query_kind'] == kind]
            summary[kind] = {name: {f'hit_at_{k}': statistics.mean(row['gold'] in row['rankings'][name][:k] for row in subset)
                                   for k in (1, 5)} | {'empty_rate': statistics.mean(not row['rankings'][name] for row in subset)}
                             for name in rows[0]['rankings']}
        results['modes'][label] = {'status': 'completed', 'queries': len(rows), 'summary': summary,
            'seconds': time.perf_counter() - began, 'model_stats': core.stats(),
            'models': {k: v['model'] for k, v in config.items()},
            'production_warm_p50_ms': statistics.median(r['production_warm_ms'] for r in rows),
            'production_warm_p95_ms': sorted(r['production_warm_ms'] for r in rows)[int(len(rows) * .95)],
            'rss_mb_at_end': psutil.Process().memory_info().rss / 1024**2,
            'cuda_peak_mb': torch.cuda.max_memory_allocated() / 1024**2 if torch.cuda.is_available() else None,
            'rows': rows}
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')
        print(label, json.dumps({'summary': summary, 'seconds': results['modes'][label]['seconds']}, ensure_ascii=False), flush=True)
        small_models.unload()
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')


if __name__ == '__main__':
    main()
