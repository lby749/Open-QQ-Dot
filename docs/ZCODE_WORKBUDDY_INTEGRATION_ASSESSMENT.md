# ZCode / WorkBuddy 接入评估

日期：2026-10-27  
范围：Open QQ Dot Agent 平台（`agent_tool_adapters.py`、TaskManager、Qt/Web Agent 控制台）  
结论：**ZCode 高可行，建议接入；WorkBuddy 目前没有公开 Agent API/CLI，不建议做非官方逆向接入。**

## 1. 现有接入面

Open QQ Dot 已经有 provider-neutral 的 Agent 适配层：

- `agent_tool_adapters.BUILTINS`：OpenCode、Cursor、Qwen Code、Kimi Code/ACP、自定义 ACP、MCP。
- `run_headless()`：非交互 CLI，子进程、超时、无 shell、显式写入授权。
- `run_agent()`：TaskManager 统一调度入口。
- Qt `adapter_settings.py`、Agent 控制台、`/api/agent-adapters` 会自动读取 `BUILTINS`。

因此新增一个 headless CLI Agent 不需要改任务系统，只需增加适配器和测试。

## 2. ZCode

### 2.1 产品与协议

- 官方站：<https://zcode.z.ai/>
- 开源仓库：<https://github.com/zai-org/ZCode>
- 许可证：Apache License 2.0（与 GPLv3 项目作为独立外部程序调用兼容；不把其代码静态链接进本项目）。
- 当前版本参考：v3.16.x；CLI 包含 TUI、Web、Agent。
- CLI 非交互入口（官方文档）：

```text
zcode --prompt "<instruction>" --cwd <workspace> --mode plan --json --no-color
zcode --prompt "<instruction>" --cwd <workspace> --mode yolo --output-format stream-json --no-color
```

- `--mode plan`：只规划，不改文件。
- `--mode yolo`：可执行工具/修改文件，**不是沙箱**；必须由 TaskManager 的显式 full-computer 授权控制。
- `--json` 返回 `sessionId`、`response` 等；`stream-json` 返回事件流和最终 `result`。
- 支持个人 provider 配置、OpenAI Chat Completions / Responses / Anthropic Messages 兼容 API。
- 支持 Windows SEA 单文件 `zcode-windows-x64.exe`，也支持 Node bundle。
- 官方批量任务文档明确要求：独立 workspace、独立 HOME/USERPROFILE/ZCODE_DATA_BASE_DIR、超时、进程树清理、独立评分；不能把模型回复当作成功。

### 2.2 建议适配方式

新增：

```text
BUILTINS['zcode'] = AgentAdapterSpec(
    'ZCode CLI', 'headless', 'zcode', session_support=True
)
```

`run_headless()` 增加 `zcode` 分支：

```text
argv = [
  program,
  '--prompt', instruction,
  '--cwd', str(cwd),
  '--mode', 'yolo' if allow_write else 'plan',
  '--json',
  '--no-color',
]
```

解析：

- `response` / `result` → output；
- `sessionId` / `session_id` → session；
- `usage` → tokens；
- 非零退出、空 response、错误 envelope 一律 fail closed；
- 不把 `stderr` 原样回传 QQ（可能包含凭据或 prompt）。

可选增强：

- 配置 `model` / `provider` 时，使用 ZCode 的个人 provider 配置；不把 API Key 写进任务 prompt。
- 需要事件流时使用 `--output-format stream-json`，但必须只取最终 result，不能把中间 reasoning 当结果。
- 任务隔离优先使用**任务专用 HOME**；但 ZCode 的登录凭据也在该 HOME 中，首次使用需要用户先在该隔离目录完成 `zcode login`，或明确接受复用用户全局登录态。两种模式都要在 UI 显示。

### 2.3 工作量与风险

| 项目 | 评估 |
|---|---|
| 代码量 | 约 100–200 行（适配器 + 解析 + 测试） |
| 改动范围 | `agent_tool_adapters.py`、测试；UI/接口自动列出 |
| 预计工期 | 0.5–1 天（不含真实账号 E2E） |
| 可行性 | 高 |
| 主要风险 | 版本参数变化、`yolo` 权限过大、Windows 路径/Node 版本、登录态隔离 |
| 许可证 | Apache-2.0，外部独立进程调用；在 `THIRD_PARTY_NOTICES.md` 增加说明即可 |

**建议：先做 `plan` 只读 smoke，再由用户显式授权 `yolo` 写入。**

## 3. WorkBuddy

### 3.1 已确认的能力

WorkBuddy 是腾讯的全场景 AI Agent 桌面工作站：

- 官方站：<https://www.workbuddy.ai/>
- 文档：<https://www.workbuddy.ai/docs/workbuddy/>
- 形态：Windows/macOS 桌面应用 + Web 工作台。
- 已文档化的集成：
  - QQ、企业微信、飞书、钉钉、Slack、Telegram、Discord 的 **机器人远程控制**；
  - **MCP 客户端**：WorkBuddy 可以连接外部 MCP Server（URL、认证、OAuth）；
  - Skills、Connectors、Automation、本地文件操作。
