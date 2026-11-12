"""Cross-check the 9B style tags of public units with BGE-M3 vector neighbours (existing vectors only).

unit vector = mean of the unit's utterance vectors (data/datasets/bge-m3-clean-vectors.sqlite3);
its K nearest labeled units vote tags weighted by cosine similarity. agree=1 when the vote's top 3
share a tag with the 9B tags, 0 when not (review), NULL when the unit has no vectors.
Run:  .venv\\Scripts\\python.exe public_label_check.py"""
import json
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parent
VECTORS = ROOT / 'data' / 'datasets' / 'bge-m3-clean-vectors.sqlite3'
LABELS = ROOT / 'data' / 'datasets' / 'persona_labels.sqlite3'
K = 10
VERSION = 'bge-knn10-v1'


def main():
    labels = sqlite3.connect(LABELS, timeout=30)
    tags = {(s, c, p): json.loads(t) for s, c, p, t in labels.execute(
        'SELECT data_source, conversation_id, speaker_id, tags_json FROM unit_labels')}
    print(f'已标单位 {len(tags)} 个；读取已有向量…', flush=True)
    sums, counts = {}, {}
    source = sqlite3.connect(VECTORS.as_uri() + '?mode=ro', uri=True)
    for i, (s, c, p, blob) in enumerate(source.execute(
            'SELECT data_source, conversation_id, speaker_id, vector FROM vectors'), 1):
        key = (s, c, p)
        if key in tags:
            vector = np.frombuffer(blob, dtype='<f4')
            sums[key] = sums[key] + vector if key in sums else vector.copy()
            counts[key] = counts.get(key, 0) + 1
        if i % 20000 == 0:
            print(f'\r  已读 {i} 条向量', end='', flush=True)
    source.close()
    keys = list(sums)
    matrix = np.stack([sums[k] / counts[k] for k in keys]).astype('float32')
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    print(f'\n有向量的单位 {len(keys)} 个，计算近邻投票…', flush=True)
    vocabulary = sorted({t for v in tags.values() for t in v})
    index = {t: i for i, t in enumerate(vocabulary)}
    onehot = np.zeros((len(keys), len(vocabulary)), dtype='float32')
    for row, key in enumerate(keys):
        for tag in tags[key]:
            onehot[row, index[tag]] = 1
    results, started = [], time.monotonic()
    for start in range(0, len(keys), 1024):
        sims = matrix[start:start + 1024] @ matrix.T
        for offset in range(sims.shape[0]):
            sims[offset, start + offset] = -1  # never vote for itself
        neighbours = np.argpartition(-sims, K, axis=1)[:, :K]
        weights = np.take_along_axis(sims, neighbours, axis=1)
        votes = np.einsum('nk,nkt->nt', weights, onehot[neighbours])
        for offset, row in enumerate(range(start, start + sims.shape[0])):
            top = [vocabulary[i] for i in np.argsort(-votes[offset])[:3] if votes[offset, i] > 0]
            results.append((keys[row], top, int(bool(set(top) & set(tags[keys[row]])))))
        print(f'\r  {len(results)}/{len(keys)}  {time.monotonic() - started:.0f}s', end='', flush=True)
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    labels.execute('CREATE TABLE IF NOT EXISTS unit_checks(data_source TEXT NOT NULL, conversation_id TEXT NOT NULL, '
                   'speaker_id TEXT NOT NULL, knn_tags_json TEXT NOT NULL, agree INTEGER, version TEXT NOT NULL, '
                   'updated_at TEXT NOT NULL, PRIMARY KEY(data_source, conversation_id, speaker_id))')
    labels.execute('DELETE FROM unit_checks')
    labels.executemany('INSERT INTO unit_checks VALUES(?,?,?,?,?,?,?)',
                       [(*key, json.dumps(top, ensure_ascii=False), agree, VERSION, now) for key, top, agree in results] +
                       [(*key, '[]', None, VERSION, now) for key in tags if key not in sums])
    labels.commit()
    agree = sum(r[2] for r in results)
    print(f'\n完成：一致 {agree} 个，待复核 {len(results) - agree} 个，无向量未核对 {len(tags) - len(results)} 个。', flush=True)
    labels.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
