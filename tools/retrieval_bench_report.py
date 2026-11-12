"""Markdown comparison tables from docs/retrieval_bench/<label>-<mode>.json (tools/retrieval_benchmark.py).

    python tools/retrieval_bench_report.py original optimized real
    python tools/retrieval_bench_report.py original-heldout optimized-heldout real"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RANKED = ('recall', 'ndcg', 'mrr', 'precision', 'noise_rate', 'duplicates', 'leaks', 'returned_avg', 'context_chars',
          'p50_ms', 'p95_ms', 'embed_texts', 'rerank_pairs')
DEDUP = ('precision', 'recall', 'f1', 'p50_ms', 'p95_ms', 'embed_texts', 'rerank_pairs')


def load(label, mode):
    return json.loads((ROOT / 'docs' / 'retrieval_bench' / f'{label}-{mode}.json').read_text('utf-8'))


def cell(value):
    if value is None:
        return '—'
    return f'{value:.3f}'.rstrip('0').rstrip('.') if isinstance(value, float) else str(value)


def main(before, after, mode):
    a, b = load(before, mode), load(after, mode)
    for name, old in a['modules'].items():
        new = b['modules'].get(name)
        if not new:
            continue
        keys = DEDUP if name == 'dedup' else RANKED
        print(f'\n**{name}**\n')
        print('| 指标 | ' + ' | '.join(keys) + ' |')
        print('| --- |' + ' --- |' * len(keys))
        print(f'| {before} | ' + ' | '.join(cell(old.get(k)) for k in keys) + ' |')
        print(f'| {after} | ' + ' | '.join(cell(new.get(k)) for k in keys) + ' |')
    if 'e2e' in a and 'e2e' in b:
        print('\n**端到端（' + a['e2e']['model'] + f"，每题 {a['e2e']['repeats']} 次）**\n")
        print('| 任务 | 成功率 前 | 成功率 后 | 平均提示 token 前 | 后 | 变化 |')
        print('| --- | --- | --- | --- | --- | --- |')
        for suite in ('memory_qa', 'topic_qa', 'experience_fix', 'tool_choice'):
            x, y = a['e2e'][suite], b['e2e'][suite]
            change = (y['prompt_tokens_avg'] - x['prompt_tokens_avg']) / x['prompt_tokens_avg'] * 100
            print(f"| {suite} | {x['success_rate']:.3f} | {y['success_rate']:.3f} | {x['prompt_tokens_avg']} | "
                  f"{y['prompt_tokens_avg']} | {change:+.1f}% |")
        print(f"| 合计 | {a['e2e']['overall_success']:.3f} | {b['e2e']['overall_success']:.3f} | | | |")


if __name__ == '__main__':
    main(*sys.argv[1:4])
