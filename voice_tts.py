"""Local speech synthesis for QQ voice replies.

Models are the 'tts' tiers of model_downloader.MODEL_CATALOG: small/base run on CPU by sherpa-onnx
(requirements-voice.txt); large is VoxCPM2 voice cloning, run on the GPU by voice_clone_worker.py in a
separate Python environment (requirements-voice-clone.txt, configured in build/voice-clone-runtime/runtime.json).
Each friend opts in through friend.json's `voice_reply`; the bridge sends the result as a OneBot `record`
segment and the QQ runtime transcodes the WAV to QQ's SILK.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import queue
import re
import subprocess
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Optional

MODES = ('off', 'always', 'sometimes')
DEFAULTS = {'mode': 'off', 'tier': 'small', 'speaker': None, 'speed': 1.0, 'max_chars': 80, 'ratio': 0.3,
            'reference_wav': '', 'reference_text': '', 'design': '',
            'atmosphere': {'preset': 'dry', 'noise_db': -52.0, 'reverb_wet': 0.08,
                           'reverb_rt60': 0.20, 'breath': True, 'jitter': True}}
REFERENCE_WAV = 'media/voice_reference.wav'  # the friend's clone reference, relative to the friend directory
CLONE_RUNTIME = 'build/voice-clone-runtime/runtime.json'
WORKER = Path(__file__).resolve().with_name('voice_clone_worker.py')
CLONE_DEFAULTS = {'python': '', 'device': 'auto', 'idle_unload_seconds': 600, 'load_timeout': 900, 'timeout': 180}
QQ_SAMPLE_RATE = 24000  # a SILK-native rate, so the runtime needs no ffmpeg resampling

_URL_RE = re.compile(r'https?://|www\.|```', re.I)
_MARKER_RE = re.compile(r'\[\[[^\]]*\]\]')
_SPOKEN_RE = re.compile(r'[\w一-鿿]')

_lock = threading.Lock()
_engine = {'key': None, 'tts': None}


def settings(profile: dict) -> dict:
    """The friend's voice settings with defaults applied and values clamped."""
    raw = profile.get('voice_reply') if isinstance(profile, dict) else None
    value = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    if value['mode'] not in MODES:
        value['mode'] = 'off'
    from model_downloader import MODEL_CATALOG
    if value['tier'] not in MODEL_CATALOG['tts']:
        value['tier'] = DEFAULTS['tier']
    try:
        value['speed'] = min(2.0, max(0.5, float(value['speed'])))
    except (TypeError, ValueError):
        value['speed'] = 1.0
    try:
        value['max_chars'] = min(300, max(5, int(value['max_chars'])))
    except (TypeError, ValueError):
        value['max_chars'] = DEFAULTS['max_chars']
    try:
        value['ratio'] = min(1.0, max(0.0, float(value['ratio'])))
    except (TypeError, ValueError):
        value['ratio'] = DEFAULTS['ratio']
    speakers = MODEL_CATALOG['tts'][value['tier']]['speakers']
    try:
        speaker = int(value['speaker'])
    except (TypeError, ValueError):
        speaker = None
    value['speaker'] = speaker if speaker is not None and 0 <= speaker < speakers \
        else MODEL_CATALOG['tts'][value['tier']]['default_speaker']
    for key in ('reference_wav', 'reference_text', 'design'):
        value[key] = value[key].strip()[:500] if isinstance(value[key], str) else ''
    try:
        import voice_acoustics
        value['atmosphere'] = voice_acoustics.normalize(value.get('atmosphere'))
    except Exception:
        value['atmosphere'] = {'preset': 'dry', 'noise_db': -52.0, 'reverb_wet': 0.08,
                               'reverb_rt60': 0.20, 'breath': True, 'jitter': True}
    return value


def clone_inputs(value: dict, directory: Optional[Path]) -> dict:
    """The clone tier's voice: the friend's reference clip (kept inside the friend directory) or a description."""
    reference = None
    if value['reference_wav'] and directory is not None:
        base = Path(directory).resolve()
        candidate = (base / value['reference_wav']).resolve()
        if candidate.is_relative_to(base) and candidate.is_file():
            reference = str(candidate)
    if reference is None and not value['design']:
        raise RuntimeError('声音克隆需要先上传参考语音，或填写一句声音描述')
    return {'reference_wav': reference, 'reference_text': value['reference_text'] if reference else '',
            'design': value['design']}


def speakable(text: str) -> str:
    """Text a voice can say: markers, emoji and decorative symbols removed, whitespace collapsed."""
    text = _MARKER_RE.sub(' ', text or '')
    text = ''.join(ch for ch in text if unicodedata.category(ch) not in ('So', 'Sk', 'Cs', 'Co', 'Cf', 'Mn'))
    text = ' '.join(text.split())
    return text if _SPOKEN_RE.search(text) else ''