- 没有找到公开的：
  - Agent HTTP/WebSocket API；
  - headless `workbuddy` CLI 任务协议；
  - 第三方 Agent 调度 SDK；
  - 可作为 Open QQ Dot 后端的官方接口。

`robots.txt` 明确禁止抓取 `/api/`、`/trpc/`、`/workspace/`；文档站也没有开发者 API 章节。WorkBuddy 的 “IDE CLI” 导航实际指向 CodeBuddy/WorkBuddy 文档入口，未提供 WorkBuddy Agent 的可编程任务 API。

### 3.2 可行方案

#### 方案 A：MCP 反向接入（推荐，低风险）

让 WorkBuddy 作为 MCP 客户端连接 Open QQ Dot 的 MCP Server：

```text
WorkBuddy → MCP stdio/URL → Open QQ Dot agent_mailbox_mcp / task tools
```

- 可行：WorkBuddy 官方支持 MCP Server。
- 方向是 **WorkBuddy 调用 Open QQ Dot 的工具**，不是 Open QQ Dot 调用 WorkBuddy 执行任务。
- 适合：把 Open QQ Dot 的好友、任务、记忆、导出能力暴露给 WorkBuddy。
- 需要：稳定 MCP 工具 schema、认证、只读/写入分级、审计。
- 工作量：1–2 天；不需要逆向 WorkBuddy。

#### 方案 B：QQ 消息桥（可行但脆弱）

WorkBuddy 有 QQ 机器人远程控制；Open QQ Dot 也有 QQ 连接。

```text
Open QQ Dot → QQ 私聊/群 → WorkBuddy QQ Bot → WorkBuddy 桌面端执行
```

- 可行：两边都使用 QQ 开放平台机器人。
- 缺点：需要两个 QQ 机器人/账号；任务结果只能以聊天文本返回；无结构化状态、超时、取消、产物回传；容易被 QQ 风控；不能作为可靠 Agent 后端。
- 适合演示/人工转发，不适合生产调度。

#### 方案 C：桌面 UI 自动化（不推荐）

用 Windows UI Automation/截图点击 WorkBuddy 桌面应用：

- 技术上可做；
- 但无稳定选择器、版本一变就坏、无法可靠处理权限/登录/验证码；
- 可能违反 WorkBuddy 服务条款；
- 不应作为开源项目的默认集成。

#### 方案 D：等待官方 API/CLI

如果腾讯发布 WorkBuddy Agent API/CLI，再按 ZCode 同样的 headless 适配方式接入。当前没有可依赖的公开协议。

### 3.3 工作量与风险

| 方案 | 可行性 | 工作量 | 风险 |
|---|---|---:|---|
| A. WorkBuddy 作为 MCP 客户端 | 高 | 1–2 天 | 工具 schema/权限/认证 |
| B. QQ 消息桥 | 中 | 2–4 天 | 双机器人、风控、无结构化结果 |
| C. UI 自动化 | 低 | 高 | 不稳定、ToS、维护成本 |
| D. 官方 API | 未知 | 未知 | 依赖腾讯 |

**建议：WorkBuddy 只做方案 A（MCP 反向接入）或等待官方 API；不要把 UI 自动化包装成“支持 WorkBuddy”。**

## 3.4 已实施（2026-10-27）

- 已加入 `zcode` headless adapter，Agent 控制台/模型来源页会自动显示勾选项。
- 默认 `--mode plan`；只有显式写入授权才使用 `--mode yolo`。
- 已覆盖参数、JSON 解析、错误退出和测试。

## 4. 推荐落地顺序

1. **ZCode headless 适配器**：`plan` 只读 → 显式授权 `yolo` → 真实仓库 smoke。
2. **ZCode 许可证/安装说明**：Apache-2.0、外部 CLI、不捆绑二进制。
3. **WorkBuddy MCP 反向接入**：先把 Open QQ Dot 的只读工具（好友查询、任务状态、记忆检索）暴露为 MCP Server，WorkBuddy 手动配置。
4. **WorkBuddy QQ 桥**：仅作为可选实验功能，默认关闭。
5. **不实现** WorkBuddy UI 自动化，除非官方提供稳定接口。

## 5. 评分影响（预评估）

- ZCode 接入：工程质量 +1～2 分（新增真实 provider 路径），真实环境分需要实际账号跑通后才给。
- WorkBuddy MCP 反向接入：工程质量 +1 分；真实环境分需 WorkBuddy 客户端实测。
- WorkBuddy 直接 Agent 后端：当前**不具备条件**，不应计分。

## 6. 参考来源

- ZCode 官网：<https://zcode.z.ai/>
- ZCode GitHub：<https://github.com/zai-org/ZCode>
- ZCode CLI 文档：<https://github.com/zai-org/ZCode/tree/main/apps/zcode-cli/docs/usage>
- ZCode 批量任务：<https://github.com/zai-org/ZCode/blob/main/apps/zcode-cli/docs/usage/13-automation.md>
- WorkBuddy：<https://www.workbuddy.ai/>
- WorkBuddy 文档：<https://www.workbuddy.ai/docs/workbuddy/>
- WorkBuddy MCP：<https://www.workbuddy.ai/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide>
- WorkBuddy QQ 集成：<https://www.workbuddy.ai/docs/workbuddy/Platform-Integration/QQ-Guide>
