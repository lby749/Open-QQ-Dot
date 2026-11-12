# Open QQ Dot — 多外部 API 与原版桌面配色

## 本次改动

统一基于 **Open-QQ-Dot-UI-Redesign-20261024** 的完整核心功能开发；未从较旧的 `openqqdot-en(2).zip` 覆盖任务、Agent 网络、Prompt Cache 或低延迟模块。

### Windows 主界面

原版配色以你提供的桌面截图为准：**浅蓝侧栏 `#EFF6FF`、白色工作区 `#FFFFFF`，蓝色选中/交互态**。Qt 六大工作区保持此前整合结构；Web 工作台也不再使用深色侧栏、深色渐变横幅。除布局调整与统一颜色外，不应擅自更换主题。

### 多模型 API 连接

在 **设置与工具 → 模型与工具 → 自定义 API** 中：

1. 点击 **新增连接**，输入独立名称（如“工作 API”、“备用 API”）。
2. 从 **兼容协议预设** 选择 *通用 OpenAI 兼容*、*Responses 兼容*、*Anthropic 兼容*，也可选 DeepSeek、OpenAI、Anthropic 或本地 Ollama/vLLM 的预设。其他服务商只要兼容上述协议，选择相应通用预设并填写其实际 Base URL 与模型 ID 即可。
3. 为每条连接单独填入 Base URL、模型 ID、思考档位和密钥。点击 **保存设置**。旧版单 API 自动迁移为名称为“默认模型 API”的条目。
4. 切换顶部 **当前连接** 并保存，即可让选中的外部 API 成为当前自定义模型连接；CLI Codex/Claude/Antigravity 的配置保留，不会被这些 API 连接取代。
5. **测试当前连接** 可用新输入的尚未保存的密钥做一次调用；**提取可用模型** 通过该服务的模型列表 API 查询。某些兼容服务不支持标准 `/models`，请直接输入模型 ID。测试 API 会产生真实调用及可能的计费。
6. **重命名** 和 **删除连接** 在桌面上操作；不同连接的密钥单独保存为当前 Windows 用户可解密的 DPAPI 文件。

目前这些连接是“多配置 + 选择当前连接”；**没有暗中把用户消息群发给多个服务商**，也不默认跨服务商自动故障转移。复杂协作仍由原有多 Agent 编排体系负责。

### 手机 Web 页的限制

Web 可以选择桌面端已经保存的 API 连接及其模型档位，但**不能修改已绑定密钥的 API Host/协议**，防止用户访问令牌被用来让模型向恶意地址发送保存的密钥。新增连接、修改 API 服务器地址及管理密钥，均需在本机桌面执行。

### 安全

- 密钥不会放进 `friend.json`、API 连接列表、截图或本次 ZIP 包；只记录 `api_key_id` 引用。
- 外部主机只允许 HTTPS；HTTP 仅允许 `localhost`/`127.0.0.1`/`::1`。模型列表的带密钥请求禁止跟随重定向。
- 任何具有同一 Windows 账户完整进程/文件读权限的恶意 Agent 仍然不能仅靠 DPAPI 提供隔离；长期多租户运行应增加操作系统沙箱。
- 修改配置前备份你的本地 `friends/`、运行数据与原 EXE。

## Windows EXE 构建说明

**此交付是在 Linux 容器中完成的，未产生也未验证 Windows PE EXE。** 项目原有 `house_bootstrap.py` 与 `Open QQ Dot.spec` 采用启动器机制，EXE 需要与完整源码、资源和 `.venv` 环境放在一起。不要把单独一个 EXE 当作完整便携程序。

在 Windows 11 电脑：

1. 安装 Python、项目所需运行依赖，以及 PyInstaller（例如在所用 Python 环境下 `python -m pip install pyinstaller`）。
2. 在项目根目录运行 `构建Windows EXE.bat`，或 PowerShell 执行 `tools/build_windows_exe.ps1`。
3. 构建脚本调用仓库已有 `Open QQ Dot.spec`，输出 `Open QQ Dot.exe` 到项目根目录，并将其作为启动器使用。
4. 在 Windows 上实际启动，验证 PySide6/qfluentwidgets、NapCat、API 连接与项目内工具功能；生成 EXE 本身不证明这些都通过。

## 验证状态

- 本轮统一回归：**82 通过、17 跳过、20 子测试通过**。跳过的主要是当前容器缺少 PySide6、qfluentwidgets 等桌面 GUI 组件。
- 本地真实 HTTP：两个独立兼容服务端，用不同 Bearer Key 完成 `/models` 与 `/chat/completions` 请求，验证地址与凭据不串用。**服务端是本机测试替身，不是公网 AI 服务。**
- Chromium：Web 控制台六个一级导航正常初始化，侧栏渲染颜色 `rgb(239, 246, 255)`，无 JavaScript 异常。未对 Windows Qt 主窗口做视觉实测。
- 不代表真实付费 API、EXE、NapCat 或全量项目集成已经通过。
