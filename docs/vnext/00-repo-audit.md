# Open QQ Dot vNext — 仓库审计（Phase 0）

> 本文中的账号均为脱敏占位示例，不可用于实际发送。

日期：2026-10-30  
审计对象：`D:\Desktop\New folder (3)\Open-QQ-Dot`  
基准 Git：分支 `baseline`，提交 `627694a69fb94c1b7ae452d54317cc9a17ee7077`  
审计方式：只读代码/配置/数据库清单 + 启动验证 + 测试基线。未替换数据库、未重构 UI、未修改总许可证。

## 1. 结论摘要

当前仓库是一个可运行的 **Windows Qt/Fluent Python 单体应用**，不是指挥书目标中的 Tauri + React + Kotlin 拓扑。

真实存在并在本轮启动验证的路径：

```text
Open QQ Dot.exe
→ house_bootstrap.py（依赖探测/准备运行时）
→ launcher.py（GUI + bridge 子进程）
→ house_qt/window.py::HouseWindow（PySide6 界面）
→ main.py::Bridge（NapCat/OneBot、QQ 私聊、任务与回复）
```

已核实的核心能力：

- QQ/NapCat 私聊文字链路
- 多智能体控制台与 CLI 适配器
- TaskManager 持久任务
- Agent Mailbox / MCP / 本机 HTTP 互联
- 长期记忆、检索、BGE-M3/重排
- 审批与电脑工具权限
- 本地语音输入/输出历史能力
- 群助手、主动聊天、话题资料库
- PyInstaller Windows 安装器与发布脚本

与 vNext 目标的真实差距：

- 没有 Tauri 2 / Rust shell
- 没有 React + TS + Tailwind 前端
- 仓库内没有 Kotlin/Compose Android 源码
- 没有 `DeviceGateway`、`ExperienceEngine`、`ProviderRouter`、`VoiceOrchestrator`、Health Connect 适配器
- 发布目录只有两个已构建 APK，无法证明其源码与当前仓库一致

因此 **Phase 0 只能完成审计、基线、文档和 checkpoint**；不能把现有 Qt 说成新架构，也不能声称 Android 源码已完成。

## 2. 源码树

顶层（节选，证据为实际目录）：

```text
main.py                     QQ Bridge、NapCat 消息处理、回复主链
launcher.py                 GUI/bridge 单入口与单实例互斥
house_bootstrap.py          运行环境探测与启动
house_qt/                   PySide6/Fluent 桌面 UI
agent_backends/             本轮已拆出的 Agent 传输入口
agent_tool_adapters.py      Agent 适配器与兼容入口
agent_messaging.py          Agent 持久邮箱
agent_mailbox_mcp.py        MCP 邮箱工具
multi_agent_orchestrator.py 多 Agent 编排
task_manager.py             持久任务状态机
app_server.py               Codex app-server 桥
claude_backend.py           Claude Code 后端
antigravity_backend.py      Antigravity 后端
dsh_backend.py              DeepSeek Harness 后端
api_backend.py / api_profiles.py  模型 API 与多连接
web_search.py / web_chat.py       网页搜索与网页模型
persona_template.py / persona_vectors.py 人格模板与向量
local_retrieval_engine.py / retrieval_core.py 混合检索
agenda.py                   主动引擎
group_bot.py                QQ 群助手
voice_input.py / voice_tts.py / voice_clone_worker.py  语音
house_web.py / cockpit_server.py  本地 Web/控制台
dotlink/                    Windows 安装器源码
tools/                     构建、发布、审计脚本
tests/                     测试
docs/                      现有工程文档
```

统计（只读命令）：

```text
git tracked files: 621
源码/测试/文档中 .py/.ts/.tsx/.kt/.java/.rs 文件（排除 .venv/build/dist）: 642
```

### 入口证据