def prompt_context(profile: dict) -> str:
    """Tell the model whether the bridge can turn its final text into QQ voice."""
    value = settings(profile)
    if value['mode'] == 'off':
        return ""
    from model_downloader import is_installed
    if not is_installed('tts', value['tier']):
        return ("VOICE_REPLY_NOT_READY: this friend opted into voice, but the local TTS model is not installed. "
                "Do not claim you can send a voice message; answer in text and say the voice model needs downloading.")
    return ("VOICE_REPLY_ENABLED: the bridge will automatically synthesize the final reply text into a QQ voice "
            "message after generation. Never claim you cannot send voice. If the user asks for voice, write a short, "
            "natural, speakable reply; do not include URLs, code blocks, markdown, or more than a few sentences.")


def requested_voice(text: str) -> bool:
    """Whether a friend explicitly asked for a spoken reply."""
    return bool(re.search(r'(发|来|整|录|听|用).{0,6}(语音|声音)|语音.{0,6}(回|发|说)|voice', text or '', re.I))


def wants_voice(profile: dict, text: str, rng=random.random, force: bool = False, forbid: bool = False) -> bool:
    """Whether this outgoing text bubble should go out as a voice message for this friend."""
    if forbid:
        return False
    value = settings(profile)
    if value['mode'] == 'off':
        return False
    if _URL_RE.search(text or '') or (text or '').count('\n') > 2:
        return False  # links and code must stay copyable
    spoken = speakable(text)
    if not spoken or len(spoken) > value['max_chars']:
        return False
    return bool(force) or value['mode'] == 'always' or rng() < value['ratio']


def _native_path(path: Path) -> str:
    """sherpa-onnx opens files through narrow-char APIs; give Windows an ASCII short path when needed."""
    text = str(path)
    if os.name != 'nt' or text.isascii():
        return text
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetShortPathNameW(text, buffer, len(buffer)) and buffer.value.isascii():
        return buffer.value
    raise RuntimeError('语音模型路径含非 ASCII 字符且系统未提供短路径，请把程序放在英文路径下')


def model_dir(tier: str, root: Optional[Path] = None) -> Path:
    from model_downloader import MODEL_CATALOG, is_installed, project_root
    if tier not in MODEL_CATALOG['tts']:
        raise ValueError(f'未知的语音模型档位: {tier}')
    if not is_installed('tts', tier, root):
        raise RuntimeError(f'语音模型（{MODEL_CATALOG["tts"][tier]["tier_name"]}）尚未下载')
    return project_root(root) / MODEL_CATALOG['tts'][tier]['target_dir']


def _build(tier: str, base: Path):
    import sherpa_onnx
    from model_downloader import MODEL_CATALOG
    cfg = MODEL_CATALOG['tts'][tier]
    path = lambda name: _native_path(base / name)
    lexicon = ','.join(path(name) for name in cfg['lexicon'].split(','))
    if cfg['engine'] == 'kokoro':
        model = sherpa_onnx.OfflineTtsModelConfig(kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=path(cfg['model_file']), voices=path('voices.bin'), tokens=path('tokens.txt'),
            lexicon=lexicon, data_dir=path('espeak-ng-data'), dict_dir=path('dict')),
            num_threads=min(4, os.cpu_count() or 1), provider='cpu')
    else:
        model = sherpa_onnx.OfflineTtsModelConfig(vits=sherpa_onnx.OfflineTtsVitsModelConfig(
            model=path(cfg['model_file']), lexicon=lexicon, tokens=path('tokens.txt')),
            num_threads=min(4, os.cpu_count() or 1), provider='cpu')
    config = sherpa_onnx.OfflineTtsConfig(model=model, rule_fsts=','.join(path(name) for name in cfg['rule_fsts']),
                                          max_num_sentences=1)
    if not config.validate():
        raise RuntimeError(f'语音模型文件不完整：{base}')
    return sherpa_onnx.OfflineTts(config)


