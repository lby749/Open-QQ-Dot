# Open QQ Dot vNext — 测试基线（Phase 0）

日期：2026-10-30  
工作树：`D:\Desktop\New folder (3)\Open-QQ-Dot`（分支 `baseline`，HEAD `627694a`，含未提交改动）  
**本文件只记录实际运行结果；未完成的项目明确标记“未完成”，不把局部测试写成全量通过。**

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Windows 11 专业工作站版 10.0.26300 |
| CPU | Intel Core i5-14600KF |
| 架构 | AMD64 |
| Python | 3.13.13（conda-forge） |
| pytest | 9.1.1 |
| PySide6 | 6.12.0 |
| numpy | 2.2.6 |
| UI 平台 | 测试用 `QT_QPA_PLATFORM=offscreen`；真实桌面验证见下 |

## 2. 全量命令

```powershell
cd "D:\Desktop\New folder (3)\Open-QQ-Dot"
python -X utf8 -m pytest -q -p no:cacheprovider `
  --basetemp="D:\Desktop\New folder (3)\Open-QQ-Dot\.pytest_vnext_baseline"
```

开始时间：2026-10-30 18:16（本地）  
输出日志：`vnext-pytest.log`（仓库根目录，本轮生成，未提交）

### 全量结果（2026-10-30 18:31 完成）

```text
11 failed, 1953 passed, 9 skipped, 6 warnings, 449 subtests passed
运行时长：835.78s（13:55）
```

失败项：

```text
tests/test_agent_tool_adapters.py::test_open_code_shared_session_identifier_not_used_by_generic_dispatch
tests/test_cli_native_workspace.py::CliNativeWorkspaceTests::test_ui_persists_all_new_options
tests/test_custom_api.py::CustomApiTests::test_custom_provider_reaches_api_with_friend_settings
tests/test_deep_refactoring_and_self_audit.py::TestDeepRefactoringAndSelfAudit::test_b_real_interactive_browser_dom_form_submit_and_screenshots
tests/test_unified_agents.py::test_every_adapter_uses_main_router_and_shared_registry[opencode|cursor|qwen_code|kimi_code|acp]
tests/test_voice_tts.py::RealVoiceModelTests::test_installed_tiers_speak_chinese_and_english[small|base]
```

失败分类：

| 类型 | 数量 | 原因 |
|---|---:|---|
| 测试仍按旧路径 patch `agent_tool_adapters.run_agent` | 5 | 已随后改为 patch `agent_backends.generate`，目标复测 10 passed |
| UI 兼容字段 `computer_tools.approval_mode` 被移除 | 1 | 已加回只读兼容镜像，目标复测通过 |
| 自定义 API 收到了内部 `_approval_mode` | 1 | 已限制为原生 Agent provider，目标复测通过 |
| OpenCode 真实服务未在测试时监听 | 1 | 外部服务状态，非断言逻辑；单独服务在线时可复测 |
| Playwright 浏览器超时 | 1 | 本机浏览器/网络环境 |
| 可选 `sherpa_onnx` 未在全局 Python 安装 | 2 subtests | 使用项目 `.venv` 安装后，真实语音测试 1 passed / 2 subtests passed |

**修正后针对性结果：10 passed**（`test_unified_agents` 5 个参数、`test_cli_native_workspace`、`test_custom_api`）。全量结果仍保留原始 11 失败事实，不用局部通过冒充全量通过。

## 3. 已完成的针对性基线

| 命令 | 结果 | 说明 |
|---|---|---|
| `tests/test_house_qt.py` | **36 passed**（2026-10-30） | Qt 主窗口、好友隔离、模型页、控制台、协作页切换 |
| `tests/test_agent_group_ui.py` | **4 passed** | 群聊/协作页签、群设置持久化、主持人分发/汇总、排队命令 |
| `tests/test_group_bot.py tests/test_collaboration_view.py tests/test_agent_group_ui.py` | **39 passed** | 群助手与协作数据 |
| `tests/test_share_links.py` | **4 passed** | B站/抖音 JSON/XML/text 分享解析与去重 |
| `test_default_retrieval_apply_does_not_enable_an_unconfigured_api` | **1 passed** | 单独复测；全量并发时会受 Windows 文件占用影响，已在 `source_state.py` 加原子替换重试 |
| `tests/test_workflow_learning.py::test_real_worker_verifies_each_stage_and_hands_data_to_another_agent` | **1 passed** | 单独复测；全量并发时曾受临时数据库路径影响 |

这些测试使用的是**临时数据目录/测试替身**，不是真实 QQ 账号与真实云端模型。

## 4. 真实外部依赖测试

| 项 | 结果 | 结论 |
|---|---|---|
| OpenCode 服务发现 | `http://127.0.0.1:28894`，API v2，71 个模型 | 服务可用 |
| OpenCode 真实生成 `opencode-go/gemini-4-argon` | 返回 `OK` | 可用 |
| OpenCode 真实生成 `opencode/gemini-4-argon` | `Insufficient account funds` | 同账号不同 provider 余额不同，不是接入代码错误 |
| B站短链 `b23.tv/BV1GJ411x7h7` | 解析到 `www.bilibili.com/video/...` | 短链跳转可用 |
| B站页面/API 元数据 | `HTTP 412 Precondition Failed` | 反爬；标题优先用 QQ 分享卡片，搜索兜底需配置 |
| 抖音真实分享 | 未用真实抖音短链测试 | 仅完成 JSON/XML 解析单测；不得声称真机验证 |
| 真实 NapCat + QQ 登录 | 未在本轮重新登录/验收 | 不能声称端到端通过 |
| 真实 Windows 安装器 | 本轮未重跑构建 | 使用 2026-10-28 产物作体积基线 |

## 5. 已知失败/风险

1. 全量原始结果为 11 failed；其中 7 个已修复并目标复测，剩余 OpenCode/Playwright 为外部环境、语音为全局解释器缺可选依赖。不能宣称“全量 100% 通过”。
2. OpenCode 的 `opencode` provider 余额不足，UI 默认已优先选择 `opencode-go`，但用户仍可能手动选到无余额模型。
3. B站/抖音页面反爬，不能稳定抓取页面标题；真实标题依赖 QQ 分享卡片或已配置的网页搜索。
4. Android APK 无源码、无真实构建链、无真机验收；体积 212.7/234.8 MiB，见 `02-apk-size-baseline.md`。
5. `PySide6-Fluent-Widgets` 为 GPLv3，项目当前 MIT，分发合规未解决，见 `01-license-inventory.md`。
6. 真实 QQ/NapCat、Codex/Claude 账号、Android 配对、Health Connect 均未在本轮完成端到端验收。

## 6. 建议的下一轮测试（不在 Phase 0 执行）

- 真实 Windows + NapCat 登录后的私聊收发、断线重连、队列恢复
- 真实 Codex/Claude/Antigravity/OpenCode 多 provider 生成与额度耗尽降级
- Android APK 安装、配对、消息、后台通知、断电恢复
- 真实 B站/抖音分享（含短链、分享卡片、无标题卡片）
- 全量 pytest 至少一次完整跑完并保存 summary

## 7. 结论

```text
Phase 0 测试基线：全量已跑完（原始结果 1953 passed / 11 failed）
失败后已修复 7 个测试期望/兼容问题，目标复测 10 passed
剩余外部环境项：OpenCode 服务、Playwright 浏览器、全局 Python 的可选 sherpa_onnx
真实外部账号/Android：未验证
```

任何后续阶段引用本文件时，必须同时引用日志和最终 summary，不能只引用“passed”。
