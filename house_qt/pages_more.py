"""Chat history, mood, long-term memory and proactive chat pages."""
from ui_locale import tr

import html
import json
import time
import uuid
from datetime import datetime

from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QTableWidgetItem
from qfluentwidgets import (BodyLabel, CheckBox, ComboBox, LineEdit, PlainTextEdit, PrimaryPushButton,
                            PushButton, TableWidget, TextEdit)

import house_status
from house_status import (ROOT, care_configuration, followup_values, invoke_bridge, load_profile, preview_proactive,
                          proactive_debug_text, read_care_slots, read_reply_pause, read_round_pause, read_settings,
                          write_profile)
from store import FriendStore

from .common import (LONG_POOL, MUTED, RED, Card, Page, TextDialog, body, confirm, hint, notice, quiet, row,
                     run_async, set_text_keep, toast)


def small_edit(width=80, text=''):
    edit = LineEdit()
    edit.setFixedWidth(width)
    edit.setText(text)
    return edit


def read_only_text(height):
    edit = PlainTextEdit()
    edit.setReadOnly(True)
    edit.setMinimumHeight(height)
    return edit


class HistoryPage(Page):
    def __init__(self, house):
        super().__init__(house, 'historyPage')
        self.text = TextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(520)
        self.count = hint('')
        clear, older, refresh = PushButton(tr('Clear Local Chat')), PushButton(tr('Load Older')), PushButton(tr('Refresh'))
        clear.setStyleSheet(f'PushButton {{ color: {RED}; }}')
        clear.clicked.connect(self.clear)
        older.clicked.connect(self.load_older)
        refresh.clicked.connect(lambda: self.refresh())
        self.add(Card('Local Chat History').add(hint('Only shows messages stored locally for this friend'), self.text,
                                     row(clear, self.count, None, older, refresh)))
        self.limit = 100
        self._history_owner = None
        self._history_request = 0
        self._history_pending = None
        self._rendered_html = None

    def load(self):
        self.limit = 100
        self.refresh()

    shown = lambda self: self.refresh() if self.loaded_for == self.house.current_qq else Page.shown(self)

    def refresh(self, scroll_top=False):
        owner, directory, limit = self.house.current_qq, self.house.friend_dir, self.limit
        key = (owner, directory, limit)
        if self._history_pending == key:
            return
        if self._history_owner != owner:
            self._history_owner = owner
            self._rendered_html = None
            self.text.clear()
            self.count.setText('正在读取当前好友的聊天记录…')
        self._history_request += 1
        request = self._history_request
        self._history_pending = key

        def current():
            return (request == self._history_request and owner == self.house.current_qq
                    and directory == self.house.friend_dir)

        def read(_):
            store = FriendStore(directory)
            try:
                return store.recent_messages(limit), store.message_count()
            finally:
                store.close()

        def show(value):
            if request == self._history_request:
                self._history_pending = None
            if not current():
                return
            rows, count = value
            parts = []
            for item in rows:
                try:
                    stamp = datetime.fromisoformat(item['created_at']).strftime('%m-%d %H:%M')
                except ValueError:
                    stamp = item['created_at']
                who = '好友' if item['direction'] == 'friend' else '我'
                parts.append(f'<p style="margin:0 0 10px 0"><span style="color:{MUTED}">{stamp}  {who}</span><br>'
                             + html.escape(item['body']).replace('\n', '<br>') + '</p>')
            content = ''.join(parts) or '暂无本地聊天记录'
            bar = self.text.verticalScrollBar()
            position, following = bar.value(), bar.value() >= bar.maximum() - 3
            if content != self._rendered_html:
                self.text.setHtml(content)
                self._rendered_html = content
                bar.setValue(bar.maximum() if following and not scroll_top else position)
            if scroll_top:
                bar.setValue(bar.minimum())
            self.count.setText(f'共 {count} 条消息，当前显示最近 {len(rows)} 条')

        def failed(exc):
            if request == self._history_request:
                self._history_pending = None
            if current():
                toast(self.house, 'error', f'读取聊天记录失败：{exc}')
        run_async(read, show, failed)

    def load_older(self):
        self.limit += 100
        self.refresh(scroll_top=True)

    def clear(self):
        house = self.house
        code, status = invoke_bridge('status')
        if code:
            toast(house, 'error', status, 'Unable to Confirm Bridge Status')
            return
        if status.lower().startswith('running') or status.startswith('Online'):
            notice(house, 'Please Stop Bridge First', 'Please stop backend bridge before clearing local chat history.')
            return
        name = house.records[house.current_qq].get('display_name', house.current_qq)
        if not confirm(house, 'Clear Local Chat History', f'确定清除“{name}”的全部本地聊天记录？\n\n'
                       '此操作无法撤销；QQ 聊天、消息游标、长期记忆和心情记录会保留。', yes='Clear'):
            return
        code, status = invoke_bridge('status')
        if code or status.lower().startswith('running') or status.startswith('Online'):
            toast(house, 'warning', 'Bridge is running; please stop it before clearing.')
            return
        try:
            store = FriendStore(house.friend_dir)
            try:
                count = store.clear_messages()
            finally:
                store.close()
            self.limit = 100
            self.refresh()
            toast(house, 'success', f'已清除 {count} 条本地聊天记录。')
        except Exception as exc:
            toast(house, 'error', str(exc), 'Clear Failed')


