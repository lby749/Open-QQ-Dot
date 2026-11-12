"""Main window of the Qt panel: navigation, friend switching, bridge status polling, tray and exit."""
from ui_locale import tr

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QBoxLayout, QHBoxLayout, QLineEdit, QMenu, QPlainTextEdit, QSystemTrayIcon,
                               QTextEdit, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, FluentIcon, FluentTranslator, FluentWindow,
                            NavigationItemPosition, PushButton, SubtitleLabel, Theme, setTheme, setThemeColor)

from house_status import REGISTRY, ROOT, friend_mode_values, invoke_bridge, load_profile, write_profile

from .common import ACCENT, GREEN, MUTED, confirm, init_ui_thread, quiet, run_async, set_window, toast
from .pages_friend import ModelPage, OverviewPage, StatusPage
from .pages_agent import AgentProfilePage, KnowledgeMemoryPage, AuditLogPage
from .pages_more import ProactivePage
from .agent_console_widget import AgentConsolePage
from .workspaces import WorkspaceTabs, OperationalPage
from .collaboration import CollaborationPage
from .cooperation_flow import CooperationFlowPage
from .agent_group import AgentGroupPage
from .autonomy import AutonomyPage
from .task_center import TaskCenterPage
from .workflows import WorkflowPage
from .capsules import CapsulePage
from .revert import RevertPage
from .migration import MigrationPage
from .tutorial import build_tutorial_pages
from .vnext_pages import AccountsVNextPage, DevicesVNextPage, HealthVNextPage, ExperienceVNextPage
from .dotlink import DotLinkPage
from .qq_connection import QQConnectionPage
from .development import DevelopmentPage
from .updates import UpdatePage
from . import file_drop
import app_backup
from version import TITLE, VERSION
# the launcher finds a running panel by this exact title


def house_icon():
    for candidate in (
        ROOT / 'assets' / 'app-icon.ico',
        ROOT / 'friend-house.ico',
        ROOT / 'Open QQ Dot.ico',
        ROOT / 'assets' / 'app-icon.png',
    ):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    return QIcon()


