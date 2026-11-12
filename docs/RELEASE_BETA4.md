# Open QQ Dot 1.0.0-beta.4 · BETA 测试版

**在 QQ 里交办任务，在自己电脑上规划、执行和检查结果。** Open QQ Dot 提供中文桌面控制台、好友独立的人设与记忆、可编辑的多智能体分工、可复用工作流、阶段式长期任务和研究，以及配对的蓝色安卓客户端。

<p align="center"><img src="https://raw.githubusercontent.com/lby749/Open-QQ-Dot/main/assets/brand/project-cover.png" width="360" alt="Open QQ Dot 项目配图：星云"/></p>

## 下载哪个

| 文件 | 适合谁、怎么用 |
|---|---|
| [Windows 一键安装器](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Setup-Windows-x64-1.0.0-beta.4.exe) | 大多数 Windows 10 / 11 x64 用户。双击后自动准备环境，无需提前安装 Python |
| [Windows 绿色包，含启动 EXE](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Portable-Windows-x64-1.0.0-beta.4.zip) | 希望自行保存程序目录的用户。完整解压后双击 `Open QQ Dot.exe`，首次需要联网 |
| [安卓蓝色版](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Android-FOSS-arm64-1.0.0-beta.4.apk) | Android 13+、arm64，与电脑配对使用 |
| [安卓蓝色版，含 Google OCR](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Android-GMS-arm64-1.0.0-beta.4.apk) | 与上一款二选一，增加 Google OCR |
| [完整源码](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Source-1.0.0-beta.4.zip) | 桌面和蓝色安卓源码、测试与文档 |
| [全部文件压缩包](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/OpenQQDot-Release-1.0.0-beta.4-All.zip) · [校验清单](https://github.com/lby749/Open-QQ-Dot/releases/download/v1.0.0-beta.4/SHA256SUMS.txt) | 一次保存，或核对下载完整性 |

本次主要更新电脑端，蓝色安卓客户端同步版本后重新打包。两个 APK 不能共存，同证书升级请覆盖安装，不要先卸载。Windows 安装器未作商业代码签名，安卓沿用测试签名。安装包不含个人资料、登录态或模型权重，需要你自己的模型与 QQ 授权。

## 有什么不一样

- **聊天和做事都有入口**：QQ 可以聊天也可以交办任务，工作台集中查看计划、工具操作、产物和验收，避免只得到一句“完成了”。
- **协作方式可以编辑**：把多个智能体安排成分支，为成员选择模型、思考强度和职责；支持已有群聊、私聊、内部邮箱及自动选择汇报。配置流程和实际运行记录分别查看。
- **长任务可以分阶段管理**：保存目标、检查点、预算、阶段产物和研究记录，支持检查与继续；完整科研闭环仍在完善。
- **成功做法可以留下来**：把验收过的任务和纠正整理成可编辑工作流，配合任务胶囊复用，普通聊天不会全部当成任务经验。
- **好友分别设置**：人设、回复方式、记忆和权限按好友管理；相同模型文件共用，避免为每位好友重复下载。
- **QQ、桌面和手机配合**：电脑统一管理，QQ 收发与交办，蓝色安卓应用配对后查看与参与。手机端依赖电脑保持运行。

## 这次改善了什么

- 语音输入改为直接点按：点一下开始、图标变为停止，再点一下结束并转写。文字先放进输入框，不自动发送；切换好友或会话不会把旧录音文字写到新会话。
- 小型语音识别模型首次自动准备，已有有效文件复用；语音设置提供一键下载、修复和模型选择。
- 修复中文输入法组词时回车误发送、旧好友的迟到响应覆盖新页面、常驻指令外部更新与未保存草稿的冲突。
- 改善快速配置恢复、模板展示、消息同步、权限拒绝和桥接启动状态，保持好友数据分别管理。
- 远程访问加入一键准备 Tailscale 与配置 Funnel 的入口，未完成登录或公网不可达时明确提示，不把本机二维码当作手机远程已连通。

## 当前状态

这仍是 BETA。已执行的检查及其范围见[本轮测试记录](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/桌面复核与Funnel连接_20261112.md)；原始失败、跳过及未验证项保留，不把用例通过率当作全部功能已经可用。

公网远程访问、实体手机、真实麦克风、多日无人值守、所有模型与思考档位、完整双工通话、完整科研论文流程及 macOS / Linux 原生安装包仍有未验证或未完成部分。自动建群目前暂缓。更多见[功能目录](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/FEATURES.md)和[已知问题](https://github.com/lby749/Open-QQ-Dot/blob/main/docs/KNOWN_ISSUES.md)。

QQ 默认官方机器人，原有个人账号方式保留。使用自己的 QQ 账号。有风险。

## 反馈与支持

当前项目还不是很完善。如果有什么问题或改进建议，欢迎通过[问题反馈与建议](https://github.com/lby749/Open-QQ-Dot/issues/new/choose)提出，非常感谢。请提供版本、系统和脱敏后的步骤，不要上传密钥、二维码或原始聊天。

感谢您的支持，如果有帮助的话，请点一个 [STAR](https://github.com/lby749/Open-QQ-Dot)，谢谢。
