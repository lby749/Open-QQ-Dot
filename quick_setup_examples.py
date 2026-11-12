"""Public synthetic examples for the manually selected style/relationship preset."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _bank():
    try:
        data = json.loads((Path(__file__).parent/'assets/quick_setup_examples.json').read_text('utf-8'))
        return data['presets'] if data.get('schema') == 'quick-setup-examples-v2' and data.get('synthetic') is True else {}
    except (OSError, ValueError, KeyError):
        return {}


def examples(profile):
    preset = profile.get('prompt_presets') or {}
    if not isinstance(preset, dict) or preset.get('examples_enabled') is False:
        return []
    parts = str(preset.get('prepared_key', '')).split('_')
    if len(parts) != 5:
        return []
    return _bank().get('_'.join(parts), [])


def retrieval_text(item):
    # Older turns belong to generation context, not to the current turn's retrieval identity.
    return '\n'.join(item.get('context', [])[-1:] + [item['user']])


def prompt_block(profile, directory, query, *, limit=2):
    from prompt_context import header
    from retrieval_core import rank_texts
    if limit <= 0:
        return ''
    candidates = examples(profile)
    if not candidates:
        return ''
    texts = [retrieval_text(item) for item in candidates]
    # All candidates are explicitly selected public style references, never factual memory.
    # Cross-encoder scales differ; use ranking here instead of an absolute knowledge-relevance gate.
    ranked = rank_texts(query, texts, task='examples', floor=0, limit=min(limit, 2), directory=directory)
    # These are style demonstrations, so an unavailable search still has bounded references.
    chosen = [candidates[i] for i, _ in ranked] or candidates[:min(limit, 2)]
    blocks = []
    for item in chosen:
        value = {'说明': '公开合成示例，只参考表达；不是真实经历，不照抄。当前身份、关系、边界和用户修改优先。',
                 '先前对话': item.get('context', []), '对方': item['user'], '示例回复': item['target']}
        blocks.append(header('STYLE_EXAMPLE', 'quick_setup_examples', True)+'\n'+json.dumps(value, ensure_ascii=False))
    return '\n\n'+'\n\n'.join(blocks)
