"""Real CPU forwards using tiny random local XLM-R artifacts, with no downloads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('torch') and importlib.util.find_spec('transformers'),
                     'optional model dependencies missing')
class LocalCPU(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'QQ_BRIDGE_HOME':str(self.root),'HF_HUB_OFFLINE':'1'})
        self.env.start();self.addCleanup(self.env.stop);self.addCleanup(self.temp.cleanup)
        (self.root/'house.local.json').write_text(json.dumps({'local_models_enabled':True,'inference_device':'cpu'}))

    def artifact(self,kind):
        import torch
        from transformers import XLMRobertaConfig,XLMRobertaModel,XLMRobertaForSequenceClassification,BertTokenizerFast
        import embedding_provider,reranker
        directory=self.root/kind;directory.mkdir()
        (directory/'vocab.txt').write_text('[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\nhello\nworld\nweather\n')
        tokenizer=BertTokenizerFast(vocab_file=str(directory/'vocab.txt'));tokenizer.save_pretrained(directory)
        config=XLMRobertaConfig(vocab_size=8,hidden_size=8,num_hidden_layers=1,num_attention_heads=2,
                               intermediate_size=16,max_position_embeddings=64,num_labels=1)
        torch.manual_seed(42)
        model=XLMRobertaModel(config) if kind=='embedding' else XLMRobertaForSequenceClassification(config)
        model.save_pretrained(directory)
        module=embedding_provider if kind=='embedding' else reranker
        files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
        (directory/'model_source.json').write_text(json.dumps({'repo_id':module.MODEL,'revision':module.REVISION,'files':files}))
        return directory

    def test_bge_encoder_cpu_load_forward_and_precision(self):
        import embedding_provider as embedding
        directory=self.artifact('embedding')
        import model_downloader
        self.assertEqual(model_downloader._convert_to_fp16(directory),'fp16')
        with patch.object(embedding,'DIMENSION',8):
            backend=embedding._DenseBackend(directory)
        self.assertEqual(backend.device,'cpu');self.assertEqual(backend.dtype,'float32')
        self.assertEqual(next(backend.model.parameters()).device.type,'cpu')
        vectors=backend.encode(['hello world','weather'])
        self.assertEqual([len(v) for v in vectors],[8,8])
        self.assertAlmostEqual(sum(x*x for x in vectors[0]),1,places=5)

    def test_bge_reranker_cpu_load_forward_and_release(self):
        import reranker
        directory=self.artifact('rerank')
        previous=dict(reranker._state)
        try:
            with patch.object(reranker,'DIRECTORY',directory),patch.object(reranker,'IDLE_UNLOAD_SECONDS',None):
                reranker._state.update(model=None,tokenizer=None,verified=False,timer=None)
                scores=reranker.score('hello',['hello world','weather'])
                self.assertEqual(reranker._state['device'],'cpu')
                self.assertEqual(str(next(reranker._state['model'].parameters()).dtype),'torch.float32')
                self.assertEqual(len(scores),2);self.assertTrue(all(0<=x<=1 for x in scores))
                reranker._unload();self.assertIsNone(reranker._state['model'])
        finally:
            reranker._state.clear();reranker._state.update(previous)

    def test_embedding_idle_unload_and_demand_reload_still_work_on_cpu(self):
        import embedding_provider as embedding
        directory=self.artifact('embedding')
        with patch.object(embedding,'DIMENSION',8),patch.object(embedding,'IDLE_UNLOAD_SECONDS',.1):
            worker=embedding._EmbeddingWorker(directory)
            first=worker.submit(('hello',),0,lambda:None)
            self.assertEqual(len(first.vectors),1)
            deadline=time.monotonic()+3
            while worker.metrics['unload_count']==0 and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertEqual(worker.metrics['unload_count'],1)
            worker.submit(('world',),0,lambda:None)
            self.assertEqual(worker.metrics['load_count'],2)
            self.assertEqual(worker.metrics['device'],'cpu')
            worker._unload()

    def test_corrupt_weights_are_not_relabelled_as_device_fallback(self):
        import embedding_provider as embedding
        from persona_store import Unavailable
        directory=self.artifact('embedding')
        (directory/'model.safetensors').write_bytes(b'corrupt')
        with patch.object(embedding,'DIMENSION',8),self.assertRaises(Unavailable):
            embedding._DenseBackend(directory)


if __name__=='__main__':
    unittest.main()
