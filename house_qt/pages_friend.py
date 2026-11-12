"""Overview and the friend's status / basic info / topic library / speaking style / model pages."""
from ui_locale import tr
from agent_tool_adapters import AGENT_BACKENDS, BUILTINS

import json
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QDoubleSpinBox, QFileDialog, QHeaderView, QLabel,
                             QListWidgetItem, QSpinBox, QTableWidgetItem, QVBoxLayout, QHBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, EditableComboBox, LineEdit, PasswordLineEdit,
                            PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton, RadioButton, SwitchButton, TableWidget,
                            TitleLabel, ListWidget)
from .common import SectionSwitcher

from api_backend import DEFAULT_BASE_URL, EFFORTS, MODELS, PROTOCOLS, PROVIDER_DEFAULTS, fetch_provider_models, key_path, save_key, load_key, settings as api_settings
from api_profiles import PRESETS, new_connection, load_connections, clean_connection, activate, discover_models, MAX_PROFILES
from claude_backend import EFFORTS as CLAUDE_EFFORTS, MODELS as CLAUDE_MODELS, settings as claude_settings
from antigravity_backend import EFFORTS as AG_EFFORTS, MODELS as AG_MODELS, settings as ag_settings
from codex_settings import COMPACT_TOKENS, CONTEXT_TOKENS, MIN_COMPACT_TOKENS, generation_settings, model_options
import provider_context
import house_status
import house_settings
from house_status import LOCAL_THINKING, ROOT, SWITCHES, invoke_bridge, load_profile, set_switch, write_profile
from store import FriendStore

from .common import (GREEN, LONG_POOL, MUTED, RED, Card, InputDialog, Page, TextDialog, body, confirm, hint, quiet,
                     row, run_async, set_text_keep, toast)
from .agent_console_widget import AgentConsoleWorkbench

_API_APPROVAL_OPTIONS = [
    ('自动执行无需审批 (Never Ask / 快速开发)', 'never'),
    ('按需请求人工审批 (On Request / 安全确认)', 'on-request'),
    ('严格审批 (每次执行命令前询问)', 'request-review'),
]
APPROVAL_MODES = {
    'codex': [
        ('完全访问权限 (不受限制地访问互联网和电脑上的任何文件)', 'full-access'),
        ('帮我批准 (仅对检测到的风险操作请求批准)', 'on-request'),
        ('请求批准 (编辑外部文件和执行命令时始终询问)', 'request-review'),
    ],
    'claude': [
        ('Bypass permissions (直接跳过权限，无需询问)', 'bypass'),
        ('Auto (由 Claude 自动权衡决定)', 'auto'),
        ('Accept edits (自动接受所有文件修改)', 'accept-edits'),
        ('Plan (修改前先出计划方案)', 'plan'),
        ('Manual (修改前始终询问确认)', 'manual'),
    ],
    'antigravity': [
        ('Turbo (无限制自动执行所有命令)', 'turbo'),
        ('Default (仅在敏感操作前询问)', 'default'),
        ('Request Review (每条命令均需人工审批)', 'request-review'),
    ],
    'api': _API_APPROVAL_OPTIONS,
    'dsh': [
        ('全自动执行无需审批 (danger-full-access / 快速执行)', 'danger-full-access'),
        ('工作区写权限按需审批 (workspace-write / 默认安全)', 'workspace-write'),
        ('严格只读权限 (read-only / 仅读取不修改)', 'read-only'),
    ],
}
CLAUDE_EFFORTS_DISPLAY = ('off', 'low', 'medium', 'high', 'extra', 'max', 'ultracode')
for _backend in AGENT_BACKENDS:
    APPROVAL_MODES[_backend] = [('沿用工具审批（需要完整电脑访问授权）', 'external-native')]


def friend_state(qq_id, profile, running):
    if not profile.get('bridge_enabled', True):
        return 'Pause'
    if not running:
        return '等待启动'
    store = FriendStore(ROOT / 'friends' / qq_id)
    try:
        error = store.get('bridge_error', '')
    finally:
        store.close()
    return '需要处理' if error else '运行中'


def state_color(state):
    return GREEN if state == '运行中' else RED if state == '需要处理' else MUTED


def phone_card(web, qq):
    """Mobile connection card with local security isolation."""
    import io
    import qrcode
    import house_web
    token = house_web.friend_token(qq)
    reset = PushButton('重置访问令牌')
    reset.setToolTip('旧二维码和链接立即失效；手机需要重新扫码。')

    def rotate_token():
        if not confirm(card, '重置访问令牌',
                       '重置后，所有旧链接、旧二维码和已保存的手机令牌立即失效。\n\n'
                       '手机需要重新扫码。继续？', '重置令牌'):
            return
        house_web.rotate_friend_token(qq)
        toast(card, 'success', '访问令牌已重置；请关闭本页后重新进入，生成新二维码。', '安全')
    reset.clicked.connect(rotate_token)
    card = Card('移动设备与网页访问 (Mobile & Web Access)', actions=(reset,))

    has_remote = any(web.get(k) for k in ('tailnet', 'public', 'radmin', 'lan'))
    hints = []
    if web.get('radmin'):
        radmin_url = web['radmin'].rstrip('/') + '/?qq=' + qq + '#t=' + token
        hints.append(hint(f'【Radmin VPN 跨电脑连接】：已加入同一 Radmin 房间的电脑直接打开：{radmin_url}'))
    if web.get('lan'):
        lan_url = web['lan'].rstrip('/') + '/?qq=' + qq + '#t=' + token
        hints.append(hint(f'【局域网跨电脑连接】：同一局域网/WiFi 电脑直接打开：{lan_url}'))
    if web.get('tailnet'):
        t_url = web['tailnet'].rstrip('/') + '/?qq=' + qq + '#t=' + token
        hints.append(hint(f'【Tailscale 虚拟网络】：异地连上 Tailscale 打开：{t_url}'))
        hints.append(hint(f'QQ {qq} 的独立入口。手机连上 Tailscale 后扫码，只能打开这位好友的小屋。'))

    hints.append(hint(f'【本机浏览器入口】：{web["local"].rstrip("/")}/?qq={qq}#t={token}'))
    if not has_remote:
        hints.append(hint('没有检测到 Tailscale 虚拟网段或远程入口。在电脑上运行 tailscale 或开启远程，这里就会出现对应配对入口。'))
        hints.append(hint('🔒 【本机安全隔离保护中】：当前仅限本机 127.0.0.1 访问，外部局域网及任何远程设备均无法扫描或连入此端口。'))
    else:
        hints.append(hint('二维码与链接内含专属访问令牌，已授权设备直接浏览器打开即可使用该智能体。'))

    base = web.get('public') or web.get('tailnet') or web.get('radmin') or web.get('lan') or web.get('local')
    url = base.rstrip('/') + '/?qq=' + qq + '#t=' + token

    buffer = io.BytesIO()
    qrcode.make(url, box_size=5, border=2).save(buffer, format='PNG')
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue())
    image = QLabel()
    image.setPixmap(pixmap)

    return card.add(row(image, None), *hints)



class OverviewPage(Page):
    def __init__(self, house):
        super().__init__(house, 'overviewPage')
        connect_btn = PushButton('QQ 连接/扫码')
        connect_btn.clicked.connect(self.house.active_connect_qq)
        add = PrimaryPushButton('+ 添加好友')
        add.clicked.connect(self.add_friend)
        self.add(Card('好友总览', actions=(connect_btn, add)).add(
            hint('部署在本地电脑的智能体助手。每位好友的设置与记录独立保存。点「进入小屋」查看和设置这位好友。')))
        self.cards = []
        self.signature = None

    def shown(self):
        self.refresh()

    on_status = shown

    def refresh(self):
        running, records = self.house.running, dict(self.house.records)

        def read(_):
            profiles = {qq: load_profile(ROOT / 'friends' / qq / 'friend.json') for qq in records}
            return profiles, {qq: friend_state(qq, profile, running) for qq, profile in profiles.items()}
        run_async(read, lambda value: self.show(records, *value), quiet)

    def show(self, records, profiles, modes):
        signature = tuple((qq, record.get('display_name', qq), bool(profiles[qq].get('bridge_enabled', True)),
                           bool(profiles[qq].get('cooperation_enabled', True)),
                           modes[qq]) for qq, record in records.items())
        if signature == self.signature:
            return
        self.signature = signature
        for card in self.cards:
            card.deleteLater()
        self.cards = []
        if not records:
            self.cards.append(self.add(Card().add(BodyLabel('还没有绑定好友。点右上角「添加好友」。'))))
        for qq_id, record in records.items():
            enabled = bool(profiles[qq_id].get('bridge_enabled', True))
            name = TitleLabel(record.get('display_name', qq_id))
            mode = BodyLabel(modes[qq_id])
            mode.setTextColor(state_color(modes[qq_id]), state_color(modes[qq_id]))
            enter, debug = PrimaryPushButton('进入小屋'), PushButton('主动调试')
            pause = PushButton(tr('Pause') if enabled else '继续')
            delete = PushButton('删除好友')
            delete.clicked.connect(lambda _=False, qq=qq_id: self.delete_friend(qq))
            # 2026-10-26: show_friend(qq) now refreshes in place; entering the house must navigate to the console.
            enter.clicked.connect(lambda _=False, qq=qq_id: self.house.show_friend(qq, 'workbenchPage'))
            debug.clicked.connect(lambda _=False, qq=qq_id: self.house.show_friend(qq, 'proactivePage'))
            pause.clicked.connect(lambda _=False, qq=qq_id: self.house.toggle_friend(qq))
            cooperate = CheckBox('允许协作')
            cooperate.setChecked(profiles[qq_id].get('cooperation_enabled', True))
            cooperate.toggled.connect(lambda value, qq=qq_id: self.set_cooperation(qq, value))
            card = Card().add(row(name, CaptionLabel(f'QQ · {qq_id}'), None, cooperate, mode, pause, debug, delete, enter))
            self.cards.append(self.add(card))

    def set_cooperation(self, qq_id, enabled):
        path = ROOT / 'friends' / qq_id / 'friend.json'
        profile = load_profile(path)
        profile['cooperation_enabled'] = bool(enabled)
        write_profile(path, profile)
        self.house.mark_all_stale()

    def delete_friend(self, qq_id):
        if not confirm(self, '删除好友',
                       f'确定删除好友 {qq_id}？\n\n'
                       '会从好友列表移除，并删除这位好友的本地文件夹：\n'
                       'friend.json、聊天记录、人格、任务和配置。\n\n'
                       '此操作不可恢复。', '删除好友'):
            return
        from house_status import delete_friend
        try:
            message = delete_friend(qq_id)
            self.house.reload_friends()
            self.signature = None
            self.refresh()
            toast(self.house, 'success', message, '删除好友')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '删除好友失败')

    def add_friend(self):
        first_friend = not self.house.records
        dialog = InputDialog(self.house, '添加好友', '请输入当前 QQ 登录账号好友列表中的 QQ 号：')
        if not dialog.exec():
            return
        qq_id = dialog.edit.text().strip()
        if not re.fullmatch(r'\d{5,12}', qq_id):
            toast(self.house, 'error', '请输入 5–12 位数字 QQ 号。')
            return
        toast(self.house, 'info', '正在添加好友…')

        def added(result):
            code, output = result
            if code:
                toast(self.house, 'error', output, '添加失败')
                return
            toast(self.house, 'success', '好友已添加。')
            self.house.reload_friends()
            self.refresh()
            if first_friend:
                from .quick_setup import QuickSetupDialog
                QuickSetupDialog(self.house, qq_id).exec()
        run_async(lambda _: invoke_bridge(f'bind -QQ {qq_id}'), added, pool=LONG_POOL)


class StatusPage(Page):
    LABELS = {'failed': '未发送，可重试', 'uncertain': '结果不确定', 'queued': '等待重试',
              'processing': '生成中', 'sending': '发送中', 'waiting': '开启后补回复'}

    def __init__(self, house):
        super().__init__(house, 'statusPage')
        self.name, self.state, self.summary = TitleLabel(), BodyLabel(), body()
        self.toggle = PrimaryPushButton('启用当前好友')
        start, stop, refresh = PushButton('启动后台桥接'), PushButton('停止后台桥接'), PushButton(tr('Refresh'))
        self.toggle.clicked.connect(lambda: self.house.toggle_friend(self.house.current_qq))
        start.clicked.connect(lambda: self.house.request('start'))
        stop.clicked.connect(lambda: self.house.request('stop'))
        refresh.clicked.connect(lambda: self.house.request('status'))
        self.add(Card('今天的连接').add(self.name, self.state, self.summary, row(self.toggle, start, stop, refresh, None)))

        #  (Agent Workbench Card)
        workbench_btn = PrimaryPushButton('🚀 进入智能体专属工作台（多项目与多会话） →')
        workbench_btn.clicked.connect(lambda: self.house.show_friend(self.house.current_qq, 'workbenchPage'))
        self.add(Card("智能体工作台与会话", actions=(workbench_btn,)).add(
            hint('多项目与多会话工作台已提升为左侧导航栏独立页面。支持自由挂载本地任意项目目录、设定模板对话参照、可折叠深度思考过程 (CoT) 与长期记忆沉淀。')))

        #  (Autonomous Tasks & Agenda)

        self.agenda_status_text = BodyLabel()
        self.agenda_detail_text = CaptionLabel()
        self.agenda_table = TableWidget()
        self.agenda_table.setColumnCount(3)
        self.agenda_table.setHorizontalHeaderLabels(['计划时间', '任务类型', '关注意向 / 规划主题'])
        self.agenda_table.verticalHeader().hide()
        self.agenda_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.agenda_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.agenda_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.agenda_table.setColumnWidth(0, 130)
        self.agenda_table.setColumnWidth(1, 120)
        self.agenda_table.setMinimumHeight(130)
        self.agenda_empty = hint('暂无待办事项（等待模型自主排班或点击下方重新规划）')

        replan_btn = PushButton(tr('重新规划待办 (Replan)'))
        replan_btn.clicked.connect(self.replan_agenda)
        preview_btn = PushButton('预览主动生成')
        preview_btn.clicked.connect(self.preview_agenda)
        goto_proactive_btn = PrimaryPushButton('任务与主动通知详细设置 →')
        goto_proactive_btn.clicked.connect(lambda: self.house.show_friend(self.house.current_qq, 'proactivePage'))

        self.add(Card("主动任务与待办计划").add(
            self.agenda_status_text,
            self.agenda_detail_text,
            self.agenda_table,
            self.agenda_empty,
            row(replan_btn, preview_btn, goto_proactive_btn, None)))

        self.switches = {}

        card = self.add(Card('功能开关').add(hint('开关即时保存，下次生成消息时生效。')))
        for key, title, note, _, _ in SWITCHES:
            switch = SwitchButton()
            switch.setOnText('开')
            switch.setOffText('关')
            switch.checkedChanged.connect(lambda on, key=key: self.save_switch(key, on))
            self.switches[key] = switch
            card.add(row(BodyLabel(title), hint(note), None, switch))

        self.log = PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(200)
        raw_log = PushButton('查看原始日志')
        raw_log.clicked.connect(self.show_raw_log)
        self.add(Card('运行记录', actions=(raw_log,)).add(
            hint('连接是否正常请看上方状态。这里保留历史记录；相同错误合并显示，原始日志不变。'), self.log))

        self.table = TableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(['消息时间', '状态', '原因'])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 130)
        self.table.setMinimumHeight(170)
        self.empty = hint('暂时没有失败的回复')
        refresh_rows, retry = PushButton('刷新失败列表'), PrimaryPushButton('重试选中消息')
        refresh_rows.clicked.connect(self.house.refresh_friend_mode)
        retry.clicked.connect(self.retry_selected)
        self.add(Card('回复失败与待处理').add(hint('选中一条明确未发送的消息重试；发送结果不确定的消息禁止重发。'),
                                       self.table, self.empty, row(refresh_rows, retry, None)))
        self.rows, self.rows_signature = {}, None
        self.phone_card_widget, self.phone_card_scope = None, None

    def load(self):
        self.name.setText(self.house.records[self.house.current_qq].get('display_name', self.house.current_qq))
        if self.house.web and self.phone_card_scope != self.house.current_qq:
            if self.phone_card_widget is not None:
                self.column.removeWidget(self.phone_card_widget)
                self.phone_card_widget.deleteLater()
            self.phone_card_widget = phone_card(self.house.web, self.house.current_qq)
            self.phone_state = body()
            self.phone_card_widget.add(self.phone_state)
            self.column.insertWidget(1, self.phone_card_widget)
            self.phone_card_scope = self.house.current_qq
        self.show_phone()
        profile = load_profile(self.house.profile_path)
        for key, _, _, get, _ in SWITCHES:
            self.switches[key].blockSignals(True)
            self.switches[key].setChecked(bool(get(profile)))
            self.switches[key].blockSignals(False)
        self.show_log()
        self.show_agenda()
        if hasattr(self, 'agent_workbench') and self.agent_workbench is not None:
            self.agent_workbench.reload()
        self.house.refresh_friend_mode()

    def save_switch(self, key, on):
        try:
            title = set_switch(self.house.profile_path, key, on)
            for name in ('proactivePage', 'topicPage'):
                self.house.mark_stale(name)
            self.house.refresh_friend_mode()
            self.show_agenda()
            toast(self.house, 'success', f"{title}已{'开启' if on else '关闭'}。")
        except Exception as exc:
            self.switches[key].blockSignals(True)
            self.switches[key].setChecked(not on)
            self.switches[key].blockSignals(False)
            toast(self.house, 'error', str(exc), '保存失败')

    def show_phone(self):
        """2026-10-12 user: whether the paired phone app is online right now (its last poll), not only paired."""
        if self.phone_card_widget is not None:
            import house_web
            self.phone_state.setText('手机 App：' + house_web.status_phone(
                load_profile(self.house.profile_path), self.house.profile_path.parent)['note'])

    def on_status(self):
        self.show_log()
        self.show_phone()
        self.show_agenda()



    def show_log(self):
        set_text_keep(self.log, house_status.recent_activity(self.house.friend_dir, compact=True))

    def show_raw_log(self):
        TextDialog(self.house, '原始运行日志', house_status.recent_activity(self.house.friend_dir, limit=1000)).exec()

    def apply(self, values):
        state, paused = values['state'], values['paused']
        self.state.setText('此好友：' + {'running': '运行中', 'paused': '已暂停',
            'stopped': '已停止', 'not running': '未运行'}.get(state, state) +
            ('（不自动回复或主动发送）' if paused else ''))
        self.state.setTextColor(state_color(state), state_color(state))
        self.toggle.setText('启用当前好友' if paused else '暂停当前好友')
        self.summary.setText(values['summary'])
        rows = values['rows']
        signature = (values['qq'], json.dumps(rows, ensure_ascii=False))
        if signature == self.rows_signature:
            return
        selected = self.selected_id()
        self.rows_signature = signature
        self.rows = {row_['message_id']: row_ for row_ in rows}
        self.table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            cells = (item['created_at'][5:16].replace('T', ' '), self.LABELS[item['state']], item['error'])
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, item['message_id'])
                self.table.setItem(index, column, cell)
            if item['message_id'] == selected:
                self.table.selectRow(index)
        self.empty.setVisible(not rows)

    def selected_id(self):
        items = self.table.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def retry_selected(self):
        message_id = self.selected_id()
        if not message_id:
            toast(self.house, 'warning', '请先选中一条失败消息。')
            return
        profile = load_profile(self.house.profile_path)
        if not self.house.running or not profile.get('bridge_enabled', True) or not profile.get('auto_reply', True):
            toast(self.house, 'warning', '请先启动桥接，并开启当前好友和自动回复。')
            return
        item = self.rows.get(message_id, {})
        if item.get('state') != 'failed':
            toast(self.house, 'warning', '仅能重试明确未发送的消息；发送中或结果不确定时不能重发。')
            return
        name = self.house.records[self.house.current_qq].get('display_name', self.house.current_qq)
        if not confirm(self.house, 'Retry Reply', f"为「{name}」的这条消息重新生成并发送回复？\n\n来信：{item.get('body', '')[:160]}\n\n仅重试明确未发送的失败记录；发送结果不确定时不会重发。"):
            return
        store = FriendStore(self.house.friend_dir)
        try:
            store.request_reply_retry(message_id)
            toast(self.house, 'success', '已提交当前好友的重试请求。')
        except ValueError as exc:
            toast(self.house, 'error', str(exc))
        finally:
            store.close()
        self.house.refresh_friend_mode()

    def send_direct_agent(self):
        text = self.agent_input.text().strip()
        if not text:
            toast(self.house, 'warning', '请输入测试指令或对话内容。')
            return
        qq_id = self.house.current_qq
        if not qq_id:
            toast(self.house, 'warning', '请先选择好友。')
            return

        self.agent_send.setEnabled(False)
        self.agent_input.setEnabled(False)
        self.agent_output.setPlainText(f"【用户/管理员指令】\n{text}\n\n正在调用当前智能体引擎生成回复，请稍候…")

        def run_call(_):
            from main import codex_text
            friend_dir = ROOT / 'friends' / qq_id
            profile = load_profile(self.house.profile_path)
            persona = profile.get('persona', '')
            basic = profile.get('basic_info', '')
            nickname = self.house.records.get(qq_id, {}).get('display_name', qq_id)
            prompt_parts = [
                f"【智能体即时对话与测试】\n你当前扮演/代表的身份是：{nickname}。",
            ]
            if persona:
                prompt_parts.append(f"【角色设定与常驻指令】\n{persona}")
            if basic:
                prompt_parts.append(f"【用户背景与画像】\n{basic}")
            prompt_parts.append(f"【测试输入】\n{text}\n\n请直接遵循以上角色与常驻指令给出回复：")
            full_prompt = "\n\n".join(prompt_parts)
            reply_text, _ = codex_text(full_prompt, friend_dir, ephemeral=True, profile=profile)
            return reply_text

        def on_done(result):
            self.agent_send.setEnabled(True)
            self.agent_input.setEnabled(True)
            self.agent_output.setPlainText(f"【智能体回复】\n{result.strip() if result else '（无回复内容）'}")

        def on_error(exc):
            self.agent_send.setEnabled(True)
            self.agent_input.setEnabled(True)
            self.agent_output.setPlainText(f"【调用失败】\n{exc}")
            toast(self.house, 'error', str(exc), '智能体测试失败')

        run_async(run_call, on_done, on_error, pool=LONG_POOL)

    def show_agenda(self):
        qq_id = self.house.current_qq
        if not qq_id:
            return
        from datetime import datetime
        import agenda
        now = datetime.now().astimezone()
        profile = load_profile(self.house.profile_path)
        from main import default_profile
        settings = {**default_profile(qq_id, '', True)['proactive'], **profile.get('proactive', {})}
        enabled = bool(profile.get('proactive', {}).get('enabled', False))
        rounds_min, rounds_max = settings.get('rounds_min', 1), settings.get('rounds_max', 2)
        window_start, window_end = settings.get('window_start', '09:00'), settings.get('window_end', '23:00')

        store = FriendStore(self.house.friend_dir)
        try:
            items = agenda.todos(store)
            sent = agenda.sent_today(store, now)
            smart_status = store.get('smart_status', '等待模型排待办')
            smart_reason = ''
            decision = json.loads(store.get('smart_proactive') or '{}')
            if decision and decision.get('reason'):
                smart_reason = f" · 最近理由: {decision['reason'][:40]}"
            next_lbl = agenda.next_label(store, now)
        finally:
            store.close()

        status_prefix = '🟢 主动任务运行中' if enabled else '⚪ 主动任务已关闭'
        self.agenda_status_text.setText(f"{status_prefix} · {next_lbl}")
        self.agenda_detail_text.setText(
            f"今日规划：{rounds_min}–{rounds_max} 轮（今日已完成 {sent} 轮） · "
            f"发送时段：{window_start}–{window_end} · 状态：{smart_status}{smart_reason}"
        )

        self.agenda_table.setRowCount(len(items))
        for idx, item in enumerate(items):
            try:
                at_dt = datetime.fromisoformat(item['at'])
                time_str = at_dt.strftime('%m-%d %H:%M')
            except Exception:
                time_str = str(item.get('at', ''))
            kind_str = '可能联系' if item.get('kind') == 'contact' else '查验状态'
            note_str = str(item.get('note', ''))

            self.agenda_table.setItem(idx, 0, QTableWidgetItem(time_str))
            self.agenda_table.setItem(idx, 1, QTableWidgetItem(kind_str))
            self.agenda_table.setItem(idx, 2, QTableWidgetItem(note_str))

        self.agenda_table.setVisible(bool(items))
        self.agenda_empty.setVisible(not items)

    def replan_agenda(self):
        qq_id = self.house.current_qq
        if not self.house.running:
            toast(self.house, 'warning', '请先启动后台桥接，再重新规划。')
            return
        store = FriendStore(self.house.friend_dir)
        try:
            store.set('smart_proactive', '{}')
            store.set('smart_retry_at', '')
            store.set('smart_status', '等待按最新设置重新规划待办时机')
            toast(self.house, 'success', '已提交重新规划请求；智能体将在下次唤醒时重新生成今日待办清单。')
        finally:
            store.close()
        self.show_agenda()

    def preview_agenda(self):
        qq_id = self.house.current_qq
        toast(self.house, 'info', '正在生成当前智能体的主动消息预览…')

        def do_preview(_):
            from house_status import preview_proactive
            profile = load_profile(self.house.profile_path)
            lines = preview_proactive(qq_id, profile)
            return '\n'.join(f'{i}. {msg}' for i, msg in enumerate(lines, 1))

        def on_done(text):
            dialog = TextDialog(self.house, '主动消息生成预览', text or '（未能生成预览消息）')
            dialog.exec()

        run_async(do_preview, on_done, lambda exc: toast(self.house, 'error', str(exc), '预览失败'), pool=LONG_POOL)


