import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import pytest
from unittest.mock import Mock, patch
import urllib.error

import inference_device as policy


def torch_stub(cuda=False, mps=False):
    return SimpleNamespace(cuda=SimpleNamespace(is_available=Mock(return_value=cuda),empty_cache=Mock()),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=Mock(return_value=mps))),
        mps=SimpleNamespace(empty_cache=Mock()),float16='fp16',float32='fp32')


class DevicePolicy(unittest.TestCase):
    def test_auto_priority_cuda_then_mps_then_cpu(self):
        self.assertEqual(policy.torch_devices(torch_stub(True,True)),['cuda:0','mps','cpu'])
        self.assertEqual(policy.torch_devices(torch_stub(False,True)),['mps','cpu'])
        self.assertEqual(policy.torch_devices(torch_stub()),['cpu'])

    def test_forced_cpu_never_probes_gpu(self):
        torch=torch_stub(True,True)
        self.assertEqual(policy.torch_devices(torch,'cpu'),['cpu'])
        torch.cuda.is_available.assert_not_called()
        torch.backends.mps.is_available.assert_not_called()

    def test_manual_unavailable_never_falls_back(self):
        for requested in ('cuda','mps'):
            with self.assertRaises(RuntimeError):
                policy.torch_devices(torch_stub(),requested)
        with self.assertRaises(ValueError):
            policy.preference('invalid')
        self.assertEqual(policy.preference('metal'),'mps')
        self.assertEqual(policy.preference('CUDA preferred; last actual CUDA verified RTX5070'),'auto')

    def test_precision_is_half_for_accelerators_and_float_for_cpu(self):
        torch=torch_stub()
        self.assertEqual(policy.dtype(torch,'cuda:0'),'fp16')
        self.assertEqual(policy.dtype(torch,'mps'),'fp16')
        self.assertEqual(policy.dtype(torch,'cpu'),'fp32')

    def test_oom_releases_failed_model_before_next_device(self):
        torch=torch_stub(True,True);events=[]
        torch.cuda.empty_cache.side_effect=lambda:events.append('cuda-released')
        def load(device):
            events.append(device)
            if device=='cuda:0':
                raise RuntimeError('CUDA out of memory')
            return device
        with self.assertLogs(policy.LOG,level='WARNING') as log:
            result=policy.run_torch(torch,'auto',load,lambda:events.append('drop'),component='test')
        self.assertEqual(result,'mps')
        self.assertEqual(events,['cuda:0','drop','cuda-released','mps'])
        self.assertIn('falling back',log.output[0])

    def test_inference_mps_operator_error_falls_back_to_cpu(self):
        torch=torch_stub(False,True);drop=Mock();attempt=Mock(return_value='cpu-result')
        result=policy.retry_torch(torch,'auto',NotImplementedError('operator is not currently implemented for the MPS device'),
            'mps',attempt,drop,component='test')
        self.assertEqual(result,'cpu-result');attempt.assert_called_once_with('cpu')
        drop.assert_called_once();torch.mps.empty_cache.assert_called_once()

    def test_manual_oom_releases_without_silent_fallback(self):
        torch=torch_stub(True,False);attempt=Mock(side_effect=RuntimeError('CUDA out of memory'));drop=Mock()
        with self.assertRaisesRegex(RuntimeError,'out of memory'):
            policy.run_torch(torch,'cuda',attempt,drop,component='test')
        attempt.assert_called_once_with('cuda:0');drop.assert_called_once()

    def test_model_config_and_program_errors_are_not_device_fallbacks(self):
        for error in (FileNotFoundError('CUDA model file missing'),ValueError('invalid config'),
                      RuntimeError('size mismatch in state_dict'),RuntimeError('tensor shape mismatch'),
                      RuntimeError('safetensors invalid header')):
            attempt=Mock(side_effect=error)
            with self.assertRaises(type(error)):
                policy.run_torch(torch_stub(True,True),'auto',attempt,Mock(),component='test')
            self.assertEqual(attempt.call_count,1)

    def test_llama_cuda_metal_cpu_priority_without_importing_torch(self):
        runner=Mock(return_value=SimpleNamespace(stdout=b'Available devices:\nCUDA0: NVIDIA\nMetal: Apple',returncode=0))
        with patch.object(policy.subprocess,'run',runner):
            choices=policy.llama_devices(Path('.'),{'server':'llama-server','cpu_server':'llama-cpu'},'auto')
        self.assertEqual([choice[1] for choice in choices],['cuda','metal','cpu'])
        self.assertEqual(choices[-1][2],['--device','none'])

    def test_llama_metal_is_detected_from_native_binary(self):
        with patch.object(policy.subprocess,'run',return_value=SimpleNamespace(stdout='Metal0: Apple M4',returncode=0)):
            choices=policy.llama_devices(Path('.'),{'server':'llama-server'},'auto')
        self.assertEqual([choice[1] for choice in choices],['metal','cpu'])

    def test_llama_forced_cpu_does_not_probe_accelerators(self):
        with patch.object(policy.subprocess,'run') as probe:
            choices=policy.llama_devices(Path('.'),{'server':'server','cpu_server':'cpu-server'},'cpu')
        probe.assert_not_called();self.assertEqual(choices[0][1],'cpu')

    def test_llama_manual_cuda_does_not_use_metal_or_cpu(self):
        with patch.object(policy.subprocess,'run',return_value=SimpleNamespace(stdout='Metal: Apple',returncode=0)):
            with self.assertRaises(RuntimeError):
                policy.llama_devices(Path('.'),{'server':'server'},'cuda')

    def test_llama_probe_configuration_error_is_not_hidden(self):
        with patch.object(policy.subprocess,'run',return_value=SimpleNamespace(stdout='invalid argument --list-devices',returncode=1)):
            with self.assertRaisesRegex(RuntimeError,'probe failed'):
                policy.llama_devices(Path('.'),{'server':'server'},'auto')

    def test_llama_inference_error_retries_only_backend_failures(self):
        instance=SimpleNamespace(actual_device='cuda',_process=None,_recover_backend=Mock())
        error=urllib.error.HTTPError('http://local',500,'error',{},io.BytesIO(b'CUDA out of memory'))
        send=Mock(side_effect=[error,{'choices':[]}])
        self.assertEqual(policy.llama_request(instance,'/v1/chat/completions',send),{'choices':[]})
        instance._recover_backend.assert_called_once_with('cuda')
        invalid=urllib.error.HTTPError('http://local',500,'error',{},io.BytesIO(b'invalid model file header'))
        with self.assertRaises(urllib.error.HTTPError):
            policy.llama_request(instance,'/v1/chat/completions',Mock(side_effect=invalid))

    def test_runtime_generation_preserves_existing_tuning_and_binary(self):
        import model_downloader
        existing={'server':'bin/native-server','cpu_server':'cpu/native-server','metal_server':'metal/native-server',
                  'device':'mps','ctx':8192,'slots':2,'server_args':['--kv-unified']}
        generated=model_downloader.runtime_manifest('new.gguf',existing)
        self.assertEqual(generated['model'],'new.gguf')
        for key,value in existing.items():
            self.assertEqual(generated[key],value)
        with patch.object(model_downloader.os,'name','posix'):
            self.assertEqual(model_downloader.runtime_manifest('new.gguf')['server'],'llama-server')

    def test_global_and_component_preferences_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'house.local.json').write_text(json.dumps({'inference_device':'auto','local_model_devices':{'rerank':'cpu'}}))
            self.assertEqual(policy.preference(component='embedding',project_root=root),'auto')
            self.assertEqual(policy.preference(component='rerank',project_root=root),'cpu')

    def test_annotation_metal_start_preserves_gpu_offload(self):
        from dataset_annotation.annotate_client import AnnotationClient
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);runtime=root/'build/message-classifier-runtime';runtime.mkdir(parents=True)
            (runtime/'model.gguf').write_bytes(b'synthetic')
            (runtime/'server').write_bytes(b'synthetic binary')
            (runtime/'runtime.json').write_text(json.dumps({'server':'server','model':'model.gguf'}))
            process=Mock();process.poll.return_value=None
            with patch.object(policy.subprocess,'run',return_value=Mock(stdout=b'Metal: Apple',returncode=0)),patch.object(policy.subprocess,'Popen',return_value=process) as launch:
                client=AnnotationClient(root,slots=1)
                try:
                    with patch.object(client,'_request',return_value={'status':'ok'}):
                        client.start()
                    args=launch.call_args.args[0]
                    self.assertEqual(client.actual_device,'metal')
                    self.assertEqual(args[args.index('-ngl')+1],'99')
                finally:
                    client.close()

    def test_annotation_forced_cpu_without_separate_cpu_binary(self):
        from dataset_annotation.annotate_client import AnnotationClient
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);runtime=root/'build/message-classifier-runtime';runtime.mkdir(parents=True)
            (runtime/'model.gguf').write_bytes(b'synthetic');(runtime/'server').write_bytes(b'synthetic binary')
            (runtime/'runtime.json').write_text(json.dumps({'server':'server','model':'model.gguf'}))
            process=Mock();process.poll.return_value=None
            with patch.object(policy.subprocess,'run') as probe,patch.object(policy.subprocess,'Popen',return_value=process) as launch:
                client=AnnotationClient(root,force_cpu=True)
                try:
                    with patch.object(client,'_request',return_value={'status':'ok'}):
                        client.start()
                    probe.assert_not_called()
                    args=launch.call_args.args[0]
                    self.assertEqual(client.actual_device,'cpu')
                    self.assertEqual(args[args.index('--device')+1],'none')
                finally:
                    client.close()

    def test_stale_reranker_idle_timer_cannot_unload_a_recent_call(self):
        import reranker
        model=object();timer=Mock()
        with patch.dict(reranker._state,{'generation':2,'model':model,'timer':timer}):
            reranker._unload(expected_generation=1)
            self.assertIs(reranker._state['model'],model)
            timer.cancel.assert_not_called()


if __name__=='__main__':
    unittest.main()


def test_directml_is_optional_and_after_cuda_mps():
    torch = torch_stub()
    with patch.object(policy, '_directml_device', return_value='privateuseone:0'):
        assert policy.torch_devices(torch) == ['privateuseone:0', 'cpu']
        assert policy.torch_devices(torch, 'directml') == ['privateuseone:0']
    with patch.object(policy, '_directml_device', return_value=None):
        with pytest.raises(RuntimeError):
            policy.torch_devices(torch, 'directml')
