"""智能体协作网络: which agents are working and what they have exchanged (2026-10-26 user).

Left: every registered agent with its live state. Right: collaborations (one mailbox thread each) and, for the
selected one, a vertical node tree (task -> lead agent -> handoffs to workers -> what came back), each round
node with its title and a five-character state under it; clicking a node opens that message below the tree. Data: collaboration_view.snapshot,
read-only; refreshes itself while shown.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                               QSplitter, QTextEdit, QVBoxLayout, QWidget)

PROVIDER_COLORS = {'codex': '#0F9D78', 'claude': '#D97757', 'antigravity': '#4285F4', 'orchestrator': '#64748B',
                   'api': '#8B5CF6', 'dsh': '#1D4ED8', 'deepseek': '#1D4ED8', 'local': '#0EA5E9', 'mcp': '#A855F7'}
KIND_COLORS = {'handoff': '#2563EB', 'response': '#16A34A', 'finding': '#D97706', 'request': '#7C3AED',
               'review': '#0D9488', 'note': '#94A3B8'}
STATUS_STYLE = {'working': ('工作中', '#15803D', '#DCFCE7'), 'waiting': ('待处理', '#B45309', '#FEF3C7'),
                'idle': ('空闲', '#64748B', '#F1F5F9')}
GOOD, BUSY, BAD = ('#15803D', '#DCFCE7'), ('#B45309', '#FEF3C7'), ('#B91C1C', '#FEE2E2')
BRIEF_STYLE = {'交回': GOOD, '通过': GOOD, '完成': GOOD, '已复': GOOD, '汇总': GOOD,
               '处理': BUSY, '等待': BUSY, '待复': BUSY, '已派': BUSY, '进行': BUSY, '待验': BUSY, '待批': BUSY,
               '等资': BUSY, '待办': BUSY, '暂停': BUSY, '失败': BAD, '未过': BAD, '取消': BAD, '受阻': BAD}


def brief_style(text: str):
    """(text colour, background) for a node's two-character state; a count like 2/2 is good when all passed."""
    if '/' in text:
        done, _, total = text.partition('/')
        return GOOD if done == total else BUSY
    return BRIEF_STYLE.get(text, ('#64748B', '#F1F5F9'))
REFRESH_MS = 4000
PANEL = 'background:white;border:1px solid #E2E8F0;border-radius:10px;'


def color_for(agent_id: str, provider: str = '') -> str:
    name = agent_id.rsplit('/', 1)[-1]
    return PROVIDER_COLORS.get('orchestrator' if name == 'supervisor' else name,
                               PROVIDER_COLORS.get(provider, '#475569'))


def clock(stamp: float) -> str:
    moment = datetime.fromtimestamp(stamp)
    return moment.strftime('%H:%M:%S' if moment.date() == datetime.now().date() else '%m-%d %H:%M')


