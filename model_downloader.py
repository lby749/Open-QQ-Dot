"""Model catalog, automated downloader, and installer for local embeddings, rerankers, LLMs and voices."""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

DEFAULT_MIRROR = "https://hf-mirror.com"
FALLBACK_MIRROR = "https://huggingface.co"
DOWNLOAD_ATTEMPTS = 8
RETRY_DELAY = 2  # seconds, multiplied by the attempt number
DOWNLOAD_TIMEOUT = 60  # a slow 390MB shard needs a longer socket timeout than metadata
CHUNK_SIZE = 4 * 1024 * 1024  # fewer read/write round trips than 1MB
PARALLEL_DOWNLOADS = 8  # concurrent small files from a model's `dirs`

# 3-tier model catalog for embedding, rerank, and LLM
MODEL_CATALOG = {
    'embedding': {
        'small': {
            'tier_name': '轻量（FP16 · 约50MB）',
            'name': 'BAAI/bge-small-zh-v1.5',
            'desc': '轻量中文向量模型，约50MB半精度权重、512维，适合日常聊天记忆与表情检索。',
            'repo_id': 'BAAI/bge-small-zh-v1.5',
            'dimension': 512,
            'pooling': 'cls',
            'query_prefix': '为这个句子生成表示以用于检索相关文章：',
            'document_prefix': '',
            'target_dir': 'data/embedding_models/bge-small-zh-v1.5',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'model.safetensors',
            ],
        },
        'base': {
            'tier_name': '均衡（FP16 · 约200MB）',
            'name': 'BAAI/bge-base-zh-v1.5',
            'desc': '中文语义检索黄金平衡点；以 FP16 半精度装配（约220MB，768维），在精度与资源消耗间取得最佳平衡。',
            'repo_id': 'BAAI/bge-base-zh-v1.5',
            'dimension': 768,
            'pooling': 'cls',
            'query_prefix': '为这个句子生成表示以用于检索相关文章：',
            'document_prefix': '',
            'target_dir': 'data/embedding_models/bge-base-zh-v1.5',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'pytorch_model.bin',
                'vocab.txt',
            ],
        },
        'large': {
            'tier_name': '旗舰（多语言 · FP16 · 约1.1GB）',
            'name': 'BAAI/bge-m3',
            'desc': '多语言顶尖检索模型；以 FP16 半精度装配（由原2.2GB压缩至约1.1GB，1024维），支持长文本混合检索。',
            'repo_id': 'BAAI/bge-m3',
            'revision': '5617a9f61b028005a4858fdac845db406aefb181',
            'dimension': 1024,
            'pooling': 'cls',
            'query_prefix': '',
            'document_prefix': '',
            'target_dir': 'data/embedding_models/bge-m3/5617a9f61b028005a4858fdac845db406aefb181',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'pytorch_model.bin',
                'sentencepiece.bpe.model',
            ],
        },
    },
    'rerank': {
        'small': {
            'tier_name': '轻量（多语言 · FP16 · 约240MB）',
            'name': 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1',
            'desc': '支持中文的多语言交叉编码重排；约240MB半精度权重，适合低占用档的聊天检索。',
            'repo_id': 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1',
            'revision': '1427fd652930e4ba29e8149678df786c240d8825',
            'target_dir': 'data/embedding_models/mmarco-mMiniLMv2-L12-H384-v1',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'model.safetensors',
            ],
        },
        'base': {
            'tier_name': '均衡（FP16 · 约530MB）',
            'name': 'BAAI/bge-reranker-base',
            'desc': '中文交叉编码重排模型，约530MB半精度权重，用于对召回候选二次排序。',
            'repo_id': 'BAAI/bge-reranker-base',
            'target_dir': 'data/embedding_models/bge-reranker-base',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'model.safetensors',
            ],
        },
        'large': {
            'tier_name': '旗舰（多语言 · FP16 · 约1.1GB）',
            'name': 'BAAI/bge-reranker-v2-m3',
            'desc': '多语言顶级重排模型；以 FP16 半精度装配（由原2.2GB压缩至约1.1GB），复杂上下文与专业知识二次精排序。',
            'repo_id': 'BAAI/bge-reranker-v2-m3',
            'revision': '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
            'target_dir': 'data/embedding_models/bge-reranker-v2-m3/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
            'files': [
                'config.json',
                'tokenizer.json',
                'tokenizer_config.json',
                'special_tokens_map.json',
                'model.safetensors',
            ],
        },
    },
    'llm': {
        'small': {
            'tier_name': '轻量 (2B · 约1.2GB)',
            'name': 'Qwen3.5-2B-Q4_K_M.gguf',
            'desc': '最新一代 Qwen3.5 轻量架构；混合线性注意力机制，极低资源消耗与极速响应，CPU即可丝滑运行。',
            'repo_id': 'unsloth/Qwen3.5-2B-GGUF',
            'target_dir': 'build/message-classifier-runtime',
            'filename': 'Qwen3.5-2B-Q4_K_M.gguf',
            'files': ['Qwen3.5-2B-Q4_K_M.gguf'],
        },
        'base': {
            'tier_name': 'medium (4B · 2.6GB)',
            'name': 'Qwen3.5-4B-Q4_K_M.gguf',
            'desc': 'Qwen3.5 黄金平衡点模型；4B 越级推理性能与沉浸式拟人对话，显存占用仅约 3GB，兼顾流畅度与高智商。',
            'repo_id': 'unsloth/Qwen3.5-4B-GGUF',
            'target_dir': 'build/message-classifier-runtime',
            'filename': 'Qwen3.5-4B-Q4_K_M.gguf',
            'files': ['Qwen3.5-4B-Q4_K_M.gguf'],
        },
        'large': {
            'tier_name': '旗舰 (9B · 约5.3GB)',
            'name': 'Qwen3.5-9B-Q4_K_M.gguf',
            'desc': 'Qwen3.5 深度逻辑旗舰；更强大的长文本理解与复杂场景决策，推荐 6GB~8GB 显卡体验极速拟真对话。',
            'repo_id': 'unsloth/Qwen3.5-9B-GGUF',
            'target_dir': 'build/message-classifier-runtime',
            'filename': 'Qwen3.5-9B-Q4_K_M.gguf',
            'files': ['Qwen3.5-9B-Q4_K_M.gguf'],
        },
    },
    # Speech for QQ voice messages (voice_tts.py). small/base run on CPU through sherpa-onnx; their ONNX
    # weights are downloaded once (`source_file`) and stored as FP16 (`model_file`, see onnx_weights_to_fp16).
    # large is VoxCPM2 voice cloning, run on the GPU by voice_clone_worker.py in its own Python environment.
    # `dirs` are expanded from the pinned revision's file listing and fetched file by file like `files`.
    'tts': {
        'small': {
            'tier_name': '轻量 (Small · FP16 · 约90MB)',
            'name': 'MeloTTS 中英混读',
            'desc': 'MeloTTS 中文女声，中英混读；以 FP16 半精度装配（由约170MB压缩至约90MB），CPU 合成速度快于实时，适合日常短语音。',
            'repo_id': 'csukuangfj/vits-melo-tts-zh_en',
            'revision': 'a0d5c6a264c0ef92d70d8661d8cc502d79627cd6',
            'target_dir': 'data/tts_models/vits-melo-tts-zh_en',
            'engine': 'vits',
            'source_file': 'model.onnx',
            'model_file': 'model.fp16.onnx',
            'lexicon': 'lexicon.txt',
            'rule_fsts': ['phone.fst', 'date.fst', 'number.fst', 'new_heteronym.fst'],
            'speakers': 1,
            'default_speaker': 0,
            'files': ['model.onnx', 'lexicon.txt', 'tokens.txt', 'phone.fst', 'date.fst',
                      'number.fst', 'new_heteronym.fst', 'LICENSE'],
        },
        'base': {
            'tier_name': 'medium (Base · FP16 · 约260MB)',
            'name': 'Kokoro-82M v1.1-zh',
            'desc': 'Kokoro v1.1 中文版，103 个音色（3~57 号为中文女声、58 号起为中文男声，0~2 号为英文）；'
                    '以 FP16 半精度装配（约260MB），语气自然，CPU 即可运行。',
            'repo_id': 'csukuangfj/kokoro-multi-lang-v1_1',
            'revision': '914313412b607d95400bcd12446233fbd1248801',
            'target_dir': 'data/tts_models/kokoro-multi-lang-v1_1',
            'engine': 'kokoro',
            'source_file': 'model.onnx',
            'model_file': 'model.fp16.onnx',
            'lexicon': 'lexicon-us-en.txt,lexicon-zh.txt',
            'rule_fsts': ['phone-zh.fst', 'date-zh.fst', 'number-zh.fst'],
            'speakers': 103,
            'default_speaker': 3,
            'files': ['model.onnx', 'voices.bin', 'tokens.txt', 'lexicon-us-en.txt', 'lexicon-zh.txt',
                      'phone-zh.fst', 'date-zh.fst', 'number-zh.fst', 'LICENSE'],
            'dirs': ['dict', 'espeak-ng-data'],
        },
        'large': {
            'tier_name': '旗舰 (Large · BF16 · 约4.9GB · 声音克隆)',
            'name': 'VoxCPM2 声音克隆',
            'desc': 'VoxCPM2（2B，Apache-2.0）：上传一段参考语音即可克隆音色，也可用一句话描述设计新声音；'
                    '原生 BF16 半精度（约4.9GB），需 NVIDIA 显卡与独立的克隆运行环境（requirements-voice-clone.txt）。',
            'repo_id': 'openbmb/VoxCPM2',
            'revision': '32279effe8c19989596f05d353d1447f51d9e915',
            'target_dir': 'data/tts_models/VoxCPM2',
            'engine': 'voxcpm',
            'precision': 'bf16',  # native half precision; FP16 would not save space and risks overflow
            'speakers': 1,
            'default_speaker': 0,
            'files': ['config.json', 'model.safetensors', 'audiovae.pth', 'tokenizer.json', 'tokenizer_config.json',
                      'special_tokens_map.json', 'tokenization_voxcpm2.py', 'README.md'],
        },
    },
    # Speech recognition for the friend's QQ voice messages (voice_input.py), CPU through sherpa-onnx.
    # SenseVoice-Small: Mandarin, Cantonese, English, Japanese, Korean; it also reports mood and laughter/crying.
    'asr': {
        'small': {
            'tier_name': '轻量 (Small · INT8 · 约230MB)',
            'name': 'SenseVoice-Small INT8',
            'desc': 'SenseVoice-Small（2025-09 版）INT8 量化：普通话、粤语、英语、日语、韩语，并能听出开心、难过、生气等情绪和笑声；'
                    'CPU 上 10 秒语音约 1 秒内识别完，不消耗模型额度。',
            'repo_id': 'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09',
            'revision': '355f4d4884d8afd08aef04b9007a8556d7b463b2',
            'target_dir': 'data/asr_models/sense-voice-int8-2025-09-09',
            'model_file': 'model.int8.onnx',
            'files': ['model.int8.onnx', 'tokens.txt'],
        },
        'base': {
            'tier_name': 'medium (Base · FP16 · 约470MB)',
            'name': 'SenseVoice-Small FP16',
            'desc': '同一 SenseVoice-Small 模型的全精度版，以 FP16 半精度装配（约930MB 压缩至约470MB）；'
                    '比 INT8 版略准，速度相近，同样只用 CPU。',
            'repo_id': 'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2025-09-09',
            'revision': 'c7c66d1179dc6600570f122e4d70c4e6c3dfa3c8',
            'target_dir': 'data/asr_models/sense-voice-2025-09-09',
            'source_file': 'model.onnx',
            'model_file': 'model.fp16.onnx',
            'files': ['model.onnx', 'tokens.txt'],
        },
    },
}
VOICE_KINDS = ('tts', 'asr')  # sherpa-onnx models: model_source.json lists their files, FP16 stored when converted


