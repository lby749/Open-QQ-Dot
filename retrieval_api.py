"""Optional per-friend OpenAI embeddings and Cohere/Jina-style rerank endpoints."""
import hashlib
import json
import math
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def config(directory, kind):
    if directory is None:
        return {}
    try:
        profile = json.loads((Path(directory) / 'friend.json').read_text('utf-8'))
        value = (profile.get('retrieval_api') or {}).get(kind) or {}
        if value.get('enabled'):
            return value
        tier = {'低占用': 'small', '中占用': 'base', '高占用': 'large'}.get(profile.get('performance_tier'))
        if tier and kind in ('embedding', 'rerank'):
            from model_downloader import MODEL_CATALOG, project_root
            model = MODEL_CATALOG[kind][tier]
            # Resolve shared weights without downloading or silently picking a larger installed model.
            value = {'enabled': True, 'mode': 'local', 'model': model['name'],
                     'model_path': str(project_root() / model['target_dir'])}
            if kind == 'embedding':
                value.update({key: model.get(key, '') for key in
                              ('dimension', 'pooling', 'query_prefix', 'document_prefix')})
        return value
    except (OSError, ValueError):
        return {}


def validate(value, kind):
    if not value.get('enabled'):
        return
    if value.get('mode', 'api') == 'local':
        from small_models import validate as validate_local
        validate_local(value, kind)
        if kind == 'embedding' and (type(value.get('dimension')) is not int or not 1 <= value['dimension'] <= 65536):
            raise ValueError('请填写本地向量模型的正确维度')
        return
    parsed = urlsplit(value.get('base_url', ''))
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('检索 API 地址须为不带凭据的 HTTP(S) 地址')
    if parsed.query or parsed.fragment or not str(value.get('model', '')).strip():
        raise ValueError('请填写检索模型，地址不要包含查询参数')
    if kind == 'embedding' and (type(value.get('dimension')) is not int or not 1 <= value['dimension'] <= 65536):
        raise ValueError('向量维度须为 1–65536 的整数，并与服务端输出一致')


def request(directory, kind, payload):
    from api_backend import key_path, load_key
    value = config(directory, kind)
    validate(value, kind)
    suffix = 'embeddings' if kind == 'embedding' else 'rerank'
    url = value['base_url'].rstrip('/')
    if not url.endswith('/' + suffix):
        url += '/' + suffix
    headers = {'Content-Type': 'application/json'}
    if key_path(Path(directory), kind).exists():
        headers['Authorization'] = 'Bearer ' + load_key(Path(directory), kind)
    req = urllib.request.Request(url, json.dumps(payload).encode('utf-8'), headers)
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('检索 API 返回内容过大')
    return json.loads(raw)


def cache_scope(directory=None, kind='embedding'):
    """Tenant, live local-model policy and credential revision; never store credentials in a cache key."""
    from house_settings import data_dir, local_models_enabled
    scope = str(Path(directory).resolve()) if directory is not None else str(data_dir().resolve())
    value = config(directory, kind)
    credential = ''
    if directory is not None and value.get('enabled') and value.get('mode', 'api') != 'local':
        from api_backend import key_path
        path = key_path(Path(directory), kind)
        credential = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''
    return scope, bool(local_models_enabled()), credential


def signature(directory=None):
    """Names the vector space: stored vectors are only compared with vectors of the same signature."""
    value = config(directory, 'embedding')
    if value.get('enabled'):
        validate(value, 'embedding')
        if value.get('mode', 'api') == 'local':
            from small_models import signature as local_signature
            return 'local-custom:' + local_signature(value, 'embedding')
        return 'api:' + hashlib.sha256(json.dumps([value, cache_scope(directory)[2]], sort_keys=True).encode()).hexdigest()
    from retrieval_core import choose_embedding
    choice = choose_embedding(directory)
    if choice.kind == 'preset':  # another installed preset (house override or BGE-M3 missing)
        from small_models import signature as local_signature
        return 'local-preset:' + choice.model + ':' + local_signature(choice.value, 'embedding')
    from embedding_provider import VERSION
    return 'local:' + VERSION  # unchanged for BGE-M3, so existing vector caches stay valid


