"""Offline Transformers encoder/cross-encoder adapter; no downloads or remote model code."""
import gc
import hashlib
import json
from pathlib import Path
import threading

PRESETS = {
    'embedding': {
        'BAAI/bge-small-zh-v1.5': {'dimension': 512, 'pooling': 'cls',
                                   'query_prefix': '为这个句子生成表示以用于检索相关文章：'},
        'BAAI/bge-base-zh-v1.5': {'dimension': 768, 'pooling': 'cls',
                                  'query_prefix': '为这个句子生成表示以用于检索相关文章：'},
        'BAAI/bge-m3': {'dimension': 1024, 'pooling': 'cls'},
        'intfloat/multilingual-e5-small': {'dimension': 384, 'pooling': 'mean',
                                         'query_prefix': 'query: ', 'document_prefix': 'passage: '},
        'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2': {'dimension': 384, 'pooling': 'mean'},
    },
    'rerank': {
        'cross-encoder/ms-marco-MiniLM-L6-v2': {},
        'BAAI/bge-reranker-base': {},
        'BAAI/bge-reranker-v2-m3': {},
        'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1': {},
    },
}
_lock = threading.Lock()
_loaded = {}


def validate(value, kind):
    directory = Path(value.get('model_path') or '')
    if not value.get('model_path') or not directory.is_dir() or not (directory / 'config.json').is_file():
        raise ValueError('请选择已有本地模型目录，目录须包含 config.json；不会自动下载')
    from inference_device import preference
    preference(value.get('device'), 'custom_' + kind)
    if kind == 'embedding' and value.get('pooling', 'mean') not in ('mean', 'cls'):
        raise ValueError('向量池化须为 mean 或 cls')


def signature(value, kind):
    validate(value, kind)
    directory = Path(value['model_path']).resolve()
    files = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(directory.iterdir())
             if p.is_file() and p.suffix in ('.json', '.safetensors', '.bin', '.model', '.txt')]
    return hashlib.sha256(json.dumps([value, files], sort_keys=True).encode()).hexdigest()


def _drop(kind):
    _loaded.pop(kind, None)


def unload(kind=None):
    """Explicitly release cached models on their actual CUDA/MPS/CPU device."""
    from inference_device import release
    with _lock:
        for key in list(_loaded) if kind is None else [kind]:
            if key in _loaded:
                entry=_loaded.pop(key)
                torch,device=entry[-2:]
                del entry
                release(torch,device)


def _load(value, kind, device_override=None):
    from house_settings import local_models_enabled
    if not local_models_enabled():
        raise RuntimeError('本地模型开关已关闭')
    identity = signature(value, kind)
    if kind in _loaded and _loaded[kind][0] == identity and (device_override is None or _loaded[kind][-1] == device_override):
        return _loaded[kind][1:]
    import torch
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
    from inference_device import preference, dtype, run_torch, release
    if kind in _loaded:
        entry=_loaded.pop(kind)
        old_torch,old_device=entry[-2:]
        del entry
        release(old_torch,old_device)
    requested = preference(device_override if device_override is not None else value.get('device'), 'custom_' + kind)
    directory = Path(value['model_path']).resolve()
    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
    constructor = AutoModel if kind == 'embedding' else AutoModelForSequenceClassification
    def construct(device):
        model = constructor.from_pretrained(directory, torch_dtype=dtype(torch,device), local_files_only=True,
                                            trust_remote_code=False).to(device).eval()
        _loaded[kind] = (identity, tokenizer, model, torch, device)
        return tokenizer, model, torch, device
    return run_torch(torch, requested, construct, lambda: _drop(kind), component='local ' + kind + ' load')


def _infer(value, kind, compute):
    from inference_device import preference, retry_torch
    bundle = _load(value, kind)
    try:
        return compute(*bundle)
    except Exception as exc:
        # Drop local references before clearing a failed accelerator allocation.
        torch, current = bundle[-2:]
        bundle = None
        return retry_torch(torch, preference(value.get('device'), 'custom_' + kind), exc, current,
            lambda device: compute(*_load(value,kind,device_override=device)),
            lambda: _drop(kind), component='local ' + kind)


def embed(texts, value, is_query=False):
    prefix = value.get('query_prefix' if is_query else 'document_prefix', '')
    def compute(tokenizer, model, torch, device):
        output = []
        for start in range(0, len(texts), 8):
            inputs = tokenizer([prefix + text for text in texts[start:start + 8]], padding=True,
                               truncation=True, max_length=min(512, int(getattr(model.config, 'max_position_embeddings', 512))), return_tensors='pt').to(device)
            with torch.inference_mode():
                hidden = model(**inputs).last_hidden_state
                if value.get('pooling', 'mean') == 'cls':
                    vectors = hidden[:, 0]
                else:
                    mask = inputs['attention_mask'].unsqueeze(-1).to(hidden.dtype)
                    vectors = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                vectors = torch.nn.functional.normalize(vectors.float(), p=2, dim=1)
            if vectors.shape[1] != value['dimension']:
                raise ValueError('本地模型实际维度与设置不符，请修改维度并重建缓存')
            output.extend(vectors.cpu().tolist())
        return output
    with _lock:
        return _infer(value,'embedding',compute)


def rerank(query, passages, value):
    def compute(tokenizer, model, torch, device):
        output = []
        for start in range(0, len(passages), 8):
            pairs = [[query, passage] for passage in passages[start:start + 8]]
            inputs = tokenizer(pairs, padding=True, truncation=True, max_length=min(512, int(getattr(model.config, 'max_position_embeddings', 512))), return_tensors='pt').to(device)
            with torch.inference_mode():
                logits = model(**inputs).logits.float()
                if logits.shape[-1] == 1:
                    scores = logits[:, 0].sigmoid()
                elif logits.shape[-1] == 2:
                    scores = logits.softmax(-1)[:, 1]
                else:
                    raise ValueError('本地重排仅支持单分数或二分类 cross-encoder；其他模型请走兼容API')
            output.extend(scores.cpu().tolist())
        return output
    with _lock:
        return _infer(value,'rerank',compute)
