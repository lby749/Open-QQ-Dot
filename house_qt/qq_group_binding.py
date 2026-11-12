"""Bind a local collaboration room to one personal QQ and several official bots."""
import re
import hashlib
import json
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QScrollArea,
                               QVBoxLayout, QWidget)
from qfluentwidgets import CheckBox, ComboBox, EditableComboBox, LineEdit, PushButton

import agent_group
import qq_group_team
import qq_official
from .common import body, hint, row, run_async, LONG_POOL


class QQGroupBindingDialog(QDialog):
    def __init__(self, house, base, owner, room_id, parent=None):
        super().__init__(parent or house)
        self.house, self.base, self.owner, self.room_id = house, Path(base), str(owner), room_id
        self.home = self.base.parent
        room = agent_group.load_room(self.base, self.owner, room_id)
        config = room.get('qq_group') or {}
        setup = room.get('qq_group_setup') or {}
        self.setWindowTitle('接入真实 QQ 群')
        self.resize(800, 650)
        self.setMinimumSize(650, 500)
        self.setStyleSheet('QDialog {background:#EFF6FF;}')
        layout = QVBoxLayout(self)
        layout.addWidget(body('当前 QQ 账号代表主持人分工与汇总；每名工作成员通过自己的官方机器人发言。'))
        layout.addWidget(hint('机器人凭据在「QQ 连接」管理。在这里选择成员与目标群后，可自动邀请并确认群连接，无须逐个扫码。'))
        area = QScrollArea()
        area.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.enabled = CheckBox('启用这个 QQ 群的协作接入')
        self.enabled.setChecked(bool(config.get('enabled')))
        form.addRow(self.enabled)
        login = getattr(house.pages.get('dotlinkPage'), 'state', {}).get('login') or {}
        self.main_account = LineEdit()
        self.main_account.setText(str(setup.get('self_id') or config.get('self_id') or login.get('uin') or ''))
        self.main_account.setPlaceholderText('当前已登录的主 QQ 账号')
        self.group = LineEdit()
        self.group.setText(str(setup.get('group_id') or config.get('group_id') or ''))
        self.operators = LineEdit()
        self.operators.setText('，'.join(setup.get('members') or config.get('operators') or []))
        self.operators.setPlaceholderText('允许发起协作的群成员 QQ，多个用逗号分隔')
        form.addRow('主 QQ 账号', self.main_account)
        form.addRow('目标 QQ 群号', self.group)
        form.addRow('可发起协作的成员', self.operators)
        host = next((m for m in room['members'] if m['id'] == room.get('host_id')), None)
        form.addRow('主智能体', body(host['name'] if host else '尚未选择主持人，请先保存群设置'))
        self.workers = {}
        bots = qq_official.bots(self.home)
        for member in room['members']:
            if member['id'] == room.get('host_id'):
                continue
            saved = (config.get('bots') or {}).get(member['id']) or {}
            bot = ComboBox()
            bot.addItem('选择官方机器人', userData='')
            for record in bots:
                bot.addItem(f"{record['name']} · {record['app_id']}", userData=record['app_id'])
            bot.setCurrentIndex(max(0, bot.findData(setup.get('bots', {}).get(member['id']) or saved.get('app_id', ''))))
            channel = EditableComboBox()
            channel.setPlaceholderText('该机器人在目标群收到的群 OpenID')
            self.workers[member['id']] = (bot, channel)
            def refresh_channels(*_, selector=bot, field=channel):
                field.clear()
                identifier = selector.currentData()
                channels = list(dict.fromkeys(row['channel'] for row in qq_official.ContactStore(self.home).list(identifier)
                                             if row['kind'] == 'group')) if identifier else []
                field.addItems(channels)
            bot.currentIndexChanged.connect(refresh_channels)
            refresh_channels()
            if saved.get('group_openid'):
                channel.setCurrentText(saved['group_openid'])
            form.addRow(member['name'] + ' · 机器人', bot)
            form.addRow('群 OpenID', channel)
        self.allow_tools = CheckBox('允许智能体建群并接入所选机器人')
        self.allow_tools.setChecked(bool(setup.get('enabled')))
        form.addRow(self.allow_tools)
        self.group_name = LineEdit()
        self.group_name.setPlaceholderText('新 QQ 群名称（建群尚待实测通过）')
        self.create_button = PushButton('创建 QQ 群')
        self.create_button.clicked.connect(lambda: self.setup_action('create'))
        self.connect_button = PushButton('自动接入所选机器人')
        self.connect_button.clicked.connect(lambda: self.setup_action('connect'))
        self.private_button = PushButton('连接机器人与主账号私聊')
        self.private_button.clicked.connect(lambda: self.setup_action('private'))
        form.addRow('新建 QQ 群', row(self.group_name, self.create_button))
        form.addRow(self.connect_button)
        form.addRow(self.private_button)
        area.setWidget(content)
        content.setAutoFillBackground(False)
        layout.addWidget(area, 1)
        self.error = hint('BETA：机器人接入已实测，建群尚待真实验收；不主动邀请普通好友。')
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('保存群接入')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def setup_config(self):
        return {'enabled': self.allow_tools.isChecked(), 'self_id': self.main_account.text(),
            'group_id': self.group.text(), 'members': [v for v in re.split(r'[,，\s]+', self.operators.text()) if v],
            'bots': {member: bot.currentData() for member, (bot, _) in self.workers.items()}}

    def setup_action(self, action):
        import qq_group_connection
        if str(self.house.current_qq or '') != self.owner:
            return self.error.setText('当前好友已切换，请关闭此窗口后重试')
        try:
            config, name = self.setup_config(), self.group_name.text()
            qq_group_connection.save_setup(self.base, self.owner, self.room_id, config)
        except (OSError, ValueError) as exc:
            return self.error.setText(str(exc))
        operation = hashlib.sha256(json.dumps([self.owner, self.room_id, action, config,
            name if action == 'create' else ''], sort_keys=True).encode()).hexdigest()
        for button in (self.create_button, self.connect_button, self.private_button):
            button.setEnabled(False)
        def job(progress):
            from qq_link import QQClient
            client = QQClient()
            try:
                service = qq_group_connection.GroupConnection(self.home, self.owner, config['self_id'], client)
                if action == 'private':
                    return service.link_private(list(config['bots'].values()), operation)
                if action == 'connect':
                    return service.connect_room(self.room_id, operation, progress)
                if config['group_id']:
                    raise ValueError('已有目标群号，未重复建群；新建群请先清空目标群号')
                return service.create_group(name, operation)
            finally:
                client.close()
        def alive():
            from shiboken6 import isValid
            return isValid(self) and self.isVisible() and str(self.house.current_qq or '') == self.owner
        def report(text):
            if alive(): self.error.setText(text)
        def finished(result):
            if not alive(): return
            if result.get('state') == 'VERIFIED':
                if result.get('group_id'): self.group.setText(result['group_id'])
                if action == 'connect':
                    self.enabled.setChecked(True)
                    updated = agent_group.load_room(self.base, self.owner, self.room_id)['qq_group']['bots']
                    for member, (_, channel) in self.workers.items():
                        channel.setCurrentText(updated[member]['group_openid'])
                report('已核对完成；无需重复执行。')
                for button in (self.create_button, self.connect_button, self.private_button):
                    button.setEnabled(True)
            else:
                report('结果尚未核实，已保留操作记录；请勿重复执行。')
        def failed(exc): report(str(exc))
        # External actions stay disabled after uncertain outcomes: reopening must not silently replay them.
        run_async(job, finished, failed, report, pool=LONG_POOL)

    def save(self):
        try:
            if str(self.house.current_qq or '') != self.owner:
                raise PermissionError('当前好友已切换，未保存旧好友的群绑定')
            import qq_group_connection
            qq_group_connection.save_setup(self.base, self.owner, self.room_id, self.setup_config())
            if not self.enabled.isChecked():
                room = agent_group.load_room(self.base, self.owner, self.room_id)
                room.setdefault('qq_group', {})['enabled'] = False
                agent_group.save_room(self.base, self.owner, room)
            else:
                qq_group_team.save_binding(self.base, self.owner, self.room_id, {
                    'enabled': True, 'self_id': self.main_account.text(), 'group_id': self.group.text(),
                    'operators': [v for v in re.split(r'[,，\s]+', self.operators.text()) if v],
                    'bots': {member: {'app_id': bot.currentData(), 'group_openid': channel.currentText()}
                             for member, (bot, channel) in self.workers.items()}})
            self.accept()
        except (OSError, ValueError, RuntimeError, PermissionError) as error:
            self.error.setText(str(error))