def embed(texts, directory=None, is_query=False):
    """Vectors for texts. Queries are cached in memory per model signature (retrieval_core.QUERY_VECTORS) and run
    ahead of document batches on the local worker."""
    import retrieval_core
    texts = list(texts)
    if not is_query or not texts:
        return _embed(texts, directory, is_query)
    import embedding_provider
    import small_models
    # the producing functions are part of the key, so a replaced (test) embedder never sees another one's vectors
    space = (signature(directory), cache_scope(directory), id(embedding_provider.embed_unscoped), id(small_models.embed), id(request))
    vectors = [retrieval_core.QUERY_VECTORS.get((space, text)) for text in texts]
    missing = [i for i, vector in enumerate(vectors) if vector is None]
    if missing:
        fresh = retrieval_core.validate_vectors(_embed([texts[i] for i in missing], directory, True), len(missing))
        for i, vector in zip(missing, fresh):
            vectors[i] = vector
            retrieval_core.QUERY_VECTORS.put((space, texts[i]), tuple(vector))
    return [list(vector) for vector in vectors]


def _embed(texts, directory, is_query):
    import retrieval_core
    retrieval_core.note('embed_model_calls')
    retrieval_core.note('embed_model_texts', len(texts))
    value = config(directory, 'embedding')
    if not value.get('enabled'):
        choice = retrieval_core.choose_embedding(directory)
        if choice.kind == 'preset':
            from small_models import embed as local_embed
            return local_embed(texts, choice.value, is_query)
        from embedding_provider import embed_unscoped
        return embed_unscoped(texts, priority=0 if is_query else 1)
    if value.get('mode', 'api') == 'local':
        from small_models import embed as local_embed
        return local_embed(texts, value, is_query)
    rows = request(directory, 'embedding', {'model': value['model'], 'input': list(texts)})['data']
    if len(rows) != len(texts) or {row['index'] for row in rows} != set(range(len(texts))):
        raise ValueError('检索 API 向量条数或序号不一致')
    vectors = []
    for row in sorted(rows, key=lambda item: item['index']):
        vector = row['embedding']
        if len(vector) != value['dimension'] or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
            raise ValueError('检索 API 向量维度或数值无效')
        norm = math.sqrt(sum(x*x for x in vector))
        vectors.append([x / norm for x in vector] if norm else [0.0] * len(vector))
    return vectors


def rerank(query, passages, directory=None, task='memory'):
    """Relevance 0..1 per passage, through retrieval_core (chosen model, cached pairs)."""
    from retrieval_core import rerank as scheduled
    return scheduled(query, list(passages), directory, task)


def rerank_pairs(pairs, directory, choice):
    """Scores for [(query, passage)] with the reranker retrieval_core chose; one model call per distinct query."""
    groups = {}
    for i, (query, passage) in enumerate(pairs):
        groups.setdefault(query, []).append(i)
    scores = [0.0] * len(pairs)
    for query, indices in groups.items():
        passages = [pairs[i][1] for i in indices]
        if choice.kind == 'builtin':
            from reranker import score
            values = score(query, passages)
        elif choice.kind == 'preset':
            from small_models import rerank as local_rerank
            values = local_rerank(query, passages, choice.value)
        else:
            values = _configured_rerank(query, passages, directory)
        if len(values) != len(indices) or any(not math.isfinite(float(value)) or not 0 <= float(value) <= 1 for value in values):
            raise ValueError('重排模型返回条数或分数无效')
        for i, value in zip(indices, values):
            scores[i] = float(value)
    return scores


def _configured_rerank(query, passages, directory):
    value = config(directory, 'rerank')
    if value.get('mode', 'api') == 'local':
        from small_models import rerank as local_rerank
        return local_rerank(query, passages, value)
    rows = request(directory, 'rerank', {'model': value['model'], 'query': query,
                                       'documents': list(passages), 'top_n': len(passages)})['results']
    if len(rows) != len(passages) or {row['index'] for row in rows} != set(range(len(passages))):
        raise ValueError('重排 API 返回的候选序号不完整')
    scores = [0.0] * len(passages)
    for row in rows:
        score = row['relevance_score']
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('重排 API 分数须为 0–1')
        scores[row['index']] = score
    return scores