def project_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    from house_settings import root as get_root
    return get_root().resolve()


def is_installed(kind: str, tier: str, root: Optional[Path] = None) -> bool:
    """Check if all files of a catalog model exist on disk and have non-zero size."""
    cfg = MODEL_CATALOG.get(kind, {}).get(tier)
    if not cfg:
        return False
    base = project_root(root) / cfg['target_dir']
    if not base.is_dir():
        return False
    # For embedding and rerank, check presence of either FP16 safetensors or bin
    if kind in ('embedding', 'rerank'):
        has_weight = ((base / 'model.safetensors').is_file() and (base / 'model.safetensors').stat().st_size > 0) or \
                     ((base / 'pytorch_model.bin').is_file() and (base / 'pytorch_model.bin').stat().st_size > 0)
        if not has_weight:
            return False
        if not (base / 'config.json').is_file() or (base / 'config.json').stat().st_size == 0:
            return False
        return all((base / name).is_file() and (base / name).stat().st_size > 0
                   for name in cfg['files'] if name not in ('model.safetensors', 'pytorch_model.bin'))
    if kind in VOICE_KINDS:
        # Directory members are only known from the download listing; model_source.json records them.
        source = base / 'model_source.json'
        if not source.is_file():
            return False
        try:
            recorded = json.loads(source.read_text('utf-8')).get('files') or {}
        except (OSError, ValueError):
            return False
        if not all(name in recorded for name in tts_runtime_files(cfg)):
            return False
        return all((base / name).is_file() and (base / name).stat().st_size > 0 for name in recorded)
    for filename in cfg['files']:
        file_path = base / filename
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return False
    return True


