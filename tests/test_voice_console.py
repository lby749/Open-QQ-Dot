import os,wave
from unittest.mock import Mock
import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication,QTimer,Qt,QObject,Signal
from PySide6.QtWidgets import QApplication,QWidget
from house_qt.voice_capture import VoiceRecorder,write_wav
import house_qt.voice_capture as capture
import voice_input

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')


def test_audio_conversion_handles_real_device_float_stereo(tmp_path):
    path=tmp_path/'audio.wav'
    data=np.array([[.5,-.5],[1,1],[-1,-1]],dtype='<f4').tobytes()
    write_wav(path,data,48000,2,'Float')
    with wave.open(str(path),'rb') as source:
        assert source.getnchannels()==1 and source.getsampwidth()==2 and source.getframerate()==48000
        assert np.frombuffer(source.readframes(3),dtype='<i2').tolist()==[0,32767,-32767]


def test_microphone_denial_is_explicit_and_never_starts_capture(tmp_path,monkeypatch):
    from types import SimpleNamespace
    app=QApplication.instance() or QApplication([]);parent=QWidget();recorder=VoiceRecorder(parent,tmp_path)
    states=[];recorder.stateChanged.connect(lambda state,text:states.append((state,text)))
    monkeypatch.setattr(app,'checkPermission',lambda _:Qt.PermissionStatus.Denied)
    try:
        recorder.start()
        assert recorder.audio is None and recorder.state=='error' and '没有麦克风权限' in states[-1][1]
    finally:recorder.cancel();parent.close()


@pytest.fixture
def microphone(monkeypatch):
    from types import SimpleNamespace
    from PySide6 import QtMultimedia as audio
    app=QApplication.instance() or QApplication([])
    permission=SimpleNamespace(checkPermission=lambda _:Qt.PermissionStatus.Granted)
    monkeypatch.setattr(app,'checkPermission',lambda p:permission.checkPermission(p))
    monkeypatch.setattr(app,'requestPermission',lambda p,context,done:permission.requestPermission(p,context,done))
    monkeypatch.setattr(audio.QMediaDevices,'defaultAudioInput',lambda:SimpleNamespace(
        isNull=lambda:False,isFormatSupported=lambda _:True))
    created=[]
    class Source(QObject):
        stateChanged=Signal(object)
        def __init__(self,device,format,parent):
            super().__init__(parent);self.stopped=False;self.pcm=np.zeros(1600,dtype='<i2').tobytes();created.append(self)
        def start(self,buffer):buffer.write(self.pcm)
        def error(self):return audio.QtAudio.NoError
        def stop(self):self.stopped=True
    monkeypatch.setattr(audio,'QAudioSource',Source)
    return permission,created


def test_inline_recording_stops_once_and_transcribes_without_dialog(tmp_path,monkeypatch,microphone):
    parent=QWidget();recorder=VoiceRecorder(parent,tmp_path);pending=[];texts=[]
    recorder.textReady.connect(texts.append)
    monkeypatch.setattr(capture,'run_async',lambda job,done,error,**kw:pending.append((job,done,error)))
    def transcribe(path,tier,root):
        with wave.open(str(path),'rb') as wav:
            assert wav.getframerate()==16000 and wav.getnchannels()==1 and wav.getnframes()==1600
        return '识别出的中文',''
    monkeypatch.setattr(voice_input,'transcribe_local',transcribe)
    try:
        recorder.start();recorder.start()
        assert recorder.state=='recording' and len(microphone[1])==1
        recorder.finish_recording();recorder.finish_recording()
        assert recorder.state=='transcribing' and microphone[1][0].stopped and len(pending)==1
        job,done,_=pending[0];done(job(None))
        assert recorder.state=='idle' and texts==['识别出的中文']
    finally:recorder.cancel();parent.close()


