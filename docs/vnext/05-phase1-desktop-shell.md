# Phase 1 — typed Python Core bridge + desktop shell

日期：2026-10-30  
状态：**部分完成，可运行只读桌面壳；Tauri 原生编译被本机 MSVC 工具链阻塞。**

## 已交付

| 组件 | 路径 | 验证 |
|---|---|---|
| 只读 JSON bridge | `vnext_bridge.py` | 3 个单测 + 真实 HTTP 端口 |
| bridge 测试 | `tests/test_vnext_bridge.py` | `3 passed` |
| Tauri 2 壳 | `desktop/src-tauri/` | 配置/命令/Rust 源码已建；cargo 因缺 `link.exe` 未通过 |
| React/TS/Tailwind UI | `desktop/` | `npm run build` 通过 |
| 真实渲染 | `build/desktop-overview3.png` | Edge headless 1440×900 截图 |
| 设计系统 | `desktop/src/styles.css` | 暖米白/近黑/低饱和，8 个工作区 |

## Bridge 实际输出（当前仓库）

```json
{
  "service": "open-qq-dot-vnext-bridge",
  "version": 1,
  "read_only": true,
  "friends": 3,
  "agents": 6,
  "tasks": 19,
  "mail_messages": 6,
  "memories": 2
}
```

真实 endpoint：

```text
GET http://127.0.0.1:8791/api/health
GET http://127.0.0.1:8791/api/overview
GET http://127.0.0.1:8791/api/friends
GET http://127.0.0.1:8791/api/agents
GET http://127.0.0.1:8791/api/tasks
GET http://127.0.0.1:8791/api/capabilities
```

## 安全边界

- 只绑定 `127.0.0.1`
- SQLite `mode=ro`
- 不返回 chat body、memory content、API key、token、cookie
- 可选 `QQDOT_VNEXT_TOKEN` Bearer
- 只读；无写接口

## 已知限制

1. Rust 编译已通过：找到已安装的 MSVC 14.44（路径含中文），执行 `cargo check` 和 `tauri build --no-bundle` 成功，生成了 release exe。

原生窗口从仓库路径（含空格、括号、中文）启动时仍报：

```text
Failed to setup app: Access is denied. (os error 5)
```

把 exe 和 `WebView2Loader.dll` 放到纯 ASCII 路径（例如 `D:\qqdot-tauri` 或 `%LOCALAPPDATA%\OpenQQDot\Desktop`）后，窗口真实启动并显示 Python Core 数据。

一键部署脚本：

```powershell
powershell -ExecutionPolicy Bypass -File tools/run_tauri.ps1
```

截图：`build/tauri-native.png`。

结论：Tauri 原生窗口已可运行；仓库路径本身不适合直接启动，正式安装器应安装到纯 ASCII 目录。

## NSIS 安装器

已切换为 per-user 安装并成功构建：

```text
desktop/src-tauri/target/release/bundle/nsis/Open QQ Dot_0.1.0_x64-setup.exe
1.93 MiB
```

安装器本身也必须从纯 ASCII 路径启动（例如先复制到 `D:\qqdot-setup.exe`），否则静默安装返回 exit 2。

实际安装结果：

```text
%LOCALAPPDATA%\Open QQ Dot\open-qq-dot-desktop.exe
%LOCALAPPDATA%\Open QQ Dot\uninstall.exe
```

安装后的 exe 已实际启动，窗口标题 `Open QQ Dot`，显示真实 Python Core 数据。
2. 新 UI 的聊天页没有发送能力，明确显示“聊天写入将在 Phase 2 接入”。
3. `OverviewPage` 的任务/好友数据来自真实 SQLite 与 profile；任务标题可能较长，需要后续截断/详情页。
4. Tauri 启动 Python bridge 的 `start_bridge` 命令已写，但未在 Tauri 原生窗口内实测。

## 下一步

1. 把 `run_tauri.ps1` 的 ASCII 路径规则接入安装器。
2. 为 bridge 增加 typed event/SSE（任务与消息更新）。
3. 迁移聊天只读会话到新 UI；写入仍走现有 bridge API，不直连 SQLite。
4. 建立 AccountService/ProviderRouter 的独立服务，再让 UI 只依赖服务契约。