class TreeView(QWidget):
    """One collaboration as a vertical node tree (2026-10-26 user, after a reference picture): a round node per row,
    a bold title beside it and, under the title, where that step stands in at most five characters
    (collaboration_view.tree's `brief`). Children are indented under their parent, joined by a trunk with rounded
    turns, so many agents grow downward and scroll instead of spreading sideways. No boxes: clicking a node opens
    that message below the tree."""
    ROW, INDENT, MARGIN, TEXT_X, HIT_PAD = 42, 18, 14, 12, 9

    def __init__(self, on_select=None, *, editable=False):
        super().__init__()
        self.root = None
        self.providers = {}
        self.selected = -1
        self.on_select = on_select
        self.editable = editable
        self.rows = []   # (node, depth, parent index or -1) in reading order
        self.boxes = []  # (QRectF around node and text, node), for clicks and tooltips
        self.zoom = 1.0
        self.setMouseTracking(True)
        self.setMinimumHeight(140)

    def set_thread(self, thread, providers):
        self.root, self.providers, self.selected = (thread or {}).get('tree'), providers, -1
        self.rows = []
        if self.root:
            for root in self.root if isinstance(self.root, list) else [self.root]:
                self._flatten(root, 0, -1)
        self.setMinimumHeight(max(140, int((2 * self.MARGIN + len(self.rows) * self.ROW) * self.zoom)))
        self.relayout()
        self.update()

    def _flatten(self, node, depth, parent):
        self.rows.append((node, depth, parent))
        index = len(self.rows) - 1
        for child in node['children']:
            self._flatten(child, depth + 1, index)

    def fonts(self):
        bold, small = QFont(self.font()), QFont(self.font())
        bold.setBold(True)
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))
        return bold, small

    def title(self, node):
        """Four or five characters wide (2026-10-26 user): the step's one-character tag, then who did it."""
        if node['type'] == 'message':
            return node['tag'] + node['short']
        return node['short']

    def centre(self, index):
        """Where row `index`'s round node sits."""
        _, depth, _ = self.rows[index]
        return QPointF(self.MARGIN + 11 + depth * self.INDENT, self.MARGIN + index * self.ROW + 12)

    def radius(self, node):
        return {'task': 8.0, 'agent': 7.0}.get(node['type'], 6.0)

    def relayout(self):
        bold, small = self.fonts()
        wide, narrow = QFontMetricsF(bold), QFontMetricsF(small)
        self.boxes = []
        for index, (node, _, _) in enumerate(self.rows):
            point = self.centre(index)
            text = max(wide.horizontalAdvance(self.title(node)),
                       narrow.horizontalAdvance(node.get('brief') or ''))
            self.boxes.append((QRectF(point.x() - self.HIT_PAD, point.y() - self.HIT_PAD,
                                      self.HIT_PAD + self.TEXT_X + text + 4, self.ROW - 6), node))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    def node_at(self, point):
        point = QPointF(point.x() / self.zoom, point.y() / self.zoom)
        return next((node for box, node in self.boxes if box.contains(point)), None)

    def wheelEvent(self, event):
        if not event.modifiers() & Qt.ControlModifier:
            event.ignore()  # normal wheel movement belongs to the surrounding scroll area
            return
        step = 1.0 if event.angleDelta().y() > 0 else -1.0
        self.zoom = max(0.55, min(2.4, self.zoom + step * 0.12))
        self.setMinimumHeight(max(140, int((2 * self.MARGIN + len(self.rows) * self.ROW) * self.zoom)))
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        node = self.node_at(event.position())
        self.setCursor(Qt.PointingHandCursor if node and node['type'] == 'message' else Qt.ArrowCursor)
        if node:
            self.setToolTip(f"{node.get('kind_label') or ''} {node['label']}\n{node['detail']}"
                            + (f"\n{clock(node['at'])}" if node.get('at') else ''))
        else:
            self.setToolTip('')

    def mousePressEvent(self, event):
        node = self.node_at(event.position())
        if node and (self.editable or node['type'] == 'message'):
            self.select(node['index'])

    def select(self, index):
        self.selected = index
        self.update()
        if self.on_select:
            self.on_select(index)

    def color(self, node):
        if node.get('disabled'):
            return QColor('#94A3B8')
        if node['type'] == 'task':
            return QColor('#2563EB')
        return QColor(PROVIDER_COLORS.get(node.get('provider', ''))
                      or color_for(node['agent_id'], self.providers.get(node['agent_id'], '')))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.scale(self.zoom, self.zoom)
        if not self.root:
            painter.setPen(QColor('#94A3B8'))
            painter.drawText(self.rect(), Qt.AlignCenter, '选择一次协作，查看它的任务树')
            return
        for box, node in self.boxes:  # the clicked node, softly
            if (self.editable or node['type'] == 'message') and node.get('index') == self.selected:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor('#EFF6FF'))
                painter.drawRoundedRect(box, 8, 8)
        self.paint_connections(painter)
        self.paint_nodes(painter)

    def paint_connections(self, painter):
        painter.setPen(QPen(QColor('#94A3B8'), 1.2))
        painter.setBrush(Qt.NoBrush)
        last_child = {}
        for index, (_, _, parent) in enumerate(self.rows):
            if parent >= 0:
                last_child[parent] = index
        for index, (node, _, parent) in enumerate(self.rows):  # trunk down from the parent, a rounded turn in
            if parent < 0:
                continue
            top, here = self.centre(parent), self.centre(index)
            if last_child[parent] == index:
                painter.drawLine(QPointF(top.x(), top.y() + self.radius(self.rows[parent][0]) + 2),
                                 QPointF(top.x(), here.y() - 8))
            path = QPainterPath(QPointF(top.x(), here.y() - 8))
            path.quadTo(top.x(), here.y(), top.x() + 8, here.y())
            path.lineTo(here.x() - self.radius(node) - 2, here.y())
            painter.drawPath(path)
    def paint_nodes(self, painter):
        bold, small = self.fonts()
        for index, (node, _, _) in enumerate(self.rows):
            point, size, color = self.centre(index), self.radius(node), self.color(node)
            painter.setPen(QPen(color, 1.8))
            painter.setBrush(QColor('white'))
            painter.drawEllipse(point, size, size)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(point, size * 0.55, size * 0.55)
            left = point.x() + self.TEXT_X
            painter.setFont(bold)
            head = QRectF(left, point.y() - 11, 2000, 22)
            if node['type'] == 'message':  # the step's tag in its colour, then who did it
                kind = node['tag']
                painter.setPen(QColor(KIND_COLORS.get(node.get('kind'), '#475569')))
                painter.drawText(head, Qt.AlignLeft | Qt.AlignVCenter, kind)
                head.adjust(QFontMetricsF(bold).horizontalAdvance(kind), 0, 0, 0)
            painter.setPen(color if node['type'] == 'task' or node.get('disabled') else QColor('#1E293B'))
            painter.drawText(head, Qt.AlignLeft | Qt.AlignVCenter,
                             self.title(node)[len(node['tag']):] if node['type'] == 'message' else self.title(node))
            painter.setFont(small)
            brief = node.get('brief') or ''
            fg, _ = brief_style(brief)
            line = QRectF(left, point.y() + 10, 2000, 18)
            painter.setPen(QColor(fg))
            painter.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, brief)  # the time is in the tooltip