def tts_runtime_files(cfg: dict) -> list[str]:
    """Files a voice tier needs at run time: the FP16 model replaces its downloaded source weights."""
    files = [name for name in cfg['files'] if name != cfg.get('source_file')]
    return files + [cfg['model_file']] if cfg.get('source_file') else files


def _convert_to_fp16(base_dir: Path, progress_callback: Optional[Callable] = None):
    """Convert model weights to FP16 safetensors format to halve disk space and VRAM."""
    temporary=base_dir / 'model.fp16.tmp.safetensors'
    try:
        import torch
        from safetensors.torch import save_file, load_file

        safetensors_path = base_dir / 'model.safetensors'
        bin_path = base_dir / 'pytorch_model.bin'

        weights = None
        if safetensors_path.is_file() and safetensors_path.stat().st_size > 0:
            weights = load_file(str(safetensors_path))
            is_fp16 = all(v.dtype == torch.float16 for v in weights.values() if v.is_floating_point())
            if is_fp16:
                return "fp16"
        elif bin_path.is_file() and bin_path.stat().st_size > 0:
            weights = torch.load(str(bin_path), map_location='cpu', weights_only=True)

        if weights:
            if progress_callback:
                progress_callback(95, '正在将模型优化转存为 FP16 半精度（体积减半）…')
            fp16_weights = {}
            for k, v in weights.items():
                if isinstance(v, torch.Tensor) and v.is_floating_point():
                    fp16_weights[k] = v.to(dtype=torch.float16,copy=True)
                else:
                    fp16_weights[k] = v.clone() if isinstance(v,torch.Tensor) else v
            weights=None
            v=None
            save_file(fp16_weights, str(temporary))
            temporary.replace(safetensors_path)
            if bin_path.is_file() and safetensors_path.is_file() and safetensors_path.stat().st_size > 0:
                bin_path.unlink()
            return "fp16"
    except ImportError:
        logging.getLogger(__name__).warning("Optional conversion dependencies missing; original weights retained")
    except Exception:
        logging.getLogger(__name__).exception("Weight conversion failed; original weights retained")
        raise
    finally:
        if temporary.is_file():
            temporary.unlink()
    return "source"


