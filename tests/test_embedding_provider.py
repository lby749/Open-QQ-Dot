from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import embedding_provider as embedding
from persona_store import ScopeMismatch, Unavailable
from persona_types import (DataScope, DimensionMismatch, EmbeddingMetadata, FriendScope,
                           InvalidSource, ModelVersionMismatch, PrivacyView, SourceOrigin, SourceRef)


class EmbeddingContractTests(unittest.TestCase):
    def setUp(self):
        enabled=patch("house_settings.local_models_enabled",return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_one_bounded_worker_privacy_metadata_and_cuda_failure(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        registry = {}
        for friend in ('12345', '23456'):
            directory = root / 'friends' / friend
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': friend}), 'utf-8')
            registry[friend] = {'qq_id': friend, 'directory': str(directory)}
        (root / 'friends.json').write_text(json.dumps({'friends': registry}), 'utf-8')
        scope = FriendScope.load('12345', project_root=root)
        other = FriendScope.load('23456', project_root=root)
        ref = SourceRef(dataset_id='private-fixture', import_id='fixture', record_id='message-1',
            message_seq=1, speaker_id='12345', origin=SourceOrigin.REAL_RELATIONSHIP,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL, friend_id='12345',
            event_time=None, recorded_at='2026-10-06T00:00:00+00:00', model_analysis_allowed=False)
        encoded = []
        def encode(texts):
            encoded.append(tuple(texts))
            return [[1.0] + [0.0]*(embedding.DIMENSION-1) for text in texts]
        backend = SimpleNamespace(device='cuda:0', dtype='float16', encode=encode)
        provider = embedding.EmbeddingProvider(scope)
        with patch.object(embedding, '_WORKER', None), patch.object(embedding, '_load_backend', return_value=backend) as load:
            for texts in ([], [''], ['text']*257):
                with self.assertRaises(InvalidSource): provider.embed_texts(texts, source_refs=[ref])
            with self.assertRaises(InvalidSource): provider.embed_query('hello', source_refs=[])
            with self.assertRaises(ScopeMismatch): provider.embed_query('hello', source_refs=[replace(ref, friend_id='23456')])
            bad_dimension = EmbeddingMetadata(model=embedding.MODEL, dimension=3, version=embedding.VERSION)
            with self.assertRaises(DimensionMismatch): provider.embed_query('hello', source_refs=[ref], expected_metadata=bad_dimension)
            bad_version = replace(embedding.METADATA, version='previous')
            with self.assertRaises(ModelVersionMismatch): provider.embed_query('hello', source_refs=[ref], expected_metadata=bad_version)
            self.assertEqual(load.call_count, 0)
            # Simultaneous first requests from separate friend providers must share one loader.
            other_provider = embedding.EmbeddingProvider(other)
            other_ref = replace(ref, friend_id='23456', speaker_id='23456')
            with ThreadPoolExecutor(max_workers=2) as pool:
                requests = [pool.submit(provider.embed_query, 'one-current-query', source_refs=[ref]),
                            pool.submit(other_provider.embed_query, 'other-current-query', source_refs=[other_ref])]
                self.assertTrue(all(len(request.result().vectors) == 1 for request in requests))
            self.assertEqual(load.call_count, 1)
            batch = provider.embed_texts([f'index-{i}' for i in range(9)], source_refs=[ref])
            self.assertEqual(len(batch.vectors), 9)
            self.assertEqual([len(items) for items in encoded[2:]], [4, 4, 1])
            before = len(encoded)
            query = provider.embed_query('current-only', source_refs=[ref])
            self.assertEqual(encoded[before:], [('current-only',)])
            self.assertEqual(query.metadata, embedding.METADATA)
            self.assertEqual(provider.stats()['load_count'], 1)
            self.assertEqual(provider.stats()['device'], 'cuda:0')
            self.assertEqual(provider.stats()['dtype'], 'float16')
            self.assertEqual(provider.stats()['max_gpu_batch'], 4)
            quota_path = root / 'docs/quota_state.json'
            quota_path.parent.mkdir()
            before = len(encoded)
            for state in ('PAUSED_QUOTA', 'PAUSED_WEEKLY'):
                quota_path.write_text(json.dumps({'status': state}), 'utf-8')
                with self.assertRaises(Unavailable): provider.embed_query('paused', source_refs=[ref])
                # This simulates a request authorized before pause but still waiting for dispatch.
                with self.assertRaises(Unavailable):
                    embedding._WORKER.submit(('queued-before-pause',), 0, lambda: provider._validate([ref]))
            self.assertEqual(len(encoded), before)
            quota_path.unlink()
            backend.encode = lambda texts: (_ for _ in ()).throw(Unavailable('CUDA out of memory'))
            with self.assertRaises(Unavailable): provider.embed_query('not-fake-on-error', source_refs=[ref])
            self.assertEqual(load.call_count, 1)
        # A failed CUDA load stays unavailable; no per-request reload/CPU/cloud/hash fallback.
        with patch.object(embedding, '_WORKER', None), patch.object(embedding, '_load_backend', side_effect=Unavailable('no CUDA')) as load:
            for _ in range(2):
                with self.assertRaises(Unavailable): provider.embed_query('same-request', source_refs=[ref])
            self.assertEqual(load.call_count, 1)
            self.assertEqual(provider.stats()['load_count'], 0)
            self.assertEqual(provider.stats()['failures'], 2)
        # A queued request has a bounded wait and is cancelled rather than silently dispatched later.
        worker = object.__new__(embedding._EmbeddingWorker)
        worker.queue = SimpleNamespace(put_nowait=lambda item: None)
        worker.sequence = iter([0])
        with patch.object(embedding, 'Future') as future_type:
            future = future_type.return_value
            future.result.side_effect = FutureTimeoutError()
            with self.assertRaisesRegex(Unavailable, 'timed out'):
                worker.submit(('queued',), 0, lambda: None)
            future.result.assert_called_once_with(timeout=45)
            future.cancel.assert_called_once_with()

    def test_idle_backend_is_unloaded_and_reloaded_on_demand(self):
        import time
        torch = SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: freed.append(1)))
        freed, loads = [], []
        def load(directory):
            loads.append(directory)
            return SimpleNamespace(device='cuda:0', dtype='float16', torch=torch,
                                   encode=lambda texts: [[0.0] * embedding.DIMENSION for _ in texts])
        with patch.object(embedding, 'IDLE_UNLOAD_SECONDS', 0.05), patch.object(embedding, '_load_backend', load):
            worker = embedding._EmbeddingWorker(Path('m'))
            worker.submit(('a',), 0, lambda: None)
            self.assertIsNotNone(worker.backend)
            deadline = time.monotonic() + 2
            # The worker clears .backend before it frees memory and counts the unload; wait for the count.
            while worker.metrics['unload_count'] < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(worker.backend)
            self.assertEqual((freed, worker.metrics['unload_count']), ([1], 1))
            worker.submit(('b',), 0, lambda: None)
            self.assertEqual((len(loads), worker.metrics['load_count']), (2, 2))


