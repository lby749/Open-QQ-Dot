# Open QQ Dot 1.0.0-beta.3

Open QQ Dot 是本地运行的中文智能助手。你可以在 QQ 中聊天、交办任务，在电脑控制台管理好友、工作流、长期研究和多个智能体，并用蓝色安卓客户端查看与协作。

<p align="center"><img src="https://raw.githubusercontent.com/lby749/Open-QQ-Dot/main/assets/brand/project-cover.png" width="720" alt="Open QQ Dot 项目配图：星云"/></p>

## 下载与安装

请在[本版下载页面](https://github.com/lby749/Open-QQ-Dot/releases/tag/v1.0.0-beta.3)的 Assets 中按用途选择：

| 文件 | 用途 |
|---|---|
| `OpenQQDot-Setup-Windows-x64-1.0.0-beta.3.exe` | Windows 10 / 11 x64 一键配置启动器；无需预先安装 Python，首次按需下载依赖 |
| `OpenQQDot-Portable-Windows-x64-1.0.0-beta.3.zip` | 完整解压，双击 `Open QQ Dot.exe`；需要首次联网准备私有运行环境 |
| `OpenQQDot-Android-FOSS-arm64-1.0.0-beta.3.apk` | Android 13+、arm64，蓝色正式客户端，不包含 Google OCR |
| `OpenQQDot-Android-GMS-arm64-1.0.0-beta.3.apk` | 同一蓝色客户端，包含 Google OCR 支持；与 FOSS 二选一 |
| `OpenQQDot-Source-1.0.0-beta.3.zip` | 完整公开源码、测试、工具、第三方许可证和构建说明 |
| `SHA256SUMS.txt` | 最终文件的 SHA-256 校验清单 |

Windows 首次安装请选择可写目录，已有用户选择原目录修复或升级并保留备份。安装器不会附带开发者的 QQ 登录、密钥、聊天或模型权重。APK 沿用旧测试版证书，`versionCode=5`，可覆盖同证书旧版；不要先卸载以免丢失配对数据。两个 APK 包名相同，不能共存。当前 Windows EXE 未作商业代码签名，安卓沿用测试签名。

模型与外部服务需要你自己的授权。先设置模型与工具，再打开 QQ 连接：默认官方机器人，原有个人账号方式保留。使用自己的 QQ 账号。有风险。

安卓需要电脑端服务及配对，不能脱离电脑独立运行智能体。默认精简 APK 不含 cloudflared、ngrok 与 ONNX 的可选 JNI 库；相关原生能力未纳入本包验收。macOS / Linux 提供源码启动脚本，尚无已验收的 DMG、AppImage 或 DEB。

## 本版改动

- 协作流程与执行记录分开：可编辑入口与节点、配置模型和思考强度、禁用整条分支，并在运行网络查看实际分工和交接。
- 既有 QQ 群支持主账号与多个官方机器人协作；汇报可选择私聊、群聊、内部邮箱或自动选择。模型与思考强度以配置标识显示，无法改平台昵称时使用消息前缀。
- 修复跨好友用量统计越界读取、切换好友后的迟到配置及关闭后的回调问题。
- 修复经验审计、任务经验和研究记录依赖时间生成编号导致的碰撞与覆盖；没有重建历史经验库，也没有把普通聊天全部作为经验。
- 修复嵌套任务计时的浮点边界和 Qt 主题更新时的控件生命周期问题。
- Windows 冻结 Qt 6.11.2，补齐应用内 C++ 运行依赖；深层导入失败不会被浅层“已安装”掩盖，也不会反复换源重装。
- 蓝色安卓包重新构建；修复 BETA 更新被 GitHub 正式发行接口漏掉及不同 BETA 通知被合并的问题。

## 已有验证与边界

Windows 最终应用快照在独立 Windows Sandbox 内完成真实首装、覆盖修复、窗口启动和 CPU 向量环境导入。初始系统无 Python、无应用目录、无对应全局 C++ DLL；最终完整套件 **2475 项通过、25 项跳过、452 个子测试通过，进程退出码 0**。另有 49 项定向检查和 5 次独立 Qt 正常退出。该环境是本机新建的干净 Windows 沙箱，不能称作远程云主机。失败轮次、跳过原因和应用快照一致性见[Windows 详细报告](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/BETA3_VALIDATION.md)。

有限真实 QQ 验收使用 **17 次 gpt-6-luna 调用**，覆盖手动私聊/内部邮箱、自动群聊/私聊、私聊鉴权过期恢复及三机器人多层分支；发送回执、实际 QQ 历史与内部邮箱确认记录分别核对。没有调用 Claude，没有要求重新登录 QQ。结果仅覆盖这些具体场景，不代表全部模型、思考强度和任意拓扑。自动建群按要求暂缓。

Android 本轮构建、签名与模拟器结果见[安卓发布检查](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/ANDROID_BETA3.md)。实体手机、真实扫码质量、厂商后台保活、Google OCR 准确率、语音通话、连续多日无人值守和所有硬件组合尚未完整验收。完整状态见[功能目录](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/FEATURES.md)与[已知问题](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/KNOWN_ISSUES.md)。

公开源码和发行包排除好友、登录态、数据库、工作区、原始聊天、模型权重、签名私钥及本机环境。模式扫描不是完整安全审计；不要把旧接口直接暴露公网。项目按 GPL-3.0-or-later 分发，第三方组件保留各自许可。

## 反馈

当前项目还不是很完善。如果有问题或改进建议，欢迎通过[问题反馈与建议](https://github.com/lby749/Open-QQ-Dot/issues/new/choose)提出，非常感谢。反馈时请提供版本、系统、脱敏后的步骤与错误，不要上传凭据或原始聊天。

感谢您的支持，如果有帮助的话，请点一个 [STAR](https://github.com/lby749/Open-QQ-Dot)，谢谢。