| 入口 | 证据 |
|---|---|
| Windows 安装器产物 | `Open QQ Dot.exe`、`Open QQ Dot.spec` |
| 启动器 | `house_bootstrap.py:144-169` |
| GUI/bridge 分发 | `launcher.py:main()` |
| 主窗口 | `house_qt/window.py::HouseWindow` |
| QQ bridge | `main.py::Bridge.receive()`、`main.py::message_parts()` |
| 本地控制台 | `cockpit_server.py` |
| 发布构建 | `tools/build_windows_installer.py`、`tools/package_release.py` |

## 3. 数据库与配置

### 3.1 真实运行库（已被 `.gitignore` 排除）

| 路径 | 大小 | 表 |
|---|---:|---|
| `friends/1000000001/history.sqlite3` | 272 KB | `messages`、`memories`、`memory_entries`、`reply_attempts`、`usage_calls`、`emotion_events`、`source_*`、`incremental_*` 等 15 表 |
| `friends/1000000001/topic_library.sqlite3` | 24 KB | 话题资料库表（2 表） |
| `friends/1000000001/media/sticker_retrieval.sqlite3` | 32 KB | 表情检索 |
| `friends/1000000002/history.sqlite3` | 104 KB | 13 表 |
| `friends/1000000002/topic_library.sqlite3` | 24 KB | 2 表 |
| `data/tasks.sqlite3` | 184 KB | `tasks`、`task_logs`、`goals`、`goal_events`、`goal_tasks`、`workflows`、`workflow_runs`、`workflow_versions` |
| `data/agent_mail.sqlite3` | 80 KB | `agents`、`agent_sessions`、`messages`、`recipients`、`contact_grants`、`mail_audit` |
| `data/approvals.sqlite3` | 60 KB | `approval_requests`、`approval_grants`、`approval_audit_logs`、`subscription_terms_acknowledgements` |
| `data/agent_memory.sqlite3` | 32 KB | `memories`、`memory_history` |
| `data/retrieval_engine.sqlite3` | 312 KB | 18 表，含 `task_experiences`、`capability_catalog`、`failure_diagnostics` 与 FTS |
| `data/retrieval_cache.sqlite3` | 72 KB | `doc_vectors` |
| `data/notifications.sqlite3` | 180 KB | `notifications` |
| `data/proactive_state.sqlite3` | 36 KB | `proactive_triggers`、`proactive_events`、`proactive_checkpoints` |
| `data/group_bot.sqlite3` | 36 KB | `group_messages`、`group_answers`、`group_prefs` |

证据：只读 SQLite 查询脚本输出的 `sqlite_master` 表名；文件大小由 `Path.stat()` 读取。  
**未复制、未提交任何聊天正文、密钥或健康数据。**

### 3.2 配置

- `friends.json`：好友注册表（被忽略）
- `friends/<QQ>/friend.json`：好友模型、权限、人格、回复策略
- `housesettings`/`house.local.json`：本机模型与界面设置（被忽略）
- `napcat.local.json`：NapCat WebSocket/OneBot 配置（被忽略）
- `house_web.local.json`：Web 访问令牌（被忽略，DPAPI）
- `data/.cockpit_admin_token`：控制台令牌（被忽略）
- `.env`、`.experience_signing_key_v1`、`.agent_mail_key_v1`：被忽略

### 3.3 被忽略的运行数据

`.gitignore` 已排除：

```text
.venv/ build/ data/ friends/ friends.json napcat/ napcat.local.json
house_web.local.json bridge.log bridge.err.log bridge.pid
*.db *.sqlite *.sqlite3 *.bin *.key .env
*.local.json workspaces/ .pytest_cache/
.cockpit_admin_token .agent_mail_key_v1 .experience_signing_key_v1
```

## 4. 功能实现状态