def run_cuda_benchmark():
    import math
    import psutil
    import statistics
    import threading
    import time
    import torch
    model_dir = Path(__file__).resolve().parents[1] / 'data/embedding_models/bge-m3' / embedding.REVISION
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stop = threading.Event()
    def sample_ram():
        while not stop.wait(.05):
            peak[0] = max(peak[0], process.memory_info().rss)
    monitor = threading.Thread(target=sample_ram, daemon=True)
    monitor.start()
    before_free, total = torch.cuda.mem_get_info()
    before_rss = process.memory_info().rss
    torch.cuda.reset_peak_memory_stats()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        directory = root / 'friends/12345'
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text(json.dumps({'qq_id':'12345'}), 'utf-8')
        (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {'qq_id': '12345',
            'directory': str(directory)}}}), 'utf-8')
        provider = embedding.EmbeddingProvider(FriendScope.load('12345', project_root=root), model_dir=model_dir)
        ref = SourceRef(dataset_id='authored-local-benchmark', import_id='manual-fixture', record_id='fixture',
            message_seq=None, speaker_id='fixture-author', origin=SourceOrigin.MANUAL,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL, friend_id=None,
            event_time=None, recorded_at='2026-10-06T00:00:00+00:00', license_id='authored-benchmark',
            model_analysis_allowed=True)
        started = time.perf_counter()
        provider.embed_query('你好，今天过得怎么样？', source_refs=[ref])
        cold_ms = (time.perf_counter()-started)*1000
        docs = ['今天心情有点低落，想找个人聊聊。', '周末去公园跑步，天气很好。',
                '我最近在学吉他，练习和弦。', '今晚早点睡，明天还要早起。',
                '这道数学题怎么解，能一步一步讲吗？', '电脑的无线网络连接不上，需要检查设置。',
                '我想做番茄炒蛋，先准备鸡蛋和西红柿。', '刚收到考试通过的消息，开心得想庆祝。']
        started = time.perf_counter()
        corpus = provider.embed_texts(docs, source_refs=[ref])
        batch_ms = (time.perf_counter()-started)*1000
        queries = [('有点难过，想和你说说话', 0), ('你能教我弹吉他吗', 2), ('这题不会，麻烦讲一下解法', 4)]
        rankings, elapsed = [], []
        before = provider.stats()
        for query, expected in queries:
            started = time.perf_counter()
            vector = provider.embed_query(query, source_refs=[ref]).vectors[0]
            elapsed.append((time.perf_counter()-started)*1000)
            scores = [sum(a*b for a,b in zip(vector, row)) for row in corpus.vectors]
            ranked = sorted(range(len(scores)), key=lambda index:scores[index], reverse=True)
            rankings.append({'query':query, 'expected':expected, 'top1':ranked[0], 'top1_score':scores[ranked[0]]})
        for _ in range(10):
            started = time.perf_counter()
            provider.embed_query('有点难过，想和你说说话', source_refs=[ref])
            elapsed.append((time.perf_counter()-started)*1000)
        after = provider.stats()
        assert after['batch_calls'] == before['batch_calls']
        assert after['encoded_texts']-before['encoded_texts'] == len(elapsed)
        assert after['load_count'] == 1
        assert all(item['expected']==item['top1'] for item in rankings)
        assert all(abs(sum(component*component for component in row)-1) < 1e-4 for row in corpus.vectors)
        stop.set()
        monitor.join()
        free, _ = torch.cuda.mem_get_info()
        result = {'torch':torch.__version__, 'cuda':torch.version.cuda, 'gpu':torch.cuda.get_device_name(),
            'capability':torch.cuda.get_device_capability(), 'model_dir':str(model_dir), 'stats':after,
            'cold_first_query_ms':cold_ms, 'warm_query_p50_ms':statistics.median(elapsed),
            'warm_query_p95_ms':sorted(elapsed)[math.ceil(.95*len(elapsed))-1], 'warm_queries':len(elapsed),
            'batch_texts':len(docs), 'batch_ms':batch_ms, 'batch_texts_per_second':len(docs)/(batch_ms/1000),
            'ram_rss_before_bytes':before_rss, 'ram_rss_after_bytes':process.memory_info().rss,
            'ram_peak_rss_bytes':peak[0], 'vram_total_bytes':total, 'vram_free_before_bytes':before_free,
            'vram_free_after_bytes':free, 'torch_allocated_bytes':torch.cuda.memory_allocated(),
            'torch_reserved_bytes':torch.cuda.memory_reserved(), 'torch_peak_allocated_bytes':torch.cuda.max_memory_allocated(),
            'semantic_queries':rankings, 'semantic_hits':sum(item['top1']==item['expected'] for item in rankings),
            'query_phase_reencoded_corpus':False}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    import sys
    if '--benchmark' in sys.argv:
        run_cuda_benchmark()
    else:
        unittest.main()
