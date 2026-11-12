"""Optional Python runtimes that certain local model families need.

The installer deliberately keeps the base environment small (no PyTorch). When the user
downloads a model that needs a heavier runtime, this module installs it into the same
``.venv`` with uv, using domestic mirrors, and verifies the import — instead of shipping
3 GB in the installer or making the user read requirements files.

Rules that matter:

* Packages are requested **by name**, never from the pinned ``requirements-*.txt``.
  Those files pin ``numpy==2.2.6`` and ``transformers==4.51.3``; force-replacing numpy
  under the running app fails on Windows (the ``.pyd`` is mapped) and can break the rest
  of the environment. Requesting names lets uv keep whatever is already installed.
* PyPI's ``torch`` is the CUDA build (~2.5 GB). CPU-only is a separate wheel index
  (~260 MB), so CPU is the default; CUDA is opt-in through the settings key.
* Every install runs through a uv subprocess, so a partial install cannot corrupt the
  interpreter that is currently running this code.
* A new runtime only takes effect after the app reloads the provider; callers should tell
  the user to restart when ``install()`` reports ``restart=True``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable, Optional

PYPI_MIRRORS = (
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.org/simple',
)
# torch is not on PyPI in a CPU flavour. Aliyun and SJTU publish flat wheel directories
# (use --find-links); download.pytorch.org publishes a simple index (use --index-url).
TORCH_CPU_SOURCES = (
    ('find-links', 'https://mirrors.aliyun.com/pytorch-wheels/cpu/'),
    ('find-links', 'https://mirror.sjtu.edu.cn/pytorch-wheels/cpu/'),
    ('index', 'https://download.pytorch.org/whl/cpu'),
)
TORCH_CUDA_SOURCES = (
    ('find-links', 'https://mirrors.aliyun.com/pytorch-wheels/cu128/'),
    ('find-links', 'https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/'),
    ('index', 'https://download.pytorch.org/whl/cu128'),
)

RUNTIMES = {    'embedding': {
        'label': '本地向量与重排（PyTorch + transformers）',
        'imports': ('torch', 'transformers'),
        'packages': ('torch>=2.6', 'transformers>=4.51,<5', 'safetensors>=0.5', 'sentencepiece>=0.2',
                     'msvc-runtime>=14.40,<15; sys_platform == "win32"'),
        'mirrors': {'cpu': TORCH_CPU_SOURCES, 'cuda': TORCH_CUDA_SOURCES},
        'size_mb': {'cpu': 260, 'cuda': 2600},
        'needs_restart': True,
    },
    'asr': {
        'label': '本地语音识别',
        'imports': ('sherpa_onnx', 'numpy'),
        'packages': ('sherpa-onnx==1.13.8', 'sherpa-onnx-core==1.13.8', 'numpy>=2.2,<3'),
        'mirrors': {'cpu': (), 'cuda': ()},
        'size_mb': {'cpu': 20, 'cuda': 20},
        'needs_restart': False,
    },
    'browser': {
        'label': '真实浏览器自动化（Playwright）',
        'imports': ('playwright',),
        'packages': ('playwright',),
        'mirrors': {'cpu': (), 'cuda': ()},
        'size_mb': {'cpu': 55, 'cuda': 55},
        'needs_restart': True,
    },
}

# Which optional runtime a model category needs. Embedding and rerank share one torch
# install; the others run on what the base environment already has.
RUNTIME_FOR_KIND = {
    'embedding': 'embedding',
    'rerank': 'embedding',
    'llm': None,
    'tts': None,
    'asr': 'asr',
}


def runtimes_for(items) -> list[str]:
    """Runtime ids needed by ``[(kind, tier), ...]``, without duplicates."""
    needed = []
    for kind, _tier in items:
        runtime = RUNTIME_FOR_KIND.get(kind)
        if runtime and runtime not in needed:
            needed.append(runtime)
    return needed

_SIZE_RE = re.compile(r'\((\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB)\)')
_COUNT_RE = re.compile(r'(?:Resolved|Installed|Prepared) (\d+) packages?')
_UNITS = {'B': 1, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3}


class RuntimeUnavailable(RuntimeError):
    """The optional runtime is missing and could not be installed."""


def _project_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.environ.get('QQ_BRIDGE_HOME') or Path(__file__).resolve().parent)


def venv_python(root: Optional[Path] = None) -> Path:
    base = _project_root(root)
    name = 'python.exe' if os.name == 'nt' else 'python'
    return base / '.venv' / ('Scripts' if os.name == 'nt' else 'bin') / name


def uv_path(root: Optional[Path] = None) -> str:
    base = _project_root(root)
    name = 'uv.exe' if os.name == 'nt' else 'uv'
    for candidate in (base / '.bootstrap' / name, base / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which('uv') or ''


def variant(root: Optional[Path] = None) -> str:
    """'cuda' only when the owner opted in and a usable NVIDIA driver is present."""
    try:
        import house_settings
        value = str(house_settings.load(root).get('embedding_runtime', 'cpu'))
    except Exception:
        value = 'cpu'
    if value != 'cuda':
        return 'cpu'
    if shutil.which('nvidia-smi') is None:
        return 'cpu'
    return 'cuda'


_READY_CACHE: dict[tuple[str, str], bool] = {}


def imports_ready(kind: str, root: Optional[Path] = None, deep: bool = False) -> bool:
    """True when the interpreter that will run the model can already import it.

    The shallow check uses ``importlib.util.find_spec`` so it does not pay torch's
    multi-second import; ``deep=True`` actually imports, which is what verifies a fresh
    install. Falls back to the current interpreter when there is no ``.venv`` (running
    from a source tree), so a development setup that already has torch is recognised.
    """
    spec = RUNTIMES.get(kind)
    if not spec:
        return False
    python = venv_python(root)
    if not python.is_file():
        python = Path(sys.executable)
    key = (kind, f'{python}|{int(deep)}')
    verified_key = (kind, f'{python}|1')
    if not deep and verified_key in _READY_CACHE:
        return _READY_CACHE[verified_key]
    if not deep and key in _READY_CACHE:
        return _READY_CACHE[key]
    names = ', '.join(repr(name) for name in spec['imports'])
    if deep:
        code = f'import {", ".join(spec["imports"])}'
    else:
        code = f'import importlib.util as _u, sys; sys.exit(0 if all(_u.find_spec(n) for n in [{names}]) else 1)'
    try:
        result = subprocess.run([str(python), '-c', code], capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return False
    _READY_CACHE[key] = result.returncode == 0
    return result.returncode == 0


def ensure_windows_cpp_runtime(root: Path, emit) -> bool:
    """Deploy the installed runtime wheel beside Torch; never change system Python or Windows."""
    if os.name != 'nt':
        return True
    emit(None, '正在配置应用内 Windows C++ 运行库……')
    script = '''
from importlib.util import find_spec
from importlib.metadata import distribution
from pathlib import Path
import shutil, sys
torch = find_spec('torch')
if torch is None:
    sys.exit(1)
target = Path(torch.origin).parent / 'lib'
if not target.resolve().is_relative_to(Path(sys.prefix).resolve()):
    sys.exit(1)
runtime = distribution('msvc-runtime')
for item in runtime.files or []:
    if item.suffix.lower() == '.dll':
        destination = target / item.name
        if not destination.exists():
            shutil.copy2(runtime.locate_file(item), destination)
    elif item.name == 'LICENSE':
        destination = target / 'LICENSE-msvc-runtime.txt'
        if not destination.exists():
            shutil.copy2(runtime.locate_file(item), destination)
'''
    try:
        result = subprocess.run([str(venv_python(root)), '-X', 'utf8', '-c', script],
                                cwd=str(root), capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def describe(kind: str, root: Optional[Path] = None) -> dict:
    spec = RUNTIMES[kind]
    variant_name = variant(root)
    return {
        'kind': kind,
        'label': spec['label'],
        'needs_install': not imports_ready(kind, root),
        'variant': variant_name,
        'size_mb': spec['size_mb'][variant_name],
    }


def _install_command(kind: str, root: Optional[Path], source, pypi: str) -> list[str]:
    """Build the uv command for one source; ``source`` is ('find-links'|'index', url) or None."""
    spec = RUNTIMES[kind]
    argv = [uv_path(root), 'pip', 'install', '--python', str(venv_python(root))]
    argv.extend(spec['packages'])
    if source is None:
        argv.extend(['--index-url', pypi])
    elif source[0] == 'find-links':
        # Flat wheel directory (Aliyun/SJTU): packages come from the PyPI mirror and the
        # torch wheels are resolved from the listing.
        argv.extend(['--index-url', pypi, '--find-links', source[1]])
    else:
        argv.extend(['--index-url', source[1], '--extra-index-url', pypi])
    return argv


def _run(argv: list[str], root: Path, emit, cancel_event: Optional[threading.Event]) -> int:
    process = subprocess.Popen(argv, cwd=str(root), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=0,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    known = 0
    done = 0
    pending = ''
    try:
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            pending += chunk.decode('utf-8', 'replace')
            while True:
                cuts = [index for index in (pending.find('\r'), pending.find('\n')) if index >= 0]
                if not cuts:
                    break
                cut = min(cuts)
                line, pending = pending[:cut].strip(), pending[cut + 1:]
                if not line:
                    continue
                count = _COUNT_RE.search(line)
                if count:
                    known = max(known, int(count.group(1)))
                if line.startswith('Downloaded ') and not line[11:12].isdigit():
                    done += 1
                percent = min(99, int(done / known * 100)) if known else None
                emit(percent, line)
            if cancel_event is not None and cancel_event.is_set():
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                else:
                    process.kill()
                return -2
        if pending.strip():
            emit(None, pending.strip())
        return process.wait(timeout=120)
    finally:
        with_value = getattr(process, 'stdout', None)
        if with_value is not None:
            try:
                with_value.close()
            except Exception:
                pass


def install(kind: str, root: Optional[Path] = None, emit=None,
            cancel_event: Optional[threading.Event] = None) -> dict:
    """Install one runtime into the app's venv.

    Returns ``{'installed': bool, 'restart': bool, 'message': str}``. Raises
    ``RuntimeUnavailable`` only for configuration errors (no uv, no venv); download
    failures come back as ``installed=False`` with the reason in ``message``.
    """
    root = _project_root(root)
    spec = RUNTIMES.get(kind)
    if spec is None:
        raise RuntimeUnavailable(f'未知的可选运行环境：{kind}')
    emit = emit or (lambda percent, text: None)
    if imports_ready(kind, root, deep=True):
        _READY_CACHE.pop((kind, f'{venv_python(root)}|0'), None)
        return {'installed': True, 'restart': False, 'message': f'{spec["label"]} 已经可用'}
    if not venv_python(root).is_file():
        raise RuntimeUnavailable('找不到 .venv，请先运行安装器')
    if not uv_path(root):
        raise RuntimeUnavailable('找不到 uv.exe，请重新运行安装器修复环境')

    variant_name = variant(root)
    sources = spec['mirrors'].get(variant_name) or (None,)
    size = spec['size_mb'][variant_name]
    last_error = ''
    for position, source in enumerate(sources):
        pypi = PYPI_MIRRORS[min(position, len(PYPI_MIRRORS) - 1)]
        host = source[1] if source else pypi
        emit(0, f'准备{spec["label"]}（约 {size} MB，来源 {host}）……')
        code = _run(_install_command(kind, root, source, pypi), root, emit, cancel_event)
        if code == -2:
            return {'installed': False, 'restart': False, 'message': '已取消运行环境安装'}
        if code == 0:
            if kind == 'embedding' and not ensure_windows_cpp_runtime(root, emit):
                return {'installed': False, 'restart': False, 'message': '应用内 Windows C++ 运行库未准备好，请修复运行依赖后重试'}
            if imports_ready(kind, root, deep=True):
                _READY_CACHE.pop((kind, f'{venv_python(root)}|0'), None)
                emit(100, f'{spec["label"]} 安装完成')
                return {'installed': True, 'restart': bool(spec['needs_restart']),
                        'message': f'{spec["label"]} 已安装'}
            return {'installed': False, 'restart': False, 'message': f'{spec["label"]} 依赖已安装，但实际导入检查失败'}
        last_error = f'退出码 {code}'
        emit(None, f'{host} 不可用（{last_error}），换下一个源')
    return {'installed': False, 'restart': False,
            'message': f'{spec["label"]} 安装失败：{last_error}'}