class EmotionPage(Page):
    def __init__(self, house):
        super().__init__(house, 'emotionPage')
        self.current = body()
        self.history = read_only_text(360)
        older, refresh = PushButton(tr('Load Older')), PushButton(tr('Refresh Mood'))
        older.clicked.connect(self.load_older)
        refresh.clicked.connect(self.refresh)
        self.mood_enabled, self.mood_after = CheckBox(tr('React emotionally when continuously ignored')), small_edit(48)
        save = PrimaryPushButton(tr('Save'))
        save.clicked.connect(self.save_mood)
        self.add(Card('Continuously Ignored Behavior').add(
            row(self.mood_enabled, BodyLabel(tr('After ignored')), self.mood_after, BodyLabel("次"), None, save),
            hint('When proactive chats, continuations, or care messages are ignored consecutively, persona feels slightly disappointed (per character style), speaks more reservedly without blaming or begging for replies, and logs it in mood records below. When friend returns, the first sentence reflects slight emotion before recovering.')))
        self.policy_state = hint('')
        self.add(Card('Mood Records').add(self.policy_state, self.current, self.history, row(None, older, refresh)))
        self.limit = 100

    def load(self):
        self.limit = 100
        profile = load_profile(self.house.profile_path)
        self.policy_state.setVisible(not str(profile.get('emotion_policy') or '').strip())
        self.policy_state.setText('情绪判断未开启：在「人设与回复风格」页填写「情绪策略」后，每次回复才会判断并记录心情。')
        mood = followup_values(profile)
        self.mood_enabled.setChecked(mood['ignored_mood_enabled'])
        self.mood_after.setText(str(mood['ignored_mood_after']))
        self.refresh()

    def save_mood(self):
        try:
            house_status.save_ignored_mood(self.house.profile_path, {
                'ignored_mood_enabled': self.mood_enabled.isChecked(), 'ignored_mood_after': self.mood_after.text()})
            toast(self.house, 'success', 'Saved; takes effect on next continuation or proactive evaluation.')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save Failed')

    shown = lambda self: self.refresh() if self.loaded_for == self.house.current_qq else Page.shown(self)

    def load_older(self):
        self.limit += 100
        self.refresh()

    def refresh(self):
        try:
            store = FriendStore(self.house.friend_dir)
            try:
                raw = store.get('emotion_state')
                rows = store.recent_emotions(self.limit)
            finally:
                store.close()
            state = json.loads(raw) if raw else {}
            updated = float(state.get('updated_at', 0))
            if updated:
                stamp = datetime.fromtimestamp(updated).strftime('%m-%d %H:%M')
                if time.time() - updated >= 5 * 3600:
                    self.current.setText(f'规则状态：好友情绪不确定，角色平静\n上次判断：{stamp}（已过期）')
                else:
                    self.current.setText(
                        f"当前：好友{tr(state.get('user', 'Uncertain'))} · 角色{tr(state.get('self', 'Calm'))}\n"
                        + (f"情绪强度：{state['self_intensity']}/100\n" if state.get('self_intensity') is not None else '')
                        + (f"原因：{state['self_reason']}\n" if state.get('self_reason') else '')
                        + f'上次判断：{stamp}')
            else:
                self.current.setText(tr('Current: Friend Uncertain · Persona Calm\nNo mood detection records yet'))
            lines = []
            for item in rows:
                try:
                    stamp = datetime.fromisoformat(item['created_at']).strftime('%m-%d %H:%M')
                except ValueError:
                    stamp = item['created_at']
                lines.append(f"{stamp}  {tr(item['source'])}：好友{tr(item['user_label'])} · 角色{tr(item['self_label'])}"
                             + (f" ({item['self_intensity']}/100)" if item.get('self_intensity') is not None else '')
                             + (f"\n  {item['self_reason']}" if item.get('self_reason') else ''))
            set_text_keep(self.history, '\n'.join(lines) or tr('Mood history is recorded starting from this version; previous versions stored current state only.'))
        except Exception as exc:
            toast(self.house, 'error', f'读取心情记录失败：{exc}')


