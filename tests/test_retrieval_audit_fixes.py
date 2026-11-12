"""Small reproductions of production defects; no model accounts, QQ or GPU required."""
import sqlite3
import struct
from unittest.mock import Mock, patch

import pytest

import retrieval_api
import retrieval_core as core
from persona_store import Conflict, Unavailable


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.setattr('house_settings.local_models_enabled', lambda: True)
    monkeypatch.setattr(core, 'overrides', lambda: {})
    monkeypatch.setattr(core, 'installed', lambda kind=None: [name for name in ('BAAI/bge-m3', 'BAAI/bge-reranker-v2-m3')
                                                            if kind is None or core.registry()[name].kind == kind])
    monkeypatch.setattr('embedding_provider.embed_unscoped', lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr('reranker.score', lambda query, passages: [1.0] * len(passages))
    core.reset_stats()


def test_query_cache_is_scoped_and_cannot_be_mutated(models, tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    with patch('embedding_provider.embed_unscoped', return_value=[[1.0, 0.0]]) as embed:
        first = retrieval_api.embed(['same private query'], a, True)
        first[0][0] = 99
        assert retrieval_api.embed(['same private query'], a, True) == [[1.0, 0.0]]
        retrieval_api.embed(['same private query'], b, True)
    assert embed.call_count == 2


def test_cache_does_not_bypass_local_model_revocation(models, monkeypatch):
    import house_settings
    state = {'enabled': True}
    monkeypatch.setattr(house_settings, 'local_models_enabled', lambda: state['enabled'])
    def embedding(texts, **kwargs):
        if not state['enabled']:
            raise Unavailable('disabled')
        return [[1.0, 0.0] for _ in texts]
    with patch('embedding_provider.embed_unscoped', side_effect=embedding):
        retrieval_api.embed(['same'], is_query=True)
        state['enabled'] = False
        with pytest.raises(Unavailable):
            retrieval_api.embed(['same'], is_query=True)


def test_rerank_cache_is_scoped_to_the_friend(models, tmp_path):
    with patch('reranker.score', return_value=[0.8]) as score:
        core.rerank('private', ['source'], tmp_path / 'a')
        core.rerank('private', ['source'], tmp_path / 'a')
        core.rerank('private', ['source'], tmp_path / 'b')
    assert score.call_count == 2


def test_embedding_choice_is_stable_when_free_vram_changes(models, monkeypatch):
    monkeypatch.setattr(core, 'installed', lambda kind=None: ['BAAI/bge-m3', 'BAAI/bge-small-zh-v1.5'])
    with patch.object(core, '_free_vram_mb', return_value=99999):
        first = core.choose_embedding().model
    with patch.object(core, '_free_vram_mb', return_value=10):
        second = core.choose_embedding().model
    assert first == second == 'BAAI/bge-m3'


def test_incomplete_embeddings_cannot_poison_the_cache(models):
    with patch('embedding_provider.embed_unscoped', return_value=[[1.0, 0.0]]), pytest.raises(ValueError):
        retrieval_api.embed(['one', 'two'], is_query=True)
    assert not core.QUERY_VECTORS.data


def test_reranker_cannot_silently_pad_incomplete_results(models):
    with patch('reranker.score', return_value=[0.8]), pytest.raises(ValueError):
        core.rerank('query', ['one', 'two'])
    assert not core.RERANK_SCORES.data


def test_doc_vectors_batch_is_atomic_and_corrupt_rows_are_repaired(tmp_path):
    cache = core.DocVectors(tmp_path / 'cache.sqlite3')
    with pytest.raises(ValueError):
        cache.get(['a', 'b'], 's', lambda texts: [[1.0, 0.0]])
    with sqlite3.connect(cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM doc_vectors').fetchone()[0] == 0
        db.execute('INSERT INTO doc_vectors VALUES (?,?,?,?,?)', ('s', core.digest('a'), 2, struct.pack('2f', float('nan'), 0), 0))
    embedded = Mock(return_value=[[1.0, 0.0]])
    assert cache.get(['a', 'a'], 's', embedded) == [(1.0, 0.0), (1.0, 0.0)]
    embedded.assert_called_once_with(['a'])


def test_document_cache_has_a_hard_size_bound(tmp_path):
    cache = core.DocVectors(tmp_path / 'cache.sqlite3')
    cache.MAX_ROWS = 2
    for text in ('one', 'two', 'three'):
        cache.get([text], 's', lambda texts: [[1.0, 0.0] for _ in texts])
    with sqlite3.connect(cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM doc_vectors').fetchone()[0] == 2


def test_subjects_dates_and_polarity_precede_semantic_duplicate_scores():
    assert not core.same_task_subject('修复购物车页报错', '修复支付页报错')
    assert not core.same_task_subject('爬取微博热搜', '抓取知乎热榜')
    assert not core.same_task_subject('启用插件', '禁用插件')
    assert core.anchors_conflict('整理十一月的发票', '整理十月的发票')
    assert core.same_task_subject('整理会议纪要', '整理本周会议纪要')


def test_diagnostic_names_and_specific_identifiers_are_independent_evidence():
    texts = ['API returns 429 Too Many Requests', 'npm proxy configuration', 'unrelated music']
    assert core.diagnostic_terms('rate limit 怎么退避') & core.diagnostic_terms(texts[0])
    assert core.evidence_cut('rate limit 怎么退避', texts, [(0, 0.00002), (2, 0.003)], {}, [0.42, 0, 0.4], 0.1, 5) == [(0, 0.00002)]
    assert core.evidence_cut('npm 代理怎么配', texts, [(1, 0.045)], {1: 5}, [0, 0.58, 0], 0.1, 5) == [(1, 0.045)]
    assert not core.evidence_cut('写首诗', texts, [(2, 0.001)], {}, [0, 0, 0.4], 0.1, 5)


def test_unverified_shared_experiences_do_not_cross_tenants(models, tmp_path):
    from local_retrieval_engine import LocalHybridSearchEngine, TaskExperienceStore
    store = TaskExperienceStore(LocalHybridSearchEngine(tmp_path / 'retrieval_engine.sqlite3'))
    store.record_experience('t', 'bob', '', 'codex', '', [], 'GPU memory failure', {}, 'disable security',
                            'execution_finished', is_shared=True)
    assert store.search_experiences('GPU memory failure', 'alice') == []
    assert store.search_experiences('GPU memory failure', 'bob')


def test_dedup_does_not_merge_different_project_scopes(models, tmp_path):
    from task_manager import TaskManager
    manager = TaskManager(tmp_path / 'tasks.sqlite3', tmp_path)
    try:
        a = manager.create_task('same title', 'same task', category='file', owner_id='alice', initial_checkpoint={'dedup': True, 'workspace_id': 'p1'})
        b = manager.create_task('same title', 'same task', category='file', owner_id='alice', initial_checkpoint={'dedup': True, 'workspace_id': 'p2'})
        assert a != b
        assert manager.create_task('same title', 'same task', category='file', owner_id='alice',
                                   initial_checkpoint={'dedup': True, 'workspace_id': 'p1'}) == a
    finally:
        manager.stop_worker()


def test_same_idempotency_key_cannot_be_reused_for_another_operation(models, tmp_path):
    from task_manager import TaskManager
    manager = TaskManager(tmp_path / 'tasks.sqlite3', tmp_path)
    try:
        tid = manager.create_task('title', 'original', category='file', owner_id='alice', idempotency_key='one')
        assert manager.create_task('title', 'original', category='file', owner_id='alice', idempotency_key='one') == tid
        with pytest.raises(ValueError, match='幂等键'):
            manager.create_task('title', 'different operation', category='file', owner_id='alice', idempotency_key='one')
        assert len(manager.list_tasks(owner_id='alice')) == 1
    finally:
        manager.stop_worker()


def test_summary_source_change_discards_stale_result_without_aborting_chat(tmp_path):
    from prompt_context import refresh_summary
    from store import FriendStore
    store = FriendStore(tmp_path)
    try:
        (tmp_path / 'friend.json').write_text('{}', 'utf-8')
        for i in range(60):
            store.add_message(str(i), 'friend' if i % 2 == 0 else 'me', f'message {i}')
        store.set('context_summary', 'keep previous summary')
        def generate(*args, **kwargs):
            store.add_message('fresh', 'friend', 'fresh evidence arrives during the model call')
            return 'stale summary', None
        model = Mock(side_effect=generate)
        refresh_summary(store, {}, tmp_path, model)
        assert store.get('context_summary') == 'keep previous summary'
        assert store.get('context_summary_seq') is None
        assert '旧结果未保存' in store.get('context_summary_status')
        refresh_summary(store, {}, tmp_path, model)
        assert model.call_count == 1
        from source_state import require_snapshot
        with pytest.raises(Conflict):
            require_snapshot(tmp_path, None)
    finally:
        store.close()


def test_short_context_read_is_coherent_against_concurrent_profile_updates(tmp_path):
    import threading
    from source_state import source_read, source_lock
    profile = tmp_path / 'friend.json'
    profile.write_text('{}', 'utf-8')
    starting, changed = threading.Event(), threading.Event()
    def writer():
        starting.set()
        with source_lock(tmp_path):
            profile.write_text('{"new":true}', 'utf-8')
            changed.set()
    thread = threading.Thread(target=writer)
    @source_read(locked=True)
    def context(directory):
        thread.start()
        assert starting.wait(2)
        assert not changed.wait(.03)
        return profile.read_text('utf-8')
    assert context(tmp_path) == '{}'
    thread.join(2)
    assert changed.is_set()


def test_short_context_lock_does_not_disable_snapshot_validation(tmp_path):
    from source_state import source_read
    (tmp_path / 'friend.json').write_text('{}', 'utf-8')
    @source_read(locked=True)
    def invalid_reader(directory):
        (directory / 'friend.json').write_text('{"changed":true}', 'utf-8')
        return 'stale value'
    with pytest.raises(Conflict):
        invalid_reader(tmp_path)


def test_work_executors_use_the_actual_query_scope_and_limit(models, tmp_path):
    from memory_hub import task_context
    from types import SimpleNamespace
    task = SimpleNamespace(task_id='t1', owner_id='alice', description='actual concrete task', checkpoint={'workspace_id': 'p1'})
    manager = SimpleNamespace(db_path=tmp_path / 'data/tasks.sqlite3', add_log=Mock())
    execution = SimpleNamespace(manager=manager)
    with patch('memory_hub.recall', return_value=[]) as recall:
        assert task_context(task, execution) == ''
    assert recall.call_args.args == ('actual concrete task', 'alice')
    assert recall.call_args.kwargs['project_id'] == 'p1'
    assert recall.call_args.kwargs['engine_db'] == tmp_path / 'data/retrieval_engine.sqlite3'
    assert recall.call_args.kwargs['limit'] == 3


def test_packaging_excludes_auth_state_wal_and_case_variants():
    from tools.package_release import is_ignored, load_gitignore_patterns
    from pathlib import Path
    patterns = load_gitignore_patterns(Path(__file__).resolve().parents[1])
    for path in ('AUTH.JSON', 'nested/credentials.json', 'nested/private.pem', 'nested/learning.sqlite3-wal',
                 'nested/jobs.db-shm', 'nested/.history.sqlite3.source-nonce', '.agent_mail_key_v1'):
        assert is_ignored(path, patterns), path
    assert not is_ignored('retrieval_core.py', patterns)


def test_packaging_scans_frontend_and_shell_sources_without_printing_keys(tmp_path):
    from tools.package_release import scan_for_leaks
    credential = 'nvapi-' + 'a' * 40
    source = tmp_path / 'frontend.ts'
    source.write_text('const key = "' + credential + '";', 'utf-8')
    leaks = scan_for_leaks(tmp_path, [source])
    assert leaks and credential not in str(leaks)
