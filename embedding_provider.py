"""Local BGE-M3 dense embeddings through one bounded process worker."""
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import gc
import hashlib
import itertools
import json
from pathlib import Path
from queue import Empty, Full, PriorityQueue
import threading
import time

from persona_store import ScopeMismatch, Unavailable
from persona_types import (AnalysisCallPolicy, AnalysisPurpose, DataScope, EmbeddingBatch,
                           EmbeddingMetadata, FriendScope, InvalidSource, ProviderKind)

MODEL = 'BAAI/bge-m3'
REVISION = '5617a9f61b028005a4858fdac845db406aefb181'
DIMENSION = 1024
MAX_LENGTH = 512
BATCH_SIZE = 4
VERSION = REVISION + ':dense-cls-l2-fp16-max512-v1'
IDLE_UNLOAD_SECONDS = 300  # same as the local 9B; None = resident (the chat program, main.keep_retrieval_models)
METADATA = EmbeddingMetadata(model=MODEL, dimension=DIMENSION, version=VERSION)
_WORKER = None
_WORKER_LOCK = threading.Lock()


class _DenseBackend:
    def __init__(self, directory):
        from inference_device import preference, run_torch
        self.model = None
        self.directory = Path(directory)
        self.requested = preference(component='embedding')
        try:
            import torch
            from transformers import AutoConfig, AutoTokenizer
            self.torch = torch
            provenance = json.loads((self.directory / 'model_source.json').read_text('utf-8'))
            if provenance['repo_id'] != MODEL or provenance['revision'] != REVISION:
                raise Unavailable('local BGE model revision differs from the frozen revision')
            self.tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
            self.config = AutoConfig.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
            if self.config.hidden_size != DIMENSION:
                raise Unavailable('local BGE model dimension differs')
            run_torch(torch, self.requested, self._place, self._drop, component='BGE-M3 load')
        except Exception as exc:
            raise Unavailable('local BGE model could not load: ' + str(exc)) from exc

    def _drop(self):
        self.model = None

    def _place(self, device):
        from inference_device import dtype
        from transformers import AutoModel
        torch = self.torch
        self.model = AutoModel.from_pretrained(
            self.directory, config=self.config, local_files_only=True, trust_remote_code=False,
            torch_dtype=dtype(torch, device)).to(device)
        self.model.eval()
        self.device, self.dtype = device, 'float32' if device == 'cpu' else 'float16'
        return self

    def _encode(self, texts):
        inputs = self.tokenizer(list(texts), padding=True, truncation=True,
                                max_length=MAX_LENGTH, return_tensors='pt').to(self.device)
        with self.torch.inference_mode():
            cls = self.model(**inputs).last_hidden_state[:, 0].float()
            vectors = self.torch.nn.functional.normalize(cls, p=2, dim=1).cpu().tolist()
        return vectors

    def encode(self, texts):
        from inference_device import retry_torch
        try:
            try:
                return self._encode(texts)
            except Exception as exc:
                def retry(device):
                    if self.model is None or self.device != device:
                        self._place(device)
                    return self._encode(texts)
                return retry_torch(self.torch, self.requested, exc, self.device,
                                   retry, self._drop, component='BGE-M3')
        except Exception as exc:
            raise Unavailable('local embedding failed: ' + str(exc)) from exc


def _load_backend(directory):
    return _DenseBackend(directory)


class _EmbeddingWorker:
    def __init__(self, directory):
        self.directory = directory
        self.queue = PriorityQueue(maxsize=16)
        self.sequence = itertools.count()
        self.backend = None
        self.load_error = None
        self.metrics = {'load_count': 0, 'load_ms': None, 'device': None, 'dtype': None,
                        'query_calls': 0, 'batch_calls': 0, 'encoded_texts': 0,
                        'last_query_ms': None, 'last_batch_ms': None, 'max_gpu_batch': 0,
                        'failures': 0, 'unload_count': 0}
        self.thread = threading.Thread(target=self._run, name='bge-m3-local-worker', daemon=True)
        self.thread.start()

    def _unload(self):
        """Free GPU memory after idle time, like the local 9B; the next request reloads."""
        if self.backend is None:
            return
        backend, self.backend = self.backend, None
        torch, device = getattr(backend, 'torch', None), getattr(backend, 'device', None)
        if torch is not None and device is not None:
            from inference_device import release
            release(torch, device)
        self.metrics['unload_count'] += 1

    def _run(self):
        while True:
            try:
                priority, sequence, texts, future, recheck = self.queue.get(timeout=IDLE_UNLOAD_SECONDS)
            except Empty:
                self._unload()
                continue
            if future.set_running_or_notify_cancel():
                try:
                    recheck()  # Friend/provenance must still be valid at dispatch.
                    if self.load_error:
                        raise Unavailable(self.load_error)
                    if self.backend is None:
                        started = time.perf_counter()
                        try:
                            self.backend = _load_backend(self.directory)
                        except Exception as exc:
                            self.load_error = 'local model unavailable: ' + str(exc)[:500]
                            raise Unavailable(self.load_error) from exc
                        self.metrics.update(load_count=self.metrics['load_count'] + 1,
                                            load_ms=(time.perf_counter()-started)*1000,
                                            device=self.backend.device, dtype=self.backend.dtype)
                    started = time.perf_counter()
                    result = EmbeddingBatch(vectors=self.backend.encode(texts), model=MODEL,
                                            dimension=DIMENSION, version=VERSION)
                    self.metrics.update(device=self.backend.device, dtype=self.backend.dtype)
                    elapsed = (time.perf_counter()-started)*1000
                    key = 'query' if priority == 0 else 'batch'
                    self.metrics[key + '_calls'] += 1
                    self.metrics['last_' + key + '_ms'] = elapsed
                    self.metrics['encoded_texts'] += len(texts)
                    self.metrics['max_gpu_batch'] = max(self.metrics['max_gpu_batch'], len(texts))
                    future.set_result(result)
                except Exception as exc:
                    self.metrics['failures'] += 1
                    future.set_exception(exc)
            self.queue.task_done()

    def submit(self, texts, priority, recheck):
        future = Future()
        try:
            self.queue.put_nowait((priority, next(self.sequence), texts, future, recheck))
        except Full:
            raise Unavailable('local embedding worker queue is full') from None
        try:
            return future.result(timeout=45)
        except FutureTimeoutError:
            future.cancel()  # Cancels queued work; an already executing GPU batch cannot be preempted.
            raise Unavailable('local embedding request timed out; CPU retrieval may continue') from None