def synthesize(text: str, tier: str = 'small', speaker: Optional[int] = None, speed: float = 1.0,
               root: Optional[Path] = None, clone: Optional[dict] = None):
    """Return (float32 samples, sample_rate). Only the most recently used CPU tier stays loaded;
    the clone tier needs `clone` from clone_inputs()."""
    spoken = speakable(text)
    if not spoken:
        raise ValueError('没有可朗读的文字')
    from model_downloader import MODEL_CATALOG
    base = model_dir(tier, root)
    if MODEL_CATALOG['tts'][tier]['engine'] == 'voxcpm':
        if not clone:
            raise RuntimeError('声音克隆需要先上传参考语音，或填写一句声音描述')
        if clone.get('design') and not clone.get('reference_wav'):
            spoken = f"({clone['design']}){spoken}"  # VoxCPM2 voice design: the description in parentheses
        return CLONE.generate(base, spoken, clone.get('reference_wav'), clone.get('reference_text'), root)
    sid = MODEL_CATALOG['tts'][tier]['default_speaker'] if speaker is None else int(speaker)
    with _lock:
        key = (tier, str(base.resolve()))
        if _engine['key'] != key:
            _engine.update(key=None, tts=None)
            _engine.update(key=key, tts=_build(tier, base))
        audio = _engine['tts'].generate(spoken, sid=sid, speed=float(speed))
    import numpy as np
    samples = np.asarray(audio.samples, dtype=np.float32)
    if not samples.size:
        raise RuntimeError('语音模型没有生成音频')
    return samples, int(audio.sample_rate)


def unload() -> None:
    with _lock:
        _engine.update(key=None, tts=None)
    CLONE.stop()


def clone_runtime(root: Optional[Path] = None) -> dict:
    from model_downloader import project_root
    path = project_root(root) / CLONE_RUNTIME
    try:
        value = json.loads(path.read_text('utf-8')) if path.is_file() else {}
    except (OSError, ValueError):
        value = {}
    return {**CLONE_DEFAULTS, **(value if isinstance(value, dict) else {})}


def save_clone_runtime(python: str, root: Optional[Path] = None) -> None:
    from model_downloader import project_root
    path = project_root(root) / CLONE_RUNTIME
    value = clone_runtime(root)
    value['python'] = python.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


