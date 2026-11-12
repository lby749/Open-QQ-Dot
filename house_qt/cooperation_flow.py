"""Owner-scoped configuration, deliberately separate from live collaboration history."""
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainterPath, QColor, QPen, QFontMetricsF
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QScrollArea, QSplitter, QFrame)
from qfluentwidgets import (LineEdit, ComboBox, CheckBox, PushButton, PrimaryPushButton, SubtitleLabel, BodyLabel)

import agent_group
from house_settings import data_dir
from house_status import load_profile
from .collaboration import TreeView, PANEL
from .common import toast, run_async, LONG_POOL, Card, InputDialog, hint

ENTRIES = {'workbench': '工作台任务', 'private': 'QQ 好友私聊', 'group': 'QQ 群'}
EFFORTS = {'': '自动选择', 'none': '关闭', 'minimal': '最低', 'low': '低', 'medium': '中', 'high': '高',
           'xhigh': '超高', 'max': '最高', 'ultra': '极高', 'off': '关闭'}


class FlowTree(TreeView):
    """Use the existing nodes, selection and zoom; only configuration branches spread left/right."""
    LEVEL, GAP = 105, 36

    def __init__(self, on_select):
        self.positions = {}
        super().__init__(on_select, editable=True)

    def centre(self, index):
        return self.positions.get(index, QPointF(self.MARGIN, self.MARGIN))

    def relayout(self):
        self.positions = {}
        children = {}
        roots = []
        for index, (_, _, parent) in enumerate(self.rows):
            (roots if parent < 0 else children.setdefault(parent, [])).append(index)
        bold, small = self.fonts()
        widths = {}
        def measure(index):
            node = self.rows[index][0]
            label = max(QFontMetricsF(bold).horizontalAdvance(self.title(node)),
                        QFontMetricsF(small).horizontalAdvance(node.get('brief') or ''))
            spans = [measure(child) for child in children.get(index, [])]
            widths[index] = max(130, label * 2 + 40, sum(spans) + self.GAP * max(0, len(spans)-1))
            return widths[index]
        for root in roots:
            measure(root)
        total = sum(widths[root] for root in roots) + self.GAP * max(0, len(roots)-1)
        available = self.width() / self.zoom
        left = max(self.MARGIN, (available-total)/2)
        def place(index, x, depth):
            self.positions[index] = QPointF(x + widths[index]/2, 30 + depth*self.LEVEL)
            descendants = children.get(index, [])
            span = sum(widths[c] for c in descendants) + self.GAP * max(0, len(descendants)-1)
            child_x = x + (widths[index]-span)/2
            for child in descendants:
                place(child, child_x, depth+1)
                child_x += widths[child] + self.GAP
        for root in roots:
            place(root, left, 0)
            left += widths[root] + self.GAP
        self.setMinimumWidth(int((total + self.MARGIN*2)*self.zoom))
        depth = max((depth for _, depth, _ in self.rows), default=0)
        self.setMinimumHeight(max(180, int((depth*self.LEVEL+90)*self.zoom)))
        super().relayout()

    def paint_connections(self, painter):
        painter.setBrush(Qt.NoBrush)
        for index, (node, _, parent) in enumerate(self.rows):
            if parent < 0:
                continue
            top, bottom = self.centre(parent), self.centre(index)
            start = QPointF(top.x(), top.y()+self.radius(self.rows[parent][0])+3)
            end = QPointF(bottom.x(), bottom.y()-self.radius(node)-3)
            middle = (start.y()+end.y())/2
            path = QPainterPath(start)
            path.cubicTo(QPointF(start.x(), middle), QPointF(end.x(), middle), end)
            painter.setPen(QPen(QColor('#CBD5E1' if node.get('disabled') else '#94A3B8'), 1.5))
            painter.drawPath(path)

    def wheelEvent(self, event):
        super().wheelEvent(event)
        if event.modifiers() & Qt.ControlModifier:
            self.relayout()
            self.update()


