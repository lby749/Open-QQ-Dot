"""Desktop workspaces built around existing editors.

Qt legacy configuration/editor widgets remain intact inside secondary tabs. The
operational summary intentionally displays only read-only, redacted data.
"""
from __future__ import annotations

import webbrowser
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTabWidget, QFrame, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea)
from PySide6.QtCore import QEvent, Qt
from .common import SectionSwitcher
from .autonomy import CATEGORY, STATUS
from ui_locale import tr

from ui_dashboard_data import system_snapshot, usage_snapshot
from house_settings import data_dir


class WorkspaceTabs(QWidget):
    """Top-level navigation wrapper preserving original pages and their save methods."""
    def __init__(self, house, key, children):
        super().__init__(house)
        self.house = house
        self.setObjectName(key)
        self.loaded_for = None
        column = QVBoxLayout(self)
        column.setContentsMargins(18, 6, 18, 18)
        column.setSpacing(10)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().hide()
        self.seg = SectionSwitcher(self)
        self._routes = {}
        self.tabs.setStyleSheet('QTabWidget::pane {border:none;background:transparent;}')
        for index, (label, widget) in enumerate(children):
            self.tabs.addTab(widget, tr(label))
            route = f'{key}_{index}'
            self._routes[route] = index
            self.seg.addItem(route, tr(label))
        self.seg.currentItemChanged.connect(lambda route: self.tabs.setCurrentIndex(self._routes[route]))
        scroll = self.switcher_scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedHeight(self.seg.height()+8)
        scroll.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        scroll.setStyleSheet('QScrollArea {background:transparent;border:none;}')
        scroll.viewport().setAutoFillBackground(False)
        self.seg.setMinimumWidth(self.seg.maximumWidth())
        scroll.setWidget(self.seg)
        scroll.viewport().installEventFilter(self)
        column.addWidget(scroll)
        scroll.setVisible(len(children) > 1)
        column.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self.shown)
        if children:
            self.seg.setCurrentItem(f'{key}_0')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_switcher()

    def eventFilter(self, watched, event):
        if hasattr(self, 'switcher_scroll') and watched is self.switcher_scroll.viewport() and event.type() == QEvent.Resize:
            self._fit_switcher()
        return super().eventFilter(watched, event)

    def _fit_switcher(self):
        if hasattr(self, 'switcher_scroll'):
            overflow = self.seg.minimumWidth() > self.switcher_scroll.viewport().width()
            height = self.seg.height()+8
            if overflow:
                height += self.switcher_scroll.horizontalScrollBar().sizeHint().height()
            if self.switcher_scroll.height() != height:
                self.switcher_scroll.setFixedHeight(height)

    def select_widget(self, widget):
        idx = self.tabs.indexOf(widget)
        if idx >= 0:
            if self.tabs.currentIndex() == idx:
                self.shown()
            else:
                self.tabs.setCurrentIndex(idx)

    def shown(self, *_):
        route = next((key for key, index in self._routes.items() if index == self.tabs.currentIndex()), None)
        if route is not None:
            self.seg.setCurrentItem(route)
        child = self.tabs.currentWidget()
        sidebar = getattr(self.house, '_sidebar_routes', {})
        if child in sidebar:
            self.house.navigationInterface.setCurrentItem(sidebar[child])
        if child is not None and hasattr(child, 'shown'):
            child.shown()

    def on_status(self):
        child = self.tabs.currentWidget()
        if child is not None and hasattr(child, 'on_status'):
            child.on_status()


