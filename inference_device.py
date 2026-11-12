"""Shared device policy, not a model manager. Torch and llama backends stay separate."""
import gc
import logging
import re
import subprocess
import traceback
import urllib.error
from pathlib import Path

LOG = logging.getLogger(__name__)
DEVICES = ('auto', 'cuda', 'mps', 'directml', 'cpu')


def preference(value=None, component=None, project_root=None):
    if value is None:
        from house_settings import load
        settings = load(project_root)
        value = (settings.get('local_model_devices') or {}).get(component, settings.get('inference_device', 'auto'))
    # Compatibility with the old runtime manifest's descriptive device field.
    if isinstance(value, str) and value.lower().startswith('cuda preferred;'):
        value = 'auto'
    if not isinstance(value, str):
        raise ValueError('设备配置须为字符串')
    value = {'cuda:0': 'cuda', 'metal': 'mps', 'dml': 'directml',
             'privateuseone:0': 'directml'}.get(value, value)
    if value not in DEVICES:
        raise ValueError('设备必须是 auto、cuda、mps、directml 或 cpu')
    return value


def _directml_device():
    """Return the torch-directml device string, or None when the package/GPU is absent."""
    try:
        import torch_directml
        return str(torch_directml.device())
    except Exception:
        return None


def torch_devices(torch, requested='auto'):
    requested = preference(requested)
    if requested == 'cpu':
        return ['cpu']
    try:
        cuda = bool(torch.cuda.is_available())
    except RuntimeError as exc:
        if requested != 'auto' or not device_error(exc, 'cuda:0'):
            raise
        LOG.warning('CUDA detection failed (%s); checking other backends', str(exc)[:240])
        cuda = False
    if requested == 'cuda':
        if not cuda:
            raise RuntimeError('手动指定的 cuda 不可用；未自动切换设备')
        return ['cuda:0']
    if requested == 'directml':
        directml = _directml_device()
        if not directml:
            raise RuntimeError('手动指定的 directml 不可用；需要 Windows AMD 显卡和 torch-directml')
        return [directml]
    mps_backend = getattr(torch.backends, 'mps', None)
    mps = bool(mps_backend and mps_backend.is_available())
    directml = _directml_device() if requested == 'auto' else None
    available = (['cuda:0'] if cuda else []) + (['mps'] if mps else []) + ([directml] if directml else []) + ['cpu']
    if requested == 'auto':
        return available
    selected = 'cuda:0' if requested == 'cuda' else requested
    if selected not in available:
        raise RuntimeError(f'手动指定的 {requested} 不可用；未自动切换设备')
    return [selected]


def dtype(torch, device):
    return torch.float32 if device == 'cpu' else torch.float16


def device_error(error, device):
    """Only explicit accelerator/resource failures; never broad RuntimeError fallback."""
    if device == 'cpu' or isinstance(error, (ValueError, KeyError, OSError, EOFError)):
        return False
    if 'safetensor' in type(error).__module__.lower() or type(error).__name__ in ('UnpicklingError', 'BadZipFile'):
        return False
    text = str(error).lower()
    # Model/config/state-dict errors take precedence over backend words in filenames.
    if any(word in text for word in ('size mismatch', 'missing key(s)', 'safetensor', 'invalid header',
                                     'file not found', 'failed to read', 'invalid magic', 'unknown model architecture', 'unexpected key')):
        return False
    compatible_failure = any(word in text for word in (
        'cuda out of memory', 'cuda error:', 'cuda driver', 'not compiled with cuda',
        'no kernel image', 'invalid device ordinal', 'cublas_status_alloc_failed',
        'cublas_status_not_supported', 'cudnn_status_not_supported', 'libcudart',
        'mps backend out of memory', 'mps backend is not available', 'mps device not found',
        'not implemented for mps', 'not implemented for the mps', "not implemented for 'mps'",
        "not implemented for 'half'", 'metal buffer allocation', 'metal out of memory',
        'ggml_cuda_init: failed', 'ggml_backend_cuda_buffer_type_alloc_buffer: allocating',
        'ggml_metal_buffer_init: failed', 'failed to allocate cuda', 'failed to allocate metal',
        'ggml_metal_init: failed', 'ggml_metal_device_init: failed',
        'ggml_metal_library_init: failed', 'failed to compile metal', 'failed to create metal',
        'directml', 'privateuseone', 'dml backend', 'dml device',
    ))
    return compatible_failure or bool(re.search(
        r'(?:not (?:currently )?(?:implemented|supported).{0,100}\b(?:mps|metal)\b|\b(?:mps|metal)\b.{0,100}(?:not supported|does not support|doesn.t support))', text))


def release(torch, device):
    gc.collect()
    if str(device).startswith('cuda'):
        torch.cuda.empty_cache()
    elif device == 'mps' and getattr(torch, 'mps', None):
        torch.mps.empty_cache()