def onnx_weights_to_fp16(source: Path, target: Path) -> None:
    """Store an ONNX model's float32 weights as FP16, halving disk size.

    Each converted weight gets a Cast back to float32 that ONNX Runtime folds when the session loads, so the
    graph still computes in FP32: CPU kernels and speed are unchanged, and no op needs an FP16 implementation."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    model = onnx.load(str(source))
    graph = model.graph
    graph_inputs = {value.name for value in graph.input}
    casts = []
    for weight in graph.initializer:
        if weight.data_type != TensorProto.FLOAT or weight.name in graph_inputs:
            continue
        values = numpy_helper.to_array(weight)
        if values.size < 16 or (values.size and float(np.max(np.abs(values))) > 65504):
            continue  # scalars stay exact; out-of-range weights would overflow FP16
        name = weight.name
        weight.CopyFrom(numpy_helper.from_array(values.astype(np.float16), name + '__fp16'))
        casts.append(helper.make_node('Cast', [name + '__fp16'], [name], to=TensorProto.FLOAT, name=name + '__to_fp32'))
    nodes = casts + list(graph.node)  # casts read only initializers: prepending keeps the graph sorted
    del graph.node[:]
    graph.node.extend(nodes)
    temporary = target.with_name(target.name + '.tmp')
    try:
        onnx.save(model, str(temporary))
        onnx.checker.check_model(str(temporary))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_manifest(model, existing=None):
    """Keep custom binaries and runtime tuning; use a native platform default for new installs."""
    value=dict(existing or {})
    binary="llama-server.exe" if os.name=="nt" else "llama-server"
    value.setdefault("server",binary)
    value.setdefault("cpu_server",value["server"])
    value.setdefault("device","auto")
    value.setdefault("slots",4)
    value.setdefault("ctx",16384)
    value.setdefault("idle_unload_seconds",300)
    value["model"]=model
    return value


def installed_models(kind: str, root: Optional[Path] = None) -> list[dict]:
    """Return list of installed models for a given category (embedding, rerank, llm)."""
    installed = []
    category = MODEL_CATALOG.get(kind, {})
    for tier, cfg in category.items():
        if is_installed(kind, tier, root):
            installed.append({**cfg, 'tier': tier, 'installed': True})
    return installed


RANGE_PARALLEL_THRESHOLD = 24 * 1024 * 1024
RANGE_PARALLEL_WORKERS = 4


class _RangeUnsupported(RuntimeError):
    pass


def _range_probe(url: str, timeout: int) -> tuple[int, bool]:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenQQDot/1.0'}
    req = urllib.request.Request(url, method='HEAD', headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        total = int(response.headers.get('Content-Length') or 0)
        accepts = 'bytes' in (response.headers.get('Accept-Ranges') or '').lower()
    return total, accepts


def download_file_ranges(url: str, dest_path: Path, progress_callback: Optional[Callable] = None,
                         cancel_event: Optional[threading.Event] = None,
                         timeout: int = DOWNLOAD_TIMEOUT) -> bool:
    """Download a large file over several HTTP Range connections.

    Returns False when the server/file does not support ranges; the caller then
    falls back to the single-stream path. Any partial pieces are removed.
    """
    try:
        total, accepts = _range_probe(url, min(timeout, 30))
    except Exception:
        return False
    if total < RANGE_PARALLEL_THRESHOLD or not accepts:
        return False

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(dest_path.suffix + '.part')
    pieces = [part_path.with_name(part_path.name + f'.piece{index}')
              for index in range(RANGE_PARALLEL_WORKERS)]
    for piece in pieces:
        piece.unlink(missing_ok=True)
    piece_size = (total + RANGE_PARALLEL_WORKERS - 1) // RANGE_PARALLEL_WORKERS
    stop = threading.Event()
    lock = threading.Lock()
    downloaded = 0

    def report():
        nonlocal downloaded
        if progress_callback:
            with lock:
                done = sum(path.stat().st_size for path in pieces if path.exists())
            progress_callback(min(done, total), total)

    def worker(index: int):
        nonlocal downloaded
        start = index * piece_size
        end = min(total, (index + 1) * piece_size) - 1
        if start > end:
            return
        length = end - start + 1
        piece = pieces[index]
        for attempt in range(1, 5):
            if stop.is_set():
                raise RuntimeError('下载已被中止')
            if cancel_event and cancel_event.is_set():
                raise RuntimeError('下载已被用户中止')
            have = piece.stat().st_size if piece.is_file() else 0
            if have >= length:
                return
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenQQDot/1.0',
                       'Range': f'bytes={start + have}-{end}'}
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    if response.status != 206:
                        raise _RangeUnsupported('server ignored Range')
                    with piece.open('ab' if have else 'wb') as out:
                        while True:
                            if stop.is_set() or (cancel_event and cancel_event.is_set()):
                                raise RuntimeError('下载已被中止')
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            out.write(chunk)
                            have += len(chunk)
                            with lock:
                                downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(min(downloaded, total), total)
                if piece.stat().st_size != length:
                    raise http.client.IncompleteRead(b'', length - piece.stat().st_size)
                return
            except _RangeUnsupported:
                raise
            except (OSError, http.client.HTTPException):
                if attempt == 4:
                    raise
                time.sleep(RETRY_DELAY * attempt)

    try:
        with ThreadPoolExecutor(max_workers=RANGE_PARALLEL_WORKERS,
                                thread_name_prefix='model-range') as pool:
            futures = [pool.submit(worker, index) for index in range(RANGE_PARALLEL_WORKERS)]
            for future in futures:
                future.result()
        for piece in pieces:
            if not piece.is_file() or piece.stat().st_size != piece_size:
                if piece.stat().st_size != min(piece_size, total - piece_size * pieces.index(piece)):
                    raise RuntimeError('分片下载不完整')
        temporary = part_path.with_name(part_path.name + '.merging')
        with temporary.open('wb') as merged:
            for piece in pieces:
                with piece.open('rb') as source:
                    shutil.copyfileobj(source, merged, CHUNK_SIZE)
        if temporary.stat().st_size != total:
            raise RuntimeError('分片合并大小不正确')
        if dest_path.exists():
            dest_path.unlink()
        temporary.replace(dest_path)
        report()
        return True
    except Exception:
        stop.set()
        for piece in pieces:
            piece.unlink(missing_ok=True)
        part_path.with_name(part_path.name + '.merging').unlink(missing_ok=True)
        if cancel_event and cancel_event.is_set():
            raise RuntimeError('下载已被用户中止')
        return False
    finally:
        for piece in pieces:
            piece.unlink(missing_ok=True)


def download_file(url: str, dest_path: Path, progress_callback: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None, timeout: int = DOWNLOAD_TIMEOUT,
                  resume: bool = False) -> None:
    """Download a single file with stream buffering, progress reporting, and interrupt support.

    With resume=True a .part file left by a dropped connection is continued with an HTTP Range request."""
    if not resume:
        try:
            if download_file_ranges(url, dest_path, progress_callback, cancel_event, timeout):
                return
        except RuntimeError:
            raise
        except Exception:
            pass
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(dest_path.suffix + '.part')
    offset = part_path.stat().st_size if resume and part_path.is_file() else 0

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenQQDot/1.0'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if offset and response.status != 206:
                offset = 0  # the server ignored the range: start over
            length = int(response.headers.get('Content-Length') or 0)
            total = offset + length if length else 0
            downloaded = offset
            chunk_size = CHUNK_SIZE

            with part_path.open('ab' if offset else 'wb') as out_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError('下载已被用户中止')
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)
            if total and downloaded != total:
                raise http.client.IncompleteRead(b'', total - downloaded)
    except (OSError, http.client.HTTPException) as exc:
        # A dropped connection keeps its bytes for the caller's resumed retry; anything else starts clean.
        if not resume or isinstance(exc, urllib.error.HTTPError):
            part_path.unlink(missing_ok=True)
        raise
    except BaseException:
        part_path.unlink(missing_ok=True)  # cancelled: never leave a truncated file behind
        raise

    if part_path.exists():
        if dest_path.exists():
            dest_path.unlink()
        part_path.rename(dest_path)


def _safe_relative(name: str) -> bool:
    """A repository path that stays inside the model directory once joined to it."""
    parts = PurePosixPath(name).parts
    return bool(parts) and not name.startswith('/') and '..' not in parts and ':' not in name and '\\' not in name


def list_repo_files(mirror: str, repo_id: str, revision: str, timeout: int = 30) -> list[str]:
    """File names of a pinned repository revision, with a mirror fallback."""
    data = None
    last_error = None
    for base in dict.fromkeys((mirror.rstrip('/'), FALLBACK_MIRROR)):
        url = f'{base}/api/models/{repo_id}/revision/{revision}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenQQDot/1.0'})
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = json.loads(response.read().decode('utf-8'))
                break
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt == DOWNLOAD_ATTEMPTS:
                    break
                time.sleep(RETRY_DELAY * attempt)
        if data is not None:
            break
    if data is None:
        raise last_error or RuntimeError('无法读取模型文件列表')
    names = [item.get('rfilename') for item in data.get('siblings') or [] if isinstance(item, dict)]
    return [name for name in names if isinstance(name, str) and _safe_relative(name)]


def catalog_files(cfg: dict, mirror: str = DEFAULT_MIRROR) -> list[str]:
    """Explicit files plus every file under the entry's `dirs` at its pinned revision."""
    files = list(cfg['files'])
    dirs = tuple(d.rstrip('/') + '/' for d in cfg.get('dirs') or ())
    if dirs:
        listed = list_repo_files(mirror, cfg['repo_id'], cfg.get('revision', 'main'))
        members = [name for name in listed if name.startswith(dirs)]
        missing = [d for d in dirs if not any(name.startswith(d) for name in members)]
        if missing:
            raise RuntimeError(f'镜像未列出模型目录 {", ".join(missing)}，请切换下载节点后重试')
        files += [name for name in members if name not in files]
    return files