class MemoryPage(Page):
    def __init__(self, house):
        super().__init__(house, 'memoryPage')
        self.text = PlainTextEdit()
        self.text.setMinimumHeight(260)
        self.lock = CheckBox(tr('Lock new rows (protected from background consolidation)'))
        self.lock.setChecked(True)
        # 2026-10-26 实机验收：这一排按钮还是英文（Reset Memory / Roll Back / …），页面其它地方已是中文。
        buttons = {k: PushButton(label) for k, label in (
            ('reset', '重置记忆'), ('restore', '回退上一版'),
            ('entries', '条目与来源'), ('reload', '重新读取'))}
        save = PrimaryPushButton(tr('Save Memory'))
        for k, slot in (('reset', self.reset), ('restore', self.restore), ('entries', self.entries), ('reload', self.reload)):
            buttons[k].clicked.connect(slot)
        save.clicked.connect(self.save)
        self.add(Card('长期记忆').add(hint('每行一条记忆。勾选后新增的条目会被锁定保护；已有条目在下方逐条锁定。'
                                         '重置记忆与清空聊天记录是两件事。'), self.text,
                                 self.lock, row(buttons['reset'], buttons['restore'], buttons['entries'], None,
                                                buttons['reload'], save)))
        # 2026-10-10 user: each entry can be locked / unlocked, moved to another slot or marked no longer valid;
        # Pinned = in the three core blocks of every prompt, otherwise it is recalled when the chat matches it
        from memory_management import CATEGORIES
        self.table = TableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([tr('Category'), tr('Content'), tr('Locked'), tr('Pinned'), tr('Date Mentioned'), tr('Expires')])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column, width in ((0, 80), (2, 50), (3, 50), (4, 90), (5, 90)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(260)
        self.slot = ComboBox()
        for category in CATEGORIES:
            self.slot.addItem(tr(category), userData=category)
        action_labels = {'lock': 'Lock / Unlock', 'category': tr('Move to Category'), 'retire': tr('Retire Entry')}
        actions = {k: PushButton(v) for k, v in action_labels.items()}
        actions['lock'] = actions['lock']
        actions['移到格子'] = actions['category']
        actions['标为失效'] = actions['retire']
        actions['lock'].clicked.connect(lambda: self.edit_selected('lock'))
        actions['category'].clicked.connect(lambda: self.edit_selected('category'))
        actions['retire'].clicked.connect(lambda: self.edit_selected('retire'))
        self.add(Card('Memory Entries').add(
            hint('Pinned: included in every prompt (user persona, relationship, recent status blocks, each with character limit). Other entries are recalled when relevant to chat context, or queried directly by the model. Status and plans expire automatically when due; retired entries are archived in "Entries & Sources".'),
            self.table, row(actions['lock'], self.slot, actions['category'], None, actions['retire'])))
        self.loaded_text, self.revision, self.rows = '', 0, []

    def unsaved(self):
        return self.loaded_for is not None and self.text.toPlainText().strip() != self.loaded_text

    def load(self):
        self.loaded_text = self.text.toPlainText().strip()  # friend changed: the guard already allowed it
        self.reload()

    def reload(self):
        if self.text.toPlainText().strip() != self.loaded_text:
            toast(self.house, 'warning', 'Persistent memory has unsaved changes. Please save before reloading.')
            return
        try:
            from memory_management import entries, live, pinned_ids
            store = FriendStore(self.house.friend_dir)
            try:
                memory = store.memory()
                self.revision = store.memory_revision()
                rows = entries(store)
            finally:
                store.close()
            self.text.setPlainText(memory)
            self.loaded_text = memory.strip()
            pinned = pinned_ids(rows)
            self.rows = [item for item in rows if live(item)]
            self.table.setRowCount(len(self.rows))
            for index, item in enumerate(self.rows):
                cells = (tr(item.get('category') or 'Other'), item['text'], '是' if item['locked'] else '',
                         '是' if item['id'] in pinned else '', (item.get('valid_from') or item['updated_at'])[:10],
                         (item.get('expires_at') or '')[:10])
                for column, value in enumerate(cells):
                    self.table.setItem(index, column, QTableWidgetItem(value))
        except Exception as exc:
            toast(self.house, 'error', f'读取长期记忆失败：{exc}')

    def edit_selected(self, action):
        from memory_management import edit_entry
        selected = self.table.selectedItems()
        if not selected:
            toast(self.house, 'warning', 'Please select a memory entry in the table first.')
            return
        if self.text.toPlainText().strip() != self.loaded_text:
            toast(self.house, 'warning', 'Text above has unsaved changes. Please save or reload first.')
            return
        item = self.rows[selected[0].row()]
        if action == 'retire' and not confirm(self.house, 'Retire Memory', 'This memory will no longer be used for generation, but will be preserved in "Entries & Sources".',
                                              yes='Retire'):
            return
        changes = {'lock': {'locked': not item['locked']}, 'category': {'category': self.slot.currentData()},
                   'retire': {'retire': True}}[action]
        try:
            store = FriendStore(self.house.friend_dir)
            try:
                edit_entry(store, item['id'], self.revision, **changes)
            finally:
                store.close()
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Failed to Modify Memory')
            return
        self.reload()

    def save(self):
        content = self.text.toPlainText().strip()
        try:
            store = FriendStore(self.house.friend_dir)
            try:
                if store.memory().strip() != self.loaded_text:
                    toast(self.house, 'warning', 'Persistent memory was updated by background process; please reload before editing.')
                    return
                if not content:
                    toast(self.house, 'warning', 'To clear persistent memory, please use the dedicated Reset button.')
                    return
                store.set_memory(content, locked=self.lock.isChecked(), expected_revision=self.revision)
                self.revision = store.memory_revision()
            finally:
                store.close()
            self.loaded_text = content
            toast(self.house, 'success', 'Persistent memory saved; takes effect on next message generation.')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Failed to Save Memory')

    def reset(self):
        if not confirm(self.house, tr('Reset Persistent Memory'), 'Reset only persistent memory, memory versions, and phrasing candidates for this friend. Chat history, friend settings, and knowledge base are preserved. '
                       'Reset cannot be undone; future consolidation will only process new messages.', yes='Reset'):
            return
        store = FriendStore(self.house.friend_dir)
        try:
            store.reset_memory()
        finally:
            store.close()
        self.loaded_text = self.text.toPlainText().strip()
        self.reload()
        toast(self.house, 'success', 'Persistent memory reset for this friend; chat and knowledge base preserved.')

    def restore(self):
        if self.text.toPlainText().strip() != self.loaded_text:
            toast(self.house, 'warning', 'Please save memory edits before rolling back version.')
            return
        if not confirm(self.house, tr('Roll Back Memory'), 'Restore previous version of persistent memory for this friend? Current version will be backed up.', yes='Roll Back'):
            return
        store = FriendStore(self.house.friend_dir)
        try:
            store.restore_memory()
        except ValueError as exc:
            toast(self.house, 'warning', str(exc))
            return
        finally:
            store.close()
        self.reload()
        toast(self.house, 'success', 'Persistent memory rolled back; chat history preserved.')

    def entries(self):
        from memory_management import entries
        store = FriendStore(self.house.friend_dir)
        try:
            rows = entries(store)
            messages = {item['qq_id']: item['created_at'] for item in store.db.execute('SELECT qq_id,created_at FROM messages')}
        finally:
            store.close()
        # 2026-10-10 Phase 5: grouped by profile slot; replaced / retired entries are kept and listed with when and why
        from memory_management import CATEGORIES
        texts = {item['id']: item['text'] for item in rows}

        def describe(item):
            source = json.loads(item['sources'])
            line = ((tr('Locked') if item['locked'] else '可整理') + ' / ' + tr(item['kind']) + ' / 生效时间：'
                    + (item.get('valid_from') or item['updated_at'])[:16].replace('T', ' ')
                    + (' / 到期时间：' + item['expires_at'][:10] if item.get('expires_at') else '') + '\n' + item['text'] + '\n来源：'
                    + (', '.join(identifier + ' (' + messages.get(identifier, '原消息不可用') + ')' for identifier in source)
                       if source else '手动填写或旧版导入，没有原消息证据') + '\n关键词：' + ', '.join(json.loads(item['keywords'])))
            if not item['active']:
                line += '\n停用时间：' + (item.get('invalid_at') or '旧记录未记载')[:16].replace('T', ' ') + (
                    '，替代条目：' + texts.get(item['replaced_by'], '（已不存在）') if item.get('replaced_by') else '')
            return line
        details = []
        for slot in CATEGORIES:
            group = [item for item in rows if item['active'] and (item.get('category') or 'Other') == slot]
            if group:
                details.append(f'[{tr(slot)}]\n' + '\n\n'.join(describe(item) for item in group))
        retired = [item for item in rows if not item['active']]
        if retired:
            details.append('[已停用条目（保留供核查）]\n' + '\n\n'.join(describe(item) for item in retired))
        TextDialog(self.house, 'Friend Memory Entries & Sources', '\n\n'.join(details) or 'No persistent memories recorded').exec()


class ProactivePage(Page):
    def __init__(self, house):
        super().__init__(house, 'proactivePage')
        # 2026-10-11 user (Settings consolidation): on/off switches on status page, proactive style on Persona & Style
        # page; one save button for the whole page
        self.numbers = {key: small_edit(64) for key in ('rounds_min', 'rounds_max', 'bubbles_min', 'bubbles_max')}
        self.window_start, self.window_end = small_edit(), small_edit()
        self.reply_pause = small_edit()
        self.round_limit, self.round_pause = small_edit(64), small_edit()
        self.agenda_gap = small_edit(64)
        self.topics = LineEdit()
        save, replan = PrimaryPushButton(tr('Save Settings')), PushButton("按已保存设置重新规划")
        save.clicked.connect(self.save)
        replan.clicked.connect(self.replan)
        n = self.numbers
        self.add(Card("主动任务规则与触发", actions=(save,)).add(
            hint("总开关位于智能体控制台；角色风格与沟通偏好在“人设与指令”中设置。"),
            row(BodyLabel("每日目标轮次"), BodyLabel("最少"), n['rounds_min'], BodyLabel("最多"), n['rounds_max'],
                BodyLabel("每轮消息条数"), BodyLabel("最少"), n['bubbles_min'], BodyLabel("最多"), n['bubbles_max'], None),
            row(BodyLabel("活跃时段"), BodyLabel("开始"), self.window_start, BodyLabel("结束"), self.window_end, None),
            hint("使用本机时间，支持跨午夜；发送间隔会结合活跃时段和每日轮次调整。"),
            hint("每日上限限制主动消息轮次，下限用于补充合适的联系计划；模型说明不宜联系时会跳过。下方时段与暂停规则不限制模型制定待办计划。"),
            row(BodyLabel("未回复暂停（分钟）"), self.reply_pause, None),
            hint("可设置 1–10080 分钟；到时恢复，收到新消息后提前解除。"),
            row(BodyLabel("连续未回复轮次上限"), self.round_limit, BodyLabel("达到上限后暂停（分钟）"), self.round_pause, None),
            hint("每次完整发送计为一轮，收到来信后重新计数；普通回复和手动测试不计入。续话次数在「交互与回复偏好」设置；续话不受这里的两轮暂停影响。"),
            row(BodyLabel("待办任务最小间隔"), self.agenda_gap, BodyLabel("分钟"), None),
            hint("模型自主安排主动聊天与定时关怀；程序负责执行频率上限和间隔约束。"),
            BodyLabel("话题类型（用逗号分隔）"), self.topics, row(None, replan)))

        from scheduled_care import DEFAULTS
        self.care = {}
        care = self.add(Card("按时段安排关怀建议").add(
            hint("这些时段和说明作为模型排班建议；模型自行决定是否联系、何时联系，已聊过时会跳过。")))
        for key, default in DEFAULTS.items():
            fields = {'enabled': CheckBox(default[0]), 'start': small_edit(), 'end': small_edit(),
                      'count_min': small_edit(48), 'count': small_edit(48), 'instruction': LineEdit()}
            fields['enabled'].setFixedWidth(110)
            self.care[key] = fields
            care.add(row(fields['enabled'], fields['start'], BodyLabel("至"), fields['end'], BodyLabel("最少条数"),
                         fields['count_min'], BodyLabel("最多"), fields['count'], fields['instruction']))
        care.add(hint("最后一列填写该时段的关怀内容，语气沿用“人设与指令”中的设置；点击“保存设置”统一保存。"))

        buttons = [(PushButton(tr('Refresh')), self.refresh), (PrimaryPushButton("仅生成预览"), self.preview),
                   (PushButton("发送真实测试轮次"), self.send_test),
                   (PushButton("查看本轮输入与思路"), lambda: self.house.switchTo(self.house.pages['contextPage']))]
        for button, slot in buttons:
            button.clicked.connect(slot)

        self.agenda_table = TableWidget()
        self.agenda_table.setColumnCount(3)
        self.agenda_table.setHorizontalHeaderLabels([tr('Scheduled Time'), tr('Task Type'), tr('Intent / Topic')])
        self.agenda_table.verticalHeader().hide()
        self.agenda_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.agenda_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.agenda_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.agenda_table.setColumnWidth(0, 140)
        self.agenda_table.setColumnWidth(1, 120)
        self.agenda_table.setMinimumHeight(130)
        self.agenda_empty = hint("暂无待办任务；模型排班后显示在这里。")

        self.state = read_only_text(240)
        self.preview_text = read_only_text(120)
        self.add(Card('待办任务队列与执行状态').add(
            row(*(button for button, _ in buttons), None),
            BodyLabel('当前自排待办任务清单'),
            self.agenda_table,
            self.agenda_empty,
            BodyLabel('任务调度与后台巡检日志'),
            self.state,
            BodyLabel('生成预览'),
            self.preview_text))
        self.preview_job = None

    def load(self):
        house = self.house
        profile = load_profile(house.profile_path)
        from main import default_profile
        schedule = {**default_profile(house.current_qq, '', True)['proactive'], **profile.get('proactive', {})}
        self.window_start.setText(schedule.get('window_start', ''))
        self.window_end.setText(schedule.get('window_end', ''))
        for key, edit in self.numbers.items():
            edit.setText(str(schedule.get(key, '')))
        self.topics.setText(', '.join(schedule.get('topic_types', [])))
        self.reply_pause.setText(str(profile.get('reply_pause_minutes', 60)))
        self.round_limit.setText(str(profile.get('unanswered_round_limit', 2)))
        self.round_pause.setText(str(profile.get('unanswered_round_pause_minutes', 300)))
        followups = followup_values(profile)
        self.agenda_gap.setText(str(followups['agenda_min_gap_minutes']))
        from scheduled_care import configuration
        care = configuration(profile)
        for key, slot in care['slots'].items():
            fields = self.care[key]
            fields['enabled'].setChecked(slot['enabled'])
            fields['start'].setText(slot['start'])
            fields['end'].setText(slot['end'])
            fields['count_min'].setText(str(slot['count_min']))
            fields['count'].setText(str(slot['count']))
            fields['instruction'].setText(slot['instruction'])
        self.preview_text.setPlainText('')
        self.refresh()

    on_status = lambda self: self.refresh()

    def refresh(self):
        qq_id, running = self.house.current_qq, self.house.running

        def show(content):
            if self.house.current_qq == qq_id:
                set_text_keep(self.state, content)
        run_async(lambda _: proactive_debug_text(qq_id, running), show,
                  lambda exc: show(f'读取主动任务状态失败：{exc}'))

        def read_todos(_):
            import agenda
            store = FriendStore(ROOT / 'friends' / qq_id)
            try:
                return agenda.todos(store)
            finally:
                store.close()

        def show_todos(items):
            if self.house.current_qq != qq_id:
                return
            self.agenda_table.setRowCount(len(items))
            for idx, item in enumerate(items):
                try:
                    at_dt = datetime.fromisoformat(item['at'])
                    time_str = at_dt.strftime('%m-%d %H:%M')
                except Exception:
                    time_str = str(item.get('at', ''))
                kind_str = 'Contact (contact)' if item.get('kind') == 'contact' else 'Status Peek (peek)'
                note_str = str(item.get('note', ''))
                self.agenda_table.setItem(idx, 0, QTableWidgetItem(time_str))
                self.agenda_table.setItem(idx, 1, QTableWidgetItem(kind_str))
                self.agenda_table.setItem(idx, 2, QTableWidgetItem(note_str))
            self.agenda_table.setVisible(bool(items))
            self.agenda_empty.setVisible(not items)

        run_async(read_todos, show_todos, quiet)


    def values(self):
        """The widgets as plain strings/booleans; the rules themselves live in house_status (the phone page shares them)."""
        return {**{key: edit.text() for key, edit in self.numbers.items()},
                'window_start': self.window_start.text(), 'window_end': self.window_end.text(),
                'topic_types': self.topics.text(), 'reply_pause_minutes': self.reply_pause.text(),
                'unanswered_round_limit': self.round_limit.text(),
                'unanswered_round_pause_minutes': self.round_pause.text(),
                'agenda_min_gap_minutes': self.agenda_gap.text()}

    def read_settings(self):
        return read_settings(load_profile(self.house.profile_path), self.values())

    def read_reply_pause(self):
        return read_reply_pause(self.values())

    def read_round_pause(self):
        return read_round_pause(self.values())

    def save(self):
        try:
            care = self.read_care(load_profile(self.house.profile_path))
            saved = house_status.save_page_settings(self.house.profile_path, {**self.values(), 'care': care['slots']})
            self.refresh()
            self.house.refresh_friend_mode()
            toast(self.house, 'success', 'Saved; click "Re-evaluate Schedule with Saved Settings" to update remaining rounds.'
                  if saved['changed'] else 'Proactive chat and scheduled care settings saved; pause timer recalculated from last reply sent.')
        except Exception as exc:
            toast(self.house, 'error', str(exc), 'Save Failed')

    def read_care(self, profile):
        """The care slots as plain values; whether care is on stays as set on the status page."""
        defaults = care_configuration(profile)['slots']
        return read_care_slots({key: {**defaults[key], **{
            name: (field.isChecked() if name == 'enabled' else field.text())
            for name, field in fields.items()}} for key, fields in self.care.items()})

    def replan(self):
        house = self.house
        profile = load_profile(house.profile_path)
        if not house.running:
            toast(house, 'warning', 'Please start the bridge before rescheduling.')
            return
        store = FriendStore(house.friend_dir)
        try:
            if profile.get('proactive', {}).get('smart_timing', True):
                store.set('smart_proactive', '{}')
                store.set('smart_retry_at', '')
                store.set('smart_status', 'Waiting to re-evaluate schedule with latest settings')
                toast(house, 'success', 'Requested re-evaluation; preserved limits, cooldown, and unreplied count.')
            elif json.loads(store.get('replan_request') or '{}').get('state') == 'pending':
                toast(house, 'info', 'A reschedule request is already pending bridge processing.')
            else:
                store.set('replan_request', json.dumps({'state': 'pending', 'requested_at': datetime.now().astimezone().isoformat()}))
                toast(house, 'success', 'Requested reschedule with saved settings; preserved completed rounds, cooldown, and unreplied count.')
        finally:
            store.close()
        self.refresh()

    def preview(self):
        if self.preview_job is not None and not self.preview_job.done():
            toast(self.house, 'info', 'A proactive chat preview is already generating.')
            return
        qq_id = self.house.current_qq
        try:
            profile = load_profile(self.house.profile_path)
            profile['proactive'] = read_settings(profile, self.values())
        except (ValueError, TypeError) as exc:
            toast(self.house, 'error', str(exc), 'Unable to Generate Preview')
            return
        toast(self.house, 'info', 'Preview uses current page topics and proactive style saved in Persona & Style; does not auto-save settings.')
        self.preview_text.setPlainText('Generating preview...')

        def show(content):
            if self.house.current_qq != qq_id:
                return
            try:
                if read_settings(profile, self.values()) != profile['proactive']:
                    content = 'Settings changed; below is preview before changes. Please re-generate.\n\n' + content
            except (ValueError, TypeError):
                content = 'Page settings changed; please check and re-generate preview.\n\n' + content
            self.preview_text.setPlainText(content)
        self.preview_job = run_async(
            lambda _: '\n\n'.join(f'{index}. {message}' for index, message in enumerate(preview_proactive(qq_id, profile), 1)),
            show, lambda exc: show(f'生成预览失败：{exc}'), pool=LONG_POOL)

    def send_test(self):
        house = self.house
        qq_id = house.current_qq
        code, status = invoke_bridge('status')
        if code or not (status.lower().startswith('running') or status.startswith('Online')):
            notice(house, tr('Cannot Send Test Round'), 'Please start the bridge first.')
            return
        profile = load_profile(ROOT / 'friends' / qq_id / 'friend.json')
        if not profile.get('bridge_enabled', True) or not profile.get('proactive', {}).get('enabled'):
            notice(house, tr('Cannot Send Test Round'), 'Proactive chat is not enabled for this friend.')
            return
        store = FriendStore(ROOT / 'friends' / qq_id)
        try:
            if store.get('proactive_paused', '0') == '1':
                notice(house, tr('Cannot Send Test Round'), 'Proactive chat paused due to 2 consecutive unreplied rounds.')
                return
            if json.loads(store.get('proactive_test') or '{}').get('state') in ('pending', 'running'):
                notice(house, 'Test In Progress', 'A test round is already pending or sending.')
                return
            name = house.records[qq_id].get('display_name', qq_id)
            if not confirm(house, 'Send Real Proactive Chat Test', f"现在向「{name}」发送一轮真实 QQ 消息？\n\n这是独立测试，可在设定时段以外发送；不会消耗计划轮次、增加未回复计数或改变冷却时间。",
                           yes='Send'):
                return
            store.set('proactive_test', json.dumps({'id': uuid.uuid4().hex, 'state': 'pending',
                                                    'requested_at': datetime.now().astimezone().isoformat()}))
        finally:
            store.close()
        toast(house, 'success', 'Test round submitted, awaiting bridge dispatch.')
        self.refresh()
