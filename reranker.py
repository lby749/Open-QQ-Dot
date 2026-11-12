"""Local bge-reranker-v2-m3 cross-encoder for hybrid_search (2026-10-10 user approved the download).

Scores (query, passage) pairs 0..1 (sigmoid of the logit). Loaded on first use, verified against
model_source.json once per process, unloaded after IDLE_UNLOAD_SECONDS like the BGE-M3 embedder."""
import gc
import hashlib
import json
import threading
from pathlib import Path

from persona_store import Unavailable

MODEL = 'BAAI/bge-reranker-v2-m3'
REVISION = '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
DIRECTORY = Path(__file__).resolve().parent / 'data' / 'embedding_models' / 'bge-reranker-v2-m3' / REVISION
MAX_LENGTH = 512
BATCH_SIZE = 16
IDLE_UNLOAD_SECONDS = 300
_lock = threading.Lock()
_state = {'model': None, 'tokenizer': None, 'torch': None, 'verified': False, 'timer': None}


def _verify():
    provenance = json.loads((DIRECTORY / 'model_source.json').read_text('utf-8'))
    if provenance['repo_id'] != MODEL or provenance['revision'] != REVISION:
        raise Unavailable('local reranker revision differs from the frozen revision')
    for name, expected in provenance['files'].items():
        path = DIRECTORY / name
        if path.resolve().parent != DIRECTORY.resolve() or not path.is_file():
            raise Unavailable('local reranker file is missing or redirects')
        with path.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                raise Unavailable('local reranker file hash differs')


def _drop_model():
    _state['model'] = None


def _place(device):
    from inference_device import dtype
    from transformers import AutoModelForSequenceClassification
    torch = _state['torch']
    _state['model'] = AutoModelForSequenceClassification.from_pretrained(
        DIRECTORY, config=_state['config'], local_files_only=True, trust_remote_code=False,
        torch_dtype=dtype(torch,device)).to(device)
    _state['model'].eval()
    _state['device'] = device


def _load():
    try:
        import torch
        from transformers import AutoConfig, AutoTokenizer
        from inference_device import preference, run_torch
        if not _state['verified']:
            _verify()
            _state['verified'] = True
        _state['tokenizer'] = AutoTokenizer.from_pretrained(DIRECTORY, local_files_only=True, trust_remote_code=False)
        _state['config'] = AutoConfig.from_pretrained(DIRECTORY, local_files_only=True, trust_remote_code=False)
        _state['torch'] = torch
        _state['requested'] = preference(component='rerank')
        run_torch(torch, _state['requested'], _place, _drop_model, component='BGE reranker load')
    except Exception as exc:
        raise Unavailable('local reranker could not load: ' + str(exc)) from exc


def _unload(expected_generation=None):
    with _lock:
        if expected_generation is not None and expected_generation != _state.get('generation',0):
            return
        timer=_state.get('timer')
        if timer is not None:
            timer.cancel()
        _state['timer']=None
        if _state['model'] is None:
            return
        from inference_device import release
        torch, device = _state['torch'], _state.get('device') or str(getattr(_state['model'],'device','cpu'))
        _state.update(model=None, tokenizer=None, timer=None, config=None)
        release(torch, device)


def score(query, passages):
    """Relevance 0..1 of each passage to the query; raises Unavailable when the model cannot run."""
    if not passages:
        return []
    from house_settings import local_models_enabled
    if not local_models_enabled():
        raise Unavailable('本地重排已关闭；使用程序相关度排序')
    with _lock:
        if _state['model'] is None:
            _load()
        if _state['timer'] is not None:
            _state['timer'].cancel()
        _state['timer']=None
        _state['generation']=_state.get('generation',0)+1
        from inference_device import retry_torch
        def compute():
            torch, tokenizer, model = _state['torch'], _state['tokenizer'], _state['model']
            scores = []
            for offset in range(0, len(passages), BATCH_SIZE):
                pairs = [[query, passage] for passage in passages[offset:offset + BATCH_SIZE]]
                inputs = tokenizer(pairs, padding=True, truncation=True, max_length=MAX_LENGTH,
                                   return_tensors='pt').to(_state['device'])
                with torch.inference_mode():
                    logits = model(**inputs).logits.view(-1).float()
                scores += torch.sigmoid(logits).cpu().tolist()
            return scores
        try:
            try:
                scores = compute()
            except Exception as exc:
                def retry(device):
                    _place(device)
                    return compute()
                scores = retry_torch(_state['torch'], _state['requested'], exc, _state['device'],
                                     retry, _drop_model, component='BGE reranker')
        except Exception as exc:
            raise Unavailable('local reranker failed: ' + str(exc)) from exc
        finally:
            if IDLE_UNLOAD_SECONDS is not None:  # None: resident (the chat program, main.keep_retrieval_models)
                _state['timer'] = threading.Timer(IDLE_UNLOAD_SECONDS, _unload, args=(_state['generation'],))
                _state['timer'].daemon = True
                _state['timer'].start()
        return scores
