import hashlib
import json

import model_downloader as downloader


def test_model_repair_reuses_good_weights_and_fetches_only_corrupt_file(tmp_path, monkeypatch):
    cfg = {'name': 'fixture', 'repo_id': 'fixture/model', 'revision': 'fixed-revision',
           'target_dir': 'model', 'files': ['config.json', 'pytorch_model.bin']}
    monkeypatch.setitem(downloader.MODEL_CATALOG['embedding'], 'fixture', cfg)
    base = tmp_path/'model'
    base.mkdir()
    (base/'model.safetensors').write_bytes(b'valid weights')
    (base/'config.json').write_bytes(b'corrupt')
    expected = {'model.safetensors': hashlib.sha256(b'valid weights').hexdigest(),
                'config.json': hashlib.sha256(b'{}').hexdigest()}
    (base/'model_source.json').write_text(json.dumps(
        {'repo_id': cfg['repo_id'], 'revision': cfg['revision'], 'files': expected}))
    fetched = []
    def fetch(url, path, *args, **kwargs):
        fetched.append(path.name)
        path.write_bytes(b'{}')
    monkeypatch.setattr(downloader, '_fetch', fetch)
    monkeypatch.setattr(downloader, '_convert_to_fp16', lambda *args: 'fp16')
    downloader.download_model('embedding', 'fixture', root=tmp_path)
    assert fetched == ['config.json']
    assert (base/'model.safetensors').read_bytes() == b'valid weights'
    provenance = json.loads((base/'model_source.json').read_text('utf-8'))
    assert provenance['files']['config.json'] == expected['config.json']
    fetched.clear()
    downloader.download_model('embedding', 'fixture', root=tmp_path)
    assert fetched == []