class AgentCard(QFrame):
    def __init__(self, agent):
        super().__init__()
        self.setObjectName('agentCard')
        text, fg, bg = STATUS_STYLE[agent['status']]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        dot = QLabel('●')
        dot.setStyleSheet(f"color:{PROVIDER_COLORS.get(agent.get('lead_provider', '')) or color_for(agent['agent_id'], agent['provider'])};"
                          'font-size:11px;border:none;')
        name = QLabel(agent['label'])
        name.setStyleSheet('font-weight:600;color:#1E293B;font-size:13px;border:none;')
        pill = QLabel(text)
        pill.setStyleSheet(f'color:{fg};background:{bg};border:none;border-radius:7px;padding:1px 7px;font-size:11px;')
        top.addWidget(dot)
        top.addWidget(name, 1)
        top.addWidget(pill)
        layout.addLayout(top)
        where = agent['task_title'] or agent['project']
        info = QLabel(f"{agent['detail']} · 发 {agent['sent']} · 收 {agent['received']}"
                      + (f" · 失败 {agent['failed']}" if agent['failed'] else ''))
        info.setStyleSheet('color:#64748B;font-size:11px;border:none;')
        task = QLabel(where)
        task.setStyleSheet('color:#94A3B8;font-size:11px;border:none;')
        task.setToolTip(where + (f"（QQ {agent['owner']}）" if agent['owner'] not in ('', 'default') else ''))
        layout.addWidget(info)
        layout.addWidget(task)
        self.setToolTip(agent['agent_id'] + ('\n能力：' + '、'.join(agent['capabilities']) if agent['capabilities'] else ''))
        self.setStyleSheet('QFrame#agentCard{background:white;border:1px solid #E2E8F0;border-radius:8px;}')


