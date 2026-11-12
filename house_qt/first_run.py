"""First-run quick start.

A fresh install has no friends, no models and no QQ login, so the first window used to
be an empty console. This shows a short, styled guide once and never again; the full
walkthrough stays on the 使用教程 page.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QVBoxLayout

from house_settings import load, save
from .common import ACCENT, MUTED

STEPS = (
    ('1', '连接 QQ',
     '在「QQ 与会话 → QQ 连接」配置官方机器人。原个人 QQ 直连界面保留在同页折叠区域，已有登录态继续保留。'),
    ('2', '添加好友',
     '机器人收到消息后，在连接页授权该会话，再选择模型与人设。个人 QQ 好友仍可从「好友总览」配置。'),
    ('3', '选智能体',
     '「智能体与工具」里选 OpenCode / ZCode / DSH，或填自己的 API Key。'),
    ('4', '本地模型按需下载',
     '小型 Q8 语音识别模型自动准备，不超过100MB；权重不随安装包分发。向量与其他语音模型按需下载，已有文件复用。'),
)

STYLE = """
QDialog { background: #F8FAFC; }
QLabel#title { color: #0F172A; font-size: 20px; font-weight: 600; }
QLabel#subtitle { color: #64748B; font-size: 12px; }
QFrame#step { background: #FFFFFF; border: 1px solid #DBEAFE; border-radius: 10px; }
QLabel#badge { color: #FFFFFF; background: %s; border-radius: 12px;
               min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
               font-size: 12px; font-weight: 600; }
QLabel#stepTitle { color: #0F172A; font-size: 13px; font-weight: 600; }
QLabel#stepBody { color: %s; font-size: 12px; }
""" % (ACCENT, MUTED)


def should_show(settings: dict | None = None, root=None) -> bool:
    """True only for a house that has never shown the guide."""
    value = load(root) if settings is None else settings
    return not (isinstance(value, dict) and value.get('onboarding_done'))


def mark_done(root=None) -> None:
    value = load(root)
    if not isinstance(value, dict):
        value = {}
    value['onboarding_done'] = True
    save(value, root)


class FirstRunGuide(QDialog):
    """Short guide shown once; the primary button closes it for good."""

    def __init__(self, parent=None, on_tutorial=None):
        super().__init__(parent)
        self.on_tutorial = on_tutorial
        self.setWindowTitle('开始使用 Open QQ Dot')
        self.setModal(True)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 18)
        layout.setSpacing(10)

        title = QLabel('开始使用 Open QQ Dot')
        title.setObjectName('title')
        layout.addWidget(title)
        subtitle = QLabel('四步就能聊起来；详细说明随时可以在侧栏「使用教程」里看。')
        subtitle.setObjectName('subtitle')
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        for number, heading, text in STEPS:
            layout.addWidget(self._step(number, heading, text))

        layout.addSpacing(4)
        footer = QHBoxLayout()
        footer.addStretch(1)
        tutorial = QLabel(f'<a href="tutorial" style="color:{ACCENT};text-decoration:none;">查看详细教程</a>')
        tutorial.setTextFormat(Qt.RichText)
        tutorial.linkActivated.connect(self._open_tutorial)
        footer.addWidget(tutorial)
        layout.addLayout(footer)
        # qfluentwidgets 的按钮由外层主题决定配色，这里只用它保证视觉一致。
        from qfluentwidgets import PrimaryPushButton
        start = PrimaryPushButton('开始使用')
        start.clicked.connect(self.accept)
        layout.addWidget(start, alignment=Qt.AlignRight)

    def _step(self, number: str, heading: str, text: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName('step')
        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)
        badge = QLabel(number)
        badge.setObjectName('badge')
        badge.setAlignment(Qt.AlignCenter)
        row.addWidget(badge, alignment=Qt.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(2)
        head = QLabel(heading)
        head.setObjectName('stepTitle')
        column.addWidget(head)
        body = QLabel(text)
        body.setObjectName('stepBody')
        body.setWordWrap(True)
        column.addWidget(body)
        row.addLayout(column, 1)
        return frame

    def _open_tutorial(self, _link: str) -> None:
        self.accept()
        if callable(self.on_tutorial):
            self.on_tutorial()

    def accept(self) -> None:
        mark_done()
        super().accept()
