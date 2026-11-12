# 未完成 / 未闭环清单（2026-11-03）

> 本文中的账号均为脱敏占位示例，不可用于实际发送。

状态来源：当前代码、`docs/vnext/16-final-audit.md`、施工指挥书第 6/9 节。
本轮开始按优先级处理，未验证项不标完成。

| 项目 | 当前状态 | 证据 / 缺口 |
|---|---|---|
| CC Switch 式账号卡片 | **28 预设 + 导入导出 + 复制** | 工具选择、卡片、启用/测试/复制/删除、28 个官方/兼容端点预设、非敏感账号 JSON 导入/导出、启用写入 profile 与 CLI adapter、真实用量、托盘切换、TaskSession 冻结；缺 90+ 预设、完整 CC Switch 配置格式 |
| 每个 Agent 独立 TaskSession 账号快照 | **已实现** | QQ 来源任务创建时写 `data/vnext_task_sessions/<task>.json` 冻结 account/provider/model；执行时覆盖 profile，UI 切换账号不影响已建任务 |
| Android 语音通话 | **连续采集 + 扬声器/AEC 状态** | API35 实际显示“正在听”，WSS 8793 已连接；有扬声器、静音、打断、挂断；模拟器显示 `AEC：不可用`，真机才可测量；仍缺真机双工/回声实测 |
| 语音接上下文/记忆/人设 | **已接后端路径** | `/api/voice/call` 调 `main.make_reply`，写入同一 `FriendStore`；模型配置无效时 fail-closed |
| Health Connect 真实授权/上传 | **API 35 已跑通** | 修复 `VIEW_PERMISSION_USAGE/HEALTH_PERMISSIONS` 清单声明；模拟器授权后读取并上传，桌面 HealthBridge `event_count=2, has_real_events=true`；模拟器无真实步数/睡眠数据 |
| 真实 QQ/NapCat 发送 | **界面可人工确认发送** | 本账号真实回执 `message_id=<已脱敏回执>`；vNext 页面有“真实测试消息”表单，发送前弹窗再次确认好友 QQ 与内容，不自动群发 |
| 真实外部 Provider 调用 | **已真实跑通** | OpenCode 免费模型 `mimo-v2.6-flash` 经 CLI fallback 返回真实中文回复；同一好友历史包含“喜欢猫/乌龙茶”时回复引用正确 |
| DeviceGateway 配对 | **本地闭环已验证** | 生成配对码→配对→列表→撤销，2 台测试设备均成功撤销，列表归零；缺真机网络验收 |
| ExperienceEngine | 已实现基础 | 候选/验证/检索；缺完整自动反思状态机与 A/B |
| Tauri 自动化 Python Core | 非主路径 | 当前主界面为 Fluent Qt；Tauri 仍不能独立启动 Core |
| 完整回归 | **2015 passed** | 2015 passed / 9 skipped / 2 failed；2 个仅为系统 Python 缺 `sherpa_onnx` 的 TTS 环境问题，项目 `.venv` 重跑 TTS 通过。 |

## 真实证据（2026-11-03）

```text
TaskSession: task_20261103_223953_a38dd2
冻结 provider=opencode model=opencode-go/mimo-v2.6-flash account=acct_ecdd0b4db1dd124a
```

```text
连续通话：接听后麦克风保持打开，回复播放完不重新申请麦克风
语音来电：invite accepted，手机显示“正在听”，WSS 8793 已连接
音频：VOICE_COMMUNICATION + AcousticEchoCanceler 已启用
```

```text
Health Connect：API35 权限页不再崩溃
授权并读取后：桌面 HealthBridge event_count=2, has_real_events=true
```

```text
真实 QQ 发送：1000000000 → message_id=<已脱敏回执>

OpenCode 免费模型：opencode-go/mimo-v2.6-flash
带历史：用户说过喜欢猫和乌龙茶
回复：当然记得呀，你喜欢猫，还爱喝乌龙茶，对吧？[微笑]

POST /api/voice/call
user_text：你还记得我喜欢什么吗？
reply：哎呀，被你这么一问……那现在正好补上呗……
wav_bytes：463701
```

## 本轮已落地

- CC Switch 风格账号卡片 + 每工具启用账号
- Android `CallScreen`，接入 `/api/voice/call`
- `/api/voice/call` 使用现有 `main.make_reply`，共享聊天历史、长期记忆、人设
- NapCat WebUI salt 修复，快速登录成功
- 恢复当前较新 Fluent 界面，并补 folder 6 的教程/QQ 直连/胶囊/迁移/回溯

## 下一步顺序

1. 选一个已配置 Provider 的好友，实际走通 `/api/voice/call` 并抓取音频/回复证据。
2. Android 通话补自动续听、打断、静音、挂断资源释放。
3. Health Connect 权限授权与真实事件上报。
4. 真实 QQ 发送一条测试消息并核对 `message_id`。
5. CC Switch 预设/用量/配置改写。
