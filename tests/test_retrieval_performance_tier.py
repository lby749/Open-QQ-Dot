import json

import pytest

import model_downloader
import retrieval_api
import retrieval_core


@pytest.fixture
def friend(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    directory = tmp_path / 'friends' / 'a'
    directory.mkdir(parents=True)
    return directory


@pytest.mark.parametrize('label,tier,dimension', [('低占用', 'small', 512), ('中占用', 'base', 768),
                                                ('高占用', 'large', 1024)])
def test_saved_quick_setup_tier_reaches_both_models_without_download(friend, monkeypatch, label, tier, dimension):
    monkeypatch.setattr(model_downloader, 'download_model', lambda *_a, **_k: pytest.fail('查询不能下载模型'))
    (friend / 'friend.json').write_text(json.dumps({'performance_tier': label}), 'utf-8')
    embed = retrieval_core.choose_embedding(friend)
    rerank = retrieval_core.choose_reranker('examples', '中文问题', ['中文段落'], friend)
    assert embed.kind == rerank.kind == 'configured'
    assert embed.model == model_downloader.MODEL_CATALOG['embedding'][tier]['name']
    assert embed.value['dimension'] == dimension
    assert rerank.model == model_downloader.MODEL_CATALOG['rerank'][tier]['name']
    assert str(friend) not in embed.value['model_path']  # weights are shared at the house root
    if tier == 'small':
        assert 'zh' in retrieval_core.registry()[rerank.model].languages


def test_missing_selected_tier_does_not_use_an_installed_large_model(friend, monkeypatch):
    (friend / 'friend.json').write_text('{"performance_tier":"低占用"}', 'utf-8')
    monkeypatch.setattr(retrieval_core, 'installed', lambda *_a: ['BAAI/bge-m3'])
    with pytest.raises(ValueError, match='不会自动下载'):
        retrieval_api.signature(friend)
    assert retrieval_core.choose_embedding(friend).model == 'BAAI/bge-small-zh-v1.5'


def test_explicit_api_and_local_setting_win_independently(friend):
    explicit = {'enabled': True, 'mode': 'api', 'base_url': 'https://example.invalid/v1',
                'model': 'chosen-api', 'dimension': 17}
    (friend / 'friend.json').write_text(json.dumps({'performance_tier': '低占用',
        'retrieval_api': {'embedding': explicit}}), 'utf-8')
    assert retrieval_api.config(friend, 'embedding') == explicit
    assert retrieval_core.choose_reranker(directory=friend).model.startswith('cross-encoder/mmarco-')
    explicit.update(mode='local', model_path='/explicit/custom/path')
    (friend / 'friend.json').write_text(json.dumps({'performance_tier': '高占用',
        'retrieval_api': {'embedding': explicit}}), 'utf-8')
    assert retrieval_api.config(friend, 'embedding') == explicit


def test_tiers_do_not_bypass_global_local_model_switch(friend, monkeypatch):
    (friend.parent.parent / 'house.local.json').write_text('{"local_models_enabled":false}', 'utf-8')
    (friend / 'friend.json').write_text('{"performance_tier":"高占用"}', 'utf-8')
    import small_models
    # A model may already be loaded; revocation must be checked before its cache is consulted.
    monkeypatch.setattr(small_models, '_loaded', {'embedding': ('already-loaded',)})
    with pytest.raises(RuntimeError, match='开关已关闭'):
        small_models.embed(['中文'], retrieval_api.config(friend, 'embedding'))


def test_changing_tier_changes_vector_space_and_friend_cache_stays_private(friend, monkeypatch):
    for tier in ('small', 'base'):
        path = friend.parent.parent / model_downloader.MODEL_CATALOG['embedding'][tier]['target_dir']
        path.mkdir(parents=True)
        (path / 'config.json').write_text('{}', 'utf-8')
    (friend / 'friend.json').write_text('{"performance_tier":"低占用"}', 'utf-8')
    first = retrieval_api.signature(friend)
    (friend / 'friend.json').write_text('{"performance_tier":"中占用"}', 'utf-8')
    assert retrieval_api.signature(friend) != first
    other = friend.parent / 'b'
    other.mkdir()
    (other / 'friend.json').write_bytes((friend / 'friend.json').read_bytes())
    assert retrieval_api.signature(friend) == retrieval_api.signature(other)
    assert retrieval_api.cache_scope(friend) != retrieval_api.cache_scope(other)


def test_minimal_mode_retains_existing_auto_policy(friend):
    (friend / 'friend.json').write_text('{"performance_tier":"极小"}', 'utf-8')
    assert retrieval_api.config(friend, 'embedding') == {}
    assert retrieval_api.config(friend, 'rerank') == {}