class BasicPage(Page):
    def __init__(self, house):

        super().__init__(house, 'basicPage')
        self.qq = CaptionLabel()
        save = PrimaryPushButton(tr('SaveBasic Info'))
        save.clicked.connect(self.save_basic)
        self.fields = {key: LineEdit() for key in ('nickname', 'age', 'birthday')}
        self.notes = PlainTextEdit()
        self.notes.setFixedHeight(130)
        fields = row(*(item for key, title in (('nickname', '称呼'), ('age', '年龄'), ('birthday', '生日'))
                       for item in (BodyLabel(title), self.fields[key])))
        # 2026-10-11 user (Settings): interests have their own fields; the notes are plain notes again
        self.interest_fields = {key: LineEdit() for key in ('interests', 'deep_interests')}
        self.interest_fields['interests'].setPlaceholderText('例如：原神、羽毛球、猫')
        self.interest_fields['deep_interests'].setPlaceholderText('例如：明朝历史、国内经济')
        self.add(Card('Basic Info', actions=(self.qq, save)).add(
            fields,
            row(BodyLabel('兴趣'), self.interest_fields['interests']),
            row(BodyLabel('深度兴趣'), self.interest_fields['deep_interests']),
            hint('用顿号或逗号分开。话题资料库按这里的兴趣和长期记忆里的兴趣联网整理；深度兴趣每个话题多找几篇、不过期。'),
            BodyLabel('备注'), self.notes))

    def load(self):
        house = self.house
        profile = load_profile(house.profile_path)
        self.qq.setText('QQ · ' + house.current_qq)
        info = profile.get('basic_info', {})
        for key, edit in {**self.fields, **self.interest_fields}.items():
            edit.setText(info.get(key, ''))
        self.notes.setPlainText(info.get('notes', ''))

    def save_basic(self):
        try:
            profile = load_profile(self.house.profile_path)
            old = profile.get('basic_info', {})
            interests = [old.get(key, '') for key in self.interest_fields]
            profile['basic_info'] = {key: edit.text().strip() for key, edit in {**self.fields, **self.interest_fields}.items()}
            profile['basic_info']['notes'] = self.notes.toPlainText().strip()
            write_profile(self.house.profile_path, profile)
            self.house.mark_stale('topicPage')
            toast(self.house, 'success', '当前好友的基本信息已保存。' + (
                '兴趣改了：话题资料库按原频率整理，想马上按新兴趣搜索请到「话题资料库」点「立即整理」。'
                if [profile['basic_info'][key] for key in self.interest_fields] != interests else ''))
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'SaveBasic Info')