class OperationalPage(QWidget):
    """Genuine DB snapshots; deliberate unknown instead of fabricated metrics."""
    TITLES = {
        'home': ('你的工作台', '任务、审批与协作网络都在这里。'),
        'tasks': ('任务执行概览', '查看真实任务状态；操作仍通过受控的任务界面执行。'),
        'agents': ('智能体协作网络', '智能体配置、控制台和任务共用同一套接入；此页显示真实协作记录。'),
        'insights': ('系统洞察', '模型服务记录的输入用量与缓存读取量，不混入离线估算。'),
    }

    def __init__(self, house, kind):
        super().__init__(house)
        self.house = house
        self.kind = kind
        self.setObjectName('operations_' + kind)
        self.setStyleSheet('''QWidget#operations_%s {background:#EFF6FF;}
          QLabel#operationTitle {font-size:27px;font-weight:750;color:#1c2d48;}
          QLabel#operationSubtitle {font-size:13px;color:#8290a6;}
          QLabel#metric {font-size:25px;font-weight:bold;color:#1f3151;}
          QFrame#metricCard {background:white;border:1px solid #DDE8F7;border-radius:15px;}
          QTableWidget {background:white;border:1px solid #DDE8F7;border-radius:12px;
                alternate-background-color:#F8FAFD;selection-background-color:#EAF3FF;
                selection-color:#243656;gridline-color:#edf1f7;}'''.replace('%s',kind))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(29, 24, 29, 23)
        layout.setSpacing(17)
        header = QHBoxLayout()
        head_text = QVBoxLayout()
        title = QLabel(tr(self.TITLES[kind][0]));title.setObjectName('operationTitle')
        sub = QLabel(tr(self.TITLES[kind][1]));sub.setObjectName('operationSubtitle')
        head_text.addWidget(title);head_text.addWidget(sub)
        header.addLayout(head_text); header.addStretch(1)
        self.refresh_button = QPushButton(tr('刷新状态'))
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh_button.setStyleSheet('padding:10px 18px;border:1px solid #C9DCF7;border-radius:9px;background:white;color:#3253a5;font-weight:bold;')
        self.web_button = QPushButton(tr('打开 Web 工作台 ↗'))
        self.web_button.setStyleSheet('padding:10px 18px;border-radius:9px;background:#2563EB;color:white;font-weight:bold;')
        if kind == 'agents':
            self.web_button.setText('配置与管理智能体')
            self.web_button.clicked.connect(lambda: house.switchTo(house.pages['modelPage']))
        else:
            self.web_button.clicked.connect(self.open_web)
        header.addWidget(self.refresh_button);header.addWidget(self.web_button)
        layout.addLayout(header)
        stats = QHBoxLayout()
        self.metrics = []
        for _ in range(4):
            card = QFrame();card.setObjectName('metricCard')
            v = QVBoxLayout(card);v.setContentsMargins(19, 15, 20, 15)
            label = QLabel();label.setStyleSheet('font-size:12px;color:#7a899f;')
            value = QLabel('—');value.setObjectName('metric')
            v.addWidget(label);v.addWidget(value)
            self.metrics.append((label, value))
            stats.addWidget(card)
        layout.addLayout(stats)
        self.caption = QLabel('');self.caption.setStyleSheet('color:#8393a8;padding:5px;')
        layout.addWidget(self.caption)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.refresh()

    def shown(self):
        self.refresh()

    def open_web(self):
        from launcher_codex import PORT, start_server_if_needed
        from .common import run_async, toast
        run_async(lambda _: start_server_if_needed(),
                  lambda _: webbrowser.open(f'http://127.0.0.1:{PORT}/#' + self.kind),
                  lambda exc: toast(self.house, 'warning', str(exc), tr('Connection Notice')))

    def refresh(self):
        try:
            # 2026-10-26 user: every counter on a friend's page is that friend's own rows.
            owner = self.house.current_qq
            report = system_snapshot(data_dir(), owner)
            def cnt(d, name):
                return '—' if d is None else str(sum(v for k,v in d.items() if any(n in k.lower() for n in name)))
            if self.kind == 'home':
                metrics = [('运行任务',cnt(report['tasks'],('running','executing'))),
                           ('待审批',cnt(report['approvals'],('pending',))),
                           ('在线 Agent 会话',str(report['sessions']) if report['sessions'] is not None else '—'),
                           ('待投递消息',cnt(report['mail'],('pending',)))]
                columns = ['任务','类型','状态','负责人'];rows=[(r['title'],r['category'],r['status'],r['owner_id']) for r in report['task_items'][:35]]
            elif self.kind == 'tasks':
                metrics = [('全部任务',str(sum(report['tasks'].values())) if report['tasks'] is not None else '—'),
                           ('运行中',cnt(report['tasks'],('running','executing'))),
                           ('已完成',cnt(report['tasks'],('completed',))),
                           ('失败',cnt(report['tasks'],('failed',)))]
                columns = ['任务','类型','状态','负责人']; rows=[(r['title'],r['category'],r['status'],r['owner_id']) for r in report['task_items']]
            elif self.kind == 'agents':
                metrics = [('已注册 Agent',str(report['agents']) if report['agents'] is not None else '—'),
                           ('在线会话',str(report['sessions']) if report['sessions'] is not None else '—'),
                           ('待处理消息',cnt(report['mail'],('pending','delivered'))),
                           ('失败投递',cnt(report['mail'],('failed','refused')))]
                columns = ['智能体','来源','项目','有效会话'];rows=[(r['name'],r['provider'],r['project_id'],r['sessions']) for r in report['agent_items']]
            else:
                u=usage_snapshot(data_dir(), owner)
                metrics = [('真实用量调用',str(u['calls']) if u['available'] else '—'),
                           ('输入 Token',str(u['input_tokens']) if u['available'] else '—'),
                           ('缓存读取 Token',str(u['cached_input_tokens']) if u['available'] else '—'),
                           ('缓存占比',f"{u['cached_input_tokens']/u['input_tokens']:.1%}" if u['input_tokens'] else '—')]
                columns=['模型','调用次数','输入 Token','缓存读取 Token'];rows=[(r['model'],r['calls'],r['input_tokens'],r['cached_input_tokens']) for r in u['by_model']]
            for (label,value),(title,amount) in zip(self.metrics,metrics):
                label.setText(tr(title));value.setText(str(amount))
            self.table.setColumnCount(len(columns)); self.table.setHorizontalHeaderLabels([tr(c) for c in columns]);self.table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                for j,val in enumerate(row):
                    raw = str(val or '—')
                    if columns[j] in ('状态', '运行状态', 'Status'):
                        display = STATUS.get(raw.upper(), tr(raw))
                    elif columns[j] == '类型':
                        display = CATEGORY.get(raw, raw)
                    else:
                        display = tr(raw) if columns[j] in ('接入方式', 'Transport') else raw
                    item=QTableWidgetItem(display);item.setToolTip(str(val or ''))
                    self.table.setItem(i,j,item)
            scope = f'当前好友 {owner} · ' if owner else '尚未选择好友 · '
            self.caption.setText(scope + tr('最近真实记录 · 最多显示 80 条 · “—” 表示尚无可靠数据') +
                                 (' | '+', '.join(report['warnings'][:2]) if report['warnings'] else ''))
        except (OSError, ValueError, RuntimeError) as exc:
            self.caption.setText(tr('数据读取失败：') + type(exc).__name__)