| 功能 | 状态 | 证据 |
|---|---|---|
| QQ 私聊文字接收/回复 | 已实现，需真实 NapCat 验收 | `main.py::Bridge.receive`、`message_parts` |
| QQ 群助手 | 已实现，需真实群验收 | `group_bot.py`、`main.py::group_bot` |
| 多智能体控制台 | 已实现 | `house_qt/agent_console_widget.py` |
| Agent Mailbox/MCP | 已实现 | `agent_messaging.py`、`agent_mailbox_mcp.py` |
| TaskManager/持久任务 | 已实现 | `task_manager.py`、`data/tasks.sqlite3` |
| 审批/电脑权限 | 已实现 | `approval_manager.py`、`provider_permissions.py`、`workspace_tools.py` |
| 长期记忆/检索 | 已实现 | `retrieval_core.py`、`local_retrieval_engine.py` |
| 人格模板/向量 | 已实现 | `persona_template.py`、`persona_vectors.py` |
| 网页模型 | 已实现，依赖浏览器登录 | `web_chat.py` |
| 本地语音输入/输出 | 历史能力保留 | `voice_input.py`、`voice_tts.py` |
| B站/抖音分享解析 | 本轮已实现 | `share_links.py`、`tests/test_share_links.py` |
| GrokBot/群聊协作 | 本轮已实现并改名 | `agent_group.py`、`house_qt/agent_group.py` |
| Tauri/React 桌面 | **不存在** | 无 `Cargo.toml`/`package.json` 主应用 |
| Kotlin Android 源码 | **不在本仓库** | 仅发布目录 APK |
| DeviceGateway | **不存在** | 无对应模块 |
| ExperienceEngine | **不存在完整模块** | 仅有 `task_experiences`、`agent_memory` 等局部 |
| Health Connect | **不存在** | 无健康适配器 |
| ProviderRouter/AccountService | **不存在独立模块** | 能力分散在 `api_*`/`model_dispatcher` |

## 5. 构建与发布

- Windows EXE：PyInstaller，`Open QQ Dot.spec`，入口 `house_bootstrap.py`
- 安装器：`dotlink/`、`tools/build_windows_installer.py`
- 发布包：`D:\Desktop\openqqdots-最终发布`
  - `OpenQQDots-Setup.exe` 68,502,580 bytes
  - `OpenQQDots-原代码.zip` 5,240,268 bytes
  - `OpenQQDots-测试版-绿色包.zip` 30,942,911 bytes
  - `APK/OpenQQDot-foss-1.0.0.apk` 223,039,112 bytes
  - `APK/OpenQQDot-gms-1.0.0.apk` 246,160,247 bytes
- `tools/package_release.py` 会扫描/排除运行配置与数据库；发布前需重跑并核对哈希。

## 6. 首次核查发现的风险

1. 当前工作树有未提交修改（本轮之前的 Agent/UI 改动）；Phase 0 checkpoint 必须明确包含/排除范围。
2. `HANDOVER.md` 的历史测试数字（2010 passed）不能直接当作当前结果。
3. 发布 APK 与当前仓库没有可核对的构建脚本，不能声称 APK 源码同步。
4. PySide6-Fluent-Widgets 是 GPLv3；当前项目 MIT，若分发组合二进制需处理 GPL 合规，不能只改项目许可证。
5. `agent_tool_adapters.py` 仍保留旧传输兼容入口，实际新调用已拆到 `agent_backends/`；Phase 0 不删除兼容层，避免破坏旧数据/旧导入。
6. 发布目录中的 APK 体积 223/246 MB，和“薄客户端”目标冲突；膨胀源审计见 `02-apk-size-baseline.md`。

## 7. 本轮未做

- 未改 UI 架构
- 未引入 Tauri/React/Kotlin 新依赖
- 未替换 SQLite
- 未改总许可证
- 未删除旧 UI
- 未声称 APK/真机验收完成

## 8. 证据命令

```powershell
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git ls-files
python -X utf8 -m pytest -q -p no:cacheprovider
python -X utf8 tools/package_release.py --check-only
```

数据库证据为只读查询：

```python
sqlite3.connect(f'file:{path}?mode=ro', uri=True)
```
