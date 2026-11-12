# Phase 6 — 手机语音（TTS 播放 / ASR 接口）

日期：2026-10-30  
状态：**桌面 TTS/ASR HTTP 接口真实跑通；Android 端可合成并播放；录音上传/流式通话未完成。**

## 桌面 Bridge

新增：

```text
POST /api/voice/synthesize
POST /api/voice/transcribe
```

- `synthesize` 接收 `text`，调用现有 `voice_tts.record_segment`，返回 24kHz WAV 的 base64
- `transcribe` 接收 `wav_base64`，调用现有 `voice_input.transcribe_local`
- 需要设备/管理员 token（`QQDOT_VNEXT_TOKEN` 或配对设备 token）
- 请求体上限 4MB
- 不保存上传音频，临时 WAV 用完删除

## 真实测试

用项目 `.venv`（含 sherpa_onnx）：

```text
生成测试 WAV：102,012 bytes
POST /api/voice/transcribe
-> {"text":"你好这是手机语音测试","mood":""}

POST /api/voice/synthesize
-> text: 你好，这是语音合成测试。
-> sample_rate: 24000
-> wav_base64 length: 128276
```

这不是 mock，调用了本机真实 TTS/ASR 模型。

## Android 端

`VoiceRecorder.kt` 使用 `AudioRecord` 录 16kHz 单声道 PCM16，并在手机本地封装 WAV；不上传原始 PCM，不做后台自动录音。

`MainActivity.kt` 设置页新增“手机语音”：

- 输入文字
- 调用桌面 `/api/voice/synthesize`
- 把 WAV 写入 cache
- 用系统 `MediaPlayer` 播放
- “录音识别”按钮：请求 RECORD_AUDIO，录制 WAV，POST 到桌面 ASR，显示识别文本
- 没有把 TTS/ASR 模型或 Python 打进 APK

Release APK：

```text
2,520,348 bytes（2.40 MiB）

权限：INTERNET / POST_NOTIFICATIONS / RECORD_AUDIO
      READ_SLEEP / READ_STEPS / READ_EXERCISE
```

## 真实模拟器验收

已安装 Android 10 / API 29 x86_64 模拟器，APK 实际安装并启动：

```text
package com.openqqdot.mobile
Activity com.openqqdot.mobile/.MainActivity
Bridge http://10.0.2.2:8791
状态：已连接
好友 3 / Agent 6 / 任务 19
```

并在手机设置页用桌面生成的配对码完成真实配对：

```text
设备名：My Android phone
scope：chat:read, tasks:read, agents:read
GET /api/devices -> 设备存在
```

截图：`build/android-emulator-final.png`

同时修复了一个实际发现的问题：设置页的访问令牌输入框之前是明文显示，现已改为密码遮罩。

模拟器 `-no-audio`，但实际 UI 已跑通：

```text
合成并播放 -> 已播放 116,858 字节的桌面合成语音
录音识别 -> 已上传 133,004 字节 WAV
识别 -> 我
```

说明手机→桌面 TTS/ASR 网络与 WAV 编解码链路真实可用；因为模拟器无真实麦克风，识别内容仅为“我”，不代表识别准确率。

同时修复了设置页无法滚动导致语音卡片不可达的问题，设置为可滚动 Column。

截图：`build/android-voice-success.png`、`build/android-asr-success.png`。

## 未完成

- 手机录音上传 ASR 的实机验收（代码和 APK 已编译，设备未连接）
- 实时双向语音流
- VAD、打断、回声消除、降噪
- 音频缓冲/重连/通话状态机
- Android 真机播放与权限验收（`adb devices` 为空）

## 下一步

1. 用 `AudioRecord` 录 PCM、写 WAV、调用 `/api/voice/transcribe`。
2. 建立 `/ws/voice` 流式音频协议与状态机。
3. 接 VAD/打断/回声消除，再在真机测试延迟和耗电。