def _shared_worker(directory):
    from house_settings import local_models_enabled
    if not local_models_enabled():
        raise Unavailable('本地模型已关闭；使用程序检索或已配置的检索 API')
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = _EmbeddingWorker(directory)
        elif _WORKER.directory != directory:
            raise Unavailable('this process already owns a different model directory; reuse its worker')
        return _WORKER


def embed_unscoped(texts, priority=1):
    """Friend-independent local embeddings (persona templates); same worker and frozen model."""
    worker = _shared_worker(Path(__file__).resolve().parent / 'data' / 'embedding_models' / 'bge-m3' / REVISION)
    vectors = []
    for offset in range(0, len(texts), BATCH_SIZE):
        vectors.extend(worker.submit(tuple(texts[offset:offset + BATCH_SIZE]), priority, lambda: None).vectors)
    return vectors


class EmbeddingProvider:
    metadata = METADATA

    def __init__(self, scope, *, model_dir=None):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('EmbeddingProvider requires a verified FriendScope')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.model_dir = Path(model_dir).resolve() if model_dir is not None else (
            scope.project_root / 'data' / 'embedding_models' / 'bge-m3' / REVISION)
        from retrieval_api import config, signature
        self.api_config = config(self.scope.friend_dir, 'embedding')
        if self.api_config.get('enabled'):
            self.metadata = EmbeddingMetadata(model=self.api_config['model'], dimension=self.api_config['dimension'],
                                              version=signature(self.scope.friend_dir))

    def _validate(self, source_refs):
        quota_path = self.scope.project_root / 'docs' / 'quota_state.json'
        if quota_path.is_file():
            try:
                paused = json.loads(quota_path.read_text('utf-8-sig')).get('status') in (
                    'PAUSED_QUOTA', 'PAUSED_WEEKLY')
            except (OSError, ValueError, AttributeError) as exc:
                raise Unavailable('project quota state could not be read') from exc
            if paused:
                raise Unavailable('project quota paused; no new local embedding dispatch')
        FriendScope.load(self.scope.friend_id, project_root=self.scope.project_root,
            friend_dir=self.scope.friend_dir, allowed_pack_versions=self.scope.allowed_pack_versions)
        AnalysisCallPolicy.validate(AnalysisPurpose.EMBEDDING,
            ProviderKind.CLOUD if self.api_config.get('enabled') and self.api_config.get('mode', 'api') == 'api' else ProviderKind.LOCAL, source_refs)
        for ref in source_refs:
            if ref.data_scope == DataScope.PRIVATE_FRIEND and ref.friend_id != self.scope.friend_id:
                raise ScopeMismatch('private embedding source belongs to another friend')
            if ref.data_scope == DataScope.PUBLIC_PACK:
                self.scope.require_pack(ref.pack_id, ref.pack_version)

    def _embed(self, texts, source_refs, priority, expected_metadata):
        if not isinstance(texts, list) or not 1 <= len(texts) <= 256 or any(
                not isinstance(text, str) or not text.strip() for text in texts):
            raise InvalidSource('embedding requires 1..256 nonempty text strings')
        refs = tuple(source_refs)
        self._validate(refs)
        if expected_metadata is not None:
            self.metadata.validate_compatible(expected_metadata)
        if self.api_config.get('enabled'):
            from retrieval_api import embed
            vectors = embed(texts, self.scope.friend_dir, is_query=priority == 0)
            return EmbeddingBatch(vectors=tuple(vectors), model=self.metadata.model,
                                  dimension=self.metadata.dimension, version=self.metadata.version)
        worker = _shared_worker(self.model_dir)
        vectors = []
        for offset in range(0, len(texts), BATCH_SIZE):
            batch = worker.submit(tuple(texts[offset:offset+BATCH_SIZE]), priority,
                                  lambda: self._validate(refs))
            vectors.extend(batch.vectors)
        return EmbeddingBatch(vectors=tuple(vectors), model=MODEL, dimension=DIMENSION, version=VERSION)

    def embed_texts(self, texts, *, source_refs, expected_metadata=None):
        return self._embed(texts, source_refs, 1, expected_metadata)

    def embed_query(self, text, *, source_refs, expected_metadata=None):
        return self._embed([text], source_refs, 0, expected_metadata)

    def stats(self):
        with _WORKER_LOCK:
            if _WORKER is None:
                return {'model': MODEL, 'dimension': DIMENSION, 'version': VERSION, 'load_count': 0}
            return dict(_WORKER.metrics, model=MODEL, dimension=DIMENSION, version=VERSION,
                        queue_size=_WORKER.queue.qsize())
