# Phase 6 — WSS 语音流基础

日期：2026-10-30  
状态：**WSS 语音流服务已真实跑通；Android 仍使用 HTTP 上传单段 WAV，尚未接入 WSS。**

## 服务

新增 [voice_ws.py](../../voice_ws.py)：

```powershell
python voice_ws.py --port 8793 --tls
```

协议：

```text
-> {"type":"start","sample_rate":24000}
-> <binary PCM chunk>...
-> {"type":"end"}
<- {"type":"ready","sample_rate":24000}
<- {"type":"final","text":"...","mood":"","bytes":N}
```

限制：

- 单次语音最多 8MB PCM
- 少于 1600 字节拒绝
- 音频只在内存/临时 WAV 中存在，转写后删除
- 不做 VAD、打断、回声消除

## 真实 WSS 测试

用生成的本地证书严格校验：

```python
ctx = ssl.create_default_context(cafile='data/vnext_tls/bridge-cert.pem')
websockets.connect('wss://127.0.0.1:8793', ssl=ctx)
```

发送真实 TTS 生成的 24kHz WAV（101,968 字节 PCM），分块传输：

```text
<- {"type":"ready","sample_rate":24000}
-> PCM chunks
-> {"type":"end"}
<- {"type":"final","text":"你好这是手机语音测试","mood":"","bytes":101968}
```

这是实际 WSS + 实际本地 ASR，不是 mock。

## Android WSS 客户端

`VoiceStream.kt` 已使用 OkHttp WebSocket + 本地证书固定接入：

- `VoiceRecorder` 的每一块 PCM16 实时发送二进制帧
- 结束时发送 `{"type":"end"}`
- 收到 `final` 后显示文本
- WSS 地址可在设置页修改，默认 `wss://10.0.2.2:8793`

API 35 模拟器实际结果：

```text
WSS 已连接，正在流式识别…
WSS 识别完成
识别：嗯
```

截图：`build/android-wss-success.png`。模拟器没有真实麦克风，识别为“嗯”，但手机→WSS→桌面 ASR→回传文本的链路真实跑通。

APK 体积因 OkHttp 增加到 `2,743,507 bytes`（2.62 MiB）。

## VAD 与打断

Android `VoiceStream` 路径新增：

- PCM16 能量 RMS VAD
- 检测到人声后静音 1.2 秒自动结束
- 4 秒未检测到人声自动结束
- 开始录音/流式识别时停止正在播放的 TTS（基础 barge-in）
- final 帧后服务端主动关闭连接，避免客户端 close 造成 `Socket closed` 误报

API 35 模拟器实测：无麦克风输入时自动结束并收到 `WSS 识别完成 / 识别：嗯`。这是 VAD 自动结束，不是用户手动停止。

## 未完成

- 实时双向流、VAD、用户打断
- 回声消除、降噪
- 音频重连/缓冲/通话状态机
- 真机网络与延迟验收

文档不把“WSS 服务可用”说成“手机实时语音已完成”。

## 测试

```text
tests/test_tls_support.py   1 passed
tests/test_tls_support.py 覆盖证书生成；WSS 协议由上面的真实客户端测试覆盖。
```