def _fetch(url, target_file: Path, progress=None, cancel_event=None, on_retry=None) -> None:
    """download_file with retries and mirror fallback.

    ``url`` may be one URL or ordered (primary, fallback) URLs. Later attempts
    switch nodes and resume the same .part; a dropped TLS connection never
    discards bytes that were already written.
    """
    urls = [url] if isinstance(url, str) else list(url)
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        active = urls[min((attempt - 1) // 3, len(urls) - 1)]
        try:
            download_file(active, target_file, progress_callback=progress, cancel_event=cancel_event,
                          timeout=DOWNLOAD_TIMEOUT,
                          **({'resume': True} if attempt > 1 else {}))
            return
        except (OSError, http.client.HTTPException) as exc:  # dropped TLS / truncated body; not 'cancelled'
            if attempt == DOWNLOAD_ATTEMPTS or isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                target_file.with_suffix(target_file.suffix + '.part').unlink(missing_ok=True)
                raise
            if on_retry:
                on_retry(attempt)
            time.sleep(RETRY_DELAY * attempt)


def download_model(kind: str, tier: str, root: Optional[Path] = None,
                   progress_callback: Optional[Callable[[int, str], None]] = None,
                   cancel_event: Optional[threading.Event] = None,
                   mirror: str = DEFAULT_MIRROR) -> dict:
    """Download and automatically configure a model from MODEL_CATALOG."""
    cfg = MODEL_CATALOG.get(kind, {}).get(tier)
    if not cfg:
        raise ValueError(f'未知的模型分类或档位: {kind}/{tier}')

    base_dir = project_root(root) / cfg['target_dir']
    base_dir.mkdir(parents=True, exist_ok=True)
    repo_id = cfg['repo_id']
    revision = cfg.get('revision', 'main')
    mirror = mirror.rstrip('/')
    files = catalog_files(cfg, mirror)

    total_files = len(files)
    file_hashes = {}
    members = []
    try:
        previous = json.loads((base_dir / 'model_source.json').read_text('utf-8'))
        previous = previous if isinstance(previous, dict) else {}
        recorded = previous.get('files')
        previous_hashes = recorded if isinstance(recorded, dict) and previous.get('repo_id') == repo_id and previous.get('revision') == revision else {}
    except (OSError, ValueError):
        previous_hashes = {}
    invalid = set()

    def reusable(filename):
        path = base_dir / filename
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if previous_hashes.get(filename) and previous_hashes[filename] != actual:
            invalid.add(filename)
            return False
        file_hashes[filename] = actual
        return True

    for idx, filename in enumerate(files, 1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError('下载已被中止')

        target_file = base_dir / filename
        if filename == 'pytorch_model.bin' and reusable('model.safetensors'):
            continue
        if kind in VOICE_KINDS and filename == cfg.get('source_file') and (base_dir / cfg['model_file']).is_file() \
                and (base_dir / cfg['model_file']).stat().st_size > 0:
            continue  # already converted to FP16
        # Skip if valid
        if reusable(filename):
            if progress_callback:
                progress_callback(int(idx / total_files * 100), f'[{idx}/{total_files}] 已存在: {filename}')
            continue

        if filename not in cfg['files']:
            members.append(filename)  # small directory member: fetched in parallel below
            continue
        urls = [f'{base}/{repo_id}/resolve/{revision}/{quote(filename)}'
                for base in dict.fromkeys((mirror, FALLBACK_MIRROR))]
        if progress_callback:
            progress_callback(int((idx - 1) / total_files * 100), f'[{idx}/{total_files}] 正在下载: {filename}…')

        def _sub_progress(cur, tot):
            if progress_callback:
                pct = int(((idx - 1) + (cur / tot if tot else 0)) / total_files * 100)
                mb_cur = cur / (1024 * 1024)
                mb_tot = tot / (1024 * 1024) if tot else 0
                progress_callback(pct, f'[{idx}/{total_files}] {filename}: {mb_cur:.1f}/{mb_tot:.1f} MB')

        def _retrying(attempt):
            if progress_callback:
                progress_callback(int((idx - 1) / total_files * 100),
                                  f'[{idx}/{total_files}] {filename}: 连接中断，第 {attempt} 次重试…')

        _fetch(urls, target_file, _sub_progress, cancel_event, _retrying)
        with target_file.open('rb') as f:
            file_hashes[filename] = hashlib.file_digest(f, 'sha256').hexdigest()

    if members:
        done = total_files - len(members)
        with ThreadPoolExecutor(PARALLEL_DOWNLOADS) as pool:
            futures = {pool.submit(_fetch, [f'{base}/{repo_id}/resolve/{revision}/{quote(name)}'
                                            for base in dict.fromkeys((mirror, FALLBACK_MIRROR))],
                                   base_dir / name, None, cancel_event): name for name in members}
            try:
                for future in as_completed(futures):
                    future.result()
                    name = futures[future]
                    with (base_dir / name).open('rb') as f:
                        file_hashes[name] = hashlib.file_digest(f, 'sha256').hexdigest()
                    done += 1
                    if progress_callback:
                        progress_callback(int(done / total_files * 100), f'[{done}/{total_files}] 已下载: {name}')
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    # Post-download configuration and assembly
    if kind in ('embedding', 'rerank'):
        if 'model.safetensors' in invalid:
            (base_dir / 'model.safetensors').unlink()
        # Automatically convert and optimize weights to FP16 safetensors (halving disk size)
        precision = _convert_to_fp16(base_dir, progress_callback)
        # Hash only actual artifacts, never a stale manifest or a removed .bin file.
        file_hashes.clear()
        for f in base_dir.iterdir():
            if f.is_file() and f.name != "model_source.json":
                with f.open('rb') as h:
                    file_hashes[f.name] = hashlib.file_digest(h, 'sha256').hexdigest()
        # Create model_source.json for provenance verification
        model_source = {
            'repo_id': repo_id,
            'revision': revision,
            'precision': precision,
            'files': file_hashes,
            'download_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        (base_dir / 'model_source.json').write_text(json.dumps(model_source, indent=2, ensure_ascii=False), encoding='utf-8')

    elif kind in VOICE_KINDS:
        source_name = cfg.get('source_file')
        precision = cfg.get('precision', 'source')
        if source_name:
            source_path, model_path = base_dir / source_name, base_dir / cfg['model_file']
            if source_path.is_file():
                if progress_callback:
                    progress_callback(95, '正在将语音模型转存为 FP16 半精度（体积减半）…')
                onnx_weights_to_fp16(source_path, model_path)
                source_path.unlink()
            file_hashes.pop(source_name, None)
            with model_path.open('rb') as f:
                file_hashes[cfg['model_file']] = hashlib.file_digest(f, 'sha256').hexdigest()
            precision = 'fp16'
        # Written last: is_installed() trusts this list, so an interrupted download stays "not installed".
        model_source = {
            'repo_id': repo_id,
            'revision': revision,
            'precision': precision,
            'files': file_hashes,
            'download_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        (base_dir / 'model_source.json').write_text(json.dumps(model_source, indent=2, ensure_ascii=False), encoding='utf-8')

    elif kind == 'llm':
        # Setup or update runtime.json in build/message-classifier-runtime
        runtime_path = base_dir / 'runtime.json'
        existing=json.loads(runtime_path.read_text('utf-8')) if runtime_path.is_file() else {}
        runtime_data=runtime_manifest(cfg['filename'],existing)
        runtime_path.write_text(json.dumps(runtime_data, indent=2, ensure_ascii=False), encoding='utf-8')

    # Downloading/assembling a model enables it immediately. The shared setter
    # notifies every model-page UI so none keeps showing a stale "off" switch.
    if kind not in VOICE_KINDS:
        try:
            import house_settings
            house_settings.set_local_models_enabled(True, project_root(root))
        except Exception:
            pass

    if progress_callback:
        progress_callback(100, f'【{cfg["tier_name"]}】下载与装配完成！')

    return {'status': 'ok', 'kind': kind, 'tier': tier, 'path': str(base_dir)}
