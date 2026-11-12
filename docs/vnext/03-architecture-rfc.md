# Open QQ Dot vNext — 架构 RFC（Phase 0，只做映射与冲突报告）

日期：2026-10-30  
性质：审计/设计文档，不是实现。  
依据：指挥书第 2、3 节；当前仓库真实代码；不把历史报告当作现状。

## 1. 目标拓扑（指挥书）

```text
Windows Tauri 2 (Rust) + React/TS/Tailwind
  └─ typed client/events
       └─ 127.0.0.1 / IPC authenticated bridge
            └─ Python Core（唯一事实源）
                 ├─ TaskManager / Agent Mailbox / Access Control / TaskVerifier
                 ├─ QQ text/NapCat / ModelDispatcher / API profiles
                 ├─ AccountService + ProviderRouter
                 ├─ ExperienceEngine
                 ├─ ProactiveEngine + EventOutbox + NotificationService
                 ├─ DeviceGateway
                 ├─ VoiceOrchestrator
                 └─ SQLite + BGE-M3/RRF
                      │ authenticated HTTPS/WSS
                 Android Kotlin/Compose thin client
                      ├─ Auth/Pairing/Message/Agent Tasks
                      ├─ Voice Call UI
                      └─ Health Connect/Brand adapters
```

## 2. 新旧模块映射

| 目标模块 | 当前真实对应 | 状态 | 冲突/缺口 |
|---|---|---|---|
| Tauri 2 Rust shell | `launcher.py` + `house_bootstrap.py` | **不存在** | 当前是 Python 启动器 + PyInstaller EXE |
| React/TS/Tailwind | `house_qt/`（PySide6/Fluent） | **不存在** | 当前 Qt 直接占用 UI；迁移会重写全部界面 |
| typed client/events | 无统一 typed IPC；各页面直接 Python 调用 | 缺失 | 页面与业务逻辑耦合 |
| authenticated bridge | `house_web.py`、`cockpit_server.py`、`agent_interop_gateway.py` | 部分 | 有本地 HTTP/令牌，但没有 Tauri IPC/统一 typed contract |
| TaskManager | `task_manager.py`、`data/tasks.sqlite3` | 已存在 | 可作为事实源保留 |
| Agent Mailbox | `agent_messaging.py`、`agent_mailbox_mcp.py`、`data/agent_mail.sqlite3` | 已存在 | 与目标邮箱模型基本一致 |
| Access Control/TaskVerifier | `approval_manager.py`、`workspace_tools.py`、`provider_permissions.py` | 部分/分散 | 权限与审批分布在多个调用点，需统一服务边界 |
| QQ text/NapCat | `main.py::Bridge`、`napcat_ws.py` | 已存在 | 仍是同步主链路之一 |
| ModelDispatcher | `model_dispatcher.py`、`multi_agent_orchestrator.py` | 部分 | 没有独立 AccountService/ProviderRouter |
| API profiles | `api_profiles.py`、`api_backend.py` | 已存在 | 不是统一账号/额度/路由服务 |
| AccountService | 无独立模块 | **缺失** | 账号、额度、订阅、登录状态分散 |
| ProviderRouter | 分散在 `main.codex_text`、`multi_agent_orchestrator` | **缺失独立实现** | 路由策略与 handover 交织 |
| ExperienceEngine | `task_experiences` 表、`agent_memory.py`、`local_retrieval_engine.py` | **只有局部** | 没有 Verify→Reflect→Recheck→Extract→Retrieve 统一服务 |
| ProactiveEngine | `agenda.py`、`proactive_state.sqlite3` | 已存在 | 与目标事件/outbox 未统一 |
| EventOutbox | `notifications.sqlite3`、`notification_manager.py` | 部分 | 没有统一 outbox 事务模型 |
| NotificationService | `notification_manager.py` | 已有 | 需要与 outbox/QQ/Android 统一 |
| DeviceGateway | 无 | **缺失** | 没有配对、作用域 HTTPS/WSS、手机身份 |
| VoiceOrchestrator | `voice_input.py`、`voice_tts.py`、`voice_clone_worker.py` | 历史能力 | 不是流式编排；QQ 语音仍是历史路径 |
| SQLite data owner | 每个好友 `history.sqlite3` + `data/*.sqlite3` | 已存在 | 数据分散，缺少统一迁移/所有权服务 |
| BGE-M3/RRF | `embedding_provider.py`、`retrieval_core.py`、`local_retrieval_engine.py` | 已存在 | 模型版本/维度固定，换模型需重建 |
| Android Kotlin/Compose | 发布目录 APK，无源码 | **缺失源码** | 不能映射；APK 223/246 MB 与薄客户端冲突 |
| Health Connect | 无 | **缺失** | 不得用 Mock 冒充 |

