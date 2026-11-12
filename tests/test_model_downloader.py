import pytest
pytest.importorskip('onnx')
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import model_downloader


class ModelDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_catalog_structure(self):
        for kind in ('embedding', 'rerank', 'llm', 'tts'):
            self.assertIn(kind, model_downloader.MODEL_CATALOG)
            for tier in ('small', 'base', 'large'):
                self.assertIn(tier, model_downloader.MODEL_CATALOG[kind])
                cfg = model_downloader.MODEL_CATALOG[kind][tier]
                self.assertIn('name', cfg)
                self.assertIn('tier_name', cfg)
                self.assertIn('files', cfg)
                self.assertIn('target_dir', cfg)

    def test_is_installed_and_installed_models(self):
        # Initially nothing is installed
        self.assertFalse(model_downloader.is_installed('embedding', 'small', root=self.root))
        self.assertEqual(model_downloader.installed_models('embedding', root=self.root), [])

        # Create dummy files for embedding small
        cfg = model_downloader.MODEL_CATALOG['embedding']['small']
        target = self.root / cfg['target_dir']
        target.mkdir(parents=True, exist_ok=True)
        for f in cfg['files']:
            (target / f).write_text('dummy content', encoding='utf-8')

        self.assertTrue(model_downloader.is_installed('embedding', 'small', root=self.root))
        installed = model_downloader.installed_models('embedding', root=self.root)
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0]['tier'], 'small')

    def test_mock_download_model_embedding(self):
        cfg = model_downloader.MODEL_CATALOG['embedding']['small']
        events = []

        def mock_download_file(url, dest_path, progress_callback=None, cancel_event=None, timeout=30):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b'{"dummy": true}')
            if progress_callback:
                progress_callback(100, 100)

        with patch('model_downloader.download_file', side_effect=mock_download_file), patch('model_downloader._convert_to_fp16',return_value='source'):
            res = model_downloader.download_model(
                'embedding', 'small', root=self.root,
                progress_callback=lambda pct, msg: events.append((pct, msg))
            )
            self.assertEqual(res['status'], 'ok')
            target = self.root / cfg['target_dir']
            self.assertTrue((target / 'config.json').is_file())
            self.assertTrue((target / 'model_source.json').is_file())
            source = json.loads((target / 'model_source.json').read_text('utf-8'))
            self.assertEqual(source['repo_id'], cfg['repo_id'])
            self.assertIn('config.json', source['files'])

    def test_mock_download_model_llm(self):
        cfg = model_downloader.MODEL_CATALOG['llm']['small']

        def mock_download_file(url, dest_path, progress_callback=None, cancel_event=None, timeout=30):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b'GGUF_DUMMY_BINARY')

        with patch('model_downloader.download_file', side_effect=mock_download_file):
            res = model_downloader.download_model('llm', 'small', root=self.root)
            self.assertEqual(res['status'], 'ok')
            target = self.root / cfg['target_dir']
            self.assertTrue((target / cfg['filename']).is_file())
            self.assertTrue((target / 'runtime.json').is_file())
            runtime = json.loads((target / 'runtime.json').read_text('utf-8'))
            self.assertEqual(runtime['model'], cfg['filename'])

    def test_incomplete_tokenizer_is_not_reported_as_installed(self):
        cfg = model_downloader.MODEL_CATALOG['embedding']['small']
        target = self.root / cfg['target_dir']
        target.mkdir(parents=True)
        (target / 'config.json').write_text('{}')
        (target / 'model.safetensors').write_bytes(b'weights')
        self.assertFalse(model_downloader.is_installed('embedding', 'small', self.root))

    def test_frozen_model_download_uses_its_revision(self):
        cfg = model_downloader.MODEL_CATALOG['embedding']['large']
        urls = []
        def download(url, dest, **kwargs):
            urls.append(url)
            dest.write_bytes(b'model-file')
        with patch.object(model_downloader, 'download_file', side_effect=download), \
                patch.object(model_downloader, '_convert_to_fp16', return_value='source'):
            model_downloader.download_model('embedding', 'large', self.root)
        revision = Path(cfg['target_dir']).name
        self.assertTrue(urls)
        self.assertTrue(all('/resolve/' + revision + '/' in url for url in urls))

    def test_download_reuses_converted_weights(self):
        cfg = model_downloader.MODEL_CATALOG['embedding']['large']
        target = self.root / cfg['target_dir']
        target.mkdir(parents=True)
        for name in cfg['files']:
            if name != 'pytorch_model.bin':
                (target / name).write_bytes(b'existing')
        (target / 'model.safetensors').write_bytes(b'converted')
        with patch.object(model_downloader, 'download_file') as download, \
                patch.object(model_downloader, '_convert_to_fp16', return_value='fp16'):
            model_downloader.download_model('embedding', 'large', self.root)
        download.assert_not_called()

    def test_tts_tiers_are_pinned_and_name_their_runtime_files(self):
        engines = {tier: cfg['engine'] for tier, cfg in model_downloader.MODEL_CATALOG['tts'].items()}
        self.assertEqual(engines, {'small': 'vits', 'base': 'kokoro', 'large': 'voxcpm'})
        for tier, cfg in model_downloader.MODEL_CATALOG['tts'].items():
            with self.subTest(tier=tier):
                self.assertRegex(cfg['revision'], r'^[0-9a-f]{40}$')
                if cfg['engine'] == 'voxcpm':
                    self.assertIn('model.safetensors', cfg['files'])
                    self.assertIn('audiovae.pth', cfg['files'])
                    continue
                # CPU tiers download full-precision ONNX and keep only the FP16 copy.
                self.assertIn(cfg['source_file'], cfg['files'])
                self.assertTrue(cfg['model_file'].endswith('.fp16.onnx'))
                runtime = model_downloader.tts_runtime_files(cfg)
                self.assertNotIn(cfg['source_file'], runtime)
                for name in [cfg['model_file'], 'tokens.txt', *cfg['lexicon'].split(','), *cfg['rule_fsts']]:
                    self.assertIn(name, runtime)
                if cfg['engine'] == 'kokoro':
                    self.assertIn('voices.bin', cfg['files'])
                    self.assertEqual(cfg['dirs'], ['dict', 'espeak-ng-data'])

    def test_onnx_weights_to_fp16_halves_weights_and_keeps_outputs(self):
        import numpy as np
        from onnx import TensorProto, helper, numpy_helper, save
        from onnx.reference import ReferenceEvaluator
        rng = np.random.default_rng(0)
        weight = rng.standard_normal((64, 32)).astype(np.float32)
        graph = helper.make_graph(
            [helper.make_node('MatMul', ['x', 'w'], ['m']), helper.make_node('Add', ['m', 'b'], ['y'])], 'g',
            [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 64])],
            [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 32])],
            [numpy_helper.from_array(weight, 'w'), numpy_helper.from_array(np.float32([0.5]), 'b')])
        source, target = self.root / 'model.onnx', self.root / 'model.fp16.onnx'
        save(helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)]), str(source))
        model_downloader.onnx_weights_to_fp16(source, target)
        self.assertLess(target.stat().st_size, source.stat().st_size * 0.6)
        x = rng.standard_normal((1, 64)).astype(np.float32)
        expected = ReferenceEvaluator(str(source)).run(None, {'x': x})[0]
        actual = ReferenceEvaluator(str(target)).run(None, {'x': x})[0]
        self.assertEqual(actual.dtype, np.float32)
        np.testing.assert_allclose(actual, expected, rtol=1e-2, atol=1e-2)
        import onnx
        converted = onnx.load(str(target))
        dtypes = {w.name: w.data_type for w in converted.graph.initializer}
        self.assertEqual(dtypes, {'w__fp16': TensorProto.FLOAT16, 'b': TensorProto.FLOAT})  # tiny bias stays exact

    def test_tts_fp16_copy_replaces_source_and_is_not_redownloaded(self):
        cfg = model_downloader.MODEL_CATALOG['tts']['small']
        target = model_downloader.project_root(self.root) / cfg['target_dir']

        def download(url, dest, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'x')

        def convert(source, model):
            model.write_bytes(b'fp16')

        with patch.object(model_downloader, 'download_file', side_effect=download), \
                patch.object(model_downloader, 'onnx_weights_to_fp16', side_effect=convert) as conversion:
            model_downloader.download_model('tts', 'small', self.root)
        conversion.assert_called_once_with(target / cfg['source_file'], target / cfg['model_file'])
        self.assertFalse((target / cfg['source_file']).exists())
        source = json.loads((target / 'model_source.json').read_text('utf-8'))
        self.assertEqual(source['precision'], 'fp16')
        self.assertIn(cfg['model_file'], source['files'])
        self.assertNotIn(cfg['source_file'], source['files'])
        self.assertTrue(model_downloader.is_installed('tts', 'small', self.root))
        with patch.object(model_downloader, 'download_file') as again:
            model_downloader.download_model('tts', 'small', self.root)
        again.assert_not_called()

    def test_tts_download_expands_directories_from_pinned_listing(self):
        cfg = model_downloader.MODEL_CATALOG['tts']['base']
        listing = cfg['files'] + ['README.md', 'dict/jieba.dict.utf8', 'dict/pos_dict/prob_emit.utf8',
                                  'espeak-ng-data/phontab', 'espeak-ng-data/voices/!v/Mr serious', '../escape.txt']
        urls, listed = [], []

        def fake_list(mirror, repo_id, revision, timeout=30):
            listed.append((mirror, repo_id, revision))
            return [name for name in listing if model_downloader._safe_relative(name)]

        def download(url, dest, **kwargs):
            urls.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'x')

        with patch.object(model_downloader, 'list_repo_files', side_effect=fake_list), \
                patch.object(model_downloader, 'download_file', side_effect=download), \
                patch.object(model_downloader, 'onnx_weights_to_fp16', side_effect=lambda a, b: b.write_bytes(b'h')):
            model_downloader.download_model('tts', 'base', self.root, mirror='https://mirror.test/')
        self.assertEqual(listed, [('https://mirror.test', cfg['repo_id'], cfg['revision'])])
        self.assertTrue(all(url.startswith(f"https://mirror.test/{cfg['repo_id']}/resolve/{cfg['revision']}/")
                            for url in urls))
        fetched = {url.rsplit(cfg['revision'] + '/', 1)[1] for url in urls}
        self.assertIn('espeak-ng-data/voices/%21v/Mr%20serious', fetched)  # names are URL-encoded
        self.assertNotIn('README.md', fetched)
        self.assertNotIn('../escape.txt', fetched)
        target = self.root / cfg['target_dir']
        self.assertTrue((target / 'dict/pos_dict/prob_emit.utf8').is_file())
        self.assertTrue(model_downloader.is_installed('tts', 'base', self.root))
        source = json.loads((target / 'model_source.json').read_text('utf-8'))
        self.assertEqual(source['revision'], cfg['revision'])
        self.assertIn('espeak-ng-data/phontab', source['files'])
        # Voice models are enabled per friend; the retrieval-model switch is left alone.
        self.assertFalse((self.root / 'house.local.json').exists())

    def test_tts_partial_download_is_not_installed(self):
        cfg = model_downloader.MODEL_CATALOG['tts']['small']
        runtime = model_downloader.tts_runtime_files(cfg)
        target = self.root / cfg['target_dir']
        target.mkdir(parents=True)
        for name in runtime:
            (target / name).write_bytes(b'x')
        self.assertFalse(model_downloader.is_installed('tts', 'small', self.root))
        (target / 'model_source.json').write_text(json.dumps({'files': {n: '' for n in cfg['files']}}))
        self.assertFalse(model_downloader.is_installed('tts', 'small', self.root))  # no FP16 copy recorded
        (target / 'model_source.json').write_text(json.dumps({'files': {n: '' for n in runtime + ['dict/a']}}))
        self.assertFalse(model_downloader.is_installed('tts', 'small', self.root))
        (target / 'dict').mkdir()
        (target / 'dict/a').write_bytes(b'x')
        self.assertTrue(model_downloader.is_installed('tts', 'small', self.root))

    def test_missing_directory_in_listing_fails_before_downloading(self):
        cfg = model_downloader.MODEL_CATALOG['tts']['base']
        with patch.object(model_downloader, 'list_repo_files', return_value=list(cfg['files']) + ['dict/a']), \
                patch.object(model_downloader, 'download_file') as download:
            with self.assertRaisesRegex(RuntimeError, 'espeak-ng-data/'):
                model_downloader.download_model('tts', 'base', self.root)
        download.assert_not_called()

    def test_dropped_connection_is_retried_per_file(self):
        failures = {'tokens.txt': 2}

        def flaky(url, dest, **kwargs):
            name = url.rsplit('/', 1)[1]
            if failures.get(name):
                failures[name] -= 1
                raise model_downloader.urllib.error.URLError('EOF occurred in violation of protocol')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'x')

        cfg = model_downloader.MODEL_CATALOG['tts']['small']
        with patch.object(model_downloader, 'list_repo_files', return_value=cfg['files'] + ['dict/a']), \
                patch.object(model_downloader, 'download_file', side_effect=flaky), \
                patch.object(model_downloader, 'onnx_weights_to_fp16', side_effect=lambda a, b: b.write_bytes(b'h')), \
                patch.object(model_downloader, 'RETRY_DELAY', 0):
            model_downloader.download_model('tts', 'small', self.root)
        self.assertEqual(failures['tokens.txt'], 0)
        self.assertTrue(model_downloader.is_installed('tts', 'small', self.root))

    def test_persistent_failure_and_missing_file_are_not_installed(self):
        cfg = model_downloader.MODEL_CATALOG['tts']['small']
        error = model_downloader.urllib.error.HTTPError('u', 404, 'Not Found', {}, None)
        with patch.object(model_downloader, 'list_repo_files', return_value=cfg['files'] + ['dict/a']), \
                patch.object(model_downloader, 'download_file', side_effect=error) as download, \
                patch.object(model_downloader, 'RETRY_DELAY', 0):
            with self.assertRaises(model_downloader.urllib.error.HTTPError):
                model_downloader.download_model('tts', 'small', self.root)
        self.assertEqual(download.call_count, 1)
        self.assertFalse(model_downloader.is_installed('tts', 'small', self.root))

    def test_truncated_body_is_not_kept(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '10'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        dest = self.root / 'model.onnx'
        with patch.object(model_downloader.urllib.request, 'urlopen', return_value=Response(b'12345')), \
                patch.object(model_downloader, 'download_file_ranges', return_value=False):
            with self.assertRaises(model_downloader.http.client.IncompleteRead):
                model_downloader.download_file('https://mirror.test/x', dest)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_suffix('.onnx.part').exists())

    def test_retry_resumes_partial_file_with_range(self):
        class Response(io.BytesIO):
            def __init__(self, body, length, status):
                super().__init__(body)
                self.headers, self.status = {'Content-Length': str(length)}, status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        requests = []

        def urlopen(req, timeout=30):
            requests.append(req.get_header('Range'))
            return Response(b'01234', 10, 200) if len(requests) == 1 else Response(b'56789', 5, 206)

        dest = self.root / 'model.onnx'
        with patch.object(model_downloader.urllib.request, 'urlopen', side_effect=urlopen):
            with self.assertRaises(model_downloader.http.client.IncompleteRead):
                model_downloader.download_file('https://mirror.test/x', dest, resume=True)
            self.assertEqual(dest.with_suffix('.onnx.part').read_bytes(), b'01234')
            model_downloader.download_file('https://mirror.test/x', dest, resume=True)
        self.assertEqual(requests, [None, 'bytes=5-'])
        self.assertEqual(dest.read_bytes(), b'0123456789')

    def test_unsafe_listing_names_are_rejected(self):
        for name in ('../x', '/abs', 'C:/x', 'a\\b', ''):
            self.assertFalse(model_downloader._safe_relative(name), name)
        self.assertTrue(model_downloader._safe_relative('espeak-ng-data/voices/cmn'))


if __name__ == '__main__':
    unittest.main()
