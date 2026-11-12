"""CPU checks with tiny random local artifacts; never download a pretrained model."""
import importlib.util
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('torch') and importlib.util.find_spec('transformers'),
                     'optional local-model libraries not installed')
class SmallModels(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root), 'HF_HUB_OFFLINE': '1'})
        self.env.start()
        import house_settings
        house_settings.save({'local_models_enabled': True}, self.root)

    def tearDown(self):
        import small_models
        small_models._loaded.clear()
        self.env.stop()
        self.temp.cleanup()

    def artifact(self, kind):
        from transformers import BertConfig, BertModel, BertForSequenceClassification, BertTokenizerFast
        directory = self.root / kind
        directory.mkdir()
        vocab = directory / 'vocab.txt'
        vocab.write_text('[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\nhello\nworld\nweather\nquery\npassage\n:\n', 'utf-8')
        tokenizer = BertTokenizerFast(vocab_file=str(vocab))
        tokenizer.save_pretrained(directory)
        config = BertConfig(vocab_size=len(tokenizer), hidden_size=8, num_hidden_layers=1,
                            num_attention_heads=2, intermediate_size=16, num_labels=1)
        (BertModel(config) if kind == 'embedding' else BertForSequenceClassification(config)).save_pretrained(directory)
        return {'mode': 'local', 'enabled': True, 'model_path': str(directory), 'model': 'synthetic-tiny-bert',
                'dimension': 8, 'pooling': 'mean', 'device': 'cpu',
                'query_prefix': 'query: ', 'document_prefix': 'passage: '}

    def test_offline_cpu_encoder_mean_cls_and_dimension_guard(self):
        import small_models
        value = self.artifact('embedding')
        for pooling in ('mean', 'cls'):
            value['pooling'] = pooling
            vectors = small_models.embed(['hello world', 'weather'], value, is_query=True)
            self.assertEqual([len(v) for v in vectors], [8, 8])
            self.assertTrue(math.isclose(sum(x*x for x in vectors[0]), 1, abs_tol=1e-5))
        value['dimension'] = 7
        with self.assertRaises(ValueError):
            small_models.embed(['hello'], value)

    def test_offline_cpu_reranker_and_global_off_switch(self):
        import house_settings
        import small_models
        value = self.artifact('rerank')
        scores = small_models.rerank('hello', ['hello world', 'weather'], value)
        self.assertEqual(len(scores), 2)
        self.assertTrue(all(0 <= x <= 1 for x in scores))
        house_settings.save({'local_models_enabled': False}, self.root)
        with self.assertRaises(RuntimeError):
            small_models.rerank('hello', ['world'], value)


if __name__ == '__main__':
    unittest.main()