## 3. 真实代码冲突

### C1. UI 架构冲突（最高）

当前所有页面直接依赖 `house_qt`、Qt 信号和 Python 对象；没有稳定的 typed API 层。  
Tauri/React 迁移不是“换壳”，而是需要先抽出 **bridge contract**，否则会复制业务逻辑到 React。

Phase 0 不重写 UI。

### C2. 权限/审批边界分散

现有：

- `approval_manager.py` 管理审批请求/授权
- `provider_permissions.py`/`workspace_tools.py` 解析 provider 与电脑权限
- `house_qt/pages_friend.py` 保存每 provider 审批
- 各 backend 读取 `computer_tools.approval_mode`

目标要求统一的 Access Control；当前没有单一服务，存在不同调用点解释不一致的风险。

### C3. AccountService/ProviderRouter 缺失

账号、额度、模型、provider 余额、handover 逻辑分散在：

- `api_profiles.py`
- `api_backend.py`
- `main.codex_text`
- `multi_agent_orchestrator.py`
- `agent_backends/capabilities.py`

没有统一账号事实源，不能直接做 Android 多账号。

### C4. 数据库所有权不统一

好友级 `history.sqlite3` + 全局 `data/*.sqlite3` 并存；没有迁移服务/版本化 schema 的统一入口。  
直接换库或合并会破坏现有聊天/记忆，Phase 0 禁止。

### C5. Android 与桌面不是同一后端契约

发布 APK 无源码，无法确认它调用的是当前 Python Core 的哪一版 API。  
DeviceGateway 尚不存在，现有 `house_web.py` 是网页令牌模型，不等于手机配对/作用域设备身份。

### C6. 语音范围

现有 QQ `record` 语音、TTS、克隆属于历史能力；指挥书第 0.3 条要求新增 AI 语音走 Android、默认关闭 QQ 语音输出。  
代码可保留兼容，但不能把现有 QQ 语音当成新 VoiceOrchestrator。

## 4. 建议的 Phase 1 边界（仅提议，不执行）

1. 先建立 **Python Core API contract**（typed request/response/event），不先写 React 页面。
2. 抽出 **AccountService + ProviderRouter**，把现有 `api_*` 与 `codex_text` 路由迁到单一服务。
3. 建 **AccessControlService**，统一 provider approval + computer permission + TaskVerifier。
4. 建 **DeviceGateway** 与配对契约，再做 Android；没有 Android 源码前不承诺薄客户端。
5. 保留 Qt 作为过渡前端，Tauri/React 逐页替换；不一次性删旧 UI。
6. 任何 APK 优化必须先取得 Android 源码与构建链。

## 5. Phase 0 判定

```text
目标拓扑：未实现
Python Core：大部分已有，需服务化/契约化
Windows 前端：Qt，非 Tauri/React
Android：只有 APK，无源码
DeviceGateway/AccountService/ProviderRouter/ExperienceEngine/Health：缺失
```

因此 Phase 0 只能交付审计、基线、许可证、APK 体积与 RFC；不得进入重构。
