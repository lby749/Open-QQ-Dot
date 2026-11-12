"""Unfinished capabilities, separated from functions with bounded real acceptance."""
from .common import Card, Page, hint
from qfluentwidgets import PushButton


FEATURES = (
    ('官方 QQ 群多机器人自动分工', '未完成',
     '已有单机器人私聊与群内被动回复的接口测试。',
     '尚未完成多个机器人按分工互相交流、调度和真实群验收。', 'qqConnectionPage'),
    ('手机实时语音通话', '未完成',
     '邀请状态与接听信令已做程序测试。',
     '完整双工、真机音频和回声处理未完成验收；不能当作 QQ 原生拨号。', 'voice'),
    ('多品牌健康数据接入', '未完成',
     '已有 Health Connect 读取代码和授权、去重检查。',
     '其他品牌缺少实际读取适配器，尚未连接真实健康设备。', 'healthPreviewPage'),
    ('通用科学发现与论文流水线', '部分实现',
     '真实 Luna 完成有限实验、Python 执行、独立验算和持久续作。',
     '完整消融、独立封存评估和论文交付流程尚未完成；有界研究仍可使用。', 'researchAgentPage'),
    ('多日无人值守与断电续作', '待实机验收',
     '真实双智能体胶囊、有限预算、持久恢复与重复执行拦截已有证据。',
     '没有连续多日运行或断电验收；未知外部副作用仍需人工核查。', 'autonomyPage'),
    ('官方机器人真实收发', '缺账号验收',
     '官方 SDK 接口、联系人隔离与发送去重已有程序检查。',
     '缺少官方 AppID 和密钥，尚未完成真实官方账号收发。连接配置入口保留。', 'qqConnectionPage'),
    ('手机端完整实机协作', '缺设备验收',
     '配对权限与真实 HTTP、Gemini → ZCode 接力已通过；Android 已构建。',
     '未连接实体手机，音频、后台保活及全部真机界面未验收。配对入口保留。', 'devicesPage'),
    ('Mac 与 Linux 安装版本', '待构建验收',
     '源码及部分跨平台启动逻辑保留。',
     '没有本轮可交付的已验收安装包，不能以 Windows 结果代替。', None),
)


class DevelopmentPage(Page):
    def __init__(self, house):
        super().__init__(house, 'developmentPage')
        self.add(Card('待开发中功能', collapsible=False).add(
            hint('按实际测试与完成度分类。未完成、部分实现和环境受限分别标明；这里的入口用于试用或补做验收。'),
            hint('正常入口保留已验证的 QQ 聊天、有限任务与胶囊、智能体协作、设备配对等功能。浏览器已验证实际页面操作，有界研究已真实执行；不把它们的全部能力都说成成熟。')))
        self.voice_status = hint('仅创建本机待领取邀请；未验证实体音频，不等于向 QQ 发起原生通话。')
        for title, status, evidence, remaining, target in FEATURES:
            card = Card(title+' · '+status, collapsed=True).add(
                hint('已验证：'+evidence), hint('待完成：'+remaining))
            if target:
                button = PushButton('创建试用邀请' if target == 'voice' else '打开相关功能')
                button.clicked.connect(lambda _=False, target=target: self.open_feature(target))
                card.add(button)
                if target == 'voice': card.add(self.voice_status)
            self.add(card)

    def open_feature(self, target):
        if target == 'voice':
            page = self.house.pages['devicesPage']
            page.invite_call()
            self.voice_status.setText(page.invite_status.text())
        else:
            self.house.switchTo(self.house.pages[target])
