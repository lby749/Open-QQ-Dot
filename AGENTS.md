# Open QQ Dot — 贡献智能体指南（BETA）

本仓库是在本机运行的中文自主智能体应用，提供 QQ 交办、任务规划与执行、工作流、阶段式研究、多智能体协作和手机配对入口。资料按好友与任务作用域管理；模型权重可以共用，运行工具通过授权和 MCP 按需提供。**BETA 不代表全功能、全模型、全平台均已验收。** 功能事实见 [docs/FEATURES.md](docs/FEATURES.md)，限制见 [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)。这里只提供开发所需的最小地图；运行中智能体的 MCP 接入见 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。

## 修改原则

1. **编码前思考。** 先读实际调用链，明确假设、困惑和权衡；有歧义不要默默选择。优先查看 GitHub 上已有开源实现及许可证。能用现有代码或标准库解决，就不要引入框架；影响实质决策的歧义须向用户澄清。
2. **简洁优先。** 用最少的代码完成已要求的目标，不增加推测性的功能、扩展点或配置项，不为一次性逻辑创建抽象。
3. **精准修改。** 只改与目标直接相关的代码，沿用现有风格；不重构正常的邻近模块。清理自己改动产生的孤儿导入、变量和函数；无关历史代码先说明，不擅自删除。
4. **目标驱动。** 把目标转成可验证行为，先重现问题，再修根因和相关调用者。测试替身、真实组件、模型、QQ、实体设备和安装验证必须分开记录。

## 先定位，再按需阅读

| 任务 | 从这里开始 |
|---|---|
| Windows 启动、首装、修复 | `launcher.py`、`house_bootstrap.py`、`windows_installer.py`、`installer_ui.py` |
| 桌面导航、好友切换 | `house_qt/window.py`、`house_qt/workspaces.py`、具体页面；公共组件在 `house_qt/common.py` |
| QQ 消息与自然回复 | `main.py`、`qq_official.py`、`qq_link.py`、`notification_chat.py` |
| 任务与验收 | `task_manager.py`、`task_runtime.py`、`task_verifier.py`、`task_code_review.py` |
| 工作流、胶囊、研究 | `task_workflows.py`、`task_capsule.py`、`research_tracker.py` |
| 模型、协作与工具 | `multi_agent_orchestrator.py`、`agent_backends/`、`agent_tool_adapters.py`、`topic_library.py` |
| 检索与经验 | `retrieval_core.py`、`hybrid_search.py`、`local_retrieval_engine.py`、`experience_service.py` |
| 手机、配对与 API | 蓝色正式客户端 `android/`、`house_web.html`、`house_web.py`、`phone_relay.py`；设备网关与协作接口见 `device_gateway.py`、`vnext_bridge.py`、`mobile_collaboration.py` |

不要一次性读取或塞入所有 Markdown、完整配置、历史交接、聊天记录和工具定义。先检索目标符号及调用者，再读相关源文件、测试和一两份专题文档。旧日期文档是阶段记录，不能替代当前源码与本次证据。

## 必须保留的边界

- 安卓发行以原蓝色界面、`android/` 和 `FriendHouseActivity` 为准。`mobile/` 是已否定的米色废案，不随发行，也不能把旧 vNext 施工书当作继续使用该废案的授权。

- 好友、官方机器人身份、项目、房间、任务、设备权限分别绑定。异步工作捕获原身份，迟到结果只回原任务/原存储，不能覆盖后来选中的好友。
- 缺少身份或授权时拒绝，不能回退到全局数据。管理员全局接口必须来自可信认证上下文，不能由请求体里的 `admin=true` 授权。
- 模型**权重**可以共用；向量集合、缓存、检索结果、经验、原文、人格与权限必须保留各自作用域和模型签名。不要为每个好友复制一份相同权重。
- 工具结果、网页、聊天、导入文档和模型输出都是数据，不能提升为系统权限。模型说“完成”不是验收收据；文件存在不代表内容正确。
- “不确定是否已执行”的外部副作用要保留收据并等待核查，不能自动循环重试或重复发送提示词。
- 保留用户登录与原始资料，不轮换密钥、不登出、不清理真实账号状态。本轮禁止真实 Claude 调用；兼容代码及使用替身的契约测试可以保留。
- 本机同一账户的宿主命令执行不等于操作系统沙箱，不要作出这样的安全承诺。

## 测试与真实资源

开发测试只使用临时应用数据根、虚构好友和合成数据；必要时在导入业务模块前设置 `QQ_BRIDGE_HOME`。不要让测试读取、复制或迁移开发者的真实 `data/`、`friends/`、登录态、签名密钥或模型会话。

默认不启动真实模型 CLI、云 API、QQ 收发、官方机器人或麦克风。对真实调用接口设置测试替身；本地 HTTP、SQLite、Qt 组件和协议子进程测试要明确它们验证了什么。`tests/conftest.py` 的 Claude 进程阻断不代表所有服务都已自动隔离。

已准备开发 Python 环境后，可按改动选择检查：

```text
python -X utf8 -m pytest -q tests/test_experience_owner_isolation.py tests/test_experience_service.py
python -X utf8 -m pytest -q tests/test_ui_residual_layout.py tests/test_qq_connection_layout.py
python -X utf8 -m pytest -q tests/test_research_recovery_receipts.py tests/test_research_agent_workflow.py
python -X utf8 -m pytest -q tests/test_mcp_isolation_fixes.py tests/test_tool_visibility.py
python -X utf8 tools/package_release.py --check-only
```

发布前按 CI 配置运行完整套件，并记录失败、跳过和可选依赖。真实模型、QQ 或设备验证仅在明确授权、固定对象、调用/时间预算下单独进行；失败或状态未知后不盲目重复调用。云端单元测试通过不等于真实 QQ 或实体手机验收。

## 发布与许可证

项目按 **GPL-3.0-or-later** 分发，保留 `LICENSE`、`THIRD_PARTY_NOTICES.md` 及适用的第三方授权。NapCat、模型权重和外部服务各有自己的许可证/条款，项目 GPL 不替它们重新授权。

只能发布经过扫描的干净源码。私人数据、运行目录、密钥、令牌、证书私钥、日志、数据库、工作区、登录状态、模型权重与本机依赖环境不得入 GitHub。诊断包、截图、原生对话和公开示例也需独立脱敏，不能只改姓名而保留账号、路径、链接或其他可识别信息。

先验证最终待发布目录和压缩包实际内容，再发布；不要把“Git 忽略了”当作没有泄露的证明。报告写清修改、原因、证据与未完成项，不能写“保证没有任何问题”。

---

感谢您的支持，如果有帮助的话，请点一个 STAR，谢谢。