def run_torch(torch, requested, attempt, cleanup, *, component, current=None):
    choices = torch_devices(torch, requested)
    if current in choices:
        choices = choices[choices.index(current):]
    for index, device in enumerate(choices):
        try:
            LOG.info('%s: requested=%s selected=%s dtype=%s', component, requested, device,
                     'float32' if device == 'cpu' else 'float16')
            return attempt(device)
        except Exception as exc:
            eligible = requested == 'auto' and device_error(exc, device) and index + 1 < len(choices)
            if not eligible:
                traceback.clear_frames(exc.__traceback__)
                cleanup()
                release(torch, device)
                raise
            reason = str(exc).splitlines()[0][:240]
            traceback.clear_frames(exc.__traceback__)
        # The exception frame has gone before releasing failed accelerator allocations.
        cleanup()
        release(torch, device)
        LOG.warning('%s: %s failed (%s); falling back to %s', component, device, reason, choices[index + 1])


def retry_torch(torch, requested, error, current, attempt, cleanup, *, component):
    """Called only after a failed inference; the healthy CUDA path has no new probes."""
    if not device_error(error, current):
        raise error
    choices = torch_devices(torch, requested)
    following = choices[choices.index(current) + 1:] if current in choices else choices
    reason = str(error).splitlines()[0][:240]
    traceback.clear_frames(error.__traceback__)
    cleanup()
    release(torch, current)
    if requested != 'auto' or not following:
        raise error
    LOG.warning('%s: inference on %s failed (%s); falling back to %s', component, current, reason, following[0])
    return run_torch(torch, requested, attempt, cleanup, component=component, current=following[0])


def llama_devices(runtime_dir, runtime, requested=None, *, component='classifier', project_root=None):
    """Probe llama.cpp binaries directly. Never infer Metal support from torch MPS."""
    requested = preference(runtime.get('device') if requested is None and 'device' in runtime else requested,
                           component, project_root)
    if requested == 'directml':
        raise RuntimeError('llama.cpp 不支持 DirectML；本地大模型请选 CPU/CUDA/Metal')
    runtime_dir = Path(runtime_dir)
    cpu = runtime_dir / runtime.get('cpu_server', runtime['server'])
    if requested == 'cpu':
        return [(cpu, 'cpu', ['--device', 'none'])]
    probes = {}
    def probe(path):
        if path not in probes:
            result = subprocess.run([str(path), '--list-devices'], cwd=runtime_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            text = result.stdout.decode('utf-8', errors='replace') if isinstance(result.stdout, bytes) else result.stdout
            if result.returncode:
                if not device_error(RuntimeError(text), 'cuda'):
                    raise RuntimeError('llama.cpp device probe failed: ' + str(text)[:240])
                LOG.warning('llama.cpp GPU probe failed; trying available backend binaries: %s', str(text)[:240])
                text = ''
            probes[path] = str(text)
        return probes[path]
    server = runtime_dir / (runtime.get('metal_server', runtime['server']) if requested == 'mps' else runtime['server'])
    cuda_text = probe(server)
    cuda_names = list(dict.fromkeys(re.findall(r'\bCUDA\d+\b', cuda_text)))
    choices = []
    if cuda_names and requested in ('auto', 'cuda'):
        choices.append((server, 'cuda', ['--device', ','.join(cuda_names)]))
    if requested in ('auto', 'mps'):
        metal_server = runtime_dir / runtime.get('metal_server', runtime['server'])
        metal_text = cuda_text if metal_server == server else probe(metal_server)
        metal_names = list(dict.fromkeys(re.findall(r'\bMetal\d*(?=\s*:)', metal_text, re.IGNORECASE)))
        if metal_names:
            choices.append((metal_server, 'metal', ['--device', ','.join(metal_names)]))
    if requested == 'auto':
        choices.append((cpu, 'cpu', ['--device', 'none']))
    if not choices:
        raise RuntimeError(f'手动指定的 llama.cpp {requested} 后端未被二进制检测到；未切换设备')
    return choices


def llama_request(instance, path, send):
    """Retry failed inference only when the server reports an accelerator failure."""
    if path != '/v1/chat/completions':
        return send()
    for _ in range(3):
        device = getattr(instance, 'actual_device', None)
        try:
            return send()
        except (OSError, RuntimeError) as exc:
            diagnostic = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code < 500:
                    raise
                diagnostic += ' ' + exc.read(16384).decode('utf-8', errors='replace')
            elif instance._process is not None and instance._process.poll() is not None:
                try:
                    diagnostic += ' ' + instance.server_log_path.read_text('utf-8', errors='replace')[-8192:]
                except OSError:
                    pass
            if not device_error(RuntimeError(diagnostic), device):
                raise
            instance._recover_backend(device)
    raise RuntimeError('llama.cpp compatible backends exhausted')


def stop_llama_process(process, timeout=5):
    """Release the owned native server and any child processes before switching backends."""
    if process is None or process.poll() is not None:
        return
    children = []
    try:
        import psutil
        children = psutil.Process(process.pid).children(recursive=True)
    except (ImportError, TypeError, ValueError, OSError):
        pass
    except Exception as exc:
        # A process may already have exited; do not hide an unrelated model error.
        LOG.debug('Server child enumeration: %s', type(exc).__name__)
    for child in reversed(children):
        try:
            child.terminate()
        except OSError:
            pass
        except Exception:
            continue
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for child in children:
        try:
            child.wait(timeout=1)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
