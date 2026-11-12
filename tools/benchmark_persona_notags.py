"""Style-proxy retrieval from dialogue fragments to persona instructions, without LLM tagging.

This is not production retrieval within a friend's already bound template. Use
benchmark_persona_examples.py for that objective and report this proxy separately.
"""
import argparse
from collections import Counter
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--models-root', type=Path, required=True)
    parser.add_argument('--queries', type=int, default=120)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='openqq-persona-notags-'))
    os.environ['QQ_BRIDGE_HOME'] = str(home)
    (home/'house.local.json').write_text('{"local_models_enabled":true}', encoding='utf-8')
    friend = home/'friends/benchmark'
    friend.mkdir(parents=True)
    def model_path(name):
        root = args.models_root/name
        return next((p for p in [root, *sorted(root.glob('*'))] if (p/'config.json').exists()), None)
    embed_path, rerank_path = model_path('bge-m3'), model_path('bge-reranker-v2-m3')
    if embed_path is None or rerank_path is None:
        raise RuntimeError('缺少已有向量或重排模型；不自动下载，不伪装为真实模型测试')
    retrieval = {'embedding': {'enabled': True, 'mode': 'local', 'model': 'BAAI/bge-m3',
        'model_path': str(embed_path), 'dimension': 1024, 'pooling': 'cls', 'device': 'auto'},
        'rerank': {'enabled': True, 'mode': 'local', 'model': 'BAAI/bge-reranker-v2-m3',
                  'model_path': str(rerank_path), 'device': 'auto'}}
    (friend/'friend.json').write_text(json.dumps({'retrieval_api': retrieval}), encoding='utf-8')
    files = sorted((args.dataset/'prompts').glob('*.json'))
    texts, names = [], []
    digest = hashlib.sha256()
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode()); digest.update(raw)
        texts.append(json.loads(raw)['system_prompt'][:1000])
        names.append(path.stem)
    if not files:
        raise ValueError('没有人设模板')
    rng = random.Random(20261110)
    indices = sorted(rng.sample(range(len(files)), min(args.queries, len(files))))
    queries = []
    for index in indices:
        path = args.dataset/'dialogues'/files[index].name
        raw = path.read_bytes(); digest.update(path.name.encode()); digest.update(raw)
        dialogue = json.loads(raw)
        if dialogue.get('schema') != 'openqqdot.transcript/1':
            raise ValueError('对话格式不匹配')
        # Held-out conversation snippets, never filenames, metadata or generated labels.
        messages = dialogue['messages']
        sample = [messages[i]['text'] for i in (16, 37, 73, 112, 151, 190, 229, 268)]
        queries.append('\n'.join(sample)[:1000])
    import numpy as np
    import torch
    torch.set_num_threads(4)
    import retrieval_core as core
    print('CORPUS', len(texts), 'QUERIES', len(queries), 'NO_LLM_TAGGING', flush=True)
    start = time.perf_counter()
    vectors = np.asarray(core.embed_documents(texts, friend), dtype=np.float32)
    query_vectors = np.asarray(core.embed_queries(queries, friend), dtype=np.float32)
    model_setup_seconds = time.perf_counter()-start
    rows = []
    for number, (gold, query, vector) in enumerate(zip(indices, queries, query_vectors), 1):
        started = time.perf_counter()
        scores = vectors @ vector / (np.maximum(np.linalg.norm(vectors, axis=1), 1e-9)*max(np.linalg.norm(vector), 1e-9))
        dense = np.argsort(-scores, kind='stable')[:5].tolist()
        dense_ms = (time.perf_counter()-started)*1000
        started = time.perf_counter()
        full = core.rank_texts(query, texts, limit=5, task='examples', pool=30, directory=friend)
        hybrid_ms = (time.perf_counter()-started)*1000
        full_ids = [i for i, _ in full]
        style = names[gold].split('_')[0]
        row = {'source': names[gold], 'dense': dense, 'combined': full_ids,
               'dense_ms': dense_ms, 'combined_ms': hybrid_ms}
        for label, ids in (('dense', dense), ('combined', full_ids)):
            row[label+'_source_at_1'] = bool(ids and ids[0] == gold)
            row[label+'_source_at_5'] = gold in ids
            row[label+'_style_at_1'] = bool(ids and names[ids[0]].split('_')[0] == style)
            row[label+'_style_at_5'] = any(names[i].split('_')[0] == style for i in ids)
        rows.append(row)
        if number % 10 == 0:
            print('PROGRESS', number, '/', len(queries), flush=True)
    summary = {}
    for label in ('dense', 'combined'):
        summary[label] = {key: statistics.mean(float(row[label+'_'+key]) for row in rows)
                          for key in ('source_at_1', 'source_at_5', 'style_at_1', 'style_at_5')}
        latencies = sorted(row[label+'_ms'] for row in rows)
        summary[label].update(p50_ms=statistics.median(latencies), p95_ms=latencies[int((len(latencies)-1)*.95)])
    result = {'status': 'completed', 'objective': 'style_proxy_not_production_example_retrieval',
        'semantic_relevance_not_measured': True, 'corpus': len(texts), 'queries': len(queries),
        'dataset_fingerprint': digest.hexdigest(), 'model_setup_seconds': model_setup_seconds,
        'query_duplicates': sum(n-1 for n in Counter(queries).values()), 'summary': summary,
        'tag_generation': False, 'model_stats': core.stats(), 'rows': rows,
        'limitations': ['来源文件匹配和原生风格族匹配是代理指标，不等于人工语义准确率。',
            '文件名只用于评分，不作为检索输入；相同对话片段可能对应多个合法人设。',
            '索引沿用生产路径的1000字符限制；单向量排序延迟不含双方共同的预计算。',
            '组合路径复用生产rank_texts；须核对model_stats确认没有静默退回关键词。']}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'completed', 'summary': summary}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