def test_cancel_ignores_old_transcription_while_new_recording_is_active(tmp_path,monkeypatch,microphone):
    parent=QWidget();recorder=VoiceRecorder(parent,tmp_path);pending=[];texts=[]
    recorder.textReady.connect(texts.append)
    monkeypatch.setattr(capture,'run_async',lambda job,done,error,**kw:pending.append((done,error)))
    try:
        recorder.start();recorder.finish_recording();recorder.cancel();recorder.start()
        pending[0][0]('旧会话的文字');pending[0][1](ValueError('旧错误'))
        assert recorder.state=='recording' and texts==[]
        recorder.timer.timeout.emit()
        assert recorder.state=='transcribing' and microphone[1][-1].stopped
    finally:recorder.cancel();parent.close()


def test_permission_result_after_cancel_does_not_start_microphone(tmp_path,monkeypatch,microphone):
    from types import SimpleNamespace
    callbacks=[];permission=microphone[0]
    permission.checkPermission=lambda _:Qt.PermissionStatus.Undetermined
    permission.requestPermission=lambda p,context,done:callbacks.append(done)
    parent=QWidget();recorder=VoiceRecorder(parent,tmp_path)
    try:
        recorder.start()
        assert recorder.state=='permission' and not microphone[1]
        recorder.cancel();callbacks[0](SimpleNamespace(status=lambda:Qt.PermissionStatus.Granted))
        assert recorder.state=='idle' and not microphone[1]
    finally:recorder.cancel();parent.close()


def test_permission_grant_starts_without_another_click(tmp_path,monkeypatch,microphone):
    from types import SimpleNamespace
    callbacks=[];permission=microphone[0]
    permission.checkPermission=lambda _:Qt.PermissionStatus.Undetermined
    permission.requestPermission=lambda p,context,done:callbacks.append(done)
    parent=QWidget();recorder=VoiceRecorder(parent,tmp_path)
    try:
        recorder.start();callbacks[0](SimpleNamespace(status=lambda:Qt.PermissionStatus.Granted))
        assert recorder.state=='recording' and len(microphone[1])==1
    finally:recorder.cancel();parent.close()


@pytest.mark.parametrize('failure', ['missing', 'device_error'])
def test_unavailable_microphone_returns_to_idle_controls(tmp_path,monkeypatch,microphone,failure):
    from types import SimpleNamespace
    from PySide6 import QtMultimedia as audio
    parent=QWidget();recorder=VoiceRecorder(parent,tmp_path);states=[]
    recorder.stateChanged.connect(lambda state,text:states.append((state,text)))
    if failure=='missing':
        monkeypatch.setattr(audio.QMediaDevices,'defaultAudioInput',lambda:SimpleNamespace(isNull=lambda:True))
    else:
        monkeypatch.setattr(audio.QAudioSource,'error',lambda _:audio.QtAudio.OpenError)
    try:
        recorder.start()
        assert recorder.state=='error' and recorder.audio is None and states[-1][1]
    finally:recorder.cancel();parent.close()


def test_existing_small_model_is_reused_without_downloading(tmp_path,monkeypatch):
    import model_downloader as models,importlib.util
    monkeypatch.setattr(importlib.util,'find_spec',lambda _:object())
    monkeypatch.setattr(models,'is_installed',lambda *args:True)
    download=Mock();monkeypatch.setattr(models,'download_model',download)
    assert voice_input.ensure_default(tmp_path)=={'ready':True,'reused':True}
    download.assert_not_called()


def test_first_use_downloads_only_missing_small_model(tmp_path,monkeypatch):
    import model_downloader as models,importlib.util
    monkeypatch.setattr(importlib.util,'find_spec',lambda _:object())
    ready=False
    monkeypatch.setattr(models,'is_installed',lambda *args:ready)
    def download(kind,tier,root,**options):
        nonlocal ready
        assert kind=='asr' and tier=='small' and root==tmp_path
        ready=True
    callback=Mock(side_effect=download);monkeypatch.setattr(models,'download_model',callback)
    assert voice_input.ensure_default(tmp_path)=={'ready':True,'reused':False}
    assert voice_input.ensure_default(tmp_path)['reused']
    assert callback.call_count==1