class CloneRuntime:
    """One resident voice_clone_worker.py process, started on demand and stopped after it sits idle.

    Loading VoxCPM2 takes minutes, so a request while it is not ready starts loading and fails fast (the
    bridge then sends text); once loaded, requests block only for generation."""

    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.replies = None
        self.key = None
        self.state = 'stopped'  # stopped | loading | ready | failed
        self.error = ''
        self.last_used = 0.0
        self.sequence = 0

    def _spawn(self, base: Path, root: Optional[Path]):
        from model_downloader import project_root
        config = clone_runtime(root)
        python = config['python']
        if not python or not Path(python).is_file():
            raise RuntimeError('未配置声音克隆运行环境：请在语音回复设置中选择已安装 voxcpm 的 Python')
        runtime_dir = project_root(root) / Path(CLONE_RUNTIME).parent
        out = runtime_dir / 'out'
        out.mkdir(parents=True, exist_ok=True)
        with (runtime_dir / 'worker.log').open('ab') as log:
            # -I: the worker's folder (this project) must not shadow modules inside VoxCPM's environment.
            process = subprocess.Popen(
                [python, '-I', '-X', 'utf8', str(WORKER), '--model', str(base), '--device', str(config['device']),
                 '--out', str(out)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8',
                cwd=str(runtime_dir), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        replies = queue.Queue()

        def read():
            for line in process.stdout:
                try:
                    replies.put(json.loads(line))
                except ValueError:
                    continue
            replies.put(None)
        threading.Thread(target=read, name='voice-clone-reader', daemon=True).start()
        return process, replies, config

    def _load(self, base: Path, root: Optional[Path]):
        try:
            process, replies, config = self._spawn(base, root)
        except Exception as exc:
            with self.lock:
                self.state, self.error = 'failed', str(exc)
            return
        with self.lock:
            self.process, self.replies = process, replies
        try:
            ready = replies.get(timeout=float(config['load_timeout']))
        except queue.Empty:
            ready = {'ready': False, 'error': '加载超时'}
        with self.lock:
            if ready and ready.get('ready'):
                self.state, self.error, self.last_used = 'ready', '', time.monotonic()
                self._watch_idle(float(config['idle_unload_seconds']))
            else:
                self.state = 'failed'
                self.error = (ready or {}).get('error') or '克隆进程已退出，详见 build/voice-clone-runtime/worker.log'
                self._kill(keep_key=True)

    def _watch_idle(self, seconds: float):
        process = self.process

        def watch():
            while True:
                time.sleep(min(30.0, seconds))
                with self.lock:
                    if self.process is not process or self.state != 'ready':
                        return
                    if time.monotonic() - self.last_used >= seconds:
                        self._kill()
                        self.state = 'stopped'
                        return
        threading.Thread(target=watch, name='voice-clone-idle', daemon=True).start()

    def _kill(self, keep_key: bool = False):
        process, self.process, self.replies = self.process, None, None
        if not keep_key:
            self.key = None
        if process is not None:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except Exception:
                process.kill()

    def start(self, base: Path, root: Optional[Path] = None):
        """Begin loading in the background; returns at once."""
        key = str(Path(base).resolve())
        with self.lock:
            if self.key == key and self.state in ('loading', 'ready'):
                return
            self._kill()
            self.key, self.state, self.error = key, 'loading', ''
        threading.Thread(target=self._load, args=(Path(base), root), name='voice-clone-load', daemon=True).start()

    def stop(self):
        with self.lock:
            self._kill()
            self.state = 'stopped'

    def status(self) -> tuple[str, str]:
        with self.lock:
            return self.state, self.error

    def wait_ready(self, base: Path, root: Optional[Path] = None, report=None) -> None:
        """Load and block until ready (desktop preview); the bridge never waits like this."""
        self.start(base, root)
        deadline = time.monotonic() + float(clone_runtime(root)['load_timeout'])
        while time.monotonic() < deadline:
            state, error = self.status()
            if state == 'ready':
                return
            if state != 'loading':
                raise RuntimeError('声音克隆进程启动失败：' + (error or state))
            if report:
                report('正在加载声音克隆模型（首次约需数分钟）…')
            time.sleep(2)
        raise RuntimeError('声音克隆模型加载超时')

    def generate(self, base: Path, text: str, reference_wav: Optional[str], reference_text: Optional[str],
                 root: Optional[Path] = None):
        key = str(Path(base).resolve())
        with self.lock:
            state, error = (self.state, self.error) if self.key == key else ('stopped', '')
            if state == 'failed':
                self.state = 'stopped'  # the next message tries again
        if state == 'failed':
            raise RuntimeError('声音克隆进程启动失败：' + error)
        if state != 'ready':
            self.start(base, root)
            raise RuntimeError('声音克隆模型正在加载（首次约需数分钟），本条先发文字')
        timeout = float(clone_runtime(root)['timeout'])
        with self.lock:
            if self.state != 'ready' or self.process is None:
                raise RuntimeError('声音克隆进程已停止，将在下一条消息重新加载')
            self.sequence += 1
            request = {'id': self.sequence, 'text': text, 'reference_wav': reference_wav or '',
                       'prompt_text': reference_text or ''}
            try:
                self.process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                reply = self.replies.get(timeout=timeout)
            except (OSError, queue.Empty):
                reply = None
            self.last_used = time.monotonic()
            if reply is None or reply.get('id') != request['id']:
                self._kill()  # an unanswered request would desynchronise the reply stream
                self.state = 'stopped'
                raise RuntimeError('声音克隆超时或进程退出')
        if not reply.get('ok'):
            raise RuntimeError('声音克隆失败：' + str(reply.get('error'))[:200])
        import numpy as np
        path = Path(reply['wav'])
        try:
            with wave.open(str(path), 'rb') as reader:
                pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
                rate = reader.getframerate()
        finally:
            path.unlink(missing_ok=True)
        if not pcm.size:
            raise RuntimeError('声音克隆没有生成音频')
        return pcm.astype(np.float32) / 32768.0, int(rate)


CLONE = CloneRuntime()


def wav_bytes(samples, sample_rate: int, target_rate: int = QQ_SAMPLE_RATE) -> bytes:
    """Mono 16-bit PCM WAV at target_rate, peak-limited."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate != target_rate:
        count = max(1, int(round(samples.size * target_rate / sample_rate)))
        samples = np.interp(np.linspace(0, samples.size - 1, count), np.arange(samples.size), samples)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.99:
        samples = samples * (0.99 / peak)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(target_rate)
        writer.writeframes(pcm.tobytes())
    return out.getvalue()


def record_segment(text: str, profile: dict, root: Optional[Path] = None, directory: Optional[Path] = None) -> dict:
    """A OneBot 11 `record` segment speaking `text` with the friend's voice settings."""
    from model_downloader import MODEL_CATALOG
    value = settings(profile)
    clone = clone_inputs(value, directory) if MODEL_CATALOG['tts'][value['tier']]['engine'] == 'voxcpm' else None
    samples, rate = synthesize(text, value['tier'], value['speaker'], value['speed'], root, clone)
    atmosphere = value.get('atmosphere') or {}
    if atmosphere.get('preset') not in (None, '', 'dry'):
        try:
            import voice_acoustics
            samples = voice_acoustics.apply(samples, rate, atmosphere, seed=str(directory or ''))
        except Exception:
            pass  # an optional effect must never stop a voice message from being sent
    payload = wav_bytes(samples, rate)
    if directory is not None:
        folder = Path(directory) / 'media' / 'voice'
        folder.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256((text or '').encode('utf-8')).hexdigest() + '.wav'
        (folder / name).write_bytes(payload)
    return {'type': 'record', 'data': {'file': 'base64://' + base64.b64encode(payload).decode('ascii')}}