class HouseWindow(FluentWindow):
    # Original editors remain accessible through focused workspaces.
    WORKSPACE_NAMES = ('tutorial', 'home', 'friends', 'connection', 'tasks', 'capsules', 'agents', 'qq', 'insights', 'settings', 'development')

    def __init__(self, startup_error='', web=None, startup=None, open_paths=None):
        super().__init__()
        self.web = web  # house_web.start() addresses; each friend's status page creates its own QR token
        self.open_paths = list(open_paths or [])
        set_window(self)
        self.setWindowTitle(TITLE)
        self.setStyleSheet('HouseWindow { background: #EFF6FF; }')
        self.setWindowIcon(house_icon())
        try:
            import ctypes
            ico = ROOT / 'assets' / 'app-icon.ico'
            if not ico.is_file():
                ico = ROOT / 'friend-house.ico'
            if ico.is_file():
                user32 = ctypes.windll.user32
                h_big = user32.LoadImageW(None, str(ico), 1, 32, 32, 0x0010)
                h_small = user32.LoadImageW(None, str(ico), 1, 16, 16, 0x0010)
                hwnd = int(self.winId())
                if h_big:
                    user32.SendMessageW(hwnd, 0x0080, 1, h_big)
                if h_small:
                    user32.SendMessageW(hwnd, 0x0080, 0, h_small)
        except Exception:
            pass
        self.resize(1460, 900)
        self.setMinimumSize(1050, 700)
        self.records = {}
        self.current_qq = None
        self.status_text = tr('Reading status...')
        self.bridge_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='house-bridge')
        self.bridge_busy, self.pending, self.closing, self.exiting = False, None, False, False
        self.reload_friends()
        self.current_qq = next(iter(self.records), None)

        self.navigationInterface.setReturnButtonVisible(False)
        self.navigationInterface.setExpandWidth(180)
        self.navigationInterface.setMinimumExpandWidth(0)  # keep descriptive labels visible at supported sizes
        self.navigationInterface.expand(useAni=False)
        self.build_header()
        # Original functional editors remain the source of truth; only navigation changes.
        self.overview = OverviewPage(self)
        self.pages = {
            'workbenchPage': AgentConsolePage(self),
            'taskCenterPage': TaskCenterPage(self),
            'workflowPage': WorkflowPage(self),
            'capsulePage': CapsulePage(self),
            'revertPage': RevertPage(self),
            'migrationPage': MigrationPage(self),
            'dotlinkPage': DotLinkPage(self),
            'autonomyPage': AutonomyPage(self, 'goal'),
            'researchAgentPage': AutonomyPage(self, 'research'),
            'browserAgentPage': AutonomyPage(self, 'browser'),
            'statusPage': StatusPage(self),
            'profilePage': AgentProfilePage(self),
            'memoryPage': KnowledgeMemoryPage(self),
            'modelPage': ModelPage(self),
            'proactivePage': ProactivePage(self),
            'historyPage': AuditLogPage(self),
        }
        self.pages.update(build_tutorial_pages(self))
        self.pages['qqConnectionPage'] = QQConnectionPage(self, self.pages['dotlinkPage'])
        self.pages['devicesPage'] = DevicesVNextPage(self)
        self.pages['healthPreviewPage'] = HealthVNextPage(self)
        self.pages['developmentPage'] = DevelopmentPage(self)
        self.pages['softwareUpdatePage'] = UpdatePage(self)
        self.workspaces = {
            'tutorial': WorkspaceTabs(self, 'workspaceTutorial', [
                ('快速开始', self.pages['tutorialQuickPage']),
                ('连接 QQ / 手机', self.pages['tutorialConnectPage']),
                ('Tailscale 网络', self.pages['tutorialTailscalePage']),
                ('模型与智能体', self.pages['tutorialModelPage']),
                ('迁移与项目', self.pages['tutorialMigrationPage']),
                ('工具 / 故障', self.pages['tutorialToolsPage']),
                ('免责声明与账号风险', self.pages['tutorialDisclaimerPage'])]),
            'home': WorkspaceTabs(self, 'workspaceHome', [
                ('智能体控制台', self.pages['workbenchPage'])]),
            'friends': WorkspaceTabs(self, 'workspaceFriends', [
                ('好友总览', self.overview), ('系统概况', OperationalPage(self, 'home'))]),
            'connection': WorkspaceTabs(self, 'workspaceConnection', [
                ('连接与会话', self.pages['statusPage'])]),
            'tasks': WorkspaceTabs(self, 'workspaceTasks', [
                ('全部任务', self.pages['taskCenterPage']),
                ('可学习工作流', self.pages['workflowPage']),
                ('自主规划与执行', self.pages['autonomyPage']),
                ('自主研究', self.pages['researchAgentPage']),
                ('浏览器自动化', self.pages['browserAgentPage']),
                ('任务状态', OperationalPage(self, 'tasks')),
                ('长期任务与计划', self.pages['proactivePage'])]),
            'capsules': WorkspaceTabs(self, 'workspaceCapsules', [
                ('任务胶囊', self.pages['capsulePage'])]),
            'agents': WorkspaceTabs(self, 'workspaceAgents', [
                ('智能体与工具', self.pages['modelPage']),
                ('协作网络', CollaborationPage(self)),
                ('协作流程', CooperationFlowPage(self)),
                ('群聊协作', AgentGroupPage(self))]),
            'qq': WorkspaceTabs(self, 'workspaceQQ', [
                ('QQ 连接', self.pages['qqConnectionPage']),
                ('聊天与审计', self.pages['historyPage']),
                ('设备配对', self.pages['devicesPage'])]),
            'insights': WorkspaceTabs(self, 'workspaceInsights', [
                ('回溯中心', self.pages['revertPage']),
                ('用量与缓存', OperationalPage(self, 'insights')),
                ('经验库', ExperienceVNextPage(self))]),
            'development': WorkspaceTabs(self, 'workspaceDevelopment', [
                ('待开发中功能', self.pages['developmentPage']),
                ('健康数据试用', self.pages['healthPreviewPage']),
                ('账号与路由', AccountsVNextPage(self))]),
            'settings': WorkspaceTabs(self, 'workspaceSettings', [
                ('人设与提示词', self.pages['profilePage']),
                ('长期记忆', self.pages['memoryPage']),
                ('迁移助手', self.pages['migrationPage']),
                ('版本与更新', self.pages['softwareUpdatePage'])]),
        }
        # Legacy links keep the same live widgets; both top and left navigation share one workspace.
        self.workspaces['collab'] = self.workspaces['agents']
        self.pages['cooperationFlowPage'] = self.workspaces['agents'].tabs.widget(2)
        self.pages['accountsPreviewPage'] = self.workspaces['development'].tabs.widget(2)
        self._workspaces_for_page = {}
        for container in self.workspaces.values():
            for index in range(container.tabs.count()):
                self._workspaces_for_page[container.tabs.widget(index)] = container
        self._sidebar_routes = {}
        direct = {
            'agents': [('协作网络', self.workspaces['agents'].tabs.widget(1)),
                       ('协作流程', self.pages['cooperationFlowPage']),
                       ('群聊协作', self.workspaces['agents'].tabs.widget(3))],
            'tasks': [('自主规划与执行', self.pages['autonomyPage']),
                      ('长期任务与计划', self.pages['proactivePage']),
                      ('自主研究', self.pages['researchAgentPage']),
                      ('浏览器自动化', self.pages['browserAgentPage']),
                      ('可学习工作流', self.pages['workflowPage'])],
            'qq': [('设备配对', self.pages['devicesPage']),
                   ('聊天与审计', self.pages['historyPage'])],
            'insights': [('回溯中心', self.pages['revertPage']),
                         ('经验库', self.workspaces['insights'].tabs.widget(2))],
            'settings': [('长期记忆', self.pages['memoryPage']),
                         ('迁移与导入', self.pages['migrationPage']),
                         ('版本与更新', self.pages['softwareUpdatePage'])],
        }
        for key, icon, title in (
            ('tutorial', FluentIcon.HELP, '使用教程'),
            ('home', FluentIcon.HOME, '主页'),
            ('friends', FluentIcon.MESSAGE, '好友总览'),
            ('settings', FluentIcon.APPLICATION, '设置与工具'),
            ('connection', FluentIcon.CONNECT, '连接与会话'),
            ('agents', FluentIcon.ROBOT, '智能体与工具'),
            ('tasks', FluentIcon.DEVELOPER_TOOLS, '任务中心'),
            ('capsules', FluentIcon.APPLICATION, '任务胶囊'),
            ('collab', FluentIcon.PEOPLE, '智能体协作'),
            ('qq', FluentIcon.MESSAGE, 'QQ 与会话'),
            ('insights', FluentIcon.HISTORY, '系统洞察'),
            ('development', FluentIcon.DEVELOPER_TOOLS, '待开发中功能'),
        ):
            if key == 'collab':
                continue
            position = NavigationItemPosition.BOTTOM if key in ('tutorial', 'development') else NavigationItemPosition.SCROLL
            self.addSubInterface(self.workspaces[key], icon, tr(title), position=position)
            self.navigationInterface.widget(self.workspaces[key].objectName()).setAccessibleName(tr(title))
            for index, (label, page) in enumerate(direct.get(key, [])):
                route = f'direct_{key}_{index}'
                self._sidebar_routes[page] = route
                item = self.navigationInterface.addItem(route, icon, label,
                    onClick=lambda checked=False, page=page: self.switchTo(page), tooltip=label,
                    position=position, parentRouteKey=self.workspaces[key].objectName())
                item.setAccessibleName(label)
            self.navigationInterface.widget(self.workspaces[key].objectName()).setExpanded(False)
        self.pages['homePage'] = self.workspaces['home']
        self.pages['friendsPage'] = self.workspaces['friends']
        self.pages['tasksPage'] = self.workspaces['tasks']
        self.pages['capsulesPage'] = self.workspaces['capsules']
        self.pages['agentsPage'] = self.workspaces['agents']
        self.pages['collabPage'] = self.workspaces['agents'].tabs.widget(3)
        self.pages['qqPage'] = self.workspaces['qq']
        self.pages['insightsPage'] = self.workspaces['insights']
        self.pages['settingsPage'] = self.workspaces['settings']

        # Wire legacy aliases for seamless internal access
        if 'profilePage' in self.pages:
            p = self.pages['profilePage']
            self.pages['templatePage'] = p.template_page
            self.pages['stylePage'] = p.style_page
            self.pages['basicPage'] = p.basic_page
            self.pages['stickerPage'] = p.sticker_page
        if 'memoryPage' in self.pages:
            m = self.pages['memoryPage']
            self.pages['topicPage'] = m.topic_page
        if 'historyPage' in self.pages:
            h = self.pages['historyPage']
            self.pages['contextPage'] = h.context_page
            self.pages['emotionPage'] = h.emotion_page

        self.stackedWidget.currentChanged.connect(self.page_changed)
        self.build_tray()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(lambda: self.request('status') if not self.bridge_busy else None)
        self.status_timer.start(5000)
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.auto_reconnect)
        self.reconnect_timer.start(30000)
        for name, keys, callback in (("undo", QKeySequence.Undo, self.undo_last),
                                     ("redo", QKeySequence.Redo, self.redo_last)):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(callback)
            setattr(self, "_shortcut_" + name, shortcut)
        if startup is not None:  # 2026-10-12 user: DotLink and the bridge start after the window shows
            self.bridge_busy = True
            self.bridge_label.setText(tr('Backend Bridge: Starting QQ link and backend...'))
            run_async(lambda _: startup(), self.startup_done, lambda exc: self.startup_done(str(exc)),
                      pool=self.bridge_pool)
        else:
            self.request('status')
        if startup_error:
            QTimer.singleShot(600, lambda: toast(self, 'warning', startup_error, tr('Startup Notice')))
        if self.current_qq:
            self.show_friend(self.current_qq)
        self.switchTo(self.workspaces['home'])
        self._auto_connect_done = False
        self.setAcceptDrops(True)
        self.open_timer = QTimer(self)
        self.open_timer.setInterval(1200)
        self.open_timer.timeout.connect(self.poll_open_requests)
        self.open_timer.start()
        QTimer.singleShot(1800, self.open_initial_paths)
        if startup is None:
            QTimer.singleShot(1000, self.auto_open_dotlink)
        QTimer.singleShot(1400, self.maybe_show_first_run)
        QTimer.singleShot(12000, self.pages['softwareUpdatePage'].check_updates)

    def maybe_show_first_run(self):
        """Show the short guide once, on a house that has never seen it."""
        if not self.isVisible():
            return
        try:
            from .first_run import FirstRunGuide, should_show
            if not should_show():
                return
            guide = FirstRunGuide(self, on_tutorial=lambda: self.switchTo(self.workspaces['tutorial']))
            guide.exec()
        except Exception:
            pass  # a first-run hint must never stop the window from opening

    # ---------- layout ----------

    def build_header(self):
        """Current friend and bridge status above every page."""
        self.widgetLayout.removeWidget(self.stackedWidget)
        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        header = self.header = QWidget()
        line = self.header_layout = QBoxLayout(QBoxLayout.TopToBottom, header)
        line.setContentsMargins(28, 4, 28, 8)
        line.setSpacing(8)
        friend_line = self.friend_header = QHBoxLayout()
        connection_line = self.connection_header = QHBoxLayout()
        self.friend_title = SubtitleLabel('智能体控制台')
        self.version_label = CaptionLabel(f'v{VERSION}')
        self.version_label.setStyleSheet('color: #64748B; font-weight: bold;')
        self.friend_qq = CaptionLabel('')
        self.friend_box = ComboBox()
        self.friend_box.setMinimumWidth(220)
        self.friend_box.currentIndexChanged.connect(self.friend_chosen)
        self.bridge_label = BodyLabel(tr('Backend Bridge: ') + self.status_text)
        self.connect_btn = PushButton(tr('Connect QQ'))
        self.connect_btn.clicked.connect(self.active_connect_qq)
        exit_button = PushButton(tr('Exit & Stop'))
        exit_button.clicked.connect(self.exit)
        friend_line.addWidget(self.friend_title)
        friend_line.addWidget(self.version_label)
        friend_line.addWidget(self.friend_qq)
        friend_line.addSpacing(16)
        friend_line.addWidget(CaptionLabel(tr('Switch Friend')))
        friend_line.addWidget(self.friend_box)
        friend_line.addStretch(1)
        connection_line.addStretch(1)
        connection_line.addWidget(self.bridge_label)
        connection_line.addSpacing(8)
        connection_line.addWidget(self.connect_btn)
        connection_line.addSpacing(8)
        connection_line.addWidget(exit_button)
        line.addLayout(friend_line)
        line.addLayout(connection_line)
        header.installEventFilter(self)
        column.addWidget(header)
        column.addWidget(self.stackedWidget, 1)
        self.widgetLayout.addWidget(container)
        self.fill_friend_box()

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'header', None) and event.type() in (QEvent.Resize, QEvent.LayoutRequest):
            margins = self.header_layout.contentsMargins()
            needed = self.friend_header.sizeHint().width() + self.connection_header.sizeHint().width() + self.header_layout.spacing()
            direction = QBoxLayout.LeftToRight if needed <= self.header.width()-margins.left()-margins.right() else QBoxLayout.TopToBottom
            if self.header_layout.direction() != direction:
                self.header_layout.setDirection(direction)
        return super().eventFilter(watched, event)

    def active_connect_qq(self):
        page = self.pages['qqConnectionPage']
        self.switchTo(page)
        page.active_connect()

    def active_connect_personal_qq(self):
        """Open the DotLink page; if OneBot is already up, start the bridge directly."""
        import socket
        page = self.pages.get('dotlinkPage')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                online = probe.connect_ex(('127.0.0.1', 3001)) == 0
        except OSError:
            online = False
        if page is not None:
            self.switchTo(page)
        if online:
            toast(self, 'info', 'OneBot 已就绪，正在启动桥接并核验 QQ 登录状态。', '连接')
            self.request('start')
            return
        if page is None:
            toast(self, 'warning', 'QQ 直连页面未加载。')
            return
        page.begin('')

    def fill_friend_box(self):
        self.friend_box.blockSignals(True)
        self.friend_box.clear()
        for qq_id, record in self.records.items():
            self.friend_box.addItem(f"{record.get('display_name', qq_id)} · {qq_id}", userData=qq_id)
        if self.current_qq in self.records:
            self.friend_box.setCurrentIndex(list(self.records).index(self.current_qq))
        self.friend_box.blockSignals(False)

    def build_tray(self):
        self.tray = QSystemTrayIcon(house_icon(), self)
        self.tray.setToolTip('Open QQ Dot')
        menu = QMenu()
        open_action, exit_action = QAction(tr('Open Window'), menu), QAction(tr('Exit & Stop Backend'), menu)
        connect_action = QAction('连接 QQ', menu)
        disconnect_action = QAction('断开消息桥接', menu)
        status_action = QAction('刷新连接状态', menu)
        open_action.triggered.connect(self.show_window)
        connect_action.triggered.connect(self.tray_connect)
        disconnect_action.triggered.connect(self.tray_disconnect)
        status_action.triggered.connect(lambda: self.request('status'))
        exit_action.triggered.connect(self.exit)
        self.account_menu = QMenu(tr('账号切换'), menu)
        self.account_menu.aboutToShow.connect(self.rebuild_account_menu)
        menu.addAction(open_action)
        menu.addMenu(self.account_menu)
        menu.addSeparator()
        menu.addAction(connect_action)
        menu.addAction(disconnect_action)
        menu.addAction(status_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.tray_menu = menu

    def tray_connect(self):
        self.show_window()
        self._user_disconnected = False
        self.active_connect_qq()

    def tray_connect_personal(self):
        page = self.pages.get('dotlinkPage')
        self._user_disconnected = False
        if page is None:
            self.request('start')
            return
        login = getattr(page, 'state', {}).get('login') or {}
        if login.get('is_login'):
            self.request('start')
        else:
            self.show_window()
            self.switchTo(page)
            page.begin('')

    def tray_disconnect(self):
        self._user_disconnected = True
        import qq_official
        qq_official.stop(ROOT)
        self.request('stop')
        toast(self, 'info', '已断开消息桥接；QQ 直连运行时仍可单独停止。', '连接')

    def auto_reconnect(self):
        from house_settings import personal_qq_enabled
        if not personal_qq_enabled(ROOT):
            return
        if (os.environ.get('QT_QPA_PLATFORM', '').lower() == 'offscreen'
                or self.closing or self.exiting or self.bridge_busy or self.running
                or getattr(self, '_user_disconnected', False)):
            return
        page = self.pages.get('dotlinkPage')
        if page is not None and not (getattr(page, 'state', {}).get('login') or {}).get('is_login'):
            return
        self.request('start')

    def _typing_has_focus(self) -> bool:
        focus = QApplication.focusWidget()
        return isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit))

    def _last_operation(self, status: str):
        import revert_center
        owner = self.current_qq or ""
        entries = revert_center.timeline(ROOT, owner)
        return next((item for item in entries
                     if item.get("kind") == "task" and item.get("status") == status), None)

    def undo_last(self):
        if self._typing_has_focus():
            return
        try:
            entry = self._last_operation("applied")
        except Exception as exc:
            return toast(self, "error", str(exc), "撤销")
        if not entry:
            return toast(self, "info", "没有可撤销的任务文件改动。", "撤销")
        import revert_center
        run_async(lambda _: revert_center.revert(ROOT, entry["operation_id"]),
                  lambda result: (toast(self, "success", f"已撤销：{entry.get('title') or entry['target']}", "撤销"),
                                  self.refresh_current()),
                  lambda exc: toast(self, "error", str(exc), "撤销失败"), pool=self.bridge_pool)

    def redo_last(self):
        if self._typing_has_focus():
            return
        try:
            entry = self._last_operation("reverted")
        except Exception as exc:
            return toast(self, "error", str(exc), "重做")
        if not entry:
            return toast(self, "info", "没有可重做的任务文件改动。", "重做")
        import revert_center
        run_async(lambda _: revert_center.redo(ROOT, entry["operation_id"]),
                  lambda result: (toast(self, "success", f"已重做：{entry.get('title') or entry['target']}", "重做"),
                                  self.refresh_current()),
                  lambda exc: toast(self, "error", str(exc), "重做失败"), pool=self.bridge_pool)

    def refresh_current(self):
        page = self.stackedWidget.currentWidget()
        if hasattr(page, "load"):
            try:
                page.load()
            except Exception:
                pass

    def rebuild_account_menu(self):
        self.account_menu.clear()
        try:
            import account_service
            accounts = {row.get('id'): row for row in account_service.list_accounts(ROOT)}
            active = account_service.active_accounts(ROOT)
        except Exception:
            self.account_menu.addAction('账号读取失败').setEnabled(False)
            return
        if not accounts:
            self.account_menu.addAction('还没有账号').setEnabled(False)
            return
        for tool, identifier in active.items():
            row = accounts.get(identifier)
            if not row:
                continue
            action = QAction(f"{tool} · {row.get('label') or identifier}", self.account_menu)
            action.triggered.connect(lambda _=False, r=row, t=tool: self.switch_account_from_tray(t, r))
            self.account_menu.addAction(action)
        self.account_menu.addSeparator()
        for row in list(accounts.values())[:12]:
            action = QAction(f"{row.get('provider')} / {row.get('label') or row.get('id')}", self.account_menu)
            action.triggered.connect(lambda _=False, r=row: self.switch_account_from_tray('opencode', r))
            self.account_menu.addAction(action)

    def switch_account_from_tray(self, tool, row):
        try:
            import account_service
            account_service.activate(ROOT, tool, row.get('id', ''))
            if self.current_qq:
                account_service.apply_to_friend(ROOT, str(self.current_qq), row.get('id', ''))
            toast(self, 'success', f"已切换到 {row.get('label') or row.get('id')}", '账号切换')
            self.mark_all_stale()
        except Exception as exc:
            toast(self, 'error', str(exc), '账号切换失败')

    def active_connect_napcat(self):
        return self.active_connect_personal_qq()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------- friends ----------

    @property
    def friend_dir(self):
        return ROOT / 'friends' / self.current_qq

    @property
    def profile_path(self):
        return self.friend_dir / 'friend.json'

    @property
    def running(self):
        record = self.records.get(self.current_qq, {})
        if record.get('platform') == 'qq_official':
            import qq_official
            return qq_official.status(ROOT, record.get('self_id')).startswith('已连接')
        return self.status_text.lower().startswith('running') or self.status_text.startswith('Online')

    def reload_friends(self):
        if not REGISTRY.exists():
            try:
                REGISTRY.write_text('{"friends": {}}\n', encoding='utf-8')
            except OSError:
                pass
        try:
            self.records = json.loads(REGISTRY.read_text(encoding='utf-8')).get('friends', {})
        except Exception:
            self.records = {}
        if getattr(self, 'current_qq', None) not in self.records:
            self.current_qq = next(iter(self.records), None)
        if hasattr(self, 'friend_box'):
            self.fill_friend_box()

    def friend_chosen(self, index):
        qq_id = self.friend_box.itemData(index)
        if qq_id and qq_id != self.current_qq:
            self.show_friend(qq_id)

    def show_friend(self, qq_id, page=None):
        """Switch friend. 2026-10-26 user: stay on the page being read and refresh it for the new friend,
        instead of dropping every switch back on the console."""
        if qq_id not in self.records:
            toast(self, 'warning', '请先在好友总览中添加并选择好友。')
            return
        if qq_id != self.current_qq and self.pages['memoryPage'].unsaved():
            toast(self, 'warning', tr('Persistent memory has unsaved changes. Please save before switching friends.'))
            self.fill_friend_box()
            self.switchTo(self.pages['memoryPage'])
            return
        self.current_qq = qq_id
        self.fill_friend_box()
        self.friend_title.setText(self.records[qq_id].get('display_name', qq_id))
        self.friend_qq.setText('QQ · ' + qq_id)
        self.mark_all_stale()
        if page is None:
            self.refresh_for_friend()
            return

        if page in ('topicPage', 'memoryPage'):
            target = self.pages['memoryPage']
            if page == 'topicPage' and hasattr(target, 'select_tab'):
                target.select_tab('topic')
            elif hasattr(target, 'select_tab'):
                target.select_tab('memory')
        elif page in ('templatePage', 'stylePage', 'basicPage', 'stickerPage', 'profilePage'):
            target = self.pages['profilePage']
            if page in ('stylePage', 'basicPage', 'stickerPage') and hasattr(target, 'select_tab'):
                tab_map = {'stylePage': 'style', 'basicPage': 'basic', 'stickerPage': 'stickers'}
                target.select_tab(tab_map[page])
            elif hasattr(target, 'select_tab'):
                target.select_tab('template')
        elif page in ('historyPage', 'contextPage', 'emotionPage'):
            target = self.pages['historyPage']
            if page in ('contextPage', 'emotionPage') and hasattr(target, 'select_tab'):
                tab_map = {'contextPage': 'context', 'emotionPage': 'emotion'}
                target.select_tab(tab_map[page])
            elif hasattr(target, 'select_tab'):
                target.select_tab('history')
        else:
            target = self.pages.get(page, self.pages.get('workbenchPage', self.pages['statusPage']))

        if self.stackedWidget.currentWidget() is target:
            if hasattr(target, 'shown'):
                target.shown()
        self.switchTo(target)

    def switchTo(self, interface):
        if interface is self.workspaces.get('agents'):
            interface.select_widget(self.pages['modelPage'])
        if interface is self.pages.get('dotlinkPage'):
            connection = self.pages['qqConnectionPage']
            connection.open_legacy()
            interface = connection
        for parent_page in (self.pages.get('profilePage'),
                            self.pages.get('memoryPage'),
                            self.pages.get('historyPage')):
            if not parent_page:
                continue
            if hasattr(parent_page, '_tab_mapping'):
                for key, child in parent_page._tab_mapping.items():
                    if child is interface:
                        parent_page.switch_tab(interface)
                        interface = parent_page
                        break
        # Legacy navigation links still resolve through the workspace shell.
        selected = interface
        workspace = self._workspaces_for_page.get(interface)
        if workspace is not None:
            workspace.select_widget(interface)
            interface = workspace
        super().switchTo(interface)
        if selected in self._sidebar_routes:
            self.navigationInterface.setCurrentItem(self._sidebar_routes[selected])

    def toggle_friend(self, qq_id):

        path = ROOT / 'friends' / qq_id / 'friend.json'
        profile = load_profile(path)
        profile['bridge_enabled'] = not profile.get('bridge_enabled', True)
        write_profile(path, profile)
        toast(self, 'success', tr('This friend is enabled.') if profile['bridge_enabled'] else tr('This friend is paused.'))
        self.overview.refresh()
        if qq_id == self.current_qq:
            self.refresh_friend_mode()

    def mark_stale(self, name):
        self.pages[name].loaded_for = None

    def scoped_views(self):
        """Every friend-scoped view: the named pages plus pages only reachable through a workspace."""
        views, seen = [], set()
        for view in [*self.pages.values(),
                     *(container.tabs.widget(i) for container in self.workspaces.values()
                       for i in range(container.tabs.count()))]:
            if view is not None and id(view) not in seen:
                seen.add(id(view))
                views.append(view)
        return views

    def mark_all_stale(self):
        """2026-10-26 user: after a switch no page may still hold the previous friend's rows."""
        for view in self.scoped_views():
            if hasattr(view, 'loaded_for') and view.loaded_for != self.current_qq:
                view.loaded_for = None

    def update_header_for(self, page):
        if page is self.workspaces['home']:
            self.friend_title.setText('智能体控制台')
            self.friend_qq.setText('QQ · ' + self.current_qq if self.current_qq else '')
        elif page is self.workspaces['friends']:
            self.friend_title.setText('好友总览')
            self.friend_qq.setText('')
        elif self.current_qq:
            self.friend_title.setText(self.records[self.current_qq].get('display_name', self.current_qq))
            self.friend_qq.setText('QQ · ' + self.current_qq)

    def refresh_for_friend(self):
        """Redraw the page on screen for the friend now selected (no navigation)."""
        self.mark_all_stale()
        page = self.stackedWidget.currentWidget()
        self.update_header_for(page)
        if hasattr(page, 'shown'):
            page.shown()

    def page_changed(self, _index):
        page = self.stackedWidget.currentWidget()
        self.update_header_for(page)
        if hasattr(page, 'shown'):
            page.shown()

    def refresh_friend_mode(self):
        if self.closing or self.exiting or not self.current_qq:
            return
        qq_id, directory, running = self.current_qq, self.friend_dir, self.running

        def apply(values):
            if not (self.closing or self.exiting) and values['qq'] == self.current_qq:
                self.pages['statusPage'].apply(values)
        run_async(lambda _: friend_mode_values(qq_id, directory, running), apply, quiet)

    # ---------- bridge ----------

    def request(self, action):
        if self.closing and action != 'exit':
            return
        if self.bridge_busy:
            self.pending = action
            return
        if action in ('start', 'stop'):
            from house_settings import load, save
            settings = load(ROOT)
            settings['qq_personal_enabled'] = action == 'start'
            try:
                save(settings, ROOT)
            except OSError:
                toast(self, 'warning', '未能保存下次启动设置，本次连接操作继续。')
        self.bridge_busy = True
        if action not in ('status', 'exit'):
            toast(self, 'info', '正在启动…' if action == 'start' else '正在停止…')
        run_async(lambda _: invoke_bridge('stop' if action == 'exit' else action),
                  lambda result: self.bridge_done(action, *(result if isinstance(result, (tuple, list)) else (0, str(result)))),
                  lambda exc: self.bridge_done(action, 1, str(exc)), pool=self.bridge_pool)

    def startup_done(self, error):
        self.bridge_busy = False
        if error:
            toast(self, 'warning', error, tr('Startup Notice'))
        pending, self.pending = self.pending, None
        self.request(pending or 'status')
        # One-click deployment: after the runtime starts, land on the QQ connection
        # page and offer quick login / the in-app QR immediately.
        QTimer.singleShot(900, self.auto_open_dotlink)

    def auto_open_dotlink(self):
        from house_settings import personal_qq_enabled
        if not personal_qq_enabled(ROOT):
            return
        if getattr(self, '_auto_connect_done', False) or self.closing or self.exiting:
            return
        if os.environ.get('QT_QPA_PLATFORM', '').lower() == 'offscreen':
            return  # UI tests must not start the local runtime or connect to a real account
        page = self.pages.get('dotlinkPage')
        if page is None:
            return
        login = getattr(page, 'state', {}).get('login') or {}
        if login.get('is_login'):
            self._auto_connect_done = True
            return
        self._auto_connect_done = True
        initial_page = self.stackedWidget.currentWidget()
        def checked(value):
            if self.closing or self.exiting:
                return
            page.apply(value)
            if (value.get('login') or {}).get('is_login'):
                return
            if self.stackedWidget.currentWidget() is initial_page:
                self.switchTo(page)
                page.begin('')
        from dotlink import state, Paths
        run_async(lambda _: state(Paths(ROOT), include_logs=False), checked, quiet)

    # ---------- files dragged in / opened by double-click ----------

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.show_window()
            file_drop.route(self, paths)

    def open_initial_paths(self):
        paths, self.open_paths = self.open_paths, []
        if paths:
            self.show_window()
            file_drop.route(self, paths)

    def poll_open_requests(self):
        try:
            paths = file_drop.drain_open(ROOT)
        except OSError:
            return
        if paths:
            self.show_window()
            file_drop.route(self, paths)

    def restore_backup(self, path):
        """Restore a dropped .oqdbackup after explicit confirmation."""
        if not confirm(self, "恢复数据备份",
                       "恢复会覆盖好友、任务、迁移记录和设置；旧数据会保留到 _before_restore/。继续？",
                       "恢复"):
            return

        def done(report):
            self.reload_friends()
            toast(self, "success", f"已恢复 {report['files']} 个文件；建议重启程序。", "备份")

        run_async(lambda _: app_backup.restore_backup(ROOT, Path(path)), done,
                  lambda exc: toast(self, "error", str(exc), "恢复失败"), pool=self.bridge_pool)

    def bridge_done(self, action, code, output, *extra):
        self.bridge_busy = False
        if action == 'exit':
            if code == 0:
                # "Exit & Stop Backend" must also stop the NapCat/DotLink
                # runtime; otherwise its node.exe survives with open DLLs and
                # keeps the onefile temp directory locked.
                try:
                    import dotlink as dotlink_runtime
                    dotlink_runtime.stop(dotlink_runtime.Paths(ROOT), all_instances=False)
                except Exception as exc:
                    toast(self, 'warning', f'后台已停止，但 NapCat 未完全关闭：{exc}', '退出')
                self.exiting = True
                self.tray.hide()
                QApplication.quit()
                return
            self.closing = False
            toast(self, 'error', output, tr('Failed to stop, bridge may still be running'))
        elif action == 'status':
            first = output.splitlines()[0] if output else 'Unable to read status'
            self.status_text = first
            from ui_locale import bridge_status_text
            record = self.records.get(self.current_qq, {})
            if record.get('platform') == 'qq_official':
                import qq_official
                self.bridge_label.setText('官方机器人：' + qq_official.status(ROOT, record.get('self_id')))
            elif not record.get('self_id') and self.pages['qqConnectionPage'].mode.currentRouteKey() == 'official':
                self.bridge_label.setText('官方机器人：请点击「连接 QQ」配置或连接')
            else:
                self.bridge_label.setText('个人 QQ 桥接：' + bridge_status_text(first))
            color = GREEN if code == 0 and (first.lower().startswith('running') or first.startswith('Online')) else MUTED
            self.bridge_label.setTextColor(color, color)
            page = self.stackedWidget.currentWidget()
            if page is self.workspaces['friends']:
                self.overview.on_status()
            elif self.current_qq:
                self.refresh_friend_mode()
                if hasattr(page, 'on_status'):
                    page.on_status()
        else:
            message = output.splitlines()[0] if code == 0 and output else 'Operation failed: ' + output[-200:]
            if action == 'start' and code == 0:
                message = tr('Backend Bridge: ') + message
                if self.current_qq and not load_profile(self.profile_path).get('bridge_enabled', True):
                    message += '; Current friend is paused. Please click "Enable Current Friend".'
            toast(self, 'success' if code == 0 else 'error', message)
            self.request('status')
        if self.pending:
            pending, self.pending = self.pending, None
            self.request(pending)

    # ---------- exit ----------

    def exit(self):
        if self.closing:
            return
        self.closing = True
        self.save_window_size()
        preview = self.pages['proactivePage'].preview_job
        if preview is not None and not preview.done():
            preview.cancel()
            from app_server import SERVER
            SERVER.close()
        toast(self, 'info', tr('Stopping and exiting...'))
        if self.bridge_busy:
            self.pending = 'exit'
        else:
            self.request('exit')

    def closeEvent(self, event):
        self.save_window_size()
        if self.exiting:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage('Open QQ Dot', '已转入后台，任务继续执行。需要停止时使用“退出并停止服务”。')

    def save_window_size(self):
        from house_settings import load, save
        settings = load(ROOT)
        settings['window_size'] = [self.width(), self.height()]
        save(settings, ROOT)


