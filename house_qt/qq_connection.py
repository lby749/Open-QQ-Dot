"""Official bots by default; the complete existing personal-account UI remains optional."""
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem
from qfluentwidgets import CheckBox, ComboBox, LineEdit, PasswordLineEdit, PushButton, TableWidget

from house_status import ROOT
import house_settings
import qq_official
from .common import Card, Page, SectionSwitcher, confirm, hint, row, toast


class QQConnectionPage(Page):
    def __init__(self, house, legacy):
        super().__init__(house, 'qqConnectionPage')
        self.legacy = legacy
        self.rows = []
        self.busy = False
        self.mode = SectionSwitcher(self)
        self.mode.addItem('official', '官方 QQ 机器人')
        self.mode.addItem('legacy', '个人 QQ 账号直连（NAPCAT）')
        self.mode.setCurrentItem('legacy' if house_settings.load(ROOT).get('qq_connection_mode') == 'legacy' else 'official')
        self.mode.currentItemChanged.connect(self.choose_mode)
        self.add(Card('QQ 连接方式', collapsible=False).add(
            self.mode,
            hint('官方机器人与个人账号可以同时在线。切换面板不会断开连接；当前 QQ 可担任主智能体，多个机器人可分别担任协作成员。')))
        self.bot_table = TableWidget()
        self.bot_table.setColumnCount(4)
        self.bot_table.setHorizontalHeaderLabels(['机器人名称', 'AppID', '下次自动连接', '运行状态'])
        self.bot_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bot_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.bot_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.bot_table.setMinimumHeight(140)
        self.bot_table.itemSelectionChanged.connect(self.select_bot_row)
        self.bots_card = Card('已配置的机器人').add(
            hint('可以接入多个机器人，每个 AppID 单独保存、连接和断开。选择一行后，可在下方编辑或查看该机器人的会话。'),
            self.bot_table, self.button('连接所有已启用机器人', self.connect_all_bots))
        self.add(self.bots_card)
        self.selector = ComboBox()
        self.selector.currentIndexChanged.connect(self.select_bot)
        self.name, self.identifier, self.secret = LineEdit(), LineEdit(), PasswordLineEdit()
        self.name.setPlaceholderText('机器人名称，例如：任务主管')
        self.identifier.setPlaceholderText('官方后台的 AppID')
        self.secret.setPlaceholderText('AppSecret（仅保存在本机；留空保留已有密钥）')
        self.enabled = CheckBox('下次打开时自动连接此机器人')
        self.enabled.setChecked(True)
        self.state = hint('尚未配置官方机器人')
        self.official_card = Card('官方 QQ 机器人', collapsible=False).add(
            hint('1. 在 QQ 开放平台创建机器人，开通私聊/群聊事件权限并添加测试成员或测试群。\n'
                 '2. 将 AppID、AppSecret 填在本页，保存后连接。密钥不要发给智能体或贴到聊天里。\n'
                 '3. 私聊机器人，或在群中 @ 它；下方会出现新会话。本地授权后，再发一条消息即可使用。\n'
                 'Windows 使用本机加密存储；Mac/Linux 可通过 QQDOT_BOT_SECRET_AppID 环境变量提供密钥（AppID 替换为数字），本页密钥留空。\n'
                 '群事件中的 OpenID 与普通 QQ 群号不同。官方回复受消息窗口和次数限制；长任务结果始终可在工作台查看。'),
            row(self.selector, self.button('新增机器人', self.new_bot)),
            row(self.name, self.identifier), self.secret, self.enabled,
            row(self.button('保存配置', self.save), self.button('连接官方机器人', self.connect_bot),
                self.button('断开所选机器人', self.disconnect_bot)), self.state)
        self.add(self.official_card)
        self.contacts = TableWidget()
        self.contacts.setColumnCount(3)
        self.contacts.setHorizontalHeaderLabels(['会话类型', '会话标识（仅此机器人）', '本地授权'])
        self.contacts.horizontalHeader().setStretchLastSection(True)
        self.contacts.setMinimumHeight(140)
        self.contact_name = LineEdit()
        self.contact_name.setPlaceholderText('为选中会话起一个本地名称')
        self.authorization_card = Card('机器人会话授权').add(
            hint('未授权的会话不会调用模型。每个机器人、群组和发言人分别隔离；授权不会继承真实 QQ 好友的记忆或电脑权限。'),
            self.contacts, row(self.contact_name, self.button('授权选中会话', self.approve)),
            self.button('刷新状态与会话', self.load))
        self.add(self.authorization_card)
        self.personal_hint = hint('使用自己的 QQ 账号。有风险。')
        self.add(self.personal_hint)
        # Keep the original login/import/export controller, but embed only its
        # content: the connection page owns the single outer scroll area.
        self.legacy_content = legacy.takeWidget()
        legacy.column.setContentsMargins(0, 0, 0, 0)
        legacy.setParent(self)
        legacy.hide()
        self.add(self.legacy_content)
        self.load()

    def button(self, text, action):
        button = PushButton(text)
        button.clicked.connect(action)
        return button

    def choose_mode(self):
        settings = house_settings.load(ROOT)
        settings.setdefault('qq_personal_enabled', house_settings.personal_qq_enabled(ROOT))
        settings['qq_connection_mode'] = self.mode.currentRouteKey()
        house_settings.save(settings, ROOT)
        self.load()
        self.verticalScrollBar().setValue(0)

    def apply_mode(self):
        personal = self.mode.currentRouteKey() == 'legacy'
        self.bots_card.setVisible(not personal)
        self.official_card.setVisible(not personal)
        self.authorization_card.setVisible(not personal)
        self.personal_hint.setVisible(personal)
        self.legacy_content.setVisible(personal)

    def open_legacy(self):
        if self.mode.currentRouteKey() != 'legacy':
            self.mode.setCurrentItem('legacy')
        else:
            self.load()
        self.verticalScrollBar().setValue(0)

    def active_connect(self):
        if self.mode.currentRouteKey() == 'legacy':
            self.open_legacy()
            self.house.active_connect_personal_qq()
        else:
            self.connect_bot()

    def load(self):
        self.apply_mode()
        if self.mode.currentRouteKey() == 'legacy':
            self.legacy.shown()
            return
        try:
            selected = self.selector.currentData()
            self.rows = qq_official.bots(ROOT)
            self.selector.blockSignals(True)
            self.selector.clear()
            self.selector.addItem('请选择机器人或填写下方信息新增', userData='')
            for record in self.rows:
                self.selector.addItem(record['name'] + ' · ' + record['app_id'], userData=record['app_id'])
            index = self.selector.findData(selected)
            self.selector.setCurrentIndex(index if index >= 0 else (1 if self.rows else 0))
            self.selector.blockSignals(False)
            self.refresh_bot_table()
            self.select_bot()
        except Exception as error:
            self.state.setText(str(error))

    def shown(self):
        self.load()

    def refresh_bot_table(self):
        self.bot_table.blockSignals(True)
        self.bot_table.setRowCount(len(self.rows))
        for index, record in enumerate(self.rows):
            values = (record['name'], record['app_id'], '开启' if record.get('enabled') else '关闭',
                      qq_official.status(ROOT, record['app_id']))
            for column, value in enumerate(values):
                self.bot_table.setItem(index, column, QTableWidgetItem(str(value)))
        self.bot_table.blockSignals(False)

    def select_bot_row(self):
        index = self.bot_table.currentRow()
        if 0 <= index < len(self.rows):
            self.selector.setCurrentIndex(self.selector.findData(self.rows[index]['app_id']))

    def connect_all_bots(self):
        if not any(record.get('enabled') for record in self.rows):
            return toast(self.house, 'info', '请先新增机器人并启用下次自动连接。')
        errors = qq_official.start_configured(ROOT)
        self.refresh_bot_table()
        toast(self.house, 'warning' if errors else 'success',
              '；'.join(errors) if errors else '已分别启动所有启用的机器人；连接状态在列表中更新。')

    def new_bot(self):
        self.selector.setCurrentIndex(0)
        self.identifier.clear()
        self.name.clear()
        self.secret.clear()
        self.enabled.setChecked(True)

    def select_bot(self):
        identifier = self.selector.currentData()
        self.secret.clear()
        record = next((r for r in self.rows if r['app_id'] == identifier), None)
        if record:
            self.identifier.setText(identifier)
            self.name.setText(record['name'])
            self.enabled.setChecked(record.get('enabled', True))
        self.refresh_contacts()

    def refresh_contacts(self):
        identifier = self.selector.currentData()
        self.contact_rows = qq_official.ContactStore(ROOT).list(identifier) if identifier else []
        self.contacts.setRowCount(len(self.contact_rows))
        for i, contact in enumerate(self.contact_rows):
            for j, value in enumerate(('群内发言人' if contact['kind'] == 'group' else '私聊',
                                      contact['sender'][:6] + '…', '已授权' if contact['enabled'] else '等待本地授权')):
                self.contacts.setItem(i, j, QTableWidgetItem(value))
        self.state.setText(qq_official.status(ROOT, identifier) if identifier else '尚未配置官方机器人')

    def on_status(self):
        if self.mode.currentRouteKey() == 'legacy':
            self.legacy.on_status()
        else:
            self.refresh_bot_table()
            self.refresh_contacts()

    def save(self):
        try:
            identifier = self.identifier.text().strip()
            qq_official.save_bot(ROOT, identifier, self.name.text(), self.secret.text(), enabled=self.enabled.isChecked())
            self.secret.clear()
            self.load()
            self.selector.setCurrentIndex(self.selector.findData(identifier))
            toast(self.house, 'success', '已在本机保存，点击连接后向机器人发消息。')
            return True
        except Exception as error:
            toast(self.house, 'error', str(error), '未保存机器人配置')
            return False

    def connect_bot(self):
        if self.busy:
            return
        identifier = self.identifier.text().strip()
        if not identifier:
            return toast(self.house, 'info', '请先填写 AppID 与 AppSecret 并保存；申请步骤见本页。', '连接官方机器人')
        if self.secret.text().strip() or identifier not in [r['app_id'] for r in self.rows]:
            if not self.save():
                return
        try:
            qq_official.connect(ROOT, identifier)
            self.state.setText('正在连接；稍后会自动刷新官方服务状态。')
        except Exception as error:
            toast(self.house, 'error', str(error), '官方机器人未连接')

    def disconnect_bot(self):
        identifier = self.selector.currentData()
        if identifier:
            qq_official.stop(ROOT, identifier)
            self.refresh_contacts()

    def approve(self):
        index = self.contacts.currentRow()
        if not 0 <= index < len(self.contact_rows):
            return toast(self.house, 'info', '请先选择一个新会话。')
        contact = self.contact_rows[index]
        identifier = self.selector.currentData()
        name = self.contact_name.text().strip() or '机器人会话 ' + contact['sender'][:6]
        if not confirm(self, '授权机器人会话', '允许这个发言人在所选机器人下使用模型？将建立独立会话，默认不开放电脑工具。', '授权'):
            return
        try:
            qq_official.ContactStore(ROOT).approve(identifier, contact['local_id'], name)
            self.house.reload_friends()
            self.house.fill_friend_box()
            self.refresh_contacts()
            toast(self.house, 'success', '已授权。在顶部选择此会话配置模型，再向 QQ 机器人发一条消息。')
        except Exception as error:
            toast(self.house, 'error', str(error), '授权未完成')

