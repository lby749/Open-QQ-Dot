"""Chinese release recommendations, shared by the whole installed application."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, InfoBar, InfoBarPosition, PlainTextEdit, PushButton, ScrollArea

import app_update
import house_settings
from house_status import ROOT
from version import VERSION
from .common import Card, LONG_POOL, body, hint, row, run_async


class UpdatePage(ScrollArea):
    def __init__(self, house):
        super().__init__()
        self.house, self.busy, self.release = house, False, None
        self.setObjectName('softwareUpdatePage')
        self.setWidgetResizable(True)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(24, 18, 24, 18)
        card = Card('版本与更新', collapsible=False)
        card.add(body(f'当前版本：{VERSION} · BETA 测试版'))
        self.automatic = CheckBox('自动检查并推荐新版本（每天最多一次）')
        self.automatic.setChecked(house_settings.load(ROOT).get('app_auto_check_updates', True))
        self.automatic.stateChanged.connect(self.save_preference)
        card.add(self.automatic)
        self.status = body('尚未检查更新。')
        self.notes = PlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(180)
        self.notes.setPlaceholderText('发现新版本后，这里会显示更新内容。')
        self.check_button = PushButton('立即检查更新')
        self.check_button.clicked.connect(lambda: self.check_updates(automatic=False))
        self.open_button = PushButton('查看更新并下载')
        self.open_button.clicked.connect(self.open_release)
        self.open_button.setEnabled(False)
        card.add(self.status, self.notes, row(self.check_button, self.open_button, None),
                 hint('更新由你选择下载和安装。安装器会保留好友设定、模型缓存和 QQ 登录状态。'))
        layout.addWidget(card)
        feedback = Card('问题反馈与建议', collapsible=False)
        feedback.add(body('当前项目仍在 BETA 测试阶段，尚不完善。欢迎提出改进建议，非常感谢。'),
                     hint('感谢您的支持，如果有帮助的话，请点一个 STAR，谢谢。'))
        self.feedback_button = PushButton('反馈问题与改进建议')
        self.feedback_button.setEnabled(bool(app_update.repository()))
        self.feedback_button.clicked.connect(self.open_feedback)
        self.feedback_status = hint('在项目的 GitHub 问题页面提交；程序不会自动上传日志。' if app_update.repository()
                                    else '独立项目仓库发布后，这里会启用反馈入口。')
        feedback.add(self.feedback_button, self.feedback_status)
        layout.addWidget(feedback)
        layout.addStretch(1)
        self.setWidget(widget)

    def save_preference(self):
        value = house_settings.load(ROOT)
        previous = value.get('app_auto_check_updates', True)
        value['app_auto_check_updates'] = self.automatic.isChecked()
        try:
            house_settings.save(value, ROOT)
        except OSError:
            self.automatic.blockSignals(True)
            self.automatic.setChecked(previous)
            self.automatic.blockSignals(False)
            self.status.setText('设置未保存，请检查应用目录权限。')

    def shown(self):
        self.feedback_button.setEnabled(bool(app_update.repository()))
        if not app_update.repository():
            self.status.setText('当前发行包尚未配置更新来源，正式发布后启用。')

    def check_updates(self, automatic=True):
        if self.busy:
            return
        if automatic and (not self.automatic.isChecked() or not app_update.repository()):
            return
        self.busy = True
        self.check_button.setEnabled(False)
        self.status.setText('正在检查新版本……')
        run_async(lambda _: app_update.check(ROOT, force=not automatic),
                  lambda result: self.finished(result, automatic),
                  lambda _error: self.finished({'status': 'error'}, automatic), pool=LONG_POOL)

    def finished(self, result, automatic):
        if getattr(self.house, 'exiting', False):
            return
        self.busy = False
        self.check_button.setEnabled(True)
        self.release = result.get('release') if result.get('status') == 'available' else None
        self.open_button.setEnabled(bool(self.release))
        self.notes.setPlainText(self.release['notes'] if self.release else '')
        messages = {'unconfigured': '当前发行包尚未配置更新来源，正式发布后启用。',
                    'disabled': '自动检查已关闭，可手动检查。', 'latest': '已是当前更新通道的最新版本。',
                    'error': '更新检查未完成，请检查网络和应用目录权限。',
                    'offline': '暂时无法检查更新，请检查网络后手动重试。'}
        self.status.setText(f'发现新版本：{self.release["version"]}' if self.release else
                            messages.get(result.get('status'), '尚未检查更新。'))
        if (automatic and self.release and result.get('notified_version') != self.release['version']):
            bar = InfoBar.info(title='有新版本可以更新', content=f'Open QQ Dot {self.release["version"]}',
                               position=InfoBarPosition.TOP_RIGHT, duration=-1, isClosable=True, parent=self.house)
            button = PushButton('查看更新')
            button.clicked.connect(lambda: (self.house.switchTo(self), bar.close()))
            bar.addWidget(button)
            app_update.mark_notified(ROOT, self.release['version'])

    def open_release(self):
        if self.release:
            QDesktopServices.openUrl(QUrl(self.release['url']))

    def open_feedback(self):
        repository = app_update.repository()
        if repository:
            QDesktopServices.openUrl(QUrl(f'https://github.com/{repository}/issues/new/choose'))
