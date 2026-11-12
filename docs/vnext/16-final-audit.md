# vNext 最终审计（2026-10-30）

状态：**未完成整体目标；以下区分“已真实验证”和“未验证”。**

## 已真实验证

| 能力 | 证据 |
|---|---|
| Python Core 只读 Bridge | `GET /api/overview`、好友 3 / Agent 6 / 任务 19 |
| 多账号目录 | 账号增删、DPAPI Key、`/v1/models` 探测、mock chat 调用 |
| 好友账号绑定 | `POST /api/accounts/bind`、`GET /api/providers/route`、删除账号清理绑定 |
| Experience 候选 | 真实 HTTP 写入 `CANDIDATE / low_unverified`，未伪造 VALIDATED |
| DeviceGateway | 配对、列出、撤销、30 天过期、token 轮换、Android 启动轮换 |
| Android 客户端 | API 29 与 API 35 安装/启动/连接；配对；TTS 播放；录音上传 ASR；WSS + VAD |
| TLS | 桌面自签名 HTTPS；Android 证书固定；WSS |
| Tauri 桌面 | 编译、NSIS per-user 安装器、安装后真实窗口显示数据 |
| 测试 | `31 passed` |

## Tauri 证据

- 安装器：`desktop/src-tauri/target/release/bundle/nsis/Open QQ Dot_0.1.0_x64-setup.exe`
- 安装后 exe：`%LOCALAPPDATA%\Open QQ Dot\open-qq-dot-desktop.exe`
- 已实际启动，窗口标题 `Open QQ Dot`
- 截图：`build/tauri-installed-final.png`
- 限制：从仓库路径启动 WebView2 会 `Access is denied`；需 ASCII 路径或安装器。

## Android 证据

- APK：`mobile/app/build/outputs/apk/release/app-release.apk`
- 大小：2,743,643 bytes
- API 29：安装、连接、配对、TTS、录音 ASR 已验证
- API 35：安装、连接、WSS/VAD、Health Connect 可见
- 限制：模拟器无真实麦克风，ASR 内容不代表真实识别准确率

## 未验证 / 未完成

1. **Health Connect 未闭环**：API 35 显示“可用”，但权限页无法完成授权；没有真实步数/睡眠数据，没有向 HealthBridge 写入真实事件。
2. **真实 QQ/NapCat 发送未验证**：没有运行中的 NapCat/QQ 会话，也没有发送测试消息。
3. **真实外部 Provider 调用未验证**：只用了本地 mock `/v1/chat/completions`；未使用用户真实密钥向外部服务发送消息。
4. **实时双向语音未完成**：已有 utterance WSS + 客户端 VAD，但没有完整双工通话、打断时序、回声消除。
5. **Tauri 安装后不自动启动 Python Core**：仍需外部 Bridge；安装器路径限制未彻底消除。
6. **未做真机验证**：全部 Android 验证在模拟器完成。
7. **未做完整回归**：本次只跑 31 个 vNext 相关测试，未重跑全仓 1974 项历史套件。

## 结论

整体目标**不能标记完成**。已交付的是可运行、可验证的 vNext 基础层与客户端原型；Health Connect、真实 QQ 发送、真实外部模型和真机验收仍是缺口。