class CooperationFlowPage(QWidget):
    def __init__(self, house, base=None):
        super().__init__(house)
        self.house, self.base = house, base
        self.loaded_for = None
        self.selection = None
        self.rooms = {}
        self.catalogs = {}
        self.fetch_token = None
        self.setObjectName('cooperationFlowPage')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#cooperationFlowPage {background:#F5F8FC;}')
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 14, 20, 14)
        top = QHBoxLayout()
        title = SubtitleLabel('协作流程')
        top.addWidget(title)
        top.addStretch()
        for label, callback in [('添加根入口', self.add_root), ('添加智能体', self.add_node), ('刷新配置', self.refresh)]:
            button = PushButton(label)
            button.clicked.connect(callback)
            top.addWidget(button)
        column.addLayout(top)
        help_text = hint('这里编辑流程，不展示执行过程。选择节点设置分工；关闭上级会停用整条分支，节点仍保留。运行过程请看“协作网络”。')
        column.addWidget(help_text)
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        self.tree = FlowTree(self.pick)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tree)
        scroll.setStyleSheet('QScrollArea{' + PANEL + '} QScrollArea > QWidget > QWidget{background:white;}')
        split.addWidget(scroll)
        detail = Card(collapsible=False)
        form = QFormLayout()
        detail.body_layout.addLayout(form)
        form.setSpacing(12)
        self.selected_title = BodyLabel('请选择一个根入口或智能体')
        self.selected_title.setWordWrap(True)
        form.addRow(self.selected_title)
        self.enabled = CheckBox('启用此节点')
        self.name, self.role, self.model = LineEdit(), LineEdit(), ComboBox()
        self.provider = ComboBox()
        from .pages_friend import ModelPage
        for value, (label, *_rest) in ModelPage.PROVIDER_TITLES.items():
            self.provider.addItem(label, userData=value)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.model.currentIndexChanged.connect(self.model_changed)
        self.role.setPlaceholderText('这名智能体具体负责什么')
        self.parent, self.effort, self.entry = ComboBox(), ComboBox(), ComboBox()
        self.report = ComboBox()
        for value, label in [('auto', '自动选择'), ('group', '在群内汇报'), ('private', '私聊主账号'), ('mailbox', '智能体邮箱')]:
            self.report.addItem(label, userData=value)
        for value, label in EFFORTS.items():
            self.effort.addItem(label, userData=value)
        for value, label in ENTRIES.items():
            self.entry.addItem(label, userData=value)
        form.addRow(self.enabled)
        for label, widget in [('名称', self.name), ('入口类型', self.entry), ('智能体工具', self.provider), ('上级节点', self.parent),
                              ('职责', self.role), ('模型', self.model), ('思考强度', self.effort), ('汇报通道', self.report)]:
            form.addRow(BodyLabel(label), widget)
        self.fetch_button = PushButton('提取可用模型与思考强度')
        self.fetch_button.clicked.connect(self.fetch_models)
        form.addRow(self.fetch_button)
        self.form = form
        self.availability = hint('')
        form.addRow(self.availability)
        self.save_button = PrimaryPushButton('保存节点')
        self.save_button.setObjectName('flowSave')
        self.save_button.clicked.connect(self.save)
        form.addRow(self.save_button)
        self.connect_button = PushButton('配置 QQ 群与机器人接入')
        self.connect_button.clicked.connect(self.bind_group)
        form.addRow(self.connect_button)
        open_button = PushButton('打开此流程的任务会话')
        open_button.clicked.connect(self.open_room)
        form.addRow(open_button)
        self.editor = detail
        self.editor.setEnabled(False)
        detail_scroll = QScrollArea()
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(detail)
        detail_scroll.setMinimumWidth(285)
        split.addWidget(detail_scroll)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        split.setSizes([650, 320])
        column.addWidget(split, 1)
        self.caption = hint('')
        column.addWidget(self.caption)
        self.refresh()

    def owner(self):
        return str(getattr(self.house, 'current_qq', '') or '')

    def storage(self):
        return self.base or data_dir()

    shown = lambda self: self.refresh()

    def refresh(self):
        owner = self.owner()
        if owner != self.loaded_for:
            self.selection = None
            self.catalogs.clear()
            self.fetch_token = None
            self.fetch_button.setEnabled(True)
            self.editor.setEnabled(False)
            self.selected_title.setText('请选择一个根入口或智能体')
            self.name.clear()
            self.role.clear()
            self.model.clear()
            self.parent.clear()
            self.availability.clear()
        self.loaded_for, self.rooms = owner, {}
        roots = []
        self.tree.set_thread(None, {})
        try:
            profile = load_profile(self.storage().parent / 'friends' / owner / 'friend.json') if owner else {}
        except (OSError, ValueError):
            self.selection = None
            self.editor.setEnabled(False)
            self.name.clear()
            self.role.clear()
            self.model.clear()
            self.caption.setText('当前好友配置不可读，请先检查好友是否仍存在。')
            return
        warnings = []
        if owner:
            for item in agent_group.list_rooms(self.storage(), owner):
                try:
                    room = agent_group.load_room(self.storage(), owner, item['id'])
                    states = agent_group.node_states(room, profile)
                except (OSError, ValueError, KeyError, TypeError):
                    warnings.append('一个流程配置损坏，未加载')
                    continue
                self.rooms[room['id']] = room
                entry = room.get('entry_kind', 'group' if room.get('qq_group', {}).get('enabled') else 'workbench')
                root = {'type': 'task', 'index': (room['id'], ''), 'short': room['title'], 'label': room['title'],
                    'detail': ENTRIES.get(entry, entry), 'brief': ENTRIES.get(entry, entry), 'children': [],
                    'disabled': not room.get('enabled', True)}
                nodes = {}
                for member in room['members']:
                    good, reason = states[member['id']]
                    nodes[member['id']] = {'type': 'agent', 'index': (room['id'], member['id']),
                        'short': member['name'], 'label': member['name'], 'agent_id': member['id'],
                        'provider': member['provider'], 'disabled': not good,
                        'brief': reason or (member.get('model') or '自动选模型'),
                        'detail': member.get('role', ''), 'children': []}
                for member in room['members']:
                    parent = member.get('parent_id', '' if member['id'] == room.get('host_id') else room.get('host_id', ''))
                    (nodes[parent] if parent else root)['children'].append(nodes[member['id']])
                roots.append(root)
        self.tree.set_thread({'tree': roots}, {})
        self.caption.setText((f'当前好友 {owner} · 配置预览，不代表正在执行' if owner else '请先选择好友') +
                             (' · ' + '；'.join(warnings) if warnings else ''))
        if self.selection and self.selection[0] in self.rooms:
            self.pick(self.selection)

    def pick(self, selection):
        from agent_model_selection import provider_name
        self.selection = selection
        room_id, node_id = selection
        room = self.rooms[room_id]
        self.editor.setEnabled(True)
        node = next((m for m in room['members'] if m['id'] == node_id), room)
        self.selected_title.setText(node.get('name') or room['title'])
        self.name.setText(node.get('name') if node_id else room['title'])
        self.enabled.setChecked(node.get('enabled', True))
        self.entry.setCurrentIndex(max(0, self.entry.findData(room.get('entry_kind', 'group' if room.get('qq_group', {}).get('enabled') else 'workbench'))))
        self.parent.clear()
        self.parent.addItem('入口主智能体', userData='')
        for member in room['members']:
            if member['id'] != node_id:
                self.parent.addItem(member['name'], userData=member['id'])
        self.parent.setCurrentIndex(max(0, self.parent.findData(node.get('parent_id', '' if node_id == room.get('host_id') else room.get('host_id', '')))))
        self.role.setText(node.get('role', ''))
        self.role.setCursorPosition(0)
        self.role.setToolTip(node.get('role', ''))
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider_name(node.get('provider', 'codex')))))
        self.provider.blockSignals(False)
        self.populate_models(node.get('model', ''), node.get('effort', ''))
        self.report.setCurrentIndex(max(0, self.report.findData(node.get('report', 'group'))))
        for widget in (self.provider, self.parent, self.role, self.model, self.effort, self.fetch_button):
            self.form.setRowVisible(widget, bool(node_id))
        self.form.setRowVisible(self.entry, not node_id)
        self.form.setRowVisible(self.report, bool(node_id) and node_id != room.get('host_id') and
                                room.get('entry_kind', 'group' if room.get('qq_group', {}).get('enabled') else 'workbench') == 'group')
        self.parent.setEnabled(node_id != room.get('host_id'))
        self.connect_button.setVisible(not node_id and self.entry.currentData() == 'group')
        profile = load_profile(self.storage().parent / 'friends' / self.owner() / 'friend.json')
        state = agent_group.node_states(room, profile).get(node_id, (room.get('enabled', True), ''))
        if state[1]:
            self.availability.setText(state[1])
        elif not node_id:
            self.availability.setText('选择入口类型；具体智能体和模型在子节点中设置。')
        self.tree.selected = selection
        self.tree.update()

    def save(self):
        if not self.selection or self.owner() != self.loaded_for:
            return
        room_id, node_id = self.selection
        try:
            if not self.name.text().strip():
                raise ValueError('节点名称不能为空')
            if node_id:
                if not self.enabled.isChecked():
                    agent_group.update_node(self.storage(), self.owner(), room_id, node_id, enabled=False)
                    self.refresh()
                    return
                from agent_model_selection import provider_name, signature
                provider = self.provider.currentData()
                entry = self.catalog_entry()
                profile = load_profile(self.storage().parent / 'friends' / self.owner() / 'friend.json')
                if not entry or entry['signature'] != signature(profile, provider):
                    raise ValueError('请先提取当前智能体的可用模型和思考强度')
                if self.model.currentData() is None or (self.model.currentData() and self.model.currentData() not in entry['models']):
                    raise ValueError('所选模型不在这次提取的目录中')
                agent_group.update_node(self.storage(), self.owner(), room_id, node_id,
                    name=self.name.text().strip(), role=self.role.text().strip(), provider=provider, model=self.model.currentData() or '',
                    effort=self.effort.currentData(), enabled=self.enabled.isChecked(), parent_id=self.parent.currentData(),
                    report=self.report.currentData(),
                    needs_discovery=False, model_catalog={provider_name(provider): entry})
            else:
                agent_group.update_room(self.storage(), self.owner(), room_id, title=self.name.text().strip(),
                    enabled=self.enabled.isChecked(), entry_kind=self.entry.currentData(), flow_version=1)
            self.refresh()
        except (OSError, ValueError, StopIteration, RuntimeError) as exc:
            toast(self, 'error', str(exc))

    def add_root(self):
        if not self.owner():
            return
        dialog = InputDialog(self.house, '添加根入口', '入口名称：')
        if dialog.exec() and (title := dialog.edit.text().strip()):
            room = agent_group.create_room(self.storage(), self.owner(), title.strip())
            agent_group.update_room(self.storage(), self.owner(), room['id'], enabled=False, flow_version=1)
            self.selection = (room['id'], '')
            self.refresh()

    def catalog_entry(self):
        from agent_model_selection import provider_name
        if not self.selection:
            return None
        room_id = self.selection[0]
        provider = provider_name(self.provider.currentData())
        return self.catalogs.get((room_id, provider)) or (self.rooms[room_id].get('model_catalogs') or {}).get(provider)

    def provider_changed(self):
        if hasattr(self, 'availability'):
            self.populate_models()

    def populate_models(self, selected='', effort=''):
        from agent_model_selection import signature
        self.model.blockSignals(True)
        self.model.clear()
        entry = self.catalog_entry()
        profile = load_profile(self.storage().parent / 'friends' / self.owner() / 'friend.json') if self.owner() else {}
        if entry and entry['signature'] != signature(profile, self.provider.currentData()):
            entry = None
        if entry:
            models = entry['models']
            if set(models) == {''}:
                self.model.addItem('工具默认（不支持模型枚举）', userData='')
            else:
                self.model.addItem('自动选择（仅限已提取的模型）', userData='')
                for model in models:
                    self.model.addItem(model, userData=model)
            self.availability.setText(entry['source'] + '；模型清单不代表额度或调用已验收。')
        else:
            self.model.addItem('尚未提取模型', userData=None)
            self.availability.setText('请提取此智能体实际返回的模型列表；不会使用预置名称冒充可用模型。')
        index = self.model.findData(selected)
        if selected and index < 0:
            self.model.addItem(selected + '（未确认可用）', userData=None)
            index = self.model.count()-1
        self.model.setCurrentIndex(max(0, index))
        self.model.blockSignals(False)
        self.model_changed()
        index = self.effort.findData(effort)
        self.effort.setCurrentIndex(max(0, index))

    def model_changed(self):
        if not hasattr(self, 'effort'):
            return
        selected = self.effort.currentData()
        self.effort.clear()
        self.effort.addItem('自动选择', userData='')
        entry = self.catalog_entry()
        if entry:
            for effort in entry['models'].get(self.model.currentData(), []):
                self.effort.addItem(EFFORTS.get(effort, effort), userData=effort)
        self.effort.setCurrentIndex(max(0, self.effort.findData(selected)))

    def fetch_models(self):
        if not self.selection or not self.selection[1]:
            return
        from agent_model_selection import discover, provider_name, signature
        from uuid import uuid4
        owner, selection, provider = self.owner(), self.selection, self.provider.currentData()
        profile = load_profile(self.storage().parent / 'friends' / owner / 'friend.json')
        directory = self.storage().parent / 'friends' / owner
        token = self.fetch_token = uuid4().hex
        self.fetch_button.setEnabled(False)
        self.availability.setText('正在从当前工具提取模型与思考强度…')
        def done(entry):
            if self.fetch_token != token or self.owner() != owner:
                return
            self.fetch_button.setEnabled(True)
            try:
                current = load_profile(directory / 'friend.json')
            except (OSError, ValueError):
                self.availability.setText('好友配置不可读，未接纳这次提取结果。')
                return
            if signature(current, provider) != entry['signature']:
                self.availability.setText('连接配置已变化，请重新提取。')
                return
            self.catalogs[(selection[0], provider_name(provider))] = entry
            if self.selection == selection and self.provider.currentData() == provider:
                node = next(m for m in self.rooms[selection[0]]['members'] if m['id'] == selection[1])
                self.populate_models(node.get('model', ''), node.get('effort', ''))
        def failed(exc):
            if self.fetch_token == token and self.owner() == owner:
                self.fetch_button.setEnabled(True)
                self.availability.setText('未能提取模型；未使用预置列表。' + (str(exc) if isinstance(exc, PermissionError) else type(exc).__name__))
        run_async(lambda _: discover(profile, provider, directory), done, failed, pool=LONG_POOL)

    def add_node(self):
        if not self.selection:
            return toast(self, 'warning', '先选择一个根入口或上级节点。')
        from .agent_group import MemberDialog
        owner, (room_id, parent) = self.owner(), self.selection
        dialog = MemberDialog(self)
        dialog.model.setReadOnly(True)
        dialog.model.setPlaceholderText('添加后在节点中提取可用模型')
        if dialog.exec() and owner == self.owner():
            member = agent_group.add_member(self.storage(), owner, room_id, dialog.provider.currentData(),
                dialog.name.text(), dialog.role.text(), dialog.model.text(), dialog.template.currentData() or '')
            room = agent_group.load_room(self.storage(), owner, room_id)
            if not room.get('host_id'):
                agent_group.update_room(self.storage(), owner, room_id, host_id=member['id'])
                parent = ''
            else:
                parent = parent or room['host_id']
            agent_group.update_node(self.storage(), owner, room_id, member['id'], parent_id=parent,
                                    enabled=True, needs_discovery=True)
            self.selection = (room_id, member['id'])
            self.refresh()

    def bind_group(self):
        if self.selection:
            from .qq_group_binding import QQGroupBindingDialog
            QQGroupBindingDialog(self.house, self.storage(), self.owner(), self.selection[0], self).exec()
            self.refresh()

    def open_room(self):
        if self.selection:
            page = self.house.pages['collabPage']
            self.house.switchTo(page)
            page.open_room(self.selection[0])
