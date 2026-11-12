"""Inline microphone capture; only recognised text leaves the recorder."""
import tempfile
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QBuffer, QIODevice, QTimer, Signal, QCoreApplication, QMicrophonePermission, Qt
from .common import run_async, LONG_POOL


def write_wav(path, raw, rate, channels, sample_format):
    import numpy as np
    dtype = {'Int16':'<i2','Int32':'<i4','Float':'<f4','UInt8':'u1'}[sample_format]
    samples = np.frombuffer(raw,dtype=dtype).astype(np.float32)
    if sample_format == 'UInt8':samples = (samples-128)/128
    elif sample_format == 'Int16':samples /= 32768
    elif sample_format == 'Int32':samples /= 2147483648
    samples = samples[:len(samples)//channels*channels].reshape(-1,channels).mean(axis=1)
    pcm = (np.clip(samples,-1,1)*32767).astype('<i2').tobytes()
    with wave.open(str(path),'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(rate);output.writeframes(pcm)


class VoiceRecorder(QObject):
    textReady = Signal(str)
    stateChanged = Signal(str, str)

    def __init__(self, parent, root, tier='small'):
        super().__init__(parent)
        self.root, self.audio, self.buffer = root, None, None
        self.tier, self.state, self._generation = tier, 'idle', 0
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.finish_recording)

    def _state(self, state, message=''):
        self.state = state
        self.stateChanged.emit(state, message)

    def _active(self, generation):
        from shiboken6 import isValid
        return isValid(self) and generation == self._generation

    def _stop_audio(self):
        self.timer.stop()
        audio, self.audio = self.audio, None
        if audio:
            audio.stop()
            audio.deleteLater()

    def _clear_buffer(self):
        if self.buffer:
            self.buffer.close()
            self.buffer.deleteLater()
            self.buffer = None

    def cancel(self):
        self._generation += 1
        self._stop_audio()
        self._clear_buffer()
        self._state('idle')

    def start(self):
        if self.state in ('permission', 'recording', 'transcribing'):
            return
        self._generation += 1
        generation = self._generation
        app = QCoreApplication.instance()
        permission = app.checkPermission(QMicrophonePermission())
        if permission == Qt.PermissionStatus.Undetermined:
            self._state('permission', '请完成麦克风授权，授权后自动开始录音。')
            def permitted(result):
                if not self._active(generation):return
                if result.status() == Qt.PermissionStatus.Granted:
                    self._start_capture(generation)
                else:
                    self._state('error', '没有麦克风权限，未开始录音。')
            try:
                app.requestPermission(QMicrophonePermission(), self, permitted)
            except (RuntimeError, TypeError):
                self._state('error', '无法请求麦克风权限，未开始录音。请检查系统授权。')
            return
        if permission!=Qt.PermissionStatus.Granted:
            self._state('error', '没有麦克风权限，未开始录音。请在系统设置中允许本应用使用麦克风。')
            return
        self._start_capture(generation)

    def _start_capture(self, generation):
        try:
            self._open_device(generation)
        except Exception as exc:
            from diagnostics import redact
            self.cancel()
            self._state('error', '无法开始录音：' + redact(str(exc))[:180])

    def _open_device(self, generation):
        from PySide6.QtMultimedia import QAudioSource,QAudioFormat,QMediaDevices,QtAudio
        device=QMediaDevices.defaultAudioInput()
        if device.isNull():
            self._state('error', '没有可用麦克风，未开始录音。');return
        format=QAudioFormat();format.setSampleRate(16000);format.setChannelCount(1);format.setSampleFormat(QAudioFormat.Int16)
        if not device.isFormatSupported(format):format=device.preferredFormat()
        self.format=format;self.buffer=QBuffer(self)
        self.buffer.open(QIODevice.WriteOnly)
        self.audio=QAudioSource(device,format,self)
        def changed(state):
            if self._active(generation) and self.audio and state==QtAudio.StoppedState and self.audio.error()!=QtAudio.NoError:
                self.cancel();self._state('error', '录音不可用，请检查麦克风连接和系统授权。')
        self.audio.stateChanged.connect(changed);self.audio.start(self.buffer)
        if not self.audio:return
        if self.audio.error()!=QtAudio.NoError:
            self.cancel();self._state('error', '录音不可用，请检查麦克风连接和系统授权。');return
        self._state('recording', '正在录音，再点一次停止并转写；最长 60 秒。')
        self.timer.start(60000)

    def finish_recording(self):
        if not self.audio:return
        self._stop_audio();raw=bytes(self.buffer.data());self._clear_buffer()
        if not raw:
            self._state('error', '没有录到声音，未转写。');return
        self._state('transcribing', '正在本机转写，完成后只填入输入框，不自动发送。')
        format, root, tier, generation = self.format, self.root, self.tier, self._generation
        def job(_):
            import voice_input
            with tempfile.TemporaryDirectory(prefix='openqq-voice-') as folder:
                path=Path(folder)/'input.wav'
                write_wav(path,raw,format.sampleRate(),format.channelCount(),format.sampleFormat().name)
                return voice_input.transcribe_local(path,tier,root)[0]
        def done(text):
            if not self._active(generation):return
            if not text.strip():return failed(ValueError('没有识别到可用文字'))
            self._state('idle', '转写完成，请确认输入框里的文字后发送。')
            self.textReady.emit(text.strip())
        def failed(error):
            if self._active(generation):
                from diagnostics import redact
                self._state('error', '转写未完成：'+redact(str(error))[:180])
        run_async(job,done,failed,pool=LONG_POOL)
