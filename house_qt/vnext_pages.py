"""Functional Fluent pages for the vNext account, device, health and experience services."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QGridLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import (BodyLabel, ComboBox, LineEdit, PasswordLineEdit, PushButton,
                            PrimaryPushButton, TableWidget)

from house_status import ROOT
from .common import Card, Page, body, hint, row, run_async, toast

from provider_presets import PRESETS

VENDOR_LABELS = {
    'codex': 'Codex CLI', 'claude': 'Claude Code', 'antigravity': 'Antigravity',
    'dsh': 'DeepSeek Harness', 'opencode': 'OpenCode', 'custom': '自定义 API',
    'deepseek': 'DeepSeek API', 'ollama': 'Ollama', 'kimi_code': 'Kimi Code',
}


def _refresh_button(slot):
    button = PushButton('刷新')
    button.clicked.connect(slot)
    return button


def _table(headers, widths=None):
    table = TableWidget()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
    for index, width in enumerate(widths or []):
        table.setColumnWidth(index, width)
    table.setMinimumHeight(190)
    return table


class AccountsVNextPage(Page):
    """CC Switch-style provider cards: one active account per agent tool."""
    TOOLS = [('claude', 'Claude Code'), ('codex', 'Codex'), ('opencode', 'OpenCode'),
             ('dsh', 'DeepSeek Harness'), ('gemini', 'Gemini CLI')]

    def __init__(self, house):
        super().__init__(house, 'vNextAccountsPage')
        self.tool = ComboBox()
        for key, label in self.TOOLS:
            self.tool.addItem(label, userData=key)
        self.tool.currentIndexChanged.connect(self.refresh)
        self.preset = ComboBox()
        self.preset.addItem('选择预设（可选）', userData='')
        for name, values in PRESETS.items():
            self.preset.addItem(name, userData=name)
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.label = LineEdit(); self.label.setPlaceholderText('账号名称，例如 OpenCode 免费')
        self.provider = ComboBox()
        for key, label in VENDOR_LABELS.items():
            self.provider.addItem(label, userData=key)
        self.provider.setCurrentIndex(max(0, self.provider.findData('opencode')))
        self.model = LineEdit(); self.model.setPlaceholderText('模型，例如 opencode-go/iceman-contributor-free')
        self.base_url = LineEdit(); self.base_url.setPlaceholderText('Base URL（可留空）')
        self.api_key = PasswordLineEdit(); self.api_key.setPlaceholderText('API Key（可留空；留空只保存引用）')
        self.add_button = PrimaryPushButton('添加账号')
        self.add_button.clicked.connect(self.add_account)
        self.cards_box = QWidget()
        self.cards_layout = QGridLayout(self.cards_box)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(12)
        self.state = hint('')
        self.usage = BodyLabel('')
        self.usage.setWordWrap(True)
        self.export_button = PushButton('导出账号')
        self.export_button.clicked.connect(self.export_accounts)
        self.import_button = PushButton('导入账号')
        self.import_button.clicked.connect(self.import_accounts)
        self.add(Card('账号与供应商切换', actions=(_refresh_button(self.refresh), self.export_button, self.import_button)).add(
            hint('像 CC Switch 一样：选工具，给每个工具启用一个供应商账号；卡片显示当前启用项。'),
            row(BodyLabel('当前工具'), self.tool, None),
            self.cards_box,
            self.usage,
            self.state))
        self.add(Card('添加供应商账号', collapsible=True, collapsed=True).add(
            row(BodyLabel('预设'), self.preset, BodyLabel('名称'), self.label, None),
            row(BodyLabel('Provider'), self.provider, None),
            row(BodyLabel('模型'), self.model, BodyLabel('Base URL'), self.base_url, None),
            row(BodyLabel('API Key'), self.api_key, None, self.add_button),
            hint('OpenCode 免费模型可填 opencode-go/iceman-contributor-free。API Key 由 DPAPI 加密。')))

    def load(self):
        self.refresh()

    def refresh(self):
        import account_service
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        rows = account_service.list_accounts(ROOT)
        tool = self.tool.currentData() or 'opencode'
        active = account_service.active_accounts(ROOT).get(tool, '')
        for index, item in enumerate(rows):
            card = Card(str(item.get('label') or item.get('id') or '未命名账号'),
                        collapsible=False)
            is_active = item.get('id') == active
            badge = BodyLabel('● 已启用' if is_active else '○ 未启用')
            badge.setStyleSheet('color:#16A34A;' if is_active else 'color:#94A3B8;')
            card.add(
                BodyLabel(f"{item.get('provider', '')} · {item.get('model', '') or '未指定模型'}"),
                hint(f"Base URL：{item.get('base_url', '') or '默认'}"),
                row(badge, self._action('测试', self.test_account, item),
                    self._action('启用', self.activate, item),
                    self._action('复制', self.clone_account, item),
                    self._action('删除', self.delete_account, item), None))
            self.cards_layout.addWidget(card, index // 2, index % 2)
        self.state.setText(f'共 {len(rows)} 个账号；{self.tool.currentText()} 当前启用：{active or "未设置"}')
        try:
            import usage_accounting
            directory = getattr(self.house, 'friend_dir', None)
            if directory:
                text = usage_accounting.display(usage_accounting.summary(Path(directory)))
                self.usage.setText('当前好友真实用量：\n' + text[-1200:])
            else:
                self.usage.setText('当前好友真实用量：未选择好友')
        except Exception as exc:
            self.usage.setText(f'用量读取失败：{type(exc).__name__}')

    @staticmethod
    def _action(text, slot, item):
        button = PushButton(text)
        button.clicked.connect(lambda _=False: slot(item))
        return button

    def apply_preset(self):
        name = self.preset.currentData()
        if not name or name not in PRESETS:
            return
        provider, model, base = PRESETS[name]
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider)))
        self.model.setText(model)
        self.base_url.setText(base)
        if not self.label.text().strip():
            self.label.setText(name)

    def export_accounts(self):
        import json
        import account_service
        path, _ = QFileDialog.getSaveFileName(self, '导出账号目录', 'vnext-accounts.json', 'JSON (*.json)')
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(account_service.export_catalog(ROOT), ensure_ascii=False, indent=2), 'utf-8')
            toast(self.house, 'success', '已导出非敏感账号目录', '账号导出')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '导出失败')

    def import_accounts(self):
        import json
        import account_service
        path, _ = QFileDialog.getOpenFileName(self, '导入账号目录', '', 'JSON (*.json)')
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text('utf-8'))
            result = account_service.import_catalog(ROOT, payload)
            self.refresh()
            toast(self.house, 'success', f"新增 {result['added']} 个账号", '账号导入')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '导入失败')

    def add_account(self):
        import account_service
        data = {'label': self.label.text().strip() or '未命名账号',
                'provider': self.provider.currentData() or 'custom',
                'model': self.model.text().strip(), 'base_url': self.base_url.text().strip()}
        try:
            if self.api_key.text().strip():
                account_service.add_with_key(ROOT, data, self.api_key.text().strip())
            else:
                account_service.add(ROOT, data)
            self.label.clear(); self.model.clear(); self.api_key.clear()
            self.refresh()
            toast(self.house, 'success', '账号已添加', 'vNext 账号')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '添加账号失败')

    def activate(self, item):
        import account_service
        try:
            tool = self.tool.currentData() or 'opencode'
            account_service.activate(ROOT, tool, item.get('id', ''))
            if self.house.current_qq:
                account_service.apply_to_friend(ROOT, str(self.house.current_qq), item.get('id', ''))
            self.refresh()
            toast(self.house, 'success', f"{self.tool.currentText()} 已切换到 {item.get('label')}", '账号切换')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '切换失败')

    def test_account(self, item):
        import account_service
        try:
            result = account_service.probe(ROOT, item.get('id', ''))
            toast(self.house, 'success', f"返回 {len(result.get('models') or [])} 个模型", '账号可用')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '账号不可用')

    def clone_account(self, item):
        try:
            import account_service
            row = account_service.clone(ROOT, item.get('id', ''))
            self.refresh()
            toast(self.house, 'success', f"已复制为 {row['label']}", 'vNext 账号')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '复制失败')

    def delete_account(self, item):
        import account_service
        try:
            account_service.remove(ROOT, item.get('id', ''))
            self.refresh()
            toast(self.house, 'success', '账号已删除', 'vNext 账号')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '删除失败')


class DevicesVNextPage(Page):
    def __init__(self, house):
        super().__init__(house, 'vNextDevicesPage')
        self.code = BodyLabel('点击生成一次性配对码')
        self.code.setStyleSheet('font-size:24px;font-weight:700;letter-spacing:4px;color:#1D4ED8;')
        self.make_button = PrimaryPushButton('生成配对码'); self.make_button.clicked.connect(self.make_code)
        self.table = _table(['设备', 'ID', '权限', '有效期'], [140, 150, 230, 150])
        self.revoke_button = PushButton('撤销所选设备'); self.revoke_button.clicked.connect(self.revoke)
        self.state = hint('')
        self.invite_status = hint('')
        self.invite_status.setParent(self)
        from qfluentwidgets import CheckBox
        self.allow_collaboration = CheckBox('允许手机查看和执行当前好友的智能体协作任务')
        self.allow_settings = CheckBox('允许手机修改当前好友的后台模型与额度回复')
        self.tls_fingerprint = BodyLabel('')
        self.tls_fingerprint.setWordWrap(True)
        self.tls_fingerprint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.add(Card('DeviceGateway 设备配对', actions=(_refresh_button(self.refresh),)).add(
            hint('配对码只在本机生成，5 分钟有效；设备令牌只保存 SHA-256 哈希，不显示明文。'),
            row(BodyLabel('一次性配对码'), self.code, self.make_button, None),
            self.allow_collaboration,
            self.allow_settings,
            self.tls_fingerprint,
            self.table, row(self.revoke_button, None), self.state))
        self.invite_status.hide()

    def store(self):
        from device_gateway import DeviceStore
        return DeviceStore(ROOT / 'data' / 'vnext_devices.json')

    def load(self): self.refresh()

    def refresh(self):
        try:
            rows = self.store().list_devices()
            from tls_support import certificate_fingerprint
            fingerprint = certificate_fingerprint(ROOT/'data/vnext_tls')
            self.tls_fingerprint.setText('本机 HTTPS 证书 SHA-256：' + fingerprint if fingerprint else
                'HTTPS 首次启用后，这里显示本机证书指纹，供手机核对并填写；不要使用其他电脑的指纹。')
        except Exception as exc:
            self.state.setText(f'读取失败：{exc}'); return
        self.table.setRowCount(len(rows))
        for index, device in enumerate(rows):
            expires = device.get('expires_at') or 0
            values = [device.get('name', ''), device.get('id', ''), '、'.join(device.get('scopes') or []),
                      time.strftime('%Y-%m-%d', time.localtime(expires)) if expires else '长期']
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self.state.setText(f'已配对 {len(rows)} 台设备。')

    def invite_call(self):
        qq = getattr(self.house, 'current_qq', None)
        if not qq:
            self.invite_status.setText('请先选择好友'); return
        try:
            import voice_invites
            row = voice_invites.create(ROOT, str(qq))
            self.invite_status.setText('已在本机创建通话邀请，2 分钟内有效，等待手机领取；尚未确认设备接收或音频连通。')
            toast(self.house, 'success', '已创建本机试用邀请', '通话试用')
        except Exception as exc:
            self.invite_status.setText(f'邀请失败：{exc}')

    def make_code(self):
        try:
            from device_gateway import DEFAULT_SCOPES
            owner = getattr(self.house, 'current_qq', None)
            if (self.allow_collaboration.isChecked() or self.allow_settings.isChecked()) and not owner:
                raise ValueError('请先选择好友，再授予手机协作或设置权限')
            scopes = (*DEFAULT_SCOPES, 'collab:read', 'collab:write') if self.allow_collaboration.isChecked() else DEFAULT_SCOPES
            if self.allow_settings.isChecked():
                scopes = (*scopes, 'settings:read', 'settings:write')
            self.code.setText(self.store().create_code(scopes=scopes, owners=(owner,) if owner else ()))
        except Exception as exc:
            toast(self.house, 'error', str(exc), '生成配对码失败')

    def revoke(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            toast(self.house, 'error', '请先选择设备', '撤销失败'); return
        item = self.table.item(rows[0].row(), 1)
        try:
            self.store().revoke(item.text() if item else '')
            self.refresh()
            toast(self.house, 'success', '设备已撤销', 'DeviceGateway')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '撤销失败')


class HealthVNextPage(Page):
    def __init__(self, house):
        super().__init__(house, 'vNextHealthPage')
        self.body = BodyLabel('读取中…'); self.body.setWordWrap(True)
        self.add(Card('HealthBridge 数据来源', actions=(_refresh_button(self.refresh),)).add(
            hint('只显示真实能力上报和已授权 scope；没有授权时不会伪造步数或睡眠数据。'),
            self.body))

    def bridge(self):
        from health_bridge import HealthBridge
        return HealthBridge(ROOT / 'data' / 'health_bridge.json')

    def load(self): self.refresh()

    def refresh(self):
        try:
            status = self.bridge().status()
        except Exception as exc:
            self.body.setText(f'读取失败：{exc}'); return
        lines = [f"事件总数：{status.get('event_count', 0)}；真实事件：{'有' if status.get('has_real_events') else '暂无'}"]
        for item in status.get('adapters', []):
            scopes = '、'.join(item.get('authorized_scopes') or []) or '未授权'
            lines.append(f"· {item.get('label')}：{'可用' if item.get('supported') else '未上报可用'}；{scopes}")
        self.body.setText('\n'.join(lines))


class ExperienceVNextPage(Page):
    def __init__(self, house):
        super().__init__(house, 'vNextExperiencePage')
        self.summary = BodyLabel('读取中…'); self.summary.setWordWrap(True)
        self.query = LineEdit(); self.query.setPlaceholderText('搜索问题、做法或原因')
        self.search_button = PushButton('检索经验'); self.search_button.clicked.connect(self.search)
        self.results = _table(['问题', '做法', '状态', '来源'], [230, 300, 100, 100])
        self.problem = LineEdit(); self.problem.setPlaceholderText('问题 / 症状')
        self.action = LineEdit(); self.action.setPlaceholderText('做法 / 修正')
        self.cause = LineEdit(); self.cause.setPlaceholderText('原因（可选）')
        self.record_button = PushButton('写入候选经验'); self.record_button.clicked.connect(self.record)
        self._experience_owner = None
        self.add(Card('ExperienceEngine 经验库', actions=(_refresh_button(self.refresh),)).add(
            self.summary,
            row(self.query, self.search_button, None),
            self.results,
            row(BodyLabel('问题'), self.problem, BodyLabel('做法'), self.action, None),
            row(BodyLabel('原因'), self.cause, None, self.record_button),
            hint('候选经验只写入 CANDIDATE / low_unverified，不会冒充已验证。')))

    def load(self): self.refresh()

    def refresh(self):
        import experience_service
        owner = getattr(self.house, 'current_qq', '')
        self.record_button.setEnabled(bool(owner))
        self.search_button.setEnabled(bool(owner))
        if owner != self._experience_owner:
            self._experience_owner = owner
            self.problem.clear(); self.action.clear(); self.cause.clear(); self.query.clear()
            self.fill([])
        if not owner:
            self.summary.setText('请先选择好友，经验按好友分别显示。')
            self.fill([])
            return
        snap = experience_service.snapshot(ROOT, owner=owner)
        self.summary.setText(f"经验总数 {snap.get('total', 0)}；已验证 {snap.get('verified', 0)}；"
                             f"候选 {int((snap.get('by_state') or {}).get('CANDIDATE', 0))}")
        self.fill(snap.get('recent') or [])

    def fill(self, rows):
        self.results.setRowCount(len(rows))
        for index, item in enumerate(rows):
            values = [item.get('problem', ''), item.get('action', ''), item.get('state', ''), item.get('provider', '')]
            for column, value in enumerate(values):
                self.results.setItem(index, column, QTableWidgetItem(str(value)))

    def search(self):
        import experience_service
        try:
            self.fill(experience_service.search(ROOT, self.query.text().strip(), owner=self.house.current_qq))
        except Exception as exc:
            toast(self.house, 'error', str(exc), '检索失败')

    def record(self):
        import experience_service
        try:
            experience_service.record_candidate(ROOT, self.problem.text(), self.action.text(), self.cause.text(), owner=self.house.current_qq)
            self.problem.clear(); self.action.clear(); self.cause.clear()
            self.refresh()
            toast(self.house, 'success', '候选经验已写入', 'ExperienceEngine')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '写入失败')