class CollaborationPage(QWidget):
    def __init__(self, house, base=None):
        super().__init__(house)
        self.house = house
        self.base = base
        self.data = {'agents': [], 'threads': [], 'summary': {}, 'warnings': []}
        self.loaded_for = None  # 2026-10-26 user: one friend's collaborations, not the whole mailbox
        self.thread_id = None
        self.thread = None
        self.setObjectName('collaborationPage')
        self.setStyleSheet('''QWidget#collaborationPage {background:#F5F8FC;}
          QLabel#collabTitle {font-size:20px;font-weight:700;color:#1c2d48;}
          QLabel#collabMetric {font-size:16px;font-weight:700;color:#1f3151;}
          QFrame#collabMetricCard {background:white;border:1px solid #E2E8F0;border-radius:8px;}
          QListWidget {background:transparent;border:none;outline:none;}
          QListWidget#threadList {background:white;border:1px solid #E2E8F0;border-radius:8px;padding:2px;}
          QListWidget#threadList::item {padding:3px 6px;border-radius:5px;color:#334155;}
          QListWidget#threadList::item:selected {background:#E8F0FE;color:#1E3A8A;}
          QTextEdit {background:white;border:1px solid #E2E8F0;border-radius:8px;color:#243656;padding:4px;}''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 10)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel('智能体协作网络')
        title.setObjectName('collabTitle')
        subtitle = QLabel('谁在工作、谁把什么交给了谁、结果有没有交回来 · 每 4 秒自动刷新')
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet('font-size:12px;color:#8290a6;padding-top:6px;')
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(subtitle)
        header.addStretch(1)
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.refresh)
        refresh.setStyleSheet('padding:6px 14px;border:1px solid #CBD5E1;border-radius:7px;background:white;color:#334155;')
        self.web_button = manage = QPushButton('编辑协作流程')
        manage.setStyleSheet('padding:6px 14px;border-radius:7px;background:#2563EB;color:white;font-weight:600;')
        manage.clicked.connect(lambda: house.switchTo(house.pages.get('cooperationFlowPage', house.pages['modelPage'])))
        header.addWidget(refresh)
        header.addWidget(manage)
        layout.addLayout(header)
        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.metrics = {}
        for key, label in (('agents', '参与智能体'), ('active', '正在工作'), ('threads', '协作记录'),
                           ('messages_24h', '24 小时内记录')):
            card = QFrame()
            card.setObjectName('collabMetricCard')
            box = QHBoxLayout(card)
            box.setContentsMargins(12, 6, 12, 6)
            name = QLabel(label)
            name.setStyleSheet('font-size:12px;color:#7a899f;border:none;')
            value = QLabel('—')
            value.setObjectName('collabMetric')
            value.setStyleSheet('border:none;')
            box.addWidget(name)
            box.addStretch(1)
            box.addWidget(value)
            self.metrics[key] = value
            metrics.addWidget(card)
        layout.addLayout(metrics)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(4)
        left_box.addWidget(self.section('智能体'))
        self.agent_list = QListWidget()
        self.agent_list.setSpacing(3)
        self.agent_list.setWordWrap(True)
        self.agent_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.agent_list.itemClicked.connect(self.pick_agent)
        left_box.addWidget(self.agent_list, 1)
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(6)
        right_box.addWidget(self.section('协作记录'))
        self.thread_list = QListWidget()
        self.thread_list.setObjectName('threadList')
        self.thread_list.currentItemChanged.connect(lambda item, _old: self.show_thread(item.data(Qt.UserRole) if item else None))
        right_box.addWidget(self.thread_list)
        self.tree = TreeView(self.show_message)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.tree)
        scroll.setStyleSheet('QScrollArea{' + PANEL + '} QScrollArea > QWidget > QWidget{background:white;}')
        right_box.addWidget(scroll, 1)
        self.legend = QLabel('派 交接　回 交回　汇 汇总　问 请求　审 审查　|　Cdx Codex · Cld Claude · Agy Antigravity　|　圆点颜色：' + '　'.join(
            f"<span style='color:{PROVIDER_COLORS[k]}'>●</span> {v}" for k, v in (
                ('codex', 'Codex'), ('claude', 'Claude'), ('antigravity', 'Antigravity'), ('api', 'API'),
                ('orchestrator', '编排'))) + '　|　小字 = 状态　|　点节点看详情')
        self.legend.setStyleSheet('color:#94A3B8;font-size:11px;')
        self.legend.setWordWrap(True)
        right_box.addWidget(self.legend)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(110)
        self.detail.hide()  # appears when a node is clicked
        right_box.addWidget(self.detail)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 900])
        layout.addWidget(split, 1)
        self.caption = QLabel('')
        self.caption.setStyleSheet('color:#A0AEC0;font-size:11px;')
        layout.addWidget(self.caption)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    @staticmethod
    def section(text):
        label = QLabel(text)
        label.setStyleSheet('font-weight:600;color:#475569;font-size:13px;padding:0 2px;')
        return label

    def shown(self):
        self.refresh()
        self.timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        import collaboration_view
        from house_settings import data_dir
        owner = getattr(self.house, 'current_qq', None)
        if owner != self.loaded_for:
            # Clear before querying: a failed read after switching must not retain another friend's tree.
            self.tree.set_thread(None, {})
            self.detail.clear()
            self.detail.hide()
            self.agent_list.clear()
            self.thread_list.clear()
            self.thread_id = self.thread = None
            self.data = {'agents': [], 'threads': [], 'summary': {}, 'warnings': []}
            for value in self.metrics.values():
                value.setText('—')
        if not owner:
            # 2026-10-26 user: never fall back to the whole mailbox; with no friend there is nothing to show.
            self.loaded_for = None
            self.data = {'agents': [], 'threads': [], 'summary': {}, 'warnings': []}
            self.thread_id = self.thread = None
            self.tree.set_thread(None, {})
            self.detail.clear()
            self.detail.hide()
            self.agent_list.clear()
            self.thread_list.clear()
            for value in self.metrics.values():
                value.setText('—')
            self.caption.setText('尚未选择好友 · 选择好友后只显示该好友自己的智能体协作')
            return
        try:
            data = collaboration_view.snapshot(self.base or data_dir(), owner=str(owner))
        except (OSError, ValueError, RuntimeError) as exc:
            self.caption.setText('协作记录读取失败：' + type(exc).__name__)
            return
        if data == self.data and self.loaded_for == owner and self.agent_list.count():
            return  # nothing new: keep the scroll position, selection and hover
        self.loaded_for = owner
        self.data = data
        summary = self.data['summary']
        for key, value in self.metrics.items():
            value.setText(str(summary.get(key, '—')))
        self.agent_list.clear()
        for agent in self.data['agents']:
            card = AgentCard(agent)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, agent['agent_id'])
            item.setSizeHint(card.sizeHint())
            self.agent_list.addItem(item)
            self.agent_list.setItemWidget(item, card)
        if not self.data['agents']:
            self.agent_list.addItem('还没有智能体协作过。开启多智能体协作的任务，或用智能体邮箱互发消息后，会出现在这里。')
        self.thread_list.blockSignals(True)
        self.thread_list.clear()
        current = None
        for thread in self.data['threads']:
            people = '、'.join(p['label'] for p in thread['participants'])
            state = thread.get('status_label') or collaboration_view.TASK_STATES.get(thread['task_status'], '进行中' if thread['task_status'] else '')
            item = QListWidgetItem(f"{clock(thread['last'])}　{thread['title'][:40]}　·　{people}　·　{thread['count']} 条"
                                   + (f'　·　{state}' if state else ''))
            item.setData(Qt.UserRole, thread['thread_id'])
            self.thread_list.addItem(item)
            if thread['thread_id'] == self.thread_id:
                current = item
        if current is None and self.thread_list.count():
            current = self.thread_list.item(0)
        if current is not None:
            self.thread_list.setCurrentItem(current)
        self.thread_list.blockSignals(False)
        rows = min(4, max(1, self.thread_list.count()))
        self.thread_list.setFixedHeight(rows * (self.thread_list.sizeHintForRow(0) if self.thread_list.count() else 24) + 10)
        self.thread_list.setVisible(bool(self.data['threads']))
        self.show_thread(current.data(Qt.UserRole) if current else None, keep=True)
        self.caption.setText(f'当前好友 {owner} · 只读显示 · 最近 30 次协作 · 24 小时只影响统计，不清除记录' + (' | ' + '；'.join(self.data['warnings'][:2])
                                                     if self.data['warnings'] else ''))

    def show_thread(self, thread_id, keep=False):
        changed = thread_id != self.thread_id
        self.thread_id = thread_id
        thread = next((t for t in self.data['threads'] if t['thread_id'] == thread_id), None)
        self.thread = thread
        providers = {a['agent_id']: a['provider'] for a in self.data['agents']}
        selected = self.tree.selected
        self.tree.set_thread(thread, providers)
        if keep and not changed and thread and 0 <= selected < len(thread['messages']):
            self.tree.selected = selected
        elif changed or not keep:
            self.detail.clear()
            self.detail.hide()

    def show_message(self, index):
        if not self.thread or not 0 <= index < len(self.thread['messages']):
            return
        message = self.thread['messages'][index]
        to = '、'.join(f"{r['label']}（{r['state_label']}）" for r in message['to']) or '—'
        lines = [f"{clock(message['at'])}  {message['from_label']} → {to}", f"{message['kind_label']}：{message['subject']}",
                 '', message.get('body', message['excerpt'])]
        if message.get('receipt'):
            lines += ['', 'QQ 消息回执：' + message['receipt']]
        if message['artifacts']:
            lines += ['', '附带产物：' + '、'.join(map(str, message['artifacts']))]
        self.detail.setPlainText('\n'.join(lines))
        self.detail.setToolTip('智能体的输出，只作参考')
        self.detail.show()

    def pick_agent(self, item):
        """Clicking an agent jumps to its most recent collaboration."""
        agent_id = item.data(Qt.UserRole)
        for index, thread in enumerate(self.data['threads']):
            if any(p['agent_id'] == agent_id for p in thread['participants']):
                self.thread_list.setCurrentRow(index)
                return
