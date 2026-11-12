# 中文优化版发布说明（2026-10-24）

## 基线与兼容策略

本次以 `Open-QQ-Dot-Unified-EN-20261024.zip` 为唯一运行代码基线，保留了此前两版统一后的控制台认证、QQ 合并消息处理、Agent 邮箱自动续跑、群聊异步队列和凭据安全发布规则。原中文版本资料保留在 `docs/zh_reference/`，但**没有用旧 Python 模块覆盖新版本实现**。

## 实施内容

1. 增加 `ui_locale.py`：125 项人类可见文案的中英映射，默认中文（`OPENQQ_UI_LANG=zh-CN`），可通过 `OPENQQ_UI_LANG=en` 切换部分已适配 Qt 组件；协议字段、模型 ID、数据库键、命令保持原样。Qt 八个模块使用这些翻译项，未完成全 UI 的国际化。
2. 恢复中文核心导航、项目与对话工作台、按钮、状态提示、模板编辑、长期记忆、检索 API 配置等常见文案；控制台剩余少量英文提示改中文，同时保留“设计预览而非真实事件”的明确标示。
3. 修复旧英文迁移时损坏的 `house_qt/agent_console_widget.py` 提示词清理正则：移至可独立测试的 `reply_sanitizer.py`，严格按中英文已知标签识别，避免把普通 `【中文内容】` 错删。兼容原导入函数接口。
4. 新增 `启动中文版.bat` 与 `Start English UI.bat`；保留原 `start.bat` 原始启动功能。
5. 将最新版英文 README 留存为 `README.en.md`；新增 `README.zh-CN.md` 并作为根 `README.md` 使用，包括部署、认证、数据迁移、已知限制和可复测测试命令。
6. 新增 `tests/test_zh_edition.py`，涵盖中文/英文指令、界面语言切换、普通文本不误删、双语内部标签过滤和主干代码功能存在性检查；调整控制台 HTML 测试，允许明确的中英文“设计预览”标签。

## 独立复测（当前 Linux 测试容器）

- `tests/test_zh_edition.py tests/test_literal_integrity.py tests/test_queue_governor.py tests/test_cockpit_server.py`：**29 passed**（另有 12 项 unittest 子测试），未发现失败。
- `tests/test_group_bot.py tests/test_literal_integrity.py`：**34 passed**。
- `tests/test_task_loop_closure.py`：**14 passed**。
- `tests/test_agent_dispatch_real_path.py`：**4 passed**。
- `tests/test_agent_messaging.py` 中六条请求跨批次完整回复的测试：**1 passed**（其余用例未在本次完成全组运行）。
- `tests/test_runtime_integration.py` 中合并消息真正进入模型提示词测试：**1 passed**。
- 所有 409 个 Python 文件经过 AST 解析，**0 个语法失败**。
- `tools/package_release.py --check-only`：**通过本项目预设的凭据文件/敏感模式扫描**；这不等同于完整的安全审计。

**注意**：这几组测试存在重复覆盖，不能把以上数字直接相加声称为唯一测试总数；本次未完整执行全项目所有测试。

## 本次没有验证或解决的部分

- Linux 环境没有 PySide6 / qfluentwidgets，也没有实际 Windows 11 桌面运行时，所以没有进行 GUI 实际渲染、中文排版和 Windows 系统托盘验收。
- 没有连接真实 NapCat QQ 账户、真实云端 Codex/Claude 或真人会话；不能声称已完成新的外部平台端到端验收。
- 许多深层配置页、异常提示和后台日志仍有英文；默认中文不是“完整翻译 100%”。英文 UI 切换仅对已经调用 `tr()` 的控件有效，切换后需要重启。
- 对于拥有本机系统账户执行权限的非可信 Agent，仍缺乏操作系统层级的执行与密钥隔离。
- 本次没有宣称科研、视频、电商等业务链路因语言修改而获得新增功能。

## 安全与迁移

先备份数据，在新目录解压试运行。不要直接覆盖活动 QQ 进程及其数据库。项目发布器过滤 `*.local.json`、朋友资料、`data/`、数据库、令牌等，不附带认证或私人数据。如旧包曾公开或传播过有效 NapCat/Web Token，请轮换。