class TopicPage(Page):
    """2026-10-11 user: per-friend topic library, web research at the frequencies set here, used by replies. Moved
    out of the basic-info page; its on/off switch is on the status page, its model settings on the model page."""

    def __init__(self, house):
        super().__init__(house, 'topicPage')
        self.interests, self.hours, self.run_status = body(), {}, {}
        library = self.add(Card('Topic Knowledge', collapsible=True, collapsed=True).add(
            hint('只给普通回复用：命中时自动带上，模型也能自己查；主动聊天一般不用。开关在「小屋状态」，整理用的思考档位在「模型设置」。'),
            self.interests))
        for kind, title in (('normal', '普通话题 · 每个话题1–2条，30天后过期'), ('deep', '深度话题 · 每个话题3–5条，不过期')):
            self.hours[kind] = LineEdit()
            self.hours[kind].setFixedWidth(80)
            save_hours, refresh = PushButton('保存频率'), PushButton('立即整理')
            save_hours.clicked.connect(lambda _=False, kind=kind: self.save_frequency(kind))
            refresh.clicked.connect(lambda _=False, kind=kind: self.request_refresh(kind))
            self.run_status[kind] = hint('')
            library.add(row(BodyLabel(title), None, BodyLabel('每'), self.hours[kind], CaptionLabel('小时（0＝只手动）'),
                             save_hours, refresh), self.run_status[kind])
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['话题', '标题', '发布', '整理于', '到期'])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column, width in ((0, 110), (2, 90), (3, 90), (4, 90)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(240)
        self.table.cellDoubleClicked.connect(self.show_item)
        remove = PushButton('删除选中资料')
        remove.clicked.connect(self.remove_item)
        library.add(self.table, row(hint('双击查看摘要和来源。'), None, remove))
        self.rows = []

    def load(self):
        profile = load_profile(self.house.profile_path)
        from topic_research import KINDS
        for kind, hours in KINDS.items():
            self.hours[kind].setText(str(profile.get('research_refresh_hours', {}).get(kind, hours)))
        self.refresh_library()

    def on_status(self):
        self.refresh_library()

    def refresh_library(self):
        import topic_research
        directory = self.house.friend_dir
        run_async(lambda _: (topic_research.state(directory), topic_research.items(directory)), self.show_library, quiet)

    def show_library(self, value):
        from datetime import datetime
        state, rows = value
        cached = state['interests']
        self.interests.setText(': ' + (', '.join(cached.get('topics', [])) or ' () ')
                               + ('; : ' + ', '.join(cached['deep']) if cached.get('deep') else ''))
        labels = {'running': '正在整理', 'ready': '已整理', 'failed': '失败', 'no_interests': '没有这类兴趣'}
        for kind, label in self.run_status.items():
            run = state[kind]
            text = labels.get(run.get('status'), '尚未整理')
            if run.get('at'):
                text += ' · ' + datetime.fromtimestamp(run['at']).strftime('%m-%d %H:%M')
            if run.get('topics'):
                text += ' · ' + ', '.join(run['topics']) + f"，新增 {run.get('added', 0)} 条"
            if run.get('error'):
                text += ' · ' + run['error']
            if run.get('next_refresh'):
                text += ' · 下次 ' + datetime.fromtimestamp(run['next_refresh']).strftime('%m-%d %H:%M')
            label.setText(text)
        if [row['id'] for row in rows] == [row['id'] for row in self.rows]:
            return
        self.rows = rows
        self.table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            for column, text in enumerate((item['topic'], item['title'], item['published'], item['fetched_at'][:10],
                                           item['expires_at'] or '—')):
                self.table.setItem(index, column, QTableWidgetItem(text))

    def show_item(self, index, _column=0):
        item = self.rows[index]
        TextDialog(self.house, item['title'], f"{item['summary']}\n\n来源：{item['url']}\n发布：{item['published']}"
                   f"\n检索词：{'、'.join(json.loads(item['keywords']))}", item['topic']).exec()

    def remove_item(self):
        import topic_research
        selected = self.table.selectedItems()
        if not selected:
            toast(self.house, 'warning', '请先在表格里选中一条资料。')
            return
        topic_research.remove(self.house.friend_dir, self.rows[selected[0].row()]['id'])
        self.rows = []
        self.refresh_library()

    def save_frequency(self, kind):
        try:
            from topic_research import refresh_hours
            value = self.hours[kind].text().strip()
            if not value.isdigit():
                raise ValueError('请输入整数小时；0表示只手动整理。')
            profile = load_profile(self.house.profile_path)
            profile.setdefault('research_refresh_hours', {})[kind] = int(value)
            refresh_hours(profile, kind)
            write_profile(self.house.profile_path, profile)
            toast(self.house, 'success', ('普通' if kind == 'normal' else '深度') + '话题的整理频率已保存。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '保存频率失败')

    def request_refresh(self, kind):
        import topic_research
        profile = load_profile(self.house.profile_path)
        if not profile.get('daily_research_enabled', True) or not profile.get('bridge_enabled', True):
            toast(self.house, 'warning', '请先在「小屋状态」开启当前好友和话题资料库。')
            return
        topic_research.request_refresh(self.house.friend_dir, kind)
        self.refresh_library()
        toast(self.house, 'success', '已请求整理；保持后台桥接运行，结果会自动显示。')


class StylePage(Page):
    """2026-10-11 user ( 4 ): persona, persona template, proactive style on one page with their order;
    the per-slot care instructions stay in the care table (they say what to care about, not how to speak)."""

    def __init__(self, house):
        super().__init__(house, 'stylePage')
        self.add(Card('谁说了算').add(hint(
            '① 人设：身份、关系、话题边界和安全底线，始终以它为准。\n'
            '② 人格模板：挂了模板时，句子长短、条数、语气词、笑声、表情和口头禅照模板来。\n'
            '③ 主动聊天风格：只管主动开场怎么说，优先于人设和模板的说法。\n'
            '④ 时段关心的指令（在「主动聊天」页）：只决定关心什么，语气仍按①②。')))

        self.style = PlainTextEdit()
        self.style.setMinimumHeight(320)
        save = PrimaryPushButton('保存人设')
        save.clicked.connect(self.save)
        self.add(Card('人设与回复风格', actions=(save,)).add(
            hint('QQ 聊天、QQ 任务接单与任务进展都沿用这里的人设和角色模板；工作台执行日志保留原始技术细节。'),
            self.style))

        # Editable categories written by the first-run wizard.  The generated
        # style above remains editable, but users can also revise the five
        # source blocks without hunting through friend.json.
        self.agent_name = LineEdit()
        self.agent_name.setPlaceholderText('例如：小助手 / 阿星；留空则显示当前智能体名称')
        save_agent_name = PushButton('保存名称')
        save_agent_name.clicked.connect(self.save_agent_name)
        self.prompt_presets = {}
        for key, label in (('identity', '身份'), ('tone', '说话方式'), ('habits', '聊天习惯'),
                           ('boundary', '边界'), ('relation', '关系')):
            edit = PlainTextEdit()
            edit.setFixedHeight(62)
            edit.setPlaceholderText('留空则不添加这一项')
            self.prompt_presets[key] = edit
        save_presets = PushButton('保存分类提示词')
        save_presets.clicked.connect(self.save_prompt_presets)
        quick_setup = PushButton('一键快速配置')
        quick_setup.clicked.connect(self.open_quick_setup)
        self.add(Card('快速配置提示词（可修改）', actions=(quick_setup, save_agent_name, save_presets)).add(
            hint('先填名字；分类提示词修改后会重新组合人设与回复风格。名字只影响发送者显示。'
                 '手动修改身份或关系后，停用旧组合的合成示例；重新快速配置可加载新组合。'),
            BodyLabel('名字'), self.agent_name,
            BodyLabel('身份'), self.prompt_presets['identity'],
            BodyLabel('说话方式'), self.prompt_presets['tone'],
            BodyLabel('聊天习惯'), self.prompt_presets['habits'],
            BodyLabel('边界'), self.prompt_presets['boundary'],
            BodyLabel('关系'), self.prompt_presets['relation']))

        self.continuation_choice = ComboBox()
        self.continuation_choice.addItems([f'{n} 次' for n in range(0, 11)])
        self.continuation_choice.setCurrentIndex(1)
        self.reply_bubble_choice = ComboBox()
        self.reply_bubble_choice.addItems([f'{n} 条' for n in range(1, 9)])
        self.reply_bubble_choice.setCurrentIndex(3)
        save_rhythm = PrimaryPushButton('保存回复节奏')
        save_rhythm.clicked.connect(self.save_reply_rhythm)
        self.rhythm_state = hint('续话次数默认 1 次；对方来信后自动刷新。')
        self.add(Card('交互与回复偏好 · 节奏', actions=(save_rhythm,)).add(
            hint('这里和「主动聊天」页、快速配置写的是同一组设置。'),
            row(BodyLabel('未回复时最多续话'), self.continuation_choice,
                BodyLabel('普通回复最多气泡'), self.reply_bubble_choice, None),
            self.rhythm_state))

        self.template = ComboBox()
        self.template.setMinimumWidth(360)
        apply = PrimaryPushButton('应用到当前好友')
        apply.clicked.connect(self.apply_template)
        self.template_status = body()
        self.add(Card('角色模板').add(
            row(BodyLabel('角色模板'), self.template, apply, None), self.template_status,
            hint('模板库的导入、导出、生成在「人格模板」页。选草稿时会自动确认。')))
        self.templates = {}

        self.proactive_style = PlainTextEdit()
        self.proactive_style.setFixedHeight(90)
        save_proactive = PushButton('保存主动聊天风格')
        save_proactive.clicked.connect(self.save_proactive_style)
        self.add(Card('主动聊天风格', actions=(save_proactive,)).add(
            hint('主动找对方聊天时开场的说法、亲近程度和接话方式；留空则按人设。'), self.proactive_style))

        # 2026-10-25 user: friend.json policies and persona switches that had no desktop editor
        self.policies = {}
        for key, placeholder in (
                ('emotion_policy', '例如：情绪随聊天自然变化；被冷落会有点失落但不抱怨；对方难过时先安慰再聊别的。'),
                ('timing_policy', '例如：深夜回复更短更轻；对方隔了很久才回时先接住，不追问为什么没回。'),
                ('memory_policy', '例如：自然提起对方说过的近况，不要一次翻出很多旧事；对方不想提的不再提。')):
            edit = PlainTextEdit()
            edit.setFixedHeight(76)
            edit.setPlaceholderText(placeholder)
            self.policies[key] = edit
        save_policies = PushButton('保存回复策略')
        save_policies.clicked.connect(self.save_policies)
        self.add(Card('回复策略（情绪 · 时机 · 记忆）', actions=(save_policies,)).add(
            BodyLabel('情绪策略'), self.policies['emotion_policy'],
            hint('填写后开启情绪：每次回复先判断对方情绪，并按人设维持自己的心情（记录在「情绪」页）。留空则关闭情绪判断。'),
            BodyLabel('时机策略'), self.policies['timing_policy'],
            hint('填写后，回复时会参考本机记录的收发时间（间隔多久、几点），按这里的说法调整。留空则不使用时间线索。'),
            BodyLabel('记忆策略'), self.policies['memory_policy'],
            hint('长期记忆始终会被检索；这里只规定怎么用、什么时候提。留空则按人设自然使用。')))

        self.flags = {key: CheckBox(label) for key, label in (
            ('persona_enabled', '人格检索'), ('hybrid_enabled', '混合检索'), ('sticker_metadata_enabled', '贴纸元数据'))}
        self.flags['sticker_metadata_enabled'].setEnabled(False)  # its retrieval path is not wired yet
        self.flag_state = body()
        apply_flags = PrimaryPushButton('应用到当前好友正式回复')
        apply_flags.clicked.connect(self.apply_feature_flags)
        from persona_workbench_host import TOOLS
        buttons = {}
        for tool, title in TOOLS.items():
            button = PushButton(title)
            button.clicked.connect(lambda _=False, tool=tool: self.open_workbench(tool))
            buttons[tool] = button

        self.template_state = body()
        self.add(Card('人格模板').add(
            hint('人格模板单独在这里管理：从聊天记录提炼人格包、查看/确认版本、做 A/B 对比。'
                 '模板文件存放在 data/persona_templates/<id>.json，好友只引用，不复制内容。'),
            row(buttons['jobs'], buttons['friend'], buttons['feedback'], None),
            self.template_state,
            hint('流程：人格提炼任务生成草稿 → 人格版本与示例确认 → 应用到当前好友正式回复。')))

        self.add(Card('检索增强', actions=(apply_flags,)).add(
            hint('知识库与检索诊断；勾选只是预览，点「应用到当前好友正式回复」才写入。'),
            row(buttons['knowledge'], buttons['retrieval'], None),
            row(*self.flags.values(), None), self.flag_state,
            hint('人格检索：回复时取用已确认的人格、示例与风格；混合检索：取用结构化资料、兴趣与知识条目。'
                 '数据或模型缺失时自动降级为原有上下文，不会中断回复。'
                 '贴纸元数据的检索路径尚未接入，暂不能开启。')))

    def load(self):
        profile = load_profile(self.house.profile_path)
        self.style.setPlainText(profile.get('style', ''))
        self.agent_name.setText(str(profile.get('agent_name') or ''))
        try:
            self.continuation_choice.setCurrentIndex(max(0, min(10, int(profile.get('continuation_max', 1) or 0))))
            self.reply_bubble_choice.setCurrentIndex(max(0, min(7, int(profile.get('reply_bubbles_max', 4) or 4)) - 1))
        except (TypeError, ValueError):
            self.continuation_choice.setCurrentIndex(1)
            self.reply_bubble_choice.setCurrentIndex(3)
        self.refresh_rhythm_state()
        presets = profile.get('prompt_presets') or {}
        for key, edit in self.prompt_presets.items():
            edit.setPlainText(str(presets.get(key) or '') if isinstance(presets, dict) else '')
        self.proactive_style.setPlainText(profile.get('proactive', {}).get('style', ''))
        self.fill_templates(profile.get('persona_template'))
        try:
            from persona_template import active, list_templates
            current = active(profile)
            all_templates = list_templates()
            if current:
                self.template_state.setText(
                    f"当前启用：{current.get('name', current.get('id'))} · {current.get('status')} · "
                    f"{len(all_templates)} 个模板")
            else:
                self.template_state.setText(f"当前未启用模板 · 共 {len(all_templates)} 个模板")
        except Exception:
            self.template_state.setText('模板状态暂不可用')
        for key, edit in self.policies.items():
            edit.setPlainText(profile.get(key, '') if isinstance(profile.get(key), str) else '')
        flags = profile.get('feature_flags') or {}
        for key, box in self.flags.items():
            box.setChecked(flags.get(key) is True)
        enabled = [box.text() for key, box in self.flags.items() if flags.get(key) is True]
        self.flag_state.setText('当前正式回复：' + ('、'.join(enabled) if enabled else '未开启检索增强'))

    def refresh_rhythm_state(self):
        try:
            from store import FriendStore
            store = FriendStore(self.house.friend_dir)
            try:
                plan = json.loads(store.get('chat_continuation') or '{}')
            finally:
                store.close()
            limit = self.continuation_choice.currentIndex()
            sent = int(plan.get('sent_count', 0))
            state = {'pending': '等待判断', 'waiting': '已判断，等下次', 'ended': '本次已结束',
                     'processing': '判断中', 'failed': '上次失败'}.get(plan.get('state'), '没有进行中的续话')
            self.rhythm_state.setText(f'当前续话状态：已续 {sent}/{limit} 次 · {state}。收到来信后自动刷新。')
        except Exception:
            pass

    def save_reply_rhythm(self):
        try:
            profile = load_profile(self.house.profile_path)
            profile['continuation_max'] = int(self.continuation_choice.currentIndex())
            profile['reply_bubbles_max'] = int(self.reply_bubble_choice.currentIndex()) + 1
            write_profile(self.house.profile_path, profile)
            self.rhythm_state.setText(f"已保存：续话 {profile['continuation_max']} 次，普通回复最多 {profile['reply_bubbles_max']} 条气泡。")
            toast(self.house, 'success', '回复节奏已保存；下次回复和续话判断生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '保存回复节奏失败')

    def open_quick_setup(self):
        from .quick_setup import QuickSetupDialog
        if not self.house.current_qq:
            return
        dialog = QuickSetupDialog(self.house, self.house.current_qq, self)
        if dialog.exec():
            self.load()
            self.house.refresh_friend_mode()
            toast(self.house, 'success', '快速配置已重新应用；名字和回复策略已同步。')

    def save_agent_name(self):
        try:
            profile = load_profile(self.house.profile_path)
            profile['agent_name'] = self.agent_name.text().strip()[:24]
            write_profile(self.house.profile_path, profile)
            toast(self.house, 'success', '智能体显示名称已保存；聊天窗口会立即使用这个名称。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '保存名称失败')

    def save_prompt_presets(self):
        try:
            values = {key: edit.toPlainText().strip() for key, edit in self.prompt_presets.items()}
            if not any(values.values()):
                raise ValueError('至少填写一项提示词')
            profile = load_profile(self.house.profile_path)
            previous = profile.get('prompt_presets') if isinstance(profile.get('prompt_presets'), dict) else {}
            prepared = previous.get('prepared_prompt') if isinstance(previous.get('prepared_prompt'), str) else ''
            profile['prompt_presets'] = values
            profile['persona'] = values.get('identity', '')[:300]
            basic = dict(profile.get('basic_info') or {})
            relation = values.get('relation', '') or '你们是普通朋友。'
            if prepared:
                style = (prepared + "\n\n【用户补充】\n"
                         f"说话方式：{values.get('tone', '')}\n"
                         f"聊天习惯：{values.get('habits', '')}\n"
                         f"边界：{values.get('boundary', '')}\n"
                         f"关系：{relation}")
                values.update(prepared_prompt=prepared,
                              prepared_key=previous.get('prepared_key', ''),
                              examples_enabled=(previous.get('examples_enabled', True) and
                                  values.get('relation') == previous.get('relation') and
                                  values.get('identity') == previous.get('identity')),
                              style_version=3)
            else:
                style = (f"【说话方式】{values.get('tone', '')}\n"
                         f"【聊天习惯】{values.get('habits', '')}\n"
                         f"【边界】{values.get('boundary', '')}\n"
                         f"【关系与身份】{relation}对方性别：{basic.get('friend_gender', '不透露')}；"
                         f"你的身份性别：{basic.get('assistant_gender', '中性')}。")
            profile['style'] = style[:6000]
            profile['agent_name'] = self.agent_name.text().strip()[:24]
            write_profile(self.house.profile_path, profile)
            self.style.setPlainText(profile['style'])
            toast(self.house, 'success', '分类提示词已保存；人设与回复风格已同步更新。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '保存提示词失败')

    def save_policies(self):
        try:
            profile = load_profile(self.house.profile_path)
            for key, edit in self.policies.items():
                text = edit.toPlainText().strip()
                if text:
                    profile[key] = text
                else:
                    profile.pop(key, None)
            write_profile(self.house.profile_path, profile)
            self.house.mark_stale('emotionPage')
            toast(self.house, 'success', '回复策略已保存；下一条回复生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save failed')

    def apply_feature_flags(self):
        from source_state import source_lock
        from persona_types import FriendScope
        try:
            directory = self.house.profile_path.parent
            with source_lock(directory):
                FriendScope.load(self.house.current_qq, project_root=ROOT, friend_dir=directory)  # identity check
                profile = load_profile(self.house.profile_path)
                if profile.get('qq_id') != self.house.current_qq:
                    raise ValueError('好友身份不一致，未保存')
                flags = dict(profile.get('feature_flags') or {})
                flags.update({key: box.isChecked() for key, box in self.flags.items() if box.isEnabled()})
                profile['feature_flags'] = flags
                write_profile(self.house.profile_path, profile)
            self.load()
            toast(self.house, 'success', '检索增强开关已应用到当前好友正式回复。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '应用失败')

    def open_workbench(self, tool):
        from persona_workbench_host import TOOLS, launch
        try:
            if not launch(tool, self.house.current_qq, ROOT):
                toast(self.house, 'info', f'{TOOLS[tool]}窗口已经打开。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), f'无法打开{TOOLS[tool]}')

    def fill_templates(self, current):
        from persona_template import list_templates
        self.templates = {'不使用模板': None, **{
            f"{t['name']} · {t['source']['speaker']}{'' if t['status'] == 'active' else '（草稿）'}": t['id']
            for t in list_templates()}}
        self.template.clear()
        self.template.addItems(list(self.templates))
        label = next((label for label, value in self.templates.items() if value == current), '不使用模板')
        self.template.setCurrentText(label)
        self.template_status.setText(f'当前好友正在使用：{label}')

    def apply_template(self):
        from persona_template import confirm as confirm_template, load
        template_id = self.templates.get(self.template.currentText())
        try:
            if template_id and load(template_id).get('status') != 'active':
                confirm_template(template_id)  # choosing a draft for a friend is its confirmation
            profile = load_profile(self.house.profile_path)
            if template_id:
                profile['persona_template'] = template_id
            else:
                profile.pop('persona_template', None)
            write_profile(self.house.profile_path, profile)
            chosen = self.template.currentText()
            self.fill_templates(template_id)
            self.house.mark_stale('templatePage')
            toast(self.house, 'success', f'已应用到当前好友：{chosen}，下一条回复生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '应用失败')

    def save_proactive_style(self):
        try:
            profile = load_profile(self.house.profile_path)
            profile.setdefault('proactive', {})['style'] = self.proactive_style.toPlainText().strip()
            write_profile(self.house.profile_path, profile)
            toast(self.house, 'success', '主动聊天风格已保存；下次主动聊天生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save failed')

    def save(self):
        try:
            profile = load_profile(self.house.profile_path)
            profile['style'] = self.style.toPlainText().strip()
            write_profile(self.house.profile_path, profile)
            toast(self.house, 'success', '人设已保存；下次生成消息时生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save failed')


class ModelPage(Page):
    def __init__(self, house):
        super().__init__(house, 'modelPage')
        self._section = 'sources'
        self._section_cards = {}
        self.options = model_options()
        self.dsh = CheckBox('DeepSeek Harness')
        self.claude = CheckBox('Claude Code')
        self.codex = CheckBox('Codex CLI')
        self.antigravity = CheckBox('Antigravity CLI')
        self.deepseek = CheckBox('自定义 API')
        self.webchat = CheckBox('网页端 · DeepSeek')
        self.webchat.setToolTip('用浏览器里已登录的 DeepSeek 网页对话；不跑本地模型，省额度。')
        self.webchat_gpt = CheckBox('网页端 · ChatGPT')
        self.webchat_gpt.setToolTip('用浏览器里已登录的 ChatGPT 网页对话；不跑本地模型，省额度。')
        self.webchat_gemini = CheckBox('网页端 · Gemini')
        self.webchat_gemini.setToolTip('用浏览器里已登录的 Gemini 网页对话；不跑本地模型，省额度。')
        from agent_tool_adapters import AGENT_BACKENDS, BUILTINS
        self.external_checks = {key: CheckBox('自定义智能体（ACP）' if key == 'acp' else BUILTINS[key].name)
                                for key in AGENT_BACKENDS}

        # Handover priority order & multi-agent controls
        self.handover_list = ListWidget()
        self.handover_list.setFixedHeight(140)
        self.btn_order_up = PushButton('上移 🔼')
        self.btn_order_up.clicked.connect(self.move_order_up)
        self.btn_order_down = PushButton('下移 🔽')
        self.btn_order_down.clicked.connect(self.move_order_down)

        self.master_agent_combo = ComboBox()
        self.master_agent_combo.setMinimumWidth(260)
        self.master_agent_combo.currentIndexChanged.connect(self.on_master_agent_changed)

        self.quota_guard_chk = CheckBox('额度守护：额度用完或剩余 ≤ 5% 时自动顺序交接给下一个智能体')
        self.quota_guard_chk.setChecked(True)

        self.capability_route_chk = CheckBox('能力自省与自主委托：干不了的活自动寻找具备能力的智能体（如联网检索/执行终端命令）')
        self.capability_route_chk.setChecked(True)

        self.claude_english_chk = CheckBox('英文协议对接：向 Claude Code 交接时自动转为高精度标准英文协议')
        self.claude_english_chk.setChecked(True)

        self.collab_mode_hint = hint('')

        for btn in (self.dsh, self.claude, self.codex, self.antigravity, self.deepseek, self.webchat, self.webchat_gpt, self.webchat_gemini, *self.external_checks.values()):
            btn.stateChanged.connect(self.on_provider_selection_changed)

        # DeepSeek Harness
        self.dsh_path = LineEdit()
        self.dsh_path.setPlaceholderText('系统默认 (D:\\dsh\\resources\\runtime\\cli\\bin\\dsh.cmd 或 PATH 中 dsh)')
        browse_dsh = PushButton('选择路径…')
        browse_dsh.clicked.connect(lambda: self.browse_executable(self.dsh_path, '选择 DeepSeek Harness 可执行文件'))
        self.dsh_url = LineEdit()
        self.dsh_url.setPlaceholderText('https://api.deepseek.com')
        self.dsh_model = EditableComboBox()
        self.dsh_model.addItems(PROVIDER_DEFAULTS['dsh']['models'])
        self.dsh_model.setMinimumWidth(240)
        self.dsh_effort = ComboBox()
        self.dsh_effort.addItems(PROVIDER_DEFAULTS['dsh']['efforts'])
        self.dsh_effort.setMinimumWidth(110)
        self.dsh_key = PasswordLineEdit()
        self.dsh_key.setPlaceholderText('使用本机桌面客户端账号额度可留空；亦可填写 API Key')
        self.dsh_key_state = hint('')
        fetch_dsh = PushButton('提取可用模型')
        fetch_dsh.clicked.connect(self.fetch_dsh_models)
        test_dsh = PushButton('测试 DeepSeek Harness 连接')
        test_dsh.clicked.connect(self.test_dsh)
        prepare_dsh = PushButton('一键安装/打开')
        prepare_dsh.clicked.connect(lambda _=False: self.prepare_agent('dsh'))
        self.dsh_card = self.add(Card('DeepSeek Harness', actions=(prepare_dsh, test_dsh)).add(
            row(BodyLabel('程序路径'), self.dsh_path, browse_dsh),
            row(BodyLabel('模型'), self.dsh_model, fetch_dsh, BodyLabel('思考档位'), self.dsh_effort, None),
            row(BodyLabel('接口地址'), self.dsh_url),
            row(BodyLabel('API Key（可选）'), self.dsh_key, None),
            self.dsh_key_state,
            hint('优先调用本机已安装的 DeepSeek Harness 桌面客户端环境与账号会话，直接消耗客户端账号额度；无需购买或配置额外 API Key 即可本地原生调用。亦支持填入 API Key 走云端兼容接口。')))

        self.ag_model, self.ag_effort = EditableComboBox(), ComboBox()
        self.ag_model.addItems(list(AG_MODELS))
        self.ag_model.setMinimumWidth(230)
        self.ag_effort.addItems(list(AG_EFFORTS))
        self.ag_path = LineEdit()
        self.ag_path.setPlaceholderText('系统默认 (tools/antigravity-cli 或 PATH 中 agy)')
        browse_ag = PushButton('选择路径…')
        browse_ag.clicked.connect(lambda: self.browse_executable(self.ag_path, '选择 Antigravity CLI (agy) 可执行文件'))
        fetch_ag = PushButton('提取可用模型')
        fetch_ag.clicked.connect(self.fetch_antigravity_models)
        test_ag = PushButton('测试 Antigravity CLI')
        test_ag.clicked.connect(self.test_antigravity)

        self.codex_path = LineEdit()
        self.codex_path.setPlaceholderText('系统默认 (PATH 中 codex 或常见目录)')
        browse_codex = PushButton('选择路径…')
        browse_codex.clicked.connect(lambda: self.browse_executable(self.codex_path, '选择 Codex 可执行文件'))
        fetch_codex = PushButton('提取可用模型')
        fetch_codex.clicked.connect(self.fetch_codex_models)
        test_codex = PushButton('测试 Codex CLI')
        test_codex.clicked.connect(self.test_codex)

        self.claude_path = LineEdit()
        self.claude_path.setPlaceholderText('系统默认 (PATH 中 claude 或常见目录)')
        browse_claude = PushButton('选择路径…')
        browse_claude.clicked.connect(lambda: self.browse_executable(self.claude_path, '选择 Claude Code 可执行文件'))
        fetch_claude = PushButton('提取可用模型')
        fetch_claude.clicked.connect(self.fetch_claude_models)
        test_claude = PushButton('测试 Claude Code')
        test_claude.clicked.connect(self.test_claude)
        self.claude_model, self.claude_effort = EditableComboBox(), ComboBox()
        self.claude_model.addItems(list(CLAUDE_MODELS))
        self.claude_model.setMinimumWidth(210)
        self.claude_effort.addItems(list(CLAUDE_EFFORTS_DISPLAY))
        self.claude_effort.setMinimumWidth(130)
        self.codex_model, self.effort, self.fast = ComboBox(), ComboBox(), CheckBox('Fast 快速模式')
        self.codex_model.addItems(list(self.options))
        self.codex_model.setMinimumWidth(180)
        self.effort.setMinimumWidth(110)
        self.codex_model.currentTextChanged.connect(self.update_efforts)
        self.local_fallback = CheckBox('额度用完或调用异常时改用本地模型兜底（同一时间只能有一位好友开启）')
        self.local_thinking = ComboBox()
        self.local_thinking.addItems(list(LOCAL_THINKING))
        # 2026-10-12 user: custom APIs configured like DeepSeek Harness (protocol, base URL, model, key)
        self.protocol, self.api_effort = ComboBox(), ComboBox()
        self.protocol.addItems(list(PROTOCOLS))
        self.protocol.setMinimumWidth(190)
        self.api_effort.addItems(list(EFFORTS))
        self.base_url, self.deepseek_model = LineEdit(), LineEdit()
        self.base_url.setPlaceholderText(DEFAULT_BASE_URL)
        self.base_url.setMinimumWidth(320)
        self.deepseek_model.setPlaceholderText(MODELS[0])
        self.deepseek_model.setMinimumWidth(220)
        fetch_api = PushButton('提取可用模型')
        fetch_api.clicked.connect(self.fetch_api_models)
        self.compact = LineEdit()
        self.compact.setPlaceholderText(str(CONTEXT_TOKENS))
        self.compact_info = hint('')
        self.compact.setMinimumWidth(160)
        self.key = PasswordLineEdit()
        self.key.setPlaceholderText('留空则保留已保存的 Key')
        self.key_state = hint('')
        save = PrimaryPushButton("保存设置")
        order_btns = QVBoxLayout()
        order_btns.setSpacing(6)
        order_btns.addWidget(self.btn_order_up)
        order_btns.addWidget(self.btn_order_down)
        order_btns.addStretch(1)

        order_box = QWidget()
        order_h = QHBoxLayout(order_box)
        order_h.setContentsMargins(0, 4, 0, 4)
        order_h.addWidget(self.handover_list, 1)
        order_h.addLayout(order_btns)

        web_ds = PushButton('登录')
        web_ds.setToolTip('打开 DeepSeek 网页登录')
        web_ds.clicked.connect(lambda _=False: self.prepare_agent('webchat_deepseek'))
        web_gpt = PushButton('登录')
        web_gpt.setToolTip('打开 ChatGPT 网页登录')
        web_gpt.clicked.connect(lambda _=False: self.prepare_agent('webchat_chatgpt'))
        web_gem = PushButton('登录')
        web_gem.setToolTip('打开 Gemini 网页登录')
        web_gem.clicked.connect(lambda _=False: self.prepare_agent('webchat_gemini'))

        flow_button = PushButton('配置协作流程')
        flow_button.clicked.connect(lambda: self.house.switchTo(self.house.pages['cooperationFlowPage']))
        self.master_agent_combo.setParent(self)
        self.master_agent_combo.hide()
        order_box.setParent(self)
        order_box.hide()
        self.sources_card = self.add(Card('模型来源与智能体设置', actions=(save,)).add(
            hint('这里选择当前好友可使用的智能体，并保留各自模型与连接配置。节点关系和分工请在“协作流程”中编辑。'),
            row(self.dsh, self.claude, self.codex, self.antigravity,
                self.external_checks['opencode'], None, spacing=16),
            row(self.deepseek, self.webchat, web_ds, self.webchat_gpt, web_gpt, self.webchat_gemini, web_gem,
                None, spacing=10),
            row(*[button for key, button in self.external_checks.items() if key != 'opencode'], None, spacing=16),
            row(flow_button, None),
            self.quota_guard_chk,
            self.capability_route_chk,
            self.claude_english_chk,
            self.collab_mode_hint))
        from .background_model_settings import BackgroundModelSettings
        self.background_settings = self.add(BackgroundModelSettings(house, self.PROVIDER_TITLES))
        # Web providers need their own model/thinking controls. Keep the DSH
        # detail card below the source chooser instead of above it.
        import web_chat as _web_chat
        self.web_model_fields, self.web_effort_fields = {}, {}
        self.web_cards, self.web_agent_fields, self.web_context_fields = {}, {}, {}
        for key, title, source in (
                ('webchat_deepseek', '网页端 · DeepSeek', 'deepseek'),
                ('webchat_chatgpt', '网页端 · ChatGPT', 'chatgpt'),
                ('webchat_gemini', '网页端 · Gemini', 'gemini')):
            model = EditableComboBox()
            model.addItems(_web_chat.models_for(source))
            model.setMinimumWidth(220)
            effort = ComboBox()
            effort.addItems(_web_chat.efforts_for(source))
            effort.setMinimumWidth(120)
            login = PushButton('登录')
            login.clicked.connect(lambda _=False, k=key: self.prepare_agent(k))
            fetch = PushButton('恢复可用模型')
            fetch.setToolTip('网页模型由站点控制，无法可靠枚举；这里恢复程序内置的已知名称，仍可手动输入。')
            fetch.clicked.connect(lambda _=False, k=key, s=source: self.restore_web_models(k, s))
            agent = CheckBox('Agent 模式')
            context = ComboBox()
            for label, value in (('32K 上下文', 32_000), ('64K 上下文', 64_000), ('128K 上下文', 128_000)):
                context.addItem(label, userData=value)
            context.setMinimumWidth(140)
            self.web_model_fields[key] = model
            self.web_effort_fields[key] = effort
            self.web_agent_fields[key] = agent
            self.web_context_fields[key] = context
            self.web_cards[key] = self.add(Card(title, actions=(login, fetch)).add(
                row(BodyLabel('模型'), model, BodyLabel('思考强度'), effort, None),
                row(agent, BodyLabel('Agent 上下文'), context, None),
                hint('使用浏览器里已登录的网页账号；程序发送前会尝试点选同名模型/强度，点不到不影响问答。'
                     'Agent 模式会循环调用当前好友的 desk_* 工作区工具；上下文长度用于裁剪历史与工具结果。')))
        self.column.removeWidget(self.dsh_card)
        self.column.insertWidget(self.column.indexOf(self.web_cards['webchat_gemini']) + 1, self.dsh_card)
        prepare_codex = PushButton('一键安装/打开')
        prepare_codex.clicked.connect(lambda _=False: self.prepare_agent('codex'))
        self.codex_card = self.add(Card('Codex CLI', actions=(prepare_codex,)).add(
            row(BodyLabel('程序路径'), self.codex_path, browse_codex),
            row(BodyLabel('模型'), self.codex_model, fetch_codex, BodyLabel('思考档位'), self.effort, self.fast, None, test_codex),
            hint('使用本机 Codex CLI 账号。点击「提取可用模型」直接从 CLI 同步当前生效模型与思考档位。Fast 会增加用量。')))
        prepare_claude = PushButton('一键安装/打开')
        prepare_claude.clicked.connect(lambda _=False: self.prepare_agent('claude'))
        self.claude_card = self.add(Card('Claude Code', actions=(prepare_claude,)).add(
            row(BodyLabel('程序路径'), self.claude_path, browse_claude),
            row(BodyLabel('模型'), self.claude_model, fetch_claude, BodyLabel('思考档位'), self.claude_effort, None, test_claude),
            hint('使用 Claude Code 原生终端与配置（data/claude-home）。点击「提取可用模型」可同步已支持的官方模型与别名。')))
        prepare_ag = PushButton('一键安装/打开')
        prepare_ag.clicked.connect(lambda _=False: self.prepare_agent('antigravity'))
        self.ag_card = self.add(Card('Antigravity CLI', actions=(prepare_ag,)).add(
            row(BodyLabel('程序路径'), self.ag_path, browse_ag),
            row(BodyLabel('模型'), self.ag_model, fetch_ag, BodyLabel('思考档位'), self.ag_effort, None, test_ag),
            hint('使用官方 agy CLI。点击「提取可用模型」可直接从 agy models 同步当前账号实际开通的可用模型。')))
        from .adapter_settings import AdapterSettings
        self.adapter_settings = AdapterSettings(self)
        self.api_model_section, self.api_retrieval_section, self.api_function_section = (
            RadioButton('模型API'), RadioButton('检索API'), RadioButton('功能API'))
        api_group = QButtonGroup(self)
        for button in (self.api_model_section, self.api_retrieval_section, self.api_function_section):
            api_group.addButton(button)
        self.api_model_section.setChecked(True)
        self._section = 'api'
        self.api_nav = self.add(Card('API Settings').add(
            row(self.api_model_section, self.api_retrieval_section, self.api_function_section, None)))
        self.api_profile = ComboBox()
        self.api_profiles, self.profile_ids, self.chat_keys = {}, [], {}
        self.profile_id = None
        self.api_preset = ComboBox()
        for preset_id, (label, _, _, _) in PRESETS.items():
            self.api_preset.addItem(label, userData=preset_id)
        self.api_preset.currentIndexChanged.connect(self.use_api_preset)
        add_api = PushButton('新增连接')
        add_api.clicked.connect(self.add_api_profile)
        rename_api = PushButton('重命名')
        rename_api.clicked.connect(self.rename_api_profile)
        delete_api = PushButton('删除连接')
        delete_api.clicked.connect(self.delete_api_profile)
        self.api_profile.currentIndexChanged.connect(self.change_api_profile)
        test_api = PushButton('测试当前连接')
        test_api.clicked.connect(self.test_api)
        self.chat_api_card = self.add(Card('外部模型 API · 多连接管理', actions=(test_api,)).add(
            hint('每个连接独立保存 Base URL、协议、模型和 Windows 加密 Key；切换连接不会共用密钥。'),
            row(BodyLabel('当前连接'), self.api_profile, add_api, rename_api, delete_api),
            row(BodyLabel('兼容协议预设'), self.api_preset, None),
            row(BodyLabel('协议'), self.protocol, BodyLabel('思考档位'), self.api_effort, None),
            row(BodyLabel(tr('Base URL')), self.base_url), row(BodyLabel('模型'), self.deepseek_model, fetch_api),
            hint('支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 兼容接口。'
                 '不同服务可保存不同地址、模型和密钥，不局限于 DeepSeek。点击「提取可用模型」可从接口 /v1/models 读取列表。'),
            row(BodyLabel('通用接口密钥'), self.key, None), self.key_state))
        from .api_panels import APIPanels
        self.service_apis = APIPanels(house)
        self.add(self.service_apis.retrieval)
        self.add(self.service_apis.function)
        self._section = 'local'
        self.add(Card('上下文', collapsible=True, collapsed=True).add(row(BodyLabel('上下文窗口（token）'), self.compact, None), self.compact_info,
            hint('上下文窗口按当前智能体/API 的兼容上限自动取值；达到 90% 自动压缩，并把重要内容整理到长期记忆。')))
        self.add(Card('本地模型兜底', collapsible=True, collapsed=True).add(
            self.local_fallback,
            row(BodyLabel('本地兜底模型思考档位'), self.local_thinking, None),
            hint('无论当前使用 Codex CLI、Claude Code、Antigravity 还是各类 API，当额度耗尽、连接超时或接口异常时，均自动无缝降级为本地模型继续回复当前好友。')))
        from house_settings import local_models_enabled
        self.local_models = SwitchButton('启用已安装的本地模型')
        self.local_models.setChecked(local_models_enabled(ROOT))
        self.local_models.checkedChanged.connect(self.save_local_models)
        house_settings.subscribe_local_models(self.mirror_local_models_state)
        # 2026-10-26: which installed retrieval models the house uses (retrieval_core); 自动 picks by task, language,
        # text amount and free GPU memory. A friend's own retrieval API setting still wins for that friend.
        self.retrieval_embedding, self.retrieval_rerank = ComboBox(), ComboBox()
        self.refresh_retrieval_models()
        self.retrieval_embedding.currentIndexChanged.connect(lambda *_: self.save_retrieval_models())
        self.retrieval_rerank.currentIndexChanged.connect(lambda *_: self.save_retrieval_models())
        self.retrieval_state = CaptionLabel()
        self.retrieval_state.setWordWrap(True)
        self.add(Card('本地模型与程序降级', collapsible=True, collapsed=True).add(self.local_models,
            hint('下载完成后会自动启用；也可在这里手动关闭。关闭后：消息/场景用规则分类；记忆和资料用关键词/BM25；'
                 '配置检索 API 后可用云端向量与重排。摘要、资料整理继续用已配置的云端模型。'),
            hint('向量模型、重排模型和本地大模型的实际选择统一放在下方「生效模型选择」卡片，避免两处重复配置。'),
            self.retrieval_state,
            hint('人格分析可选程序统计或云端模型。没有任何可用云端模型时不能生成自由聊天；'
                 '当前云端额度用尽会暂停生成。开关改变后重启后台可释放已加载的本地模型。')))

        # ----------  ----------
        self.dl_category = ComboBox()
        self.dl_category.addItem('向量模型 (Embedding · 检索/记忆/表情)', userData='embedding')
        self.dl_category.addItem('重排模型 (Reranker · 候选精确打分)', userData='rerank')
        self.dl_category.addItem('本地大模型 (LLM · 兜底聊天与消息分析)', userData='llm')
        self.dl_category.addItem('语音模型 (TTS · QQ 语音回复)', userData='tts')
        self.dl_category.addItem('语音识别 (ASR · 听懂好友语音)', userData='asr')
        self.dl_category.setMinimumWidth(300)

        self.dl_tier = ComboBox()
        self.dl_tier.setMinimumWidth(380)

        self.dl_mirror = ComboBox()
        self.dl_mirror.addItem('国内高速镜像 (hf-mirror.com)', userData='https://hf-mirror.com')
        self.dl_mirror.addItem('HuggingFace 官方 (huggingface.co)', userData='https://huggingface.co')
        self.dl_mirror.setMinimumWidth(210)

        self.dl_btn = PrimaryPushButton('一键自动下载并装配')
        self.dl_btn.clicked.connect(self.start_download_model)
        self.dl_bg_btn = PushButton('后台下载')
        self.dl_bg_btn.clicked.connect(lambda: self.start_download_model(background=True))

        self.dl_status_label = BodyLabel('')
        self.dl_desc = hint('')

        self.dl_progress = ProgressBar()
        self.dl_progress.setValue(0)
        self.dl_progress.hide()
        self.dl_progress_text = CaptionLabel('')

        self.dl_category.currentIndexChanged.connect(self.update_download_tiers)
        self.dl_tier.currentIndexChanged.connect(self.update_download_info)

# Note
        self.model_status_table = TableWidget()
        self.model_status_table.setColumnCount(6)
        self.model_status_table.setHorizontalHeaderLabels(['模型类别', '档位', '具体模型型号', '规格与体积', '安装状态', '装配目录'])
        self.model_status_table.verticalHeader().hide()
        self.model_status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model_status_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_status_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.model_status_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for col, width in ((0, 130), (1, 65), (3, 140), (4, 110), (5, 200)):
            self.model_status_table.setColumnWidth(col, width)
        self.model_status_table.setMinimumHeight(340)
        self.model_status_table.itemClicked.connect(self.on_model_table_row_clicked)

        refresh_table_btn = PushButton('刷新安装状态')
        refresh_table_btn.clicked.connect(self.refresh_all_model_views)

        download_card = Card('本地模型自动下载与装配', actions=(refresh_table_btn,), collapsible=True).add(
            row(BodyLabel('模型类别'), self.dl_category, BodyLabel('权重档位'), self.dl_tier, None),
            row(BodyLabel('下载节点'), self.dl_mirror, None, self.dl_btn, self.dl_bg_btn),
            self.dl_status_label,
            self.dl_desc,
            self.dl_progress,
            self.dl_progress_text,
            hint('所有种类模型具体安装情况一览（点击任意一行可快速切换至对应下载项）：'),
            self.model_status_table,
            # 2026-10-26 user: "下载的模型应该都是共用的" — one copy on disk, the choice is per friend.
            hint('模型文件只保存一份，所有好友共用：任何一位好友下载后，其他好友都能直接选用，不会重复下载。'
                 '下方「生效模型选择」按好友分别保存，所以不同好友可以用不同的模型。'),
            hint('系统按模型种类分别提供轻量、中量、旗舰三档权重。'
                 '点击后自动多线程下载并直接装配到对应目录结构，自动检测并识别，无需手动解压或放置。')
        )
        self.add(download_card)

        # ----------  ( / API)  ----------
        self.active_embedding = ComboBox()
        self.active_embedding.setMinimumWidth(320)
        self.active_rerank = ComboBox()
        self.active_rerank.setMinimumWidth(320)
        self.active_llm = ComboBox()
        self.active_llm.setMinimumWidth(320)

        self.local_device = ComboBox()
        self.local_device.addItem('自动检测 (CUDA → Apple MPS / Metal → CPU)', userData='auto')
        self.local_device.addItem('强制使用 GPU 加速 (CUDA · 极速计算，需独立显卡)', userData='cuda')
        self.local_device.addItem('强制使用苹果 GPU (PyTorch MPS / llama.cpp Metal)', userData='mps')
        self.local_device.addItem('AMD DirectML（需 torch-directml，仅 Python 3.11/3.12）', userData='directml')
        self.local_device.addItem('强制使用 CPU 运行 (内存计算，零占用显卡显存)', userData='cpu')
        self.local_device.setMinimumWidth(320)

        apply_active_btn = PushButton('应用生效模型选择')
        apply_active_btn.clicked.connect(self.apply_active_models)

        active_card = Card('生效模型选择（本地模型 / 云端API）', actions=(apply_active_btn,), collapsible=True).add(
            row(BodyLabel('向量模型'), self.active_embedding, None),
            row(BodyLabel('重排模型'), self.active_rerank, None),
            row(BodyLabel('本地大模型'), self.active_llm, None),
            row(BodyLabel('运行硬件'), self.local_device, None),
            hint('每个模块均可在云端 API 与已下载的本地模型间自由选用与切换：'
                 '选择「云端API」将调用上方 API 设置中的检索接口；选择「本地模型」将优先调用本机权重。'),
            hint('「运行硬件」同时对本地大模型（LLM）、向量模型与重排模型生效：'
                 '自动模式按 CUDA、MPS / Metal、DirectML、CPU 依次选择；手动模式失败不切换。DirectML 需要 Windows AMD 显卡和 torch-directml（Python 3.11/3.12）；本地大模型 llama.cpp 不支持 DirectML。已加载的模型需卸载或重启后应用设备选择。')
        )
        self.add(active_card)

        # 2026-10-28: generated image/video sending, with the same context/frequency
        # controls as stickers and voice. A real file is still required.
        self.media_mode = ComboBox()
        self.media_mode.addItem('关闭（用户明确要求时才发）', userData='off')
        self.media_mode.addItem('偶尔主动发（按比例）', userData='sometimes')
        self.media_mode.addItem('合适语境都可以主动发', userData='always')
        self.media_mode.setMinimumWidth(240)
        self.media_ratio = QSpinBox()
        self.media_ratio.setRange(0, 100)
        self.media_ratio.setSuffix('%')
        self.media_ratio.setMinimumWidth(100)
        self.media_proactive = CheckBox('允许在合适语境主动发梗图/情绪图')
        save_media = PushButton('保存图片/视频设置')
        save_media.clicked.connect(self.save_media_settings)
        self.add(Card('图片/视频回复（生成后发送）', actions=(save_media,), collapsible=True).add(
            row(BodyLabel('发送方式'), self.media_mode, BodyLabel('主动频率'), self.media_ratio, None),
            self.media_proactive,
            hint('适用于 Codex / Antigravity 等真正能生成图片/视频的 Agent：生成后保存到当前好友的 '
                 'media/generated/，回复中用 [[LOCAL_IMAGE:...]] / [[LOCAL_VIDEO:...]] 作为一条气泡发出。'
                 '模型会返回 media=yes/no/uncertain；yes 直接发，no 不发，uncertain 才按这里的概率决定。'
                 '频率只限制“主动发梗图/情绪图”，用户明确要图时不受限制；没有真实文件不会伪造。'
                 '模型会结合当前话题和情绪判断，不合适就不发。')))

        # 2026-10-25 user: local voice model so replies can go out as QQ voice messages
        self.voice_mode = ComboBox()
        self.voice_mode.addItem('关闭（只发文字）', userData='off')
        self.voice_mode.addItem('短消息全部用语音', userData='always')
        self.voice_mode.addItem('偶尔用语音（按比例）', userData='sometimes')
        self.voice_mode.setMinimumWidth(220)
        self.voice_tier = ComboBox()
        self.voice_tier.setMinimumWidth(320)
        self.voice_tier.currentIndexChanged.connect(self.update_voice_speakers)
        self.voice_speaker = QSpinBox()
        self.voice_speed = QDoubleSpinBox()
        self.voice_speed.setRange(0.5, 2.0)
        self.voice_speed.setSingleStep(0.1)
        self.voice_max_chars = QSpinBox()
        self.voice_max_chars.setRange(5, 300)
        self.voice_ratio = QSpinBox()
        self.voice_ratio.setRange(0, 100)
        self.voice_ratio.setSuffix('%')
        for field in (self.voice_speaker, self.voice_speed, self.voice_max_chars, self.voice_ratio):
            field.setMinimumWidth(110)
            field.setStyleSheet('QAbstractSpinBox {color:#243656;background:white;border:1px solid #DDE8F7;'
                                'border-radius:5px;padding:6px;}')
        self.voice_state = CaptionLabel('')
        self.voice_state.setWordWrap(True)
        # Clone tier (VoxCPM2): the friend's reference clip or a voice description, and the worker's Python.
        self.voice_reference = CaptionLabel('')
        pick_reference = PushButton('选择参考语音…')
        pick_reference.clicked.connect(self.pick_voice_reference)
        self.voice_reference_text = LineEdit()
        self.voice_reference_text.setPlaceholderText('参考语音里说的原话（可选；填写后音色更像）')
        self.voice_design = LineEdit()
        self.voice_design.setPlaceholderText('没有参考语音时用一句话描述声音，例如：年轻女声，温柔，语速稍慢')
        self.voice_clone_python = LineEdit()
        self.voice_clone_python.setPlaceholderText('已安装 voxcpm 的 Python，例如 ...\\.venv\\Scripts\\python.exe')
        pick_python = PushButton('浏览…')
        pick_python.clicked.connect(self.pick_voice_clone_python)
        self.voice_clone_box = QWidget()
        clone_layout = QVBoxLayout(self.voice_clone_box)
        clone_layout.setContentsMargins(0, 0, 0, 0)
        for line in (row(BodyLabel('参考语音'), self.voice_reference, pick_reference, None),
                     row(BodyLabel('参考原文'), self.voice_reference_text),
                     row(BodyLabel('声音描述'), self.voice_design),
                     row(BodyLabel('克隆运行环境'), self.voice_clone_python, pick_python)):
            clone_layout.addLayout(line)
        clone_layout.addWidget(hint('声音克隆只用于你有权使用的声音（自己的声音，或已获本人同意）。参考语音建议 5~15 秒、单人、'
                                    '无背景音乐；首次使用需加载模型数分钟，加载期间照常发文字。'))
        preview_voice = PushButton('试听')
        preview_voice.clicked.connect(self.preview_voice)
        save_voice = PushButton('保存语音设置')
        save_voice.clicked.connect(self.save_voice_settings)
        self.add(Card('QQ 语音回复（本地语音模型）', actions=(preview_voice, save_voice), collapsible=True, collapsed=True).add(
            row(BodyLabel('发送方式'), self.voice_mode, BodyLabel('语音比例'), self.voice_ratio, None),
            row(BodyLabel('语音模型'), self.voice_tier, None),
            row(BodyLabel('音色编号'), self.voice_speaker, BodyLabel('语速'), self.voice_speed,
                BodyLabel('最长字数'), self.voice_max_chars, None),
            self.voice_clone_box,
            self.voice_state,
            hint('只对当前好友生效。模型会结合当前对话判断该发语音还是文字（例如好友问能不能听到、让你说两句时会倾向语音）；'
                 '这里的比例只影响模型不确定时的选择。纯文字气泡在字数以内时改用本机合成的语音发送；含链接、代码、QQ 表情或图片的气泡仍发文字。'
                 '合成失败或模型未下载时自动改发文字，不会漏发。聊天记录照常保存文字原文。'),
            hint('语音模型在上方「本地模型自动下载与装配」选择「语音模型」下载。轻量与 medium 档在 CPU 上运行，不占显存；'
                 'Kokoro 音色：0~2 为英文，3~57 为中文女声，58 起为中文男声。旗舰档为声音克隆，使用显卡。')))

        # 2026-10-26 user: the friend's voice messages are heard by a local model or by an AI model's multimodal ability
        self.voice_in_switch = SectionSwitcher(self)
        for key, text in (('local', '本地模型识别'), ('agent', 'AI 模型直接听'), ('off', '关闭')):
            self.voice_in_switch.addItem(key, text, onClick=lambda *_, key=key: self.switch_voice_input(key))
        self.voice_in_tier = ComboBox()
        self.voice_in_tier.setMinimumWidth(320)
        self.voice_in_agent = ComboBox()
        for key, text in (('auto', '自动（好友当前模型能听就用它，否则用 Antigravity 的 Gemini）'),
                          ('antigravity', 'Antigravity · Gemini'), ('api', '自定义 API（OpenAI 兼容，需支持音频输入）')):
            self.voice_in_agent.addItem(text, userData=key)
        self.voice_in_agent.setMinimumWidth(320)
        self.voice_in_model = LineEdit()
        self.voice_in_model.setPlaceholderText('留空：Gemini 用 gemini-3.8-flash-low；API 用当前 API 模型')
        self.voice_in_state = CaptionLabel('')
        self.voice_in_state.setWordWrap(True)
        for field in (self.voice_in_tier, self.voice_in_agent):
            field.currentIndexChanged.connect(self.update_voice_input_state)
        save_voice_in = PushButton('保存识别设置')
        save_voice_in.clicked.connect(lambda: self.save_voice_input())
        self.add(Card('收到 QQ 语音时（本地模型 / AI 多模态）', actions=(save_voice_in,), collapsible=True, collapsed=True).add(
            row(BodyLabel('识别方式'), self.voice_in_switch, None),
            row(BodyLabel('本地识别模型'), self.voice_in_tier, None),
            row(BodyLabel('听语音的 AI'), self.voice_in_agent, None),
            row(BodyLabel('AI 模型名'), self.voice_in_model),
            self.voice_in_state,
            hint('点上方按钮立即切换并保存，只对当前好友生效。好友的语音会先变成文字（附情绪），再照常交给回复模型，'
                 '聊天记录里保存的也是这段文字。'),
            hint('本地模型：SenseVoice 在 CPU 上运行，不消耗模型额度，在上方「本地模型自动下载与装配」选择「语音识别」下载。'
                 'AI 模型：每条语音多一次模型调用；Codex、Claude Code 和 DeepSeek 不能直接听音频，所以由 Gemini 或支持音频的 API 模型来听。'
                 'AI 模型失败时会改用已下载的本地模型；本地模型没下载时不会悄悄改用付费的 AI 模型。')))

        self.refresh_all_model_views()
        for button in (self.dsh, self.claude, self.codex, self.antigravity, self.deepseek,
                       self.api_model_section, self.api_retrieval_section, self.api_function_section):
            button.toggled.connect(self.update_provider_page)
        self.codex.setChecked(True)
        self.update_provider_page()
        self.computer_enabled = CheckBox('允许当前 QQ 使用本地电脑工作台与 CLI 原生开发')
        self.computer_project = LineEdit()
        self.computer_project.setPlaceholderText('留空默认使用 workspaces/<QQ>/，或选择本地项目目录')
        browse_project = PushButton('浏览项目…')
        browse_project.clicked.connect(self.browse_computer_project)
        self.computer_full_access = CheckBox('允许访问整台电脑（不受限于项目目录）')
        self.chat_config_edits = CheckBox('允许聊天/Agent 修改配置（模型、提示词、权限等）')
        self.agent_history_sharing = CheckBox('允许同一好友的 Agent 互相检索历史')
        self.computer_mode = ComboBox()
        self.computer_mode.addItems(['可读写模式（允许写代码与修改文件）', '只读模式（仅允许读取与查询代码）'])
        self.computer_shell = ComboBox()
        self.computer_shell.addItem('PowerShell 7 (pwsh)', userData='pwsh')
        self.computer_shell.addItem('Windows PowerShell 5', userData='powershell')
        self.computer_shell.addItem('CMD', userData='cmd')
        self.computer_commands = CheckBox('允许在本机执行命令（PowerShell / 终端原生能力）')
        self.computer_approval = ComboBox()
        self.computer_approval.setMinimumWidth(280)
        save_computer = PushButton('保存电脑工具与项目权限')
        save_computer.clicked.connect(self.save_computer_tools)
        self._section = 'runtime'
        self.add(Card('本地电脑工作台与项目权限', actions=(save_computer,), collapsible=True, collapsed=True).add(
            self.computer_enabled,
            hint('开启后允许通过本地工作台和已配置的 CLI 进行文件、命令与项目级开发。'),
            row(BodyLabel('项目文件夹'), self.computer_project, browse_project),
            hint('类似 IDE 项目目录：CLI 将直接以此项目目录为开发根目录；留空则使用专属隔离工作区。'),
            self.computer_full_access,
            hint('开启后允许访问整台电脑（全盘读写）；关闭时严格限制在上述项目文件夹内。'),
            hint('⚠ 完全访问等于把当前 Windows 用户的权限交给 Agent。即使本程序拒绝直接读取 '
                 'house_web.local.json、friend.json 和密钥文件，Agent 仍可能通过命令、进程或同用户权限绕过。'
                 '只给可信任务；需要真正隔离时，用独立的低权限 Windows 账户运行 Agent。'),
            self.chat_config_edits,
            hint('允许好友在聊天/任务里让 Agent 改配置，例如换模型、改提示词、增加权限。'
                 '默认关闭；关闭后 Agent 不提供这类能力。你在桌面或手机设置页手动修改不受影响。'),
            self.agent_history_sharing,
            hint('只允许同一好友下的不同 Agent 互相检索聊天片段；跨好友永远隔离。'
                 '默认开启，可随时关闭。'),
            row(BodyLabel('操作权限'), self.computer_mode, None),
            hint('只读模式下禁止修改文件与写代码；可读写模式允许完整代码编辑与创建。'),
            row(BodyLabel('命令解释器'), self.computer_shell, None),
            hint('选择 Agent / 网页 Agent 执行 desk_run_command 时使用的命令行工具。'),
            self.computer_commands,
            hint('命令执行拥有当前 Windows 用户权限。开启后解锁 CLI 原生终端、编译与脚本执行。'),
            row(BodyLabel('CLI 审批模式'), self.computer_approval, None),
            hint('根据当前选择的 CLI（Codex / Claude Code / Antigravity / API）动态应用审批与沙箱执行策略。')))
        # 2026-10-11 user (Settings): the topic library's own model settings, moved here from its card
        self.research_effort, self.research_fast = ComboBox(), CheckBox("快速模式")
        self.research_effort.setMinimumWidth(110)
        save_research = PushButton("保存设置")
        save_research.clicked.connect(self.save_research_model)
        self.research_hint = hint('只用于话题资料库联网整理，和上面聊天用的设置分开；默认 low + Fast，省额度。'
                                  '选择 Antigravity 时，整理沿用上方 Antigravity 模型与思考档位，Fast 不生效。'
                                  '人格模板的分析模型在「人格模板」页生成时选择。')
        self.add(Card('Topic Knowledge', collapsible=True, collapsed=True).add(
            row(BodyLabel('思考档位'), self.research_effort, self.research_fast, None, save_research),
            self.research_hint))
        usage = PushButton('查看此好友全部用量')
        usage.clicked.connect(self.open_usage)
        self.add(Card('模型用量与额度核查', collapsible=True, collapsed=True).add(
            hint('汇总此好友全部模型调用：普通回复、主动、续话、记忆和摘要。'),
            hint('显示输入、输出、缓存和参考费用，每 5 秒刷新。支持 Codex CLI、Claude Code、Antigravity 与各类 API。'),
            row(usage, None)))
        self.update_approval_modes()
        self.update_research_model_ui()
        self._build_model_tabs()

    def add(self, widget):
        result = super().add(widget)
        self._section_cards.setdefault(self._section, []).append(widget)
        return result

    def _build_model_tabs(self):
        self.model_tabs = SectionSwitcher(self)
        for key, label in (('sources', '模型来源'), ('api', 'API 与检索'),
                           ('local', '本地模型与语音'), ('runtime', '运行与权限')):
            self.model_tabs.addItem(key, label)
        self.model_tabs.currentItemChanged.connect(self._on_model_tab_changed)
        self.column.insertWidget(0, self.model_tabs)
        self.switch_model_section('sources')
        self._build_runtime_card()

    def _build_runtime_card(self):
        """Top card of 模型来源: the optional PyTorch runtime local retrieval needs."""
        from qfluentwidgets import PrimaryPushButton
        self.runtime_status = BodyLabel('正在检查…')
        self.runtime_button = PrimaryPushButton('安装 / 修复')
        self.runtime_button.clicked.connect(self.install_runtime)
        card = Card('本地向量运行环境（PyTorch CPU）',
                    actions=(self.runtime_button,)).add(
            hint('本地向量与重排需要 PyTorch。安装器里默认已勾选安装；这里可以补装或修复，'
                 '约 260 MB，走阿里云 PyTorch 镜像，装完重启程序生效。'),
            self.runtime_status)
        card.setVisible(self._section == 'sources')
        self.column.insertWidget(1, card)
        self._section_cards.setdefault('sources', []).insert(0, card)
        self.refresh_runtime_status()

    def refresh_runtime_status(self):
        from .common import run_async

        def probe(_):
            import local_runtimes
            return local_runtimes.describe('embedding')

        def show(info):
            if info.get('needs_install'):
                self.runtime_status.setText('未安装：本地向量检索当前退回关键词（BM25）。')
            else:
                self.runtime_status.setText('已安装，本地向量与重排可用。')
            self.runtime_button.setEnabled(True)

        self.runtime_button.setEnabled(False)
        run_async(probe, done=show, error=lambda exc: (
            self.runtime_status.setText(f'检查失败：{exc}'), self.runtime_button.setEnabled(True)))

    def install_runtime(self):
        from .common import run_async, toast
        self.runtime_button.setEnabled(False)
        self.runtime_status.setText('正在安装 PyTorch（约 260 MB）……')

        def job(report):
            import local_runtimes
            return local_runtimes.install(
                'embedding', emit=lambda percent, text: report(text if percent is None
                                                               else f'{percent}%  {text}'))

        def done(outcome):
            self.runtime_button.setEnabled(True)
            self.refresh_runtime_status()
            if outcome.get('installed'):
                toast(self.house, 'success', '向量运行环境已安装，重启程序后生效。', '运行环境')
            else:
                toast(self.house, 'error', outcome.get('message') or '安装失败', '运行环境')

        def failed(exc):
            self.runtime_button.setEnabled(True)
            self.refresh_runtime_status()
            toast(self.house, 'error', str(exc), '运行环境')

        run_async(job, done=done, error=failed,
                  progress=lambda text: self.runtime_status.setText(str(text)[:120]))

    def _on_model_tab_changed(self, section):
        if section != self._section:
            self.switch_model_section(section)

    def switch_model_section(self, section):
        self._section = section
        for key, cards in self._section_cards.items():
            for card in cards:
                card.setVisible(key == section)
        self.model_tabs.setCurrentItem(section)
        self.update_provider_page()
        self._reset_model_scroll()
        QTimer.singleShot(0, self._reset_model_scroll)

    def _reset_model_scroll(self):
        self.content.layout().activate()
        self.content.updateGeometry()
        self.content.adjustSize()
        bar = self.verticalScrollBar()
        bar.setValue(bar.minimum())

    PROVIDER_TITLES = {
        'codex': ('Codex CLI', '💻 代码实现与终端执行'),
        'claude': ('Claude Code', '🧠 架构与安全审查 [英文协议]'),
        'antigravity': ('Antigravity CLI', '🌐 深度推理与全网检索'),
        'dsh': ('DeepSeek Harness', '⚡ 算法与代码推导'),
        'custom': ('自定义 API', '🔧 领域与微调接口'),
        'webchat_deepseek': ('网页端 · DeepSeek', '🌐 浏览器网页对话，省本地额度'),
        'webchat_chatgpt': ('网页端 · ChatGPT', '🌐 浏览器网页对话，省本地额度'),
        'webchat_gemini': ('网页端 · Gemini', '🌐 浏览器网页对话，省本地额度'),
    }
    PROVIDER_TITLES.update({key: (BUILTINS[key].name, '本机执行智能体') for key in AGENT_BACKENDS})

    def move_order_up(self):
        row_idx = self.handover_list.currentRow()
        if row_idx > 0:
            item = self.handover_list.takeItem(row_idx)
            self.handover_list.insertItem(row_idx - 1, item)
            self.handover_list.setCurrentRow(row_idx - 1)
            self.refresh_order_display()

    def move_order_down(self):
        row_idx = self.handover_list.currentRow()
        if 0 <= row_idx < self.handover_list.count() - 1:
            item = self.handover_list.takeItem(row_idx)
            self.handover_list.insertItem(row_idx + 1, item)
            self.handover_list.setCurrentRow(row_idx + 1)
            self.refresh_order_display()

    def get_checked_providers(self) -> list[str]:
        checked = []
        if self.dsh.isChecked(): checked.append('dsh')
        if self.claude.isChecked(): checked.append('claude')
        if self.codex.isChecked(): checked.append('codex')
        if self.antigravity.isChecked(): checked.append('antigravity')
        if self.deepseek.isChecked(): checked.append('custom')
        if self.webchat.isChecked(): checked.append('webchat_deepseek')
        if self.webchat_gpt.isChecked(): checked.append('webchat_chatgpt')
        if self.webchat_gemini.isChecked(): checked.append('webchat_gemini')
        checked.extend(key for key, button in self.external_checks.items() if button.isChecked())
        return checked

    def get_handover_order(self) -> list[str]:
        order = []
        for i in range(self.handover_list.count()):
            item = self.handover_list.item(i)
            key = item.data(Qt.UserRole)
            if key:
                order.append(key)
        return order

    def sync_handover_list_with_checks(self, preferred_order=None, preferred_master=None):
        if getattr(self, '_syncing_provider', False):
            return
        self._syncing_provider = True
        try:
            self._do_sync_handover_list(preferred_order, preferred_master)
        finally:
            self._syncing_provider = False

    def _do_sync_handover_list(self, preferred_order=None, preferred_master=None):
        checked = self.get_checked_providers()
        if not checked:
            self.codex.blockSignals(True)
            self.codex.setChecked(True)
            self.codex.blockSignals(False)
            checked = ['codex']

        current_order = self.get_handover_order() if preferred_order is None else list(preferred_order)
        new_order = [p for p in current_order if p in checked]
        for p in checked:
            if p not in new_order:
                new_order.append(p)

        self.handover_list.blockSignals(True)
        self.handover_list.clear()
        for p in new_order:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, p)
            self.handover_list.addItem(item)
        self.handover_list.blockSignals(False)

        curr_master = preferred_master or self.master_agent_combo.currentData() or new_order[0]
        if curr_master not in new_order:
            curr_master = new_order[0]

        self.master_agent_combo.blockSignals(True)
        self.master_agent_combo.clear()
        selected_idx = 0
        for idx, p in enumerate(new_order):
            name, desc = self.PROVIDER_TITLES.get(p, (p, ''))
            suffix = " (👑 任务分配主智能体)" if len(new_order) > 1 else " (单引擎独立运行)"
            self.master_agent_combo.addItem(f"{name}{suffix}", userData=p)
            if p == curr_master:
                selected_idx = idx

        if self.master_agent_combo.count() > 0:
            self.master_agent_combo.setCurrentIndex(selected_idx)
        self.master_agent_combo.blockSignals(False)

        self.refresh_order_display()
        self.update_provider_page()

    def refresh_order_display(self):
        master_key = self.master_agent_combo.currentData()
        for i in range(self.handover_list.count()):
            item = self.handover_list.item(i)
            key = item.data(Qt.UserRole)
            name, desc = self.PROVIDER_TITLES.get(key, (key, ''))
            crown = " 👑 主智能体" if key == master_key else ""
            item.setText(f"[{i + 1}] {name}{crown}  —  {desc}")

        order = self.get_handover_order()
        self.collab_mode_hint.setText(f'当前好友已启用 {len(order)} 种智能体；协作流程按节点配置，实际调用与交回结果见“协作网络”。')

    def on_provider_selection_changed(self, *_):
        self.sync_handover_list_with_checks()

    def on_master_agent_changed(self, index):
        if index < 0:
            return
        self.refresh_order_display()
        self.update_approval_modes()
        self.update_research_model_ui()

    def current_provider_key(self):
        master = getattr(self, 'master_agent_combo', None)
        if master and master.currentData():
            key = master.currentData()
            return 'api' if key == 'custom' else key
        if self.dsh.isChecked():
            return 'dsh'
        if self.claude.isChecked():
            return 'claude'
        if self.antigravity.isChecked():
            return 'antigravity'
        if self.deepseek.isChecked():
            return 'api'
        return 'codex'

    def update_approval_modes(self, preferred=None):
        if not hasattr(self, 'computer_approval'):
            return
        provider = self.current_provider_key()
        options = APPROVAL_MODES.get(provider, APPROVAL_MODES['codex'])
        current_data = preferred if preferred is not None else self.computer_approval.currentData()
        self.computer_approval.blockSignals(True)
        self.computer_approval.clear()
        target_index = 0
        for idx, (label, key) in enumerate(options):
            self.computer_approval.addItem(label, userData=key)
            if current_data == key or (not current_data and idx == 0):
                target_index = idx
            elif current_data in ('on-request', 'on_request', 'ask', 'request-review', 'manual') and key in ('request-review', 'manual', 'on-request'):
                target_index = idx
            elif current_data in ('bypass', 'turbo', 'full-access', 'never', 'auto') and key in ('full-access', 'bypass', 'turbo', 'never', 'auto'):
                if target_index == 0:
                    target_index = idx
        self.computer_approval.setCurrentIndex(target_index)
        self.computer_approval.blockSignals(False)

    def browse_computer_project(self):
        current = self.computer_project.text().strip() or str(ROOT)
        path = QFileDialog.getExistingDirectory(self, '选择项目文件夹', current)
        if path:
            self.computer_project.setText(path)

    def save_computer_tools(self):
        profile = load_profile(self.house.profile_path)
        tools = {'enabled': self.computer_enabled.isChecked(),
                 'commands': self.computer_commands.isChecked()}
        project = self.computer_project.text().strip()
        if project:
            tools['project_path'] = project
        if self.computer_full_access.isChecked():
            tools['full_computer_access'] = True
        if self.computer_mode.currentIndex() != 0:
            tools['mode'] = 'read_only'
        if self.computer_approval.currentIndex() != 0:
            mode_data = self.computer_approval.currentData()
            if not mode_data:
                provider = self.current_provider_key()
                options = APPROVAL_MODES.get(provider, APPROVAL_MODES['codex'])
                idx = max(0, min(self.computer_approval.currentIndex(), len(options) - 1))
                mode_data = options[idx][1]
            tools['approval_mode'] = mode_data
        tools['shell'] = self.computer_shell.currentData() or 'pwsh'
        profile['computer_tools'] = tools
        profile['chat_config_edits'] = self.chat_config_edits.isChecked()
        profile['agent_history_sharing'] = self.agent_history_sharing.isChecked()
        write_profile(self.house.profile_path, profile)
        toast(self.house, 'success', '当前 QQ 的电脑工具与项目权限已保存；其他 QQ 不受影响。')

    def update_provider_page(self, *_):
        sources = self._section == 'sources'
        api = self._section == 'api'
        self.dsh_card.setVisible(sources and self.dsh.isChecked())
        self.codex_card.setVisible(sources and self.codex.isChecked())
        self.claude_card.setVisible(sources and self.claude.isChecked())
        self.ag_card.setVisible(sources and self.antigravity.isChecked())
        for key, card in getattr(self, 'web_cards', {}).items():
            check = {'webchat_deepseek': self.webchat, 'webchat_chatgpt': self.webchat_gpt,
                     'webchat_gemini': self.webchat_gemini}.get(key)
            card.setVisible(sources and bool(check and check.isChecked()))
        for key, button in self.external_checks.items():
            self.adapter_settings.cards[key].setVisible(sources and button.isChecked())
        self.api_nav.setVisible(api)
        self.chat_api_card.setVisible(api and self.api_model_section.isChecked())
        self.service_apis.retrieval.setVisible(api and self.api_retrieval_section.isChecked())
        self.service_apis.function.setVisible(api and self.api_function_section.isChecked())
        snd = self.sender()
        mapping = {getattr(self, 'dsh', None): 'dsh', getattr(self, 'claude', None): 'claude',
                   getattr(self, 'codex', None): 'codex', getattr(self, 'antigravity', None): 'antigravity',
                   getattr(self, 'deepseek', None): 'custom'}
        target_p = mapping.get(snd)
        target_p = next((key for key, button in self.external_checks.items() if button is snd), target_p)
        if target_p and snd and getattr(snd, 'isChecked', lambda: False)():
            self.sync_handover_list_with_checks(preferred_master=target_p)
        else:
            self.sync_handover_list_with_checks()
        self.update_approval_modes()
        self.update_research_model_ui()

    def update_research_model_ui(self, *_):
        if not hasattr(self, 'research_effort') or not hasattr(self, 'research_hint'):
            return
        provider = self.current_provider_key()
        if provider == 'claude':
            efforts = list(CLAUDE_EFFORTS_DISPLAY)
            self.research_fast.setEnabled(False)
            hint_text = ('只用于话题资料库联网整理，和上面聊天用的设置分开。'
                         '基于 Claude Code 自动搜集整理话题与联网检索；支持配置思考档位（从 low 到 ultracode），'
                         'Fast 模式在此渠道下不生效。人格模板的分析模型在「人格模板」页生成时选择。')
        elif provider == 'antigravity':
            efforts = list(AG_EFFORTS) if AG_EFFORTS else ['low', 'high', 'max']
            self.research_fast.setEnabled(False)
            hint_text = ('只用于话题资料库联网整理，和上面聊天用的设置分开。'
                         '基于 Google Antigravity 代理能力整理话题资料，沿用上方配置的模型与思考档位，'
                         'Fast 模式不生效。人格模板的分析模型在「人格模板」页生成时选择。')
        elif provider == 'dsh':
            efforts = list(PROVIDER_DEFAULTS.get('dsh', {}).get('efforts', ['off', 'low', 'medium', 'high', 'max']))
            self.research_fast.setEnabled(False)
            hint_text = ('只用于话题资料库联网整理，和上面聊天用的设置分开。'
                         '基于 DeepSeek (DSH) 官方渠道搜集与整理话题资料；支持配置 off（关闭深度思考）以及 low / medium / high / max 思考档位以控制深度思考耗时与 token 消耗。'
                         '人格模板的分析模型在「人格模板」页生成时选择。')
        elif provider == 'api':
            efforts = list(EFFORTS) if EFFORTS else ['low', 'medium', 'high']
            self.research_fast.setEnabled(True)
            hint_text = ('只用于话题资料库联网整理，和上面聊天用的设置分开。'
                         '基于自定义 API 与网络检索服务整理话题内容；支持调整思考深度，Fast 模式根据上游模型能力生效。'
                         '人格模板的分析模型在「人格模板」页生成时选择。')
        else:  # codex
            efforts = self.options.get(self.codex_model.currentText(), ['low', 'medium', 'high', 'extra'])
            if not efforts:
                efforts = ['none', 'low', 'medium', 'high', 'xhigh', 'max']
            self.research_fast.setEnabled(True)
            hint_text = ('只用于话题资料库联网整理，和上面聊天用的设置分开；默认 low + Fast，省额度。'
                         '基于 Codex CLI 进行联网搜索与资料提炼；支持自定义思考档位并开启快速模式。'
                         '人格模板的分析模型在「人格模板」页生成时选择。')

        current_val = self.research_effort.currentText()
        self.research_effort.blockSignals(True)
        self.research_effort.clear()
        self.research_effort.addItems(efforts)
        if current_val and current_val in efforts:
            self.research_effort.setCurrentText(current_val)
        elif 'low' in efforts:
            self.research_effort.setCurrentText('low')
        elif 'medium' in efforts:
            self.research_effort.setCurrentText('medium')
        elif efforts:
            self.research_effort.setCurrentIndex(0)
        self.research_effort.blockSignals(False)
        self.research_hint.setText(hint_text)

    def refresh_retrieval_models(self):
        """自动 plus the installed presets (nothing is downloaded); the saved choice selected."""
        import retrieval_core
        saved = retrieval_core.overrides()
        for combo, kind, extra in ((self.retrieval_embedding, 'embedding', ()), (self.retrieval_rerank, 'rerank', ('off',))):
            combo.blockSignals(True)
            combo.clear()
            values = ['auto', *retrieval_core.installed(kind), *extra]
            for value in values:
                combo.addItem({'auto': '自动（按任务、语言、显存选择）', 'off': '关闭重排（只用融合排序）'}.get(value, value),
                              userData=value)
            current = saved.get(kind, 'auto')
            combo.setCurrentIndex(values.index(current) if current in values else 0)
            combo.blockSignals(False)
        if hasattr(self, 'retrieval_state'):
            self.retrieval_state.setText(self.retrieval_state_text())

    def retrieval_state_text(self):
        import retrieval_core
        try:
            choice = retrieval_core.choose_reranker('memory', '示例', ['示例'])
            embedding = retrieval_core.choose_embedding()
            return (f'当前：向量 {embedding.model or embedding.kind}；重排 {choice.model or choice.kind}（{choice.reason}）。'
                    '换向量模型后，已存向量按模型分开保存，查询只和同一模型的向量比较；好友自己配置的检索 API 仍优先。')
        except Exception as exc:
            return '检索模型状态暂不可用：' + type(exc).__name__

    def save_retrieval_models(self):
        import house_settings
        value = house_settings.load(ROOT)
        value['retrieval_models'] = {'embedding': self.retrieval_embedding.currentData() or 'auto',
                                     'rerank': self.retrieval_rerank.currentData() or 'auto'}
        house_settings.save(value, ROOT)
        self.retrieval_state.setText(self.retrieval_state_text())
        toast(self.house, 'success', '已保存检索模型选择；没有下载任何模型。')

    def save_local_models(self, enabled):
        import house_settings
        house_settings.set_local_models_enabled(bool(enabled), ROOT)
        toast(self.house, 'success', '已保存本地模型开关；没有下载任何模型。')

    def mirror_local_models_state(self, enabled):
        """Downloader/save may run outside this page; mirror the saved truth."""
        def apply():
            self.local_models.blockSignals(True)
            self.local_models.setChecked(bool(enabled))
            self.local_models.blockSignals(False)
            self.refresh_all_model_views()
        QTimer.singleShot(0, apply)

    def current_api_values(self):
        return {'api_protocol': self.protocol.currentText(), 'api_base_url': self.base_url.text().strip(),
                'model': self.deepseek_model.text().strip(), 'api_effort': self.api_effort.currentText(),
                'api_key_id': self.profile_id or 'chat'}

    def keep_api_draft(self):
        if self.profile_id in self.api_profiles:
            self.api_profiles[self.profile_id].update(self.current_api_values())
            if self.key.text().strip():
                self.chat_keys[self.profile_id] = self.key.text().strip()

    def change_api_profile(self, index):
        self.keep_api_draft()
        if not 0 <= index < len(self.profile_ids):
            return
        self.profile_id = self.profile_ids[index]
        value = self.api_profiles[self.profile_id]
        self.protocol.setCurrentText(value.get('api_protocol') or PROTOCOLS[0])
        self.api_effort.setCurrentText(value.get('api_effort') or 'low')
        self.base_url.setText(value.get('api_base_url', ''))
        self.deepseek_model.setText(value.get('model', ''))
        self.key.clear()
        self.show_key_state()

    def use_api_preset(self, index):
        if not getattr(self, 'profile_id', None) or index < 0:
            return
        preset_id = self.api_preset.currentData()
        _, protocol, url, model = PRESETS[preset_id]
        # A generic preset intentionally leaves the address to the user.
        self.protocol.setCurrentText(protocol)
        self.base_url.setText(url)
        self.deepseek_model.setText(model)

    def add_api_profile(self):
        if len(self.api_profiles) >= MAX_PROFILES:
            toast(self.house, 'error', f'最多保存 {MAX_PROFILES} 个外部 API 连接')
            return
        dialog = InputDialog(self.house, '新增外部 API 连接', '连接名称（任意服务商或用途）：')
        if not dialog.exec() or not dialog.edit.text().strip():
            return
        self.keep_api_draft()
        value = new_connection(dialog.edit.text(), self.api_preset.currentData() or 'generic_openai')
        identifier = value['api_key_id']
        self.api_profiles[identifier] = value
        self.profile_ids.append(identifier)
        self.api_profile.addItem(value['label'])
        self.api_profile.setCurrentIndex(len(self.profile_ids) - 1)

    def rename_api_profile(self):
        if self.profile_id not in self.api_profiles:
            return
        dialog = InputDialog(self.house, '重命名连接', '新的连接名称：')
        dialog.edit.setText(self.api_profiles[self.profile_id].get('label', ''))
        if dialog.exec() and dialog.edit.text().strip():
            self.api_profiles[self.profile_id]['label'] = dialog.edit.text().strip()[:64]
            self.api_profile.setItemText(self.api_profile.currentIndex(), self.api_profiles[self.profile_id]['label'])

    def delete_api_profile(self):
        if len(self.profile_ids) <= 1:
            toast(self.house, 'warning', '至少保留一个 API 连接')
            return
        identifier = self.profile_id
        if not confirm(self.house, '删除 API 连接',
                       '移除此连接及其保存的密钥？其他连接不受影响。'):
            return
        index = self.profile_ids.index(identifier)
        self.profile_ids.remove(identifier)
        self.api_profiles.pop(identifier, None)
        self.chat_keys.pop(identifier, None)
        self.profile_id = None
        self.api_profile.blockSignals(True)
        self.api_profile.removeItem(index)
        self.api_profile.setCurrentIndex(min(index, len(self.profile_ids) - 1))
        self.api_profile.blockSignals(False)
        self.change_api_profile(self.api_profile.currentIndex())
        # Only this named connection's encrypted file is removed.
        try:
            key_path(self.house.friend_dir, identifier).unlink(missing_ok=True)
        except OSError as exc:
            toast(self.house, 'warning', f'连接已从页面移除，但密钥文件删除失败：{type(exc).__name__}')

    def load_api_profiles(self, profile, api):
        self.profile_id = None
        self.chat_keys = {}
        self.api_profiles, active = load_connections(profile)
        self.profile_ids = list(self.api_profiles)
        self.api_profile.blockSignals(True)
        self.api_profile.clear()
        self.api_profile.addItems([self.api_profiles[x]['label'] for x in self.profile_ids])
        self.api_profile.setCurrentIndex(self.profile_ids.index(active))
        self.api_profile.blockSignals(False)
        self.change_api_profile(self.profile_ids.index(active))
        self.service_apis.load(profile)
        self.update_provider_page()

    def browse_executable(self, edit, title):
        path, _ = QFileDialog.getOpenFileName(
            self, title, edit.text().strip() or 'D:\\',
            '可执行程序 (*.cmd *.exe *.bat *.ps1);;所有文件 (*.*)'
        )
        if path:
            if edit == self.dsh_path:
                import dsh_backend
                resolved = dsh_backend.cli_path(path)
                edit.setText(resolved or path)
            else:
                edit.setText(path)

    def _default_dsh_path(self):
        import dsh_backend
        return dsh_backend.cli_path() or ''

    def prepare_agent(self, provider):
        """One-click install/open/login for a model source, without storing credentials."""
        def job(report):
            import agent_launcher
            return agent_launcher.prepare(provider, lambda *args: report(args[-1]))

        def done(message):
            toast(self, 'success', message, '模型来源')
            if provider == 'opencode':
                self.adapter_settings.fields['opencode']['status'].setText(message)

        def failed(exc):
            toast(self, 'error', str(exc), '模型来源')

        run_async(job, done, failed, pool=LONG_POOL)

    def test_dsh(self):
        import subprocess, os
        from pathlib import Path
        import dsh_backend
        custom = self.dsh_path.text().strip() or self._default_dsh_path()
        cli = dsh_backend.cli_path(custom)
        api_key_file = key_path(self.house.friend_dir, 'dsh')
        has_key = bool(self.dsh_key.text().strip()) or api_key_file.exists()

        toast(self.house, 'info', '正在检测 DeepSeek Harness 环境…')
        def do_test(_):
            results = []
            if cli and os.path.exists(cli) and os.path.isfile(cli):
                try:
                    res = subprocess.run([cli, '--version'], capture_output=True, text=True, timeout=10,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    version_text = res.stdout.strip() or res.stderr.strip()
                    results.append(f'CLI 本地客户端就绪：{version_text or cli}')
                except Exception as e:
                    results.append(f'CLI 调用测试警告：{e}')
            else:
                results.append('未检测到本地 dsh.cmd，将使用 API 模式')

            desktop_patch = Path.home() / '.dsh' / 'profiles' / 'desktop' / 'cordis.patch.yml'
            if desktop_patch.exists():
                results.append('检测到本地已登录桌面端配置 (cordis.patch.yml)')

            if has_key:
                results.append('已配置 API Key（可双模调用）')
            else:
                results.append('使用本机桌面客户端账号额度')

            return '\n'.join(results)

        run_async(do_test,
                  done=lambda text: toast(self.house, 'success', text, 'DeepSeek Harness 检测通过'),
                  error=lambda exc: toast(self.house, 'error', str(exc), 'DeepSeek Harness 检测失败'),
                  pool=LONG_POOL)

    def fetch_dsh_models(self):
        from pathlib import Path
        import dsh_backend
        toast(self.house, 'info', '正在提取 DeepSeek 可用模型与思考档位…')
        def do_fetch(_):
            custom = self.dsh_path.text().strip()
            models = dsh_backend.fetch_available_models(custom)
            api_key = self.dsh_key.text().strip()
            if not api_key:
                kfile = key_path(self.house.friend_dir, 'dsh')
                if kfile.exists():
                    api_key = kfile.read_text('utf-8').strip()
            if api_key:
                try:
                    from api_backend import fetch_provider_models
                    base_url = self.dsh_url.text().strip() or PROVIDER_DEFAULTS['dsh']['base_url']
                    api_models = fetch_provider_models('dsh', base_url, api_key)
                    for m in api_models:
                        if m not in models:
                            models.append(m)
                except Exception:
                    pass
            return models

        def on_done(models):
            current = self.dsh_model.currentText()
            self.dsh_model.clear()
            self.dsh_model.addItems(models)
            if current and current in models:
                self.dsh_model.setCurrentText(current)
            elif models:
                self.dsh_model.setCurrentText(models[0])

            # Sync official reasoning efforts
            curr_effort = self.dsh_effort.currentText()
            self.dsh_effort.clear()
            self.dsh_effort.addItems(list(dsh_backend.EFFORTS))
            if curr_effort in dsh_backend.EFFORTS:
                self.dsh_effort.setCurrentText(curr_effort)
            else:
                self.dsh_effort.setCurrentText('low')

            toast(self.house, 'success', f'已提取 {len(models)} 个可用模型，并同步官方思考档位 (off/low/high/max)', '提取成功')

        run_async(do_fetch, done=on_done, error=lambda exc: toast(self.house, 'error', str(exc), '提取模型失败'), pool=LONG_POOL)

    def test_antigravity(self):
        import antigravity_backend
        custom = self.ag_path.text().strip() or None
        cli = antigravity_backend.cli_path(custom)
        if not cli:
            toast(self.house, 'error', '未找到 Antigravity CLI（agy 命令）。若安装在 D 盘或非标准目录，请点击「选择路径…」指定 agy.exe', '测试失败')
            return
        api = {'antigravity_model': self.ag_model.text().strip(),
               'antigravity_effort': self.ag_effort.currentText(),
               'antigravity_path': custom}
        directory = self.house.friend_dir
        toast(self.house, 'success', f'正在测试 CLI（{cli}，仅测试文本连接，不发送 QQ）')
        run_async(lambda _: antigravity_backend.generate('只回复 ANTIGRAVITY_OK，不使用工具。', directory, api, ag_path=custom)[0],
                  done=lambda text: toast(self.house, 'success', text, 'Antigravity CLI 返回'),
                  error=lambda exc: toast(self.house, 'error', str(exc), 'CLI 测试失败'), pool=LONG_POOL)

    def test_codex(self):
        import subprocess
        from codex_settings import cli_path as codex_cli_path
        custom = self.codex_path.text().strip() or None
        codex = codex_cli_path(custom)
        if not codex:
            toast(self.house, 'error', '未找到 Codex CLI（codex 命令）。若安装在 D 盘或非标准目录，请点击「选择路径…」指定 codex.cmd 或 codex.exe；若未安装，请在终端执行 npm i -g @openai/codex', '测试失败')
            return
        toast(self.house, 'success', f'正在检测 Codex CLI（{codex}）…')
        def run_test(_):
            res = subprocess.run([codex, '--version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
            return res.stdout.strip() or 'Codex CLI 连接正常'
        run_async(run_test,
                  done=lambda text: toast(self.house, 'success', text, 'Codex CLI 检测通过'),
                  error=lambda exc: toast(self.house, 'error', str(exc), 'Codex CLI 测试失败'), pool=LONG_POOL)

    def test_claude(self):
        import subprocess
        import claude_backend
        custom = self.claude_path.text().strip() or None
        claude = claude_backend.cli_path(custom)
        if not claude:
            toast(self.house, 'error', '未找到 Claude Code 命令行（claude 命令）。若安装在 D 盘或非标准目录，请点击「选择路径…」指定 claude.cmd 或 claude.exe；若未安装，请在终端执行 npm i -g @anthropic-ai/claude-code', '测试失败')
            return
        toast(self.house, 'success', f'正在检测 Claude Code（{claude}）…')
        def run_test(_):
            res = subprocess.run([claude, '--version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)
            return res.stdout.strip() or 'Claude Code 连接正常'
        run_async(run_test,
                  done=lambda text: toast(self.house, 'success', text, 'Claude Code 检测通过'),
                  error=lambda exc: toast(self.house, 'error', str(exc), 'Claude Code 测试失败'), pool=LONG_POOL)

    def test_api(self):
        import api_backend
        base_url = self.base_url.text().strip() or DEFAULT_BASE_URL
        model = self.deepseek_model.text().strip() or MODELS[0]
        protocol = self.protocol.currentText()
        effort = self.api_effort.currentText()
        api = {'api_protocol': protocol, 'api_base_url': base_url, 'model': model,
               'api_effort': effort, 'api_key_id': self.profile_id or 'chat'}
        directory = self.house.friend_dir
        current_key = self.key.text().strip() or self.chat_keys.get(self.profile_id or 'chat')
        toast(self.house, 'info', '正在测试当前 API 连接…')
        run_async(lambda _: api_backend.generate('只回复 API_OK', directory, api,
                                                 api_key_override=current_key),
                  done=lambda text: toast(self.house, 'success', text, '模型 API 连接正常'),
                  error=lambda exc: toast(self.house, 'error', str(exc), '模型 API 测试失败'), pool=LONG_POOL)

    def fetch_antigravity_models(self):
        import antigravity_backend
        custom = self.ag_path.text().strip() or None
        toast(self.house, 'success', '正在从 Antigravity CLI 读取模型列表…')
        def do_fetch(_):
            return antigravity_backend.fetch_available_models(custom)
        def on_done(models):
            current = self.ag_model.currentText()
            self.ag_model.clear()
            for model_id, label in models:
                self.ag_model.addItem(model_id)
            if current and any(m[0] == current for m in models):
                self.ag_model.setCurrentText(current)
            elif models:
                self.ag_model.setCurrentText(models[0][0])
            summary = ", ".join(m[0] for m in models[:4])
            toast(self.house, 'success', f'已提取 {len(models)} 个可用模型：{summary}…', '模型提取成功')
        run_async(do_fetch, done=on_done, error=lambda exc: toast(self.house, 'error', str(exc), '提取模型失败'), pool=LONG_POOL)

    def restore_web_models(self, key, source):
        import web_chat
        field = self.web_model_fields.get(key)
        if field is None:
            return
        current = field.currentText().strip()
        field.clear()
        field.addItems(web_chat.models_for(source))
        if current:
            field.setCurrentText(current)
        toast(self.house, 'info', '网页模型名称由站点控制，已恢复内置候选；仍可直接输入你控制台里看到的名称。', '网页模型')

    def fetch_codex_models(self):
        import codex_settings
        custom = self.codex_path.text().strip() or None
        toast(self.house, 'success', '正在从 Codex CLI 提取可用模型…')
        def do_fetch(_):
            return codex_settings.fetch_available_models(custom)
        def on_done(options):
            self.options = options
            current = self.codex_model.currentText()
            self.codex_model.blockSignals(True)
            self.codex_model.clear()
            self.codex_model.addItems(list(options.keys()))
            if current in options:
                self.codex_model.setCurrentText(current)
            elif options:
                self.codex_model.setCurrentText(next(iter(options)))
            self.codex_model.blockSignals(False)
            self.update_efforts()
            summary = ", ".join(list(options.keys())[:4])
            toast(self.house, 'success', f'已提取 {len(options)} 个可用模型：{summary}…', '模型提取成功')
        run_async(do_fetch, done=on_done, error=lambda exc: toast(self.house, 'error', str(exc), '提取模型失败'), pool=LONG_POOL)

    def fetch_claude_models(self):
        import claude_backend
        custom = self.claude_path.text().strip() or None
        toast(self.house, 'success', '正在从 Claude Code 提取可用模型…')
        def do_fetch(_):
            return claude_backend.fetch_available_models(custom)
        def on_done(models):
            current = self.claude_model.currentText()
            self.claude_model.clear()
            self.claude_model.addItems(models)
            if current and current in models:
                self.claude_model.setCurrentText(current)
            elif models:
                self.claude_model.setCurrentText(models[0])
            summary = ", ".join(models[:4])
            toast(self.house, 'success', f'已提取 {len(models)} 个可用模型：{summary}…', '模型提取成功')
        run_async(do_fetch, done=on_done, error=lambda exc: toast(self.house, 'error', str(exc), '提取模型失败'), pool=LONG_POOL)

    def fetch_api_models(self):
        base_url = self.base_url.text().strip() or DEFAULT_BASE_URL
        protocol = self.protocol.currentText()
        key_id = self.profile_id or 'chat'
        try:
            api_key = self.key.text().strip() or self.chat_keys.get(key_id) or load_key(self.house.friend_dir, key_id)
        except (OSError, RuntimeError) as exc:
            toast(self.house, 'error', str(exc), '请先配置此连接的密钥')
            return
        toast(self.house, 'info', '正在从当前连接查询模型列表…')
        def on_done(models):
            # Do not silently replace a model the user deliberately selected.
            if not self.deepseek_model.text().strip():
                self.deepseek_model.setText(models[0])
            toast(self.house, 'success', f'查询到 {len(models)} 个模型：{", ".join(models[:6])}', '模型列表')
        run_async(lambda _: discover_models(base_url, protocol, api_key), done=on_done,
                  error=lambda exc: toast(self.house, 'error', str(exc), '模型列表查询失败'), pool=LONG_POOL)

    def update_efforts(self, *_):
        current = self.effort.currentText()
        levels = self.options.get(self.codex_model.currentText(), ['low'])
        self.effort.clear()
        self.effort.addItems(levels)
        self.effort.setCurrentText(current if current in levels else 'low' if 'low' in levels else levels[0])
        if self.codex.isChecked():
            self.update_research_model_ui()

    def refresh_all_model_views(self):
        try:
            profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        except (OSError, ValueError):
            return  # A delayed refresh must not read a removed or half-replaced friend profile.
        self.refresh_dl_categories()
        self.update_download_tiers()
        self.refresh_model_status_table()
        self.refresh_active_models_dropdown(profile)
        self.refresh_voice_settings(profile)
        self.refresh_voice_input(profile)

    def refresh_voice_input(self, profile=None):
        from model_downloader import MODEL_CATALOG, is_installed
        import voice_input
        if profile is None:
            profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        value = voice_input.settings(profile)
        self.voice_in_switch.setCurrentItem(value['mode'])
        for field in (self.voice_in_tier, self.voice_in_agent):
            field.blockSignals(True)
        self.voice_in_tier.clear()
        for tier, cfg in MODEL_CATALOG['asr'].items():
            state = '' if is_installed('asr', tier) else '（未下载）'
            self.voice_in_tier.addItem(f"【{cfg['tier_name'].split()[0]}】{cfg['name']}{state}", userData=tier)
        self.voice_in_tier.setCurrentIndex(list(MODEL_CATALOG['asr']).index(value['tier']))
        self.voice_in_agent.setCurrentIndex(list(voice_input.AGENTS).index(value['agent']))
        for field in (self.voice_in_tier, self.voice_in_agent):
            field.blockSignals(False)
        self.voice_in_model.setText(value['agent_model'])
        self._voice_in_mode = value['mode']
        self.update_voice_input_state()

    def voice_input_value(self):
        return {'mode': getattr(self, '_voice_in_mode', 'local'), 'tier': self.voice_in_tier.currentData() or 'small',
                'agent': self.voice_in_agent.currentData() or 'auto', 'agent_model': self.voice_in_model.text().strip()}

    def update_voice_input_state(self):
        from model_downloader import MODEL_CATALOG
        import voice_input
        profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        value = voice_input.settings({'voice_input': self.voice_input_value()})
        mode = value['mode']
        self.voice_in_tier.setEnabled(mode != 'off')
        self.voice_in_agent.setEnabled(mode == 'agent')
        self.voice_in_model.setEnabled(mode == 'agent')
        local = ('✅ 本地模型已下载' if voice_input.local_ready(value)
                 else f"⚪ 本地模型（{MODEL_CATALOG['asr'][value['tier']]['name']}）尚未下载")
        if mode == 'off':
            text = '已关闭：好友的语音在聊天里只显示为「[语音]」，回复模型听不到内容。'
        elif mode == 'local':
            text = f'本地模型识别。{local}' + ('' if voice_input.local_ready(value) else '：下载前收到的语音无法识别。')
        else:
            agent, detail = voice_input.agent_choice(profile, value)
            text = (f'AI 模型直接听：{"Antigravity · " if agent == "antigravity" else "自定义 API · "}{detail or "（未填写模型）"}'
                    if agent else f'AI 模型直接听：⚠ {detail}') + f'。失败时的备用：{local}'
        self.voice_in_state.setText(text)

    def switch_voice_input(self, mode):
        self._voice_in_mode = mode
        self.update_voice_input_state()
        if self.house.current_qq:
            self.save_voice_input(quiet_toast=False)

    def save_voice_input(self, quiet_toast=False):
        import voice_input
        if not self.house.current_qq:
            return
        profile = load_profile(self.house.profile_path)
        profile['voice_input'] = voice_input.settings({'voice_input': self.voice_input_value()})
        write_profile(self.house.profile_path, profile)
        names = {'local': '本地模型识别', 'agent': 'AI 模型直接听', 'off': '关闭'}
        if not quiet_toast:
            toast(self.house, 'success', f'收到语音时：{names[profile["voice_input"]["mode"]]}（下一条语音起生效）', '语音识别')

    def refresh_voice_settings(self, profile=None):
        from model_downloader import MODEL_CATALOG, is_installed
        import voice_tts
        if profile is None:
            profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        value = voice_tts.settings(profile)
        self.voice_tier.blockSignals(True)
        self.voice_tier.clear()
        for tier, cfg in MODEL_CATALOG['tts'].items():
            state = '' if is_installed('tts', tier) else '（未下载）'
            self.voice_tier.addItem(f"【{cfg['tier_name'].split()[0]}】{cfg['name']}{state}", userData=tier)
        self.voice_tier.setCurrentIndex(list(MODEL_CATALOG['tts']).index(value['tier']))
        self.voice_tier.blockSignals(False)
        self.update_voice_speakers()
        self.voice_mode.setCurrentIndex(voice_tts.MODES.index(value['mode']))
        self.voice_speaker.setValue(value['speaker'])
        self.voice_speed.setValue(value['speed'])
        self.voice_max_chars.setValue(value['max_chars'])
        self.voice_ratio.setValue(round(value['ratio'] * 100))
        self._voice_reference = value['reference_wav']
        self.voice_reference.setText(value['reference_wav'] or '未选择')
        self.voice_reference_text.setText(value['reference_text'])
        self.voice_design.setText(value['design'])
        self.voice_clone_python.setText(voice_tts.clone_runtime()['python'])

    def update_voice_speakers(self):
        from model_downloader import MODEL_CATALOG, is_installed
        tier = self.voice_tier.currentData() or 'small'
        cfg = MODEL_CATALOG['tts'][tier]
        clone = cfg['engine'] == 'voxcpm'
        self.voice_speaker.setRange(0, cfg['speakers'] - 1)
        self.voice_speaker.setEnabled(cfg['speakers'] > 1)
        self.voice_speed.setEnabled(not clone)
        self.voice_clone_box.setVisible(clone)
        self.voice_state.setText(f"{cfg['desc']}\n" + ('✅ 已下载，可直接使用' if is_installed('tts', tier)
                                 else '⚪ 尚未下载：在此期间照常发文字，下载完成后自动改用语音'))

    def pick_voice_reference(self):
        from pathlib import Path
        import shutil
        import voice_tts
        path, _ = QFileDialog.getOpenFileName(self, '选择参考语音', '', '音频 (*.wav *.mp3 *.flac *.ogg *.m4a)')
        if not path:
            return
        # Copied into the friend's own folder; friend.json stores only that relative path.
        relative = Path(voice_tts.REFERENCE_WAV).with_suffix(Path(path).suffix.lower()).as_posix()
        target = Path(self.house.profile_path).parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        self._voice_reference = relative
        self.voice_reference.setText(f'{relative}（来自 {Path(path).name}，保存后生效）')

    def pick_voice_clone_python(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择已安装 voxcpm 的 Python', '', 'Python (python.exe python python3)')
        if path:
            self.voice_clone_python.setText(path)

    def voice_settings_value(self):
        return {'mode': self.voice_mode.currentData() or 'off', 'tier': self.voice_tier.currentData() or 'small',
                'speaker': self.voice_speaker.value(), 'speed': round(self.voice_speed.value(), 2),
                'max_chars': self.voice_max_chars.value(), 'ratio': self.voice_ratio.value() / 100,
                'reference_wav': getattr(self, '_voice_reference', ''),
                'reference_text': self.voice_reference_text.text().strip(), 'design': self.voice_design.text().strip()}

    def save_voice_settings(self):
        import voice_tts
        profile = load_profile(self.house.profile_path)
        profile['voice_reply'] = self.voice_settings_value()
        write_profile(self.house.profile_path, profile)
        if self.voice_clone_python.text().strip() != voice_tts.clone_runtime()['python']:
            voice_tts.save_clone_runtime(self.voice_clone_python.text())
        toast(self.house, 'success', '语音回复设置已保存，下一条消息起生效。', '语音回复')

    def preview_voice(self):
        from pathlib import Path
        import voice_tts
        from model_downloader import MODEL_CATALOG
        value = voice_tts.settings({'voice_reply': self.voice_settings_value()})
        friend_dir = Path(self.house.profile_path).parent
        target = friend_dir / 'media' / 'voice_preview.wav'
        python = self.voice_clone_python.text().strip()

        def _render(report):
            clone = None
            if MODEL_CATALOG['tts'][value['tier']]['engine'] == 'voxcpm':
                if python != voice_tts.clone_runtime()['python']:
                    voice_tts.save_clone_runtime(python)
                clone = voice_tts.clone_inputs(value, friend_dir)
                voice_tts.CLONE.wait_ready(voice_tts.model_dir(value['tier']), report=report)
            samples, rate = voice_tts.synthesize('你好呀，这是语音回复的试听，听起来还自然吗？',
                                                 value['tier'], value['speaker'], value['speed'], clone=clone)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(voice_tts.wav_bytes(samples, rate))
            return len(samples) / rate

        def _done(seconds):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            self.voice_state.setText(f'试听已生成：{seconds:.1f} 秒（{target}）')
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        def _error(exc):
            self.voice_state.setText(f'试听失败：{exc}')
            toast(self.house, 'error', str(exc), '语音试听')

        self.voice_state.setText('正在合成试听…')
        run_async(_render, done=_done, error=_error, pool=LONG_POOL)

    def refresh_current_category_label(self):
        """Keep the category count in sync with the tier/table views."""
        from model_downloader import MODEL_CATALOG, is_installed
        titles = {
            'embedding': '向量模型 (Embedding · 检索/记忆/表情)',
            'rerank': '重排模型 (Reranker · 候选精确打分)',
            'llm': '本地大模型 (LLM · 兜底聊天与消息分析)',
            'tts': '语音模型 (TTS · QQ 语音回复)',
            'asr': '语音识别 (ASR · 听懂好友语音)',
        }
        self.dl_category.blockSignals(True)
        try:
            for index in range(self.dl_category.count()):
                key = self.dl_category.itemData(index)
                if key not in titles:
                    continue
                tiers = MODEL_CATALOG.get(key, {})
                installed = sum(1 for tier in tiers if is_installed(key, tier))
                stat = f"[{installed}/{len(tiers)} 已就绪]" if installed else f"[0/{len(tiers)} 未安装]"
                self.dl_category.setItemText(index, f"{titles[key]}  {stat}")
        finally:
            self.dl_category.blockSignals(False)

    def refresh_dl_categories(self):
        from model_downloader import MODEL_CATALOG, is_installed
        curr_cat = self.dl_category.currentData() or 'embedding'
        self.dl_category.blockSignals(True)
        self.dl_category.clear()
        category_titles = {
            'embedding': '向量模型 (Embedding · 检索/记忆/表情)',
            'rerank': '重排模型 (Reranker · 候选精确打分)',
            'llm': '本地大模型 (LLM · 兜底聊天与消息分析)',
            'tts': '语音模型 (TTS · QQ 语音回复)',
            'asr': '语音识别 (ASR · 听懂好友语音)',
        }
        for cat_key, cat_name in category_titles.items():
            tiers = MODEL_CATALOG.get(cat_key, {})
            installed_count = sum(1 for t in tiers if is_installed(cat_key, t))
            total_count = len(tiers)
            stat = f"[{installed_count}/{total_count} 已就绪]" if installed_count > 0 else f"[0/{total_count} 未安装]"
            self.dl_category.addItem(f"{cat_name}  {stat}", userData=cat_key)
        for i in range(self.dl_category.count()):
            if self.dl_category.itemData(i) == curr_cat:
                self.dl_category.setCurrentIndex(i)
                break
        self.dl_category.blockSignals(False)

    def update_download_tiers(self):
        category = self.dl_category.currentData() or 'embedding'
        from model_downloader import MODEL_CATALOG, is_installed
        cat_data = MODEL_CATALOG.get(category, {})
        curr_tier = self.dl_tier.currentData()
        self.dl_tier.blockSignals(True)
        self.dl_tier.clear()
        for tier_key, tier_data in cat_data.items():
            installed = is_installed(category, tier_key)
            status_text = ' [已安装 ✅]' if installed else ' [未安装 ⚪]'
            model_name = tier_data.get('name', '')
            tier_display = tier_data.get('tier_name', tier_key)
            self.dl_tier.addItem(f"{tier_display} · {model_name}{status_text}", userData=tier_key)
        if curr_tier:
            for i in range(self.dl_tier.count()):
                if self.dl_tier.itemData(i) == curr_tier:
                    self.dl_tier.setCurrentIndex(i)
                    break
        self.dl_tier.blockSignals(False)
        self.refresh_current_category_label()
        self.update_download_info()

    def update_download_info(self):
        category = self.dl_category.currentData() or 'embedding'
        tier = self.dl_tier.currentData() or 'small'
        from model_downloader import MODEL_CATALOG, is_installed
        cfg = MODEL_CATALOG.get(category, {}).get(tier, {})
        installed = is_installed(category, tier)
        category_name = {
            'embedding': '向量模型 (Embedding)',
            'rerank': '重排模型 (Reranker)',
            'llm': '本地大模型 (LLM)',
            'tts': '语音模型 (TTS)',
            'asr': '语音识别 (ASR)',
        }.get(category, category)
        self.dl_desc.setText(f"[{cfg.get('name', '')}]\n{cfg.get('desc', '')}")
        if installed:
            self.dl_status_label.setText(f"当前选中项 [{category_name} - {cfg.get('tier_name', tier)}]：✅ 已装配就绪（位于 {cfg.get('target_dir', '')}）")
            self.dl_btn.setText("重新下载覆盖装配")
        else:
            self.dl_status_label.setText(f"当前选中项 [{category_name} - {cfg.get('tier_name', tier)}]：⚪ 尚未下载安装")
            self.dl_btn.setText("一键自动下载并装配")
        self.refresh_current_category_label()

    def refresh_model_status_table(self):
        from model_downloader import MODEL_CATALOG, is_installed
        table_rows = []
        category_names = {
            'embedding': '向量模型 (Embedding)',
            'rerank': '重排模型 (Reranker)',
            'llm': '本地大模型 (LLM)',
            'tts': '语音模型 (TTS)',
            'asr': '语音识别 (ASR)',
        }
        tier_names = {
            'small': '轻量档',
            'base': '中量档',
            'large': '旗舰档',
        }
        specs_map = {
            ('embedding', 'small'): '512维 · FP16 · 约65MB',
            ('embedding', 'base'): '768维 · FP16 · 约220MB',
            ('embedding', 'large'): '1024维 · FP16 · 约1.1GB',
            ('rerank', 'small'): '交叉编码 · FP16 · 约40MB',
            ('rerank', 'base'): '中文重排 · FP16 · 约220MB',
            ('rerank', 'large'): '多语言重排 · FP16 · 约1.1GB',
            ('llm', 'small'): '2B Q4_K_M · 约1.2GB',
            ('llm', 'base'): '4B Q4_K_M · 约2.6GB',
            ('llm', 'large'): '9B Q4_K_M · 约5.3GB',
            ('tts', 'small'): '单音色中英 · FP16 · 约90MB',
            ('tts', 'base'): '103 音色 · FP16 · 约260MB',
            ('tts', 'large'): '声音克隆 · BF16 · 约4.9GB',
            ('asr', 'small'): 'SenseVoice · INT8 · 约230MB',
            ('asr', 'base'): 'SenseVoice · FP16 · 约470MB',
        }
        for cat_key in ('embedding', 'rerank', 'llm', 'tts', 'asr'):
            for tier_key in MODEL_CATALOG.get(cat_key, {}):
                cfg = MODEL_CATALOG.get(cat_key, {}).get(tier_key, {})
                installed = is_installed(cat_key, tier_key)
                status = '✅ 已装配就绪' if installed else '⚪ 尚未安装'
                spec = specs_map.get((cat_key, tier_key), cfg.get('tier_name', ''))
                table_rows.append({
                    'cat_key': cat_key,
                    'tier_key': tier_key,
                    'cat_name': category_names.get(cat_key, cat_key),
                    'tier_name': tier_names.get(tier_key, tier_key),
                    'model_name': cfg.get('name', ''),
                    'spec': spec,
                    'status': status,
                    'target_dir': cfg.get('target_dir', ''),
                })
        self._table_model_rows = table_rows
        self.model_status_table.setRowCount(len(table_rows))
        for row_idx, r in enumerate(table_rows):
            self.model_status_table.setItem(row_idx, 0, QTableWidgetItem(r['cat_name']))
            self.model_status_table.setItem(row_idx, 1, QTableWidgetItem(r['tier_name']))
            self.model_status_table.setItem(row_idx, 2, QTableWidgetItem(r['model_name']))
            self.model_status_table.setItem(row_idx, 3, QTableWidgetItem(r['spec']))
            self.model_status_table.setItem(row_idx, 4, QTableWidgetItem(r['status']))
            self.model_status_table.setItem(row_idx, 5, QTableWidgetItem(r['target_dir']))

    def on_model_table_row_clicked(self, item):
        row_idx = item.row()
        if hasattr(self, '_table_model_rows') and 0 <= row_idx < len(self._table_model_rows):
            target = self._table_model_rows[row_idx]
            cat_key = target['cat_key']
            tier_key = target['tier_key']
            for i in range(self.dl_category.count()):
                if self.dl_category.itemData(i) == cat_key:
                    self.dl_category.setCurrentIndex(i)
                    break
            for i in range(self.dl_tier.count()):
                if self.dl_tier.itemData(i) == tier_key:
                    self.dl_tier.setCurrentIndex(i)
                    break

    def start_download_model(self, background=False):
        category = self.dl_category.currentData() or 'embedding'
        tier = self.dl_tier.currentData() or 'small'
        mirror = self.dl_mirror.currentData() or 'https://hf-mirror.com'
        from model_downloader import MODEL_CATALOG, download_model
        cfg = MODEL_CATALOG.get(category, {}).get(tier, {})
        tier_title = cfg.get('tier_name', tier)
        if category in ('tts', 'asr'):
            try:
                import onnx  # noqa: F401
            except ImportError:
                toast(self.house, 'error', '缺少 onnx 模块，无法把语音模型装配为 FP16。请重新运行安装器或执行 pip install onnx。', '下载失败')
                return
        self.dl_btn.setEnabled(False)
        self.dl_bg_btn.setEnabled(False)
        self.dl_progress.setVisible(not background)
        self.dl_progress.setValue(0)
        self.dl_progress_text.setText(f"{'后台' if background else ''}正在准备下载 {tier_title}…")

        def _do_download(report):
            # A model family may need a Python runtime the installer left out on purpose
            # (torch for BGE embeddings). Install it first so the download ends usable.
            import local_runtimes
            runtime = local_runtimes.RUNTIME_FOR_KIND.get(category)
            self._runtime_installed = False
            if runtime and not local_runtimes.imports_ready(runtime):
                def runtime_report(percent, message):
                    report((max(0, min(100, percent or 0)) // 3, message))
                try:
                    outcome = local_runtimes.install(runtime, emit=runtime_report)
                except Exception as exc:  # no venv / no uv: still let the model download
                    report((0, f'运行环境未安装（{exc}），继续下载模型……'))
                else:
                    if outcome.get('installed'):
                        self._runtime_installed = True
                        report((33, '运行环境已就绪，开始下载模型……'))
                    else:
                        report((0, f"{outcome.get('message')}；仅下载模型，稍后需手动启用向量检索"))

            def cb(percent, msg):
                report((33 + int(max(0, min(100, percent)) * 0.67), msg))
            return download_model(category, tier, progress_callback=cb, mirror=mirror)

        def _on_progress(val):
            if isinstance(val, tuple):
                pct, msg = val
                self.dl_progress.setValue(pct)
                self.dl_progress_text.setText(msg)
            else:
                self.dl_progress_text.setText(str(val))

        def _on_done(res):
            self.dl_btn.setEnabled(True)
            self.dl_bg_btn.setEnabled(True)
            self.dl_progress.setValue(100)
            self.dl_progress_text.setText("下载并自动装配成功！")
            self.local_models.setChecked(True)
            self.refresh_all_model_views()
            # The filesystem may still be settling after the final rename;
            # refresh once more so category/tier/table cannot disagree.
            QTimer.singleShot(500, self.refresh_all_model_views)
            if getattr(self, '_runtime_installed', False):
                toast(self.house, 'info', '向量运行环境（PyTorch）已一并安装，重启程序后生效。', '运行环境')
            toast(self.house, 'success', f"{tier_title} 已自动装配到程序中，已可在下方选用！", '下载完成')

        def _on_error(exc):
            self.dl_btn.setEnabled(True)
            self.dl_bg_btn.setEnabled(True)
            self.dl_progress_text.setText(f"下载失败: {exc}")
            self.refresh_all_model_views()
            QTimer.singleShot(500, self.refresh_all_model_views)
            toast(self.house, 'error', str(exc), '下载失败')

        run_async(_do_download, done=_on_done, error=_on_error, progress=_on_progress, pool=LONG_POOL)

    def refresh_active_models_dropdown(self, profile=None):
        from pathlib import Path
        if profile is None:
            profile = load_profile(self.house.profile_path) if self.house.current_qq else {}
        retrieval = profile.get('retrieval_api') or {}
        from model_downloader import MODEL_CATALOG, is_installed, project_root
        performance = profile.get('performance_tier')
        performance_tier = {'低占用': 'small', '中占用': 'base', '高占用': 'large'}.get(performance)

        # Embedding options
        self.active_embedding.blockSignals(True)
        self.active_embedding.clear()
        self.active_embedding.addItem(f"按{performance}：{MODEL_CATALOG['embedding'][performance_tier]['name']}" if performance_tier else
                                      '自动选择已安装的向量模型（受本地模型开关控制）', userData='default')
        self.active_embedding.addItem('【云端API】使用检索API中配置的向量服务 (OpenAI兼容)', userData='api')
        for tier in ('small', 'base', 'large'):
            cfg = MODEL_CATALOG['embedding'][tier]
            if is_installed('embedding', tier):
                self.active_embedding.addItem(f"【本地{cfg['tier_name'].split()[0]}】{cfg['name']} ({cfg['dimension']}维)", userData=('local', 'embedding', tier))
        curr_emb = retrieval.get('embedding') or {}
        if curr_emb.get('enabled') and curr_emb.get('mode') == 'local':
            model_path = curr_emb.get('model_path')
            idx = 0
            for i in range(2, self.active_embedding.count()):
                d = self.active_embedding.itemData(i)
                if isinstance(d, tuple) and d[1] == 'embedding':
                    cfg = MODEL_CATALOG['embedding'][d[2]]
                    if model_path and Path(model_path).resolve() == (project_root() / cfg['target_dir']).resolve():
                        idx = i
                        break
            if idx == 0:
                self.active_embedding.addItem('已配置的本地向量：' + curr_emb.get('model', ''), userData='custom')
                idx = self.active_embedding.count() - 1
            self.active_embedding.setCurrentIndex(idx)
        else:
            self.active_embedding.setCurrentIndex(1 if curr_emb.get('enabled') else 0)
        self.active_embedding.blockSignals(False)

        # Rerank options
        self.active_rerank.blockSignals(True)
        self.active_rerank.clear()
        self.active_rerank.addItem(f"按{performance}：{MODEL_CATALOG['rerank'][performance_tier]['name']}" if performance_tier else
                                   '自动选择已安装的重排模型（受本地模型开关控制）', userData='default')
        self.active_rerank.addItem('【云端API】使用检索API中配置的重排服务 (Cohere/Jina兼容)', userData='api')
        for tier in ('small', 'base', 'large'):
            cfg = MODEL_CATALOG['rerank'][tier]
            if is_installed('rerank', tier):
                self.active_rerank.addItem(f"【本地{cfg['tier_name'].split()[0]}】{cfg['name']}", userData=('local', 'rerank', tier))
        curr_rerank = retrieval.get('rerank') or {}
        if curr_rerank.get('enabled') and curr_rerank.get('mode') == 'local':
            model_path = curr_rerank.get('model_path')
            idx = 0
            for i in range(2, self.active_rerank.count()):
                d = self.active_rerank.itemData(i)
                if isinstance(d, tuple) and d[1] == 'rerank':
                    cfg = MODEL_CATALOG['rerank'][d[2]]
                    if model_path and Path(model_path).resolve() == (project_root() / cfg['target_dir']).resolve():
                        idx = i
                        break
            if idx == 0:
                self.active_rerank.addItem('已配置的本地重排：' + curr_rerank.get('model', ''), userData='custom')
                idx = self.active_rerank.count() - 1
            self.active_rerank.setCurrentIndex(idx)
        else:
            self.active_rerank.setCurrentIndex(1 if curr_rerank.get('enabled') else 0)
        self.active_rerank.blockSignals(False)

        # LLM options
        self.active_llm.blockSignals(True)
        self.active_llm.clear()
        has_llm = False
        for tier in ('small', 'base', 'large'):
            cfg = MODEL_CATALOG['llm'][tier]
            if is_installed('llm', tier):
                has_llm = True
                self.active_llm.addItem(f"【本地{cfg['tier_name'].split()[0]}】{cfg['name']}", userData=tier)
        if not has_llm:
            self.active_llm.addItem('未安装本地大模型（可在上方一键下载）', userData=None)
        self.active_llm.blockSignals(False)

        # Device preference
        saved_device = profile.get('local_model_device') or retrieval.get('device') or 'auto'
        self.local_device.blockSignals(True)
        for i in range(self.local_device.count()):
            if self.local_device.itemData(i) == saved_device:
                self.local_device.setCurrentIndex(i)
                break
        self.local_device.blockSignals(False)

    def apply_active_models(self):
        profile = load_profile(self.house.profile_path)
        retrieval = profile.setdefault('retrieval_api', {})
        emb_data = self.active_embedding.currentData()
        rerank_data = self.active_rerank.currentData()
        llm_data = self.active_llm.currentData()
        device_choice = self.local_device.currentData() or 'auto'
        from retrieval_api import validate
        try:
            for kind, selection in (('embedding', emb_data), ('rerank', rerank_data)):
                if selection == 'api':
                    validate({**retrieval.get(kind, {}), 'enabled': True, 'mode': 'api'}, kind)
        except ValueError as exc:
            toast(self.house, 'error', str(exc), '检索服务配置不完整')
            return
        profile['local_model_device'] = device_choice
        retrieval['device'] = device_choice
        from model_downloader import MODEL_CATALOG, project_root, runtime_manifest
        import house_settings
        machine_settings=house_settings.load(project_root())
        machine_settings['inference_device']=device_choice
        house_settings.save(machine_settings,project_root())

        if emb_data == 'default':
            retrieval.setdefault('embedding', {})['enabled'] = False
        elif emb_data == 'custom':
            retrieval['embedding']['device'] = device_choice
        elif emb_data == 'api':
            retrieval.setdefault('embedding', {})['mode'] = 'api'
            retrieval['embedding']['enabled'] = True
        elif isinstance(emb_data, tuple) and emb_data[0] == 'local':
            _, kind, tier = emb_data
            cfg = MODEL_CATALOG['embedding'][tier]
            p = (project_root() / cfg['target_dir']).resolve()
            retrieval['embedding'] = {
                'enabled': True,
                'mode': 'local',
                'model': cfg['name'],
                'dimension': cfg['dimension'],
                'model_path': str(p),
                'pooling': cfg['pooling'],
                'query_prefix': cfg.get('query_prefix', ''),
                'document_prefix': cfg.get('document_prefix', ''),
                'device': device_choice,
                'base_url': '',
            }

        if rerank_data == 'default':
            retrieval.setdefault('rerank', {})['enabled'] = False
        elif rerank_data == 'custom':
            retrieval['rerank']['device'] = device_choice
        elif rerank_data == 'api':
            retrieval.setdefault('rerank', {})['mode'] = 'api'
            retrieval['rerank']['enabled'] = True
        elif isinstance(rerank_data, tuple) and rerank_data[0] == 'local':
            _, kind, tier = rerank_data
            cfg = MODEL_CATALOG['rerank'][tier]
            p = (project_root() / cfg['target_dir']).resolve()
            retrieval['rerank'] = {
                'enabled': True,
                'mode': 'local',
                'model': cfg['name'],
                'model_path': str(p),
                'device': device_choice,
                'base_url': '',
            }

        runtime_path = project_root() / 'build/message-classifier-runtime/runtime.json'
        if runtime_path.is_file():
            try:
                curr_runtime = json.loads(runtime_path.read_text('utf-8'))
                curr_runtime['device'] = device_choice
                runtime_path.write_text(json.dumps(curr_runtime, indent=2, ensure_ascii=False), encoding='utf-8')
            except Exception:
                pass

        if llm_data and llm_data in MODEL_CATALOG['llm']:
            cfg = MODEL_CATALOG['llm'][llm_data]
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            existing=json.loads(runtime_path.read_text('utf-8')) if runtime_path.is_file() else {}
            runtime_info=runtime_manifest(cfg['filename'],existing)
            runtime_info['device']=device_choice
            runtime_path.write_text(json.dumps(runtime_info, indent=2, ensure_ascii=False), encoding='utf-8')

        if (isinstance(emb_data, tuple) and emb_data[0] == 'local') or (isinstance(rerank_data, tuple) and rerank_data[0] == 'local') or llm_data:
            import house_settings
            house_settings.set_local_models_enabled(True, ROOT)
            self.local_models.setChecked(True)

        write_profile(self.house.profile_path, profile)
        self.service_apis.load(profile)
        toast(self.house, 'success', '生效模型设置已保存；向量与重排模块将使用所选配置。', 'Settings')

    def load(self):
        profile = load_profile(self.house.profile_path)
        api = profile.get('model_api') or {}
        info = provider_context.info(api)
        self.compact_info.setText(f'当前 {info.provider} 兼容窗口：{info.window:,} token；达到 90%（{info.compact_at:,}）自动压缩并整理记忆。')
        self.compact.setPlaceholderText(str(info.window))
        provider = api.get('provider', 'codex')
        multi = api.get('multi_agent') or {}
        active = multi.get('active_providers') or api.get('active_providers')
        if not active:
            if provider in ('dsh', 'deepseek_harness'): active = ['dsh']
            elif provider == 'antigravity': active = ['antigravity']
            elif provider in ('custom', 'deepseek', 'api'): active = ['custom']
            elif provider == 'claude': active = ['claude']
            elif isinstance(provider, str) and provider.startswith('webchat_'): active = [provider]
            elif provider in self.external_checks: active = [provider]
            else: active = ['codex']

        saved_order = multi.get('handover_order') or api.get('handover_order') or active
        master_key = multi.get('master') or api.get('master_provider') or provider
        if master_key in ('deepseek_harness',): master_key = 'dsh'
        if master_key in ('deepseek', 'api'): master_key = 'custom'

        for btn in (self.dsh, self.claude, self.codex, self.antigravity, self.deepseek, self.webchat, self.webchat_gpt, self.webchat_gemini, *self.external_checks.values()):
            btn.blockSignals(True)
        self.dsh.setChecked('dsh' in active or 'deepseek_harness' in active)
        self.claude.setChecked('claude' in active)
        self.codex.setChecked('codex' in active)
        self.antigravity.setChecked('antigravity' in active)
        self.deepseek.setChecked('custom' in active or 'deepseek' in active or 'api' in active)
        self.webchat.setChecked('webchat_deepseek' in active)
        self.webchat_gpt.setChecked('webchat_chatgpt' in active)
        self.webchat_gemini.setChecked('webchat_gemini' in active)
        for key, button in self.external_checks.items():
            button.setChecked(key in active)
        self.adapter_settings.load(profile)
        for btn in (self.dsh, self.claude, self.codex, self.antigravity, self.deepseek, self.webchat, self.webchat_gpt, self.webchat_gemini, *self.external_checks.values()):
            btn.blockSignals(False)

        self.quota_guard_chk.setChecked(multi.get('quota_guard', True))
        self.capability_route_chk.setChecked(multi.get('capability_routing', True))
        self.claude_english_chk.setChecked(multi.get('claude_english', True))

        self.sync_handover_list_with_checks(preferred_order=saved_order, preferred_master=master_key)

        dsh_cfg = PROVIDER_DEFAULTS.get('dsh', {})
        dsh_data = api.get('dsh_settings') or {}
        if provider in ('dsh', 'deepseek_harness'):
            d_model = api.get('model') or dsh_data.get('model') or dsh_cfg.get('model', 'deepseek-flash')
            d_effort = api.get('api_effort') or dsh_data.get('effort') or 'low'
            d_url = api.get('api_base_url') or dsh_data.get('base_url') or dsh_cfg.get('base_url', '')
            d_path = api.get('dsh_path') or dsh_data.get('path', '')
        else:
            d_model = dsh_data.get('model') or dsh_cfg.get('model', 'deepseek-flash')
            d_effort = dsh_data.get('effort') or 'low'
            d_url = dsh_data.get('base_url') or dsh_cfg.get('base_url', '')
            d_path = dsh_data.get('path', '')

        self.dsh_url.setText(d_url)
        self.dsh_model.setCurrentText(d_model)
        self.dsh_effort.setCurrentText(d_effort)
        self.dsh_path.setText(d_path or self._default_dsh_path())
        self.dsh_key.clear()
        web_models = api.get('webchat_models') if isinstance(api.get('webchat_models'), dict) else {}
        web_efforts = api.get('webchat_efforts') if isinstance(api.get('webchat_efforts'), dict) else {}
        for key, field in self.web_model_fields.items():
            field.setCurrentText(str(web_models.get(key) or field.currentText()))
        for key, field in self.web_effort_fields.items():
            field.setCurrentText(str(web_efforts.get(key) or field.currentText()))
        web_agent = api.get('web_agent') if isinstance(api.get('web_agent'), dict) else {}
        web_agents = api.get('web_agent_providers') if isinstance(api.get('web_agent_providers'), dict) else {}
        for key, field in self.web_agent_fields.items():
            cfg = web_agents.get(key) if isinstance(web_agents.get(key), dict) else web_agent
            field.setChecked(bool(cfg.get('enabled', False)))
        for key, field in self.web_context_fields.items():
            cfg = web_agents.get(key) if isinstance(web_agents.get(key), dict) else web_agent
            try:
                value = int(cfg.get('context_chars', 64_000))
            except (TypeError, ValueError):
                value = 64_000
            index = field.findData(value)
            field.setCurrentIndex(max(0, index))

        self.ag_model.setText(api.get('antigravity_model') or AG_MODELS[0])
        self.ag_effort.setCurrentText(api.get('antigravity_effort') or 'low')
        self.ag_path.setText(api.get('antigravity_path', ''))
        self.claude_model.setText(api.get('claude_model') or CLAUDE_MODELS[0])
        self.claude_effort.setCurrentText(api.get('claude_effort') or 'low')
        self.claude_path.setText(api.get('claude_path', ''))
        self.protocol.setCurrentText(api.get('api_protocol') or PROTOCOLS[0])
        self.api_effort.setCurrentText(api.get('api_effort') or 'low')
        self.base_url.setText(api.get('api_base_url', ''))
        self.deepseek_model.setText(api.get('model', ''))
        self.compact.setText(str(api.get('compact_tokens') or ''))
        self.codex_model.setCurrentText(api.get('codex_model', 'gpt-6-luna'))
        self.codex_path.setText(api.get('codex_path', ''))
        self.update_efforts()
        self.effort.setCurrentText(api.get('codex_effort', 'low'))
        self.fast.setChecked(api.get('codex_fast', False))
        self.local_fallback.setChecked(api.get('local_fallback', False))
        self.local_thinking.setCurrentText(next((label for label, value in LOCAL_THINKING.items()
                                                 if value == api.get('local_thinking', 'off')), next(iter(LOCAL_THINKING))))
        self.key.clear()
        self.show_key_state()
        saved_effort = profile.get('research_effort', 'low')
        saved_fast = profile.get('research_fast', True)
        self.update_research_model_ui()
        if saved_effort:
            self.research_effort.setCurrentText(saved_effort)
        self.research_fast.setChecked(saved_fast)
        computer = profile.get('computer_tools') or {}
        self.computer_enabled.setChecked(computer.get('enabled', False))
        self.computer_commands.setChecked(computer.get('commands', False))
        self.computer_project.setText(computer.get('project_path', ''))
        self.computer_full_access.setChecked(computer.get('full_computer_access', False))
        self.chat_config_edits.setChecked(bool(profile.get('chat_config_edits', False)))
        self.agent_history_sharing.setChecked(bool(profile.get('agent_history_sharing', True)))
        self.computer_mode.setCurrentIndex(1 if computer.get('mode') == 'read_only' or computer.get('read_only') is True else 0)
        shell_index = self.computer_shell.findData(str(computer.get('shell') or 'pwsh'))
        self.computer_shell.setCurrentIndex(max(0, shell_index))
        self.update_approval_modes(preferred=computer.get('approval_mode'))
        self.load_api_profiles(profile, api)
        self.update_download_tiers()
        self.refresh_active_models_dropdown(profile)
        self.refresh_voice_settings(profile)
        self.refresh_voice_input(profile)
        self.refresh_media_settings(profile)

        self.background_settings.load(profile)

    def refresh_media_settings(self, profile):
        cfg = profile.get('media_reply') if isinstance(profile.get('media_reply'), dict) else {}
        index = self.media_mode.findData(str(cfg.get('mode') or 'sometimes'))
        self.media_mode.setCurrentIndex(max(0, index))
        try:
            self.media_ratio.setValue(int(cfg.get('ratio', 25)))
        except (TypeError, ValueError):
            self.media_ratio.setValue(25)
        self.media_proactive.setChecked(bool(cfg.get('proactive', True)))

    def save_media_settings(self):
        profile = load_profile(self.house.profile_path)
        profile['media_reply'] = {'mode': self.media_mode.currentData() or 'sometimes',
                                  'ratio': self.media_ratio.value(),
                                  'proactive': self.media_proactive.isChecked()}
        write_profile(self.house.profile_path, profile)
        toast(self.house, 'success', '图片/视频回复设置已保存，下一条消息起生效。', '图片/视频')

    def save_research_model(self):
        try:
            profile = load_profile(self.house.profile_path)
            provider = self.current_provider_key()
            effort = self.research_effort.currentText()
            fast = self.research_fast.isChecked()
            if provider == 'codex':
                generation_settings({**profile.get('model_api', {}), 'codex_effort': effort,
                                     'codex_fast': fast})
            profile.update(research_effort=effort, research_fast=fast)
            write_profile(self.house.profile_path, profile)
            toast(self.house, 'success', f'整理模型设置已保存（{effort}，快速模式：{"开" if fast else "关"}）；下次整理生效。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save failed')

    def show_key_state(self):
        self.key_state.setText('接口密钥已保存' if key_path(self.house.friend_dir, self.profile_id or 'chat').exists()
                               else '接口密钥尚未设置')
        has_key = key_path(self.house.friend_dir, 'dsh').exists()
        self.dsh_key_state.setText('接口密钥已保存' if has_key else '接口密钥未设置（可使用本机桌面客户端账号）')

    def save(self):
        house = self.house
        try:
            if self.deepseek.isChecked() and not self.api_model_section.isChecked():
                self.service_apis.save('retrieval' if self.api_retrieval_section.isChecked() else 'functions')
                toast(house, 'success', 'API 配置已保存；各 CLI 与模型 API 可使用已启用的功能。')
                return

            adapter_values = self.adapter_settings.values()
            active = self.get_checked_providers()
            if not active:
                active = ['codex']
            order = self.get_handover_order()
            if not order:
                order = list(active)
            master_key = self.master_agent_combo.currentData() or (order[0] if order else active[0])
            multi_enabled = len(active) > 1

            ag = {'antigravity_model': self.ag_model.text().strip(), 'antigravity_effort': self.ag_effort.currentText()}
            if 'antigravity' in active:
                ag_settings(ag)
            claude = {'claude_model': self.claude_model.text().strip(), 'claude_effort': self.claude_effort.currentText()}
            if 'claude' in active:
                claude_settings(claude)
            self.keep_api_draft()
            custom = self.current_api_values()
            compact = self.compact.text().strip()
            if compact and (not compact.isdigit() or int(compact) < MIN_COMPACT_TOKENS):
                raise ValueError(f'自动压缩请填不小于 {MIN_COMPACT_TOKENS} 的整数（token 数），或留空用默认')
            custom['api_key_id'] = self.profile_id or 'chat'
            if 'custom' in active:
                api_settings(custom)
                if not self.key.text().strip() and not self.chat_keys.get(custom['api_key_id']) and not key_path(house.friend_dir, custom['api_key_id']).exists():
                    raise ValueError('请先输入 API Key')
                if self.key.text().strip():
                    save_key(house.friend_dir, self.key.text(), custom['api_key_id'])

            dsh_vals = {}
            if 'dsh' in active:
                cfg = PROVIDER_DEFAULTS.get('dsh', {})
                p_url = self.dsh_url.text().strip() or cfg.get('base_url', '')
                p_model = self.dsh_model.currentText().strip() or cfg.get('model', 'deepseek-flash')
                p_effort = self.dsh_effort.currentText().strip() or 'low'
                p_path = self.dsh_path.text().strip()
                p_key = self.dsh_key.text().strip()
                key_id = cfg.get('key_id', 'dsh')
                if p_key:
                    save_key(house.friend_dir, p_key, key_id)
                dsh_vals = {
                    'provider': 'dsh',
                    'api_protocol': cfg.get('protocol', 'openai-completions'),
                    'api_base_url': p_url,
                    'model': p_model,
                    'api_effort': p_effort,
                    'api_key_id': key_id,
                }
                if p_path:
                    dsh_vals['dsh_path'] = p_path

            web_models = {key: field.currentText().strip() for key, field in self.web_model_fields.items()}
            web_efforts = {key: field.currentText().strip() for key, field in self.web_effort_fields.items()}
            profile = load_profile(house.profile_path)
            api = dict(profile.get('model_api') or {})
            api['webchat_models'] = web_models
            api['webchat_efforts'] = web_efforts
            api['web_agent_providers'] = {
                key: {'enabled': field.isChecked(),
                      'context_chars': max(8000, min(1_000_000, int(
                          re.sub(r'\D', '', self.web_context_fields[key].currentText()) or 64000)))}
                for key, field in self.web_agent_fields.items()}
            api['web_agent'] = {
                'enabled': any(field.isChecked() for field in self.web_agent_fields.values()),
                'max_steps': 24,
            }
            api['provider'] = master_key
            api['master_provider'] = master_key
            api['active_providers'] = active
            api['handover_order'] = order
            api['multi_agent_enabled'] = multi_enabled
            api['multi_agent'] = {
                'enabled': multi_enabled,
                'master': master_key,
                'collaborators': [p for p in active if p != master_key],
                'active_providers': active,
                'handover_order': order,
                'quota_guard': self.quota_guard_chk.isChecked(),
                'capability_routing': self.capability_route_chk.isChecked(),
                'claude_english': self.claude_english_chk.isChecked(),
            }

            api.update(codex_model=self.codex_model.currentText(),
                       codex_effort=self.effort.currentText(), codex_fast=self.fast.isChecked())
            generation_settings(api)
            if 'custom' in active:
                api.update(custom)
            if 'dsh' in active:
                api.update(dsh_vals)
            if master_key == 'custom':
                api.update(custom)
                api['provider'] = 'custom'
            elif master_key == 'dsh':
                api.update(dsh_vals)
                api['provider'] = 'dsh'
            elif master_key == 'claude':
                api.update(claude)
                api['provider'] = 'claude'
            elif master_key == 'antigravity':
                api.update(ag)
                api['provider'] = 'antigravity'
            elif master_key == 'codex':
                api['provider'] = 'codex'
            elif str(master_key).startswith('webchat_'):
                api['provider'] = master_key

            dsh_cfg = PROVIDER_DEFAULTS.get('dsh', {})
            d_url = self.dsh_url.text().strip() or dsh_cfg.get('base_url', '')
            d_model = self.dsh_model.currentText().strip() or dsh_cfg.get('model', 'deepseek-flash')
            d_effort = self.dsh_effort.currentText().strip() or 'low'
            d_path = self.dsh_path.text().strip()
            d_key = self.dsh_key.text().strip()
            if d_key:
                save_key(house.friend_dir, d_key, dsh_cfg.get('key_id', 'dsh'))
            api['dsh_settings'] = {
                'base_url': d_url,
                'model': d_model,
                'effort': d_effort,
                'path': d_path,
            }
            if d_path:
                api['dsh_path'] = d_path
            else:
                api.pop('dsh_path', None)

            for old_pk in ('qwen', 'hunyuan', 'glm', 'kimi'):
                api.pop(f'{old_pk}_settings', None)
                api.pop(f'{old_pk}_path', None)

            api.update(claude)
            api.update(ag)
            if self.codex_path.text().strip():
                api['codex_path'] = self.codex_path.text().strip()
            else:
                api.pop('codex_path', None)
            if self.claude_path.text().strip():
                api['claude_path'] = self.claude_path.text().strip()
            else:
                api.pop('claude_path', None)
            if self.ag_path.text().strip():
                api['antigravity_path'] = self.ag_path.text().strip()
            else:
                api.pop('antigravity_path', None)
            if compact:
                api['compact_tokens'] = int(compact)
            else:
                api.pop('compact_tokens', None)
            api['local_fallback'] = self.local_fallback.isChecked()
            api['local_thinking'] = LOCAL_THINKING[self.local_thinking.currentText()]
            profile['agent_adapters'] = adapter_values
            profile['model_api'] = api
            # Persist all named connections even when another agent is primary.
            self.keep_api_draft()
            profile['api_profiles'] = {identifier: clean_connection(value, identifier=identifier)
                                       for identifier, value in self.api_profiles.items()}
            for identifier, value in self.chat_keys.items():
                save_key(house.friend_dir, value, identifier)
            write_profile(house.profile_path, profile)
            if api['local_fallback']:
                for qq_id in house.records:  # only one friend may use the local model
                    path = ROOT / 'friends' / qq_id / 'friend.json'
                    if qq_id == house.current_qq or not path.exists():
                        continue
                    other = load_profile(path)
                    if (other.get('model_api') or {}).get('local_fallback'):
                        other['model_api']['local_fallback'] = False
                        write_profile(path, other)
            self.key.clear()
            self.chat_keys.clear()  # No need to retain decrypted secrets after save.
            for pk in ('dsh', 'qwen', 'hunyuan', 'glm', 'kimi'):
                getattr(self, f'{pk}_key').clear()
            self.show_key_state()
            if multi_enabled:
                toast(house, 'success', f'已保存当前好友的 {len(active)} 种智能体及原有三个开关；节点配置见“协作流程”。')
            else:
                toast(house, 'success', f'单智能体设置已保存（当前生效：{self.PROVIDER_TITLES.get(master_key, (master_key,))[0]}）')
        except Exception as exc:
            toast(house, 'error', str(exc), "保存设置")

    def open_usage(self):
        from PySide6.QtCore import QTimer
        from usage_accounting import display, summary
        directory = self.house.friend_dir
        dialog = TextDialog(self.house, '当前好友全部模型用量与额度', '正在读取当前好友全部模型用量…',
                            '每 5 秒自动刷新。')
        timer = QTimer(dialog)

        def refresh():
            def shown(value):
                if dialog.isVisible():
                    set_text_keep(dialog.body, value)
            run_async(lambda _: display(summary(directory)), shown,
                      lambda exc: shown('用量读取失败：' + str(exc)), pool=LONG_POOL)
        timer.timeout.connect(refresh)
        timer.start(5000)
        refresh()
        dialog.exec()
        timer.stop()