def fit_window(window):
    """Apply geometry after page construction, in Qt logical screen coordinates."""
    from house_settings import load
    area = window.screen().availableGeometry()
    saved = load(ROOT).get('window_size', [1460, 900])
    if not isinstance(saved, list) or len(saved) != 2 or any(type(n) is not int or n < 1 for n in saved):
        saved = [1460, 900]
    minimum = (min(1050, area.width()), min(700, area.height()))
    width = min(area.width(), max(minimum[0], saved[0]))
    height = min(area.height(), max(minimum[1], saved[1]))
    window.setMinimumSize(*minimum)
    window.resize(width, height)
    window.move(area.x()+(area.width()-width)//2, area.y()+(area.height()-height)//2)


def install_language(app):
    """Apply the same locale to app labels, Fluent controls and Qt's built-in menus."""
    from PySide6.QtCore import QLibraryInfo, QTranslator
    for translator in getattr(app, '_ui_translators', []):
        app.removeTranslator(translator)
    language = os.environ.get('OPENQQ_UI_LANG', 'zh-CN').strip().lower().replace('_', '-')
    locale = QLocale(QLocale.Chinese, QLocale.China) if language.startswith('zh') else QLocale(QLocale.English, QLocale.UnitedStates)
    QLocale.setDefault(locale)
    translator = FluentTranslator(locale)
    app.installTranslator(translator)
    app._ui_translators = [translator]
    qt_translator = QTranslator(app)
    if qt_translator.load(locale, 'qtbase', '_', QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(qt_translator)
        app._ui_translators.append(qt_translator)


def run(startup_error='', startup=None, open_paths=None):
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('OpenQQDot.Client.Desktop.1.0')
    except Exception:
        pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(house_icon())
    app.setQuitOnLastWindowClosed(False)
    install_language(app)
    setTheme(Theme.LIGHT)
    setThemeColor(ACCENT)
    init_ui_thread()
    try:  # 2026-10-11 user: the friend house on the phone (house_web), in this process
        import house_web
        web = house_web.start()
    except Exception as exc:
        web = None
        startup_error = '\n'.join(filter(None, (startup_error, f'网页访问服务启动失败：{exc}')))
    window = HouseWindow(startup_error, web, startup, open_paths)
    app.aboutToQuit.connect(window.pages['workbenchPage']._voice_cancel.set)
    fit_window(window)
    window.show()
    QTimer.singleShot(1600,window.pages['workbenchPage'].prepare_voice)
    QTimer.singleShot(0, lambda: fit_window(window))
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(run())
