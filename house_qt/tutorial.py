"""Categorised in-app user guide.

Each sidebar tab is a small, self-contained topic; no single wall of text.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from qfluentwidgets import BodyLabel, TitleLabel

from .common import Card, Page, hint, row


def _text(value: str) -> BodyLabel:
    label = BodyLabel(value)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setStyleSheet("font-size:13px;color:#334155;line-height:1.7;")
    return label


class TutorialTopicPage(Page):
    def __init__(self, house, key: str, title: str, subtitle: str, sections):
        super().__init__(house, key)
        head = Card().add(TitleLabel(title), hint(subtitle))
        self.add(head)
        for heading, lines in sections:
            card = Card(heading, collapsible=True, collapsed=False)
            for line in lines:
                if line.startswith("- "):
                    card.add(_text("• " + line[2:]))
                else:
                    card.add(_text(line))
            self.add(card)


QUICK = [
    ("第一次打开", [
        "左侧先看「使用教程」，按顺序完成：连接 QQ → 配好模型 → 手机配对。",
        "第一次添加好友时会弹出快速配置：用途、Agent、性格、关系、主动频率、权限。",
        "所有选项保存后仍可在「设置与工具」和好友的小屋里修改。",
    ]),
    ("建议顺序", [
        "- 1. 启动 Open QQ Dot，确认顶部显示 QQ 已连接。",
        "- 2. 在「智能体与工具」选择模型来源，先用低成本模型测试。",
        "- 3. 在好友状态页打开手机配对二维码。",
        "- 4. 手机和电脑装好 Tailscale 后扫码；同一网络下也可以直接用局域网地址。",
    ]),
    ("最重要的三个按钮", [
        "「QQ 连接/扫码」：登录和刷新 QQ。",
        "「添加好友」：绑定好友；第一次会进入快速配置。",
        "「进入小屋」：进入这位好友的控制台、设置和记忆。",
    ]),
]

CONNECT = [
    ("连接 QQ", [
        "默认使用 QQ 官方机器人：在 QQ 开放平台创建机器人，开通私聊和群消息事件，加入测试成员或测试群。",
        "进入「QQ 与会话 → QQ 连接」，填写 AppID 和 AppSecret，保存后连接。密钥仅保存在本机，不要发到聊天里。",
        "私聊机器人或在群里 @ 它，在连接页授权新会话；再为该会话选择模型和人设。各机器人、群组、发言人分别隔离。",
        "原个人 QQ 登录界面在同页「个人 QQ 账号直连（NapCat，可选）」折叠区域。切换连接方式后仍可扫码、快捷登录或启动原有桥接。",
        "官方连接不会自动借用 NapCat；官方回复窗口结束时，长任务结果保留在工作台，需再次来信后才能回复。",
    ]),
    ("添加好友", [
        "在「好友总览」点「+ 添加好友」，输入 5–12 位 QQ 号。",
        "第一次添加会弹出三步快速配置；之后添加不会重复弹。",
        "每位好友的模型、性格、记忆、权限和聊天记录独立保存。",
    ]),
    ("手机配对", [
        "进入好友的「连接与会话」页，找到手机配对二维码。",
        "手机打开 Open QQ Dot，扫二维码；如果二维码在截图里，可以用 App 里的「从图片识别」。",
        "二维码含一次性访问令牌；不要发到公开群或截图给陌生人。",
        "配对后手机只打开这一位好友的小屋，不会看到其他好友。",
    ]),
    ("二维码刷新", [
        "二维码过期或换网络后，回到状态页刷新；旧令牌会失效。",
        "如果手机提示连不上，先确认电脑上的小屋 Web 服务正在运行，再重新扫码。",
    ]),
]

TAILSCALE = [
    ("为什么用 Tailscale", [
        "Tailscale 把电脑和手机放进同一个私有虚拟网络，不需要公网 IP，也不需要把端口映射到路由器。",
        "Open QQ Dot 的手机配对优先使用 Tailscale 地址；没有 Tailscale 时才退回局域网地址。",
    ]),
    ("电脑端", [
        "- 1. 安装 Tailscale 并登录同一个账号。",
        "- 2. 打开终端执行 tailscale status，确认能看到本机 100.x.x.x 地址。",
        "- 3. 保持 Tailscale 在线；Windows 防火墙允许 Open QQ Dot 的 Web 端口。",
        "- 4. 回到好友状态页刷新二维码，二维码里的地址应变成 100.x 开头。",
    ]),
    ("手机端", [
        "- 1. 安装 Tailscale，登录和电脑相同的账号。",
        "- 2. 打开 Tailscale 开关，确认能看到电脑设备。",
        "- 3. 用手机浏览器或 Open QQ Dot App 扫码。",
        "- 4. 打不开时先在手机浏览器访问二维码里的地址，排除 Tailscale 未连通。",
    ]),
    ("常见问题", [
        "二维码还是 192.168 地址：Tailscale 没启动或没有检测到 tailnet，先启动再刷新。",
        "手机能打开网页但 App 连不上：检查 App 是否更新到同一版本，并重新扫码。",
        "公司网络限制：Tailscale 可能被防火墙拦截，换手机热点测试。",
        "不要在公网直接暴露 8790 端口；优先用 Tailscale 或 SSH 隧道。",
    ]),
]

MODELS = [
    ("模型来源", [
        "「智能体与工具」里可以选本地 CLI（Codex、Claude Code、DeepSeek Harness、Antigravity）、自定义 API 或网页端。",
        "没有本地 Agent 时，快速配置默认使用网页端 DeepSeek；之后仍可切换。",
        "Codex、Claude 和自定义 API 默认按 1M 上下文配置，达到 90% 自动压缩并整理记忆。",
    ]),
    ("低成本起步", [
        "先用低思考档位和低成本模型验证连接，再换高质量模型。",
        "网页端需要先在该浏览器里登录对应账号；登录状态由 Chrome 配置保存。",
        "境外服务（Codex、Claude、ChatGPT、Gemini、Antigravity）默认走代理；没有代理时会停止调用，避免真实 IP 泄露。",
    ]),
    ("本地辅助模型", [
        "快速配置里的性能档位只决定向量、重排和语音模型，不会下载本地大语言模型。",
        "低占用：轻量向量 + 轻量重排 + MeloTTS 最小语音。",
        "中占用/高占用：更大的检索和语音模型；本地 LLM 始终需要手动选择。",
    ]),
    ("多智能体", [
        "「智能体与工具」可以选择多个来源并指定主智能体。",
        "控制台里可以查看每个 Agent 的技能、插件和当前会话。",
        "按 / 调用 Skill，按 @ 引用文件夹或插件。",
    ]),
    ("图片与视频", [
        "如果 Codex、Claude、Antigravity 等多模态 Agent 在 media/generated/ 里生成了图片或视频，回复可用 "
        "[[LOCAL_IMAGE:media/generated/文件名]] 或 [[LOCAL_VIDEO:media/generated/文件名]] 单独发一条。",
        "只允许这个目录内的真实文件，不能发任意磁盘路径或外部链接；每轮最多 1 个，文件不存在或类型不对会被拒绝。",
        "生成或发送前确认素材、肖像和版权授权；不要生成或传播他人隐私、伪造身份或违法内容。",
    ]),
]

MIGRATION = [
    ("入口", [
        "左侧「主页」→「迁移与导入」；也可以在「设置与工具」→「迁移助手」找到。",
        "迁移不会修改原文件；导入记录可在「回溯中心」撤销。",
    ]),
    ("平台导出", [
        "支持 ChatGPT / Claude / Gemini 的 conversations.json、zip、jsonl、Markdown。",
        "会导入对话和项目，并创建可继续使用的会话。",
    ]),
    ("CLI / 桌面应用", [
        "Codex、Claude Code、DeepSeek Harness 等项目按目录迁移。",
        "CLI 模式只复制项目目录，不导入聊天记录、上下文、账号或令牌。",
        "「扫描本机应用」会读取已知数据目录的会话首行，列出项目路径，不会扫描整个磁盘。",
        "迁移完成后自动在控制台建立空会话入口，之后从该目录调用本地 CLI。",
    ]),
]

DISCLAIMER = [
    ("这不是腾讯官方产品", [
        "Open QQ Dot 是第三方开源项目，与腾讯 / QQ 官方没有隶属、授权或合作关系。",
        "QQ、QQNT、NapCat 等名称和组件归各自权利人所有。",
    ]),
    ("账号风险（请优先看）", [
        "通过 NapCat 或其他非官方 OneBot 协议接入 QQ，可能违反 QQ 服务条款，存在账号被限制、冻结或永久封禁的风险。",
        "自动回复、主动消息、群发、频繁重新登录、多账号同时在线、代理或 IP 异常，都会增加触发平台风控的概率。",
        "强烈建议先用单独的测试账号验证，不要直接使用主账号、重要账号或绑定重要资料的账号。",
        "因使用本软件造成的账号限制、数据丢失、设备或第三方服务损失，由使用者自行承担。",
        "本项目不提供绕过风控、解封、伪造身份或规避平台限制的功能。",
    ]),
    ("模型与内容风险", [
        "模型可能生成错误、冒犯、侵权或不适宜的内容；发送前应由使用者审核。",
        "生成或发送图片、视频时，必须确保你拥有相应素材、肖像和版权授权；不要生成或传播他人隐私、伪造身份或违法内容。",
        "不要把 API Key、Cookie、二维码令牌、聊天记录或好友数据发给陌生人，也不要提交到公开仓库。",
    ]),
    ("降低风险的建议", [
        "先用小号或测试群，保留人工确认，避免高频主动消息和短时间大量操作。",
        "定期备份 friends、data、workspaces、exports 和 capsules；更新前先导出。",
        "发现账号异常时立即停止自动回复、断开 QQ 登录并检查最近的发送记录。",
    ]),
]

TOOLS = [
    ("控制台", [
        "控制台左侧是项目和会话树，右侧是消息流。",
        "输入 / 打开 Skill 菜单；输入 @ 引用文件夹或插件。",
        "模型身份和好友身份可以在聊天栏切换；切换后界面颜色会随身份变化。",
    ]),
    ("任务与回溯", [
        "「任务中心」查看全部任务、自主规划和执行状态。",
        "「系统洞察」→「回溯中心」可以撤销任务、工作流、迁移等操作。",
        "重要操作前建议在「迁移助手」里创建 .oqdbackup 数据备份。",
    ]),
    ("常见故障", [
        "页面打不开：先看顶部连接状态，再重启后台桥接。",
        "模型报额度不足：换模型或开启本地兜底；额度恢复后会自动继续。",
        "境外模型报代理错误：先启动代理，再重试；程序不会绕过代理直连。",
        "日志和诊断：设置与工具 → 系统概况 → 诊断，导出后再排查。",
    ]),
]


def build_tutorial_pages(house):
    return {
        'tutorialQuickPage': TutorialTopicPage(house, 'tutorialQuickPage', '快速开始',
                                               '第一次使用按这个顺序走，五分钟能跑通。', QUICK),
        'tutorialConnectPage': TutorialTopicPage(house, 'tutorialConnectPage', '连接 QQ 与手机配对',
                                                 'QQ 登录、二维码刷新和手机扫码的完整步骤。', CONNECT),
        'tutorialTailscalePage': TutorialTopicPage(house, 'tutorialTailscalePage', 'Tailscale 网络',
                                                   '让手机在任意网络下安全连到这台电脑。', TAILSCALE),
        'tutorialModelPage': TutorialTopicPage(house, 'tutorialModelPage', '模型与智能体',
                                               '选择来源、控制成本、配置多智能体和技能。', MODELS),
        'tutorialMigrationPage': TutorialTopicPage(house, 'tutorialMigrationPage', '迁移与项目',
                                                   '从其他平台或本机 CLI 接管已有项目。', MIGRATION),
        'tutorialToolsPage': TutorialTopicPage(house, 'tutorialToolsPage', '工具与故障排查',
                                               '控制台、任务、回溯、备份和常见错误。', TOOLS),
        'tutorialDisclaimerPage': TutorialTopicPage(house, 'tutorialDisclaimerPage', '免责声明与账号风险',
                                                      '使用前必须了解的账号、模型与内容风险。', DISCLAIMER),
    }
