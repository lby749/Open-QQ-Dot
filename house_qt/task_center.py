"""Task center: every kind of autonomous work in one place — create from templates, view, undo."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QBoxLayout, QFormLayout, QHeaderView, QSpinBox, QTableWidgetItem, QTreeWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CheckBox, ComboBox, LineEdit, PlainTextEdit, PrimaryPushButton, PushButton,
                            TableWidget, TreeWidget)

from .autonomy import CATEGORY, STATUS, describe_record
from ui_locale import tr
from .common import Card, InputDialog, Page, confirm, hint, row, run_async, set_text_keep, toast

FILTERS = [('全部', None), ('进行中', ('PENDING', 'PLANNING', 'RUNNING', 'VERIFYING', 'WAITING_RESOURCE',
                                    'BLOCKED_BY_DEPENDENCY', 'ACTIVE')),
           ('等我处理', ('WAITING_APPROVAL', 'WAITING_REVIEW', 'PAUSED', 'BLOCKED')),
           ('已结束', ('COMPLETED', 'FAILED', 'CANCELLED'))]
SPIN_STYLE = 'QSpinBox {color:#243656;background:white;border:1px solid #DDE8F7;border-radius:5px;padding:6px;}'


def record_type(kind, record):
    from task_catalog import TEMPLATES
    if kind == 'goal':
        return '长期目标'
    template = TEMPLATES.get((record.get('checkpoint') or {}).get('template'))
    label = template['title'] if template else CATEGORY.get(record['category'], record['category'])
    return label + ('（阶段）' if record.get('goal_stage') else '')


class TaskCenterPage(Page):
    def __init__(self, house):
        super().__init__(house, 'taskCenterPage')
        self.items, self.current, self.inputs = {}, None, {}
        self.records, self.records_owner, self.busy = [], None, False

        # ---- create ----
        self.tree = TreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(180)
        self.tree.setMinimumHeight(420)
        self.tree.itemSelectionChanged.connect(self.choose_template)
        self.title = BodyLabel('')
        self.title.setStyleSheet('font-size: 17px; font-weight: 600;')
        self.about = hint('')
        self.warning = hint('')
        self.warning.setTextColor('#B45309', '#FBBF24')
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.calls, self.tokens = QSpinBox(), QSpinBox()
        self.calls.setRange(1, 200)
        self.calls.setValue(20)
        self.tokens.setRange(1000, 2000000)
        self.tokens.setSingleStep(10000)
        # 2026-10-26 实测：Codex/Claude CLI 单次调用就要 10–15 万 token（绝大多数是 CLI 自己的系统提示词），
        # 默认给 10 万会立刻判「预算耗尽」。默认抬到 30 万，并在旁边写清楚。
        self.tokens.setValue(300000)
        for field in (self.calls, self.tokens):
            field.setMinimumWidth(120)
            field.setStyleSheet(SPIN_STYLE)
        self.notify = CheckBox('通过 QQ 按好友人设汇报进展与结果')
        self.auto_start = CheckBox('AI 拆分阶段后直接开始执行（不勾选则先给我确认）')
        self.auto_start.setChecked(True)
        self.submit = PrimaryPushButton('创建')
        self.submit.clicked.connect(self.create)
        self.save_template = PushButton('保存为我的模板')
        self.save_template.clicked.connect(self.save_as_template)
        self.delete_template = PushButton('删除此模板')
        self.delete_template.clicked.connect(self.remove_template)
        column = QWidget()
        fields = QVBoxLayout(column)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(10)
        for widget in (self.title, self.about, self.warning, self.form_host):
            fields.addWidget(widget)
        fields.addLayout(row(hint('最多模型调用'), self.calls,
            hint('词元预算（CLI 单次约 10–15 万，建议 30 万起）'), self.tokens, None))
        fields.addWidget(self.notify)
        fields.addWidget(self.auto_start)
        fields.addLayout(row(self.delete_template, None, self.save_template, self.submit))
        self.create_row = row(self.tree, column)
        create = Card('新建任务').add(
            hint('按类型选择模板；长期目标会由 AI 拆成可单独验收的阶段并持续推进。灰色条目是当前好友还不能用的，选中可看原因。'),
            self.create_row)
        self.add(create)

        self.study_path = LineEdit()
        self.study_path.setPlaceholderText('当前好友默认项目中的资料相对路径，例如 materials/教材.pdf')
        self.study_query = LineEdit()
        self.study_query.setPlaceholderText('检索学习资料中的原文')
        self.study_result = PlainTextEdit()
        self.study_result.setReadOnly(True)
        self.study_result.setMaximumHeight(190)
        self.study_result.setPlaceholderText('各智能体共用资料、笔记、计划和练习；不同好友和项目分别保存。')
        import_study, search_study, list_study = PushButton('导入资料'), PushButton('检索原文'), PushButton('资料与学习记录')
        import_study.clicked.connect(lambda: self.study_action('import'))
        search_study.clicked.connect(lambda: self.study_action('search'))
        list_study.clicked.connect(lambda: self.study_action('list'))
        self.add(Card('公共学习能力').add(hint('无需切换智能体。这里管理当前好友默认项目；任务中使用任务所选项目。'),
            row(self.study_path, import_study), row(self.study_query, search_study, list_study), self.study_result))

        # ---- list ----
        self.filter = ComboBox()
        for label, _ in FILTERS:
            self.filter.addItem(label)
        self.filter.currentIndexChanged.connect(self.show_records)
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['类型', '目标 / 任务', '状态', '进度', '更新时间'])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column_index, width in ((0, 150), (2, 110), (3, 110), (4, 140)):
            self.table.setColumnWidth(column_index, width)
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setMinimumHeight(260)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.status = hint('')
        controls = row(BodyLabel('筛选'), self.filter, None)
        actions = [row(), row()]
        for index, (action, label) in enumerate((('refresh', '刷新'), ('pause', '暂停'), ('resume', '继续'), ('accept', '确认规划'),
                              ('approve', '确认验收'), ('reject', '拒绝结果'), ('undo', '撤销'), ('learn','学习为工作流'))):
            button = PushButton(label)
            button.clicked.connect(lambda _=False, action=action: self.control(action))
            actions[index // 4].addWidget(button)
        for action_row in actions:
            action_row.addStretch(1)
        self.add(Card('全部任务').add(controls, *actions, self.status, self.table,
            hint('「撤销」：未结束的任务先终止；已结束的任务还原它在工作区里新建、修改、删除的文件（移出的文件保留在撤销记录中，可找回）。'
                 '已发出的 QQ 消息、网页上的提交等外部操作无法撤销。')))
        self.detail = PlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(260)
        self.add(Card('详情：阶段、日志、产物与可撤销的改动').add(self.detail))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'create_row'):
            compact = self.viewport().width() < 850
            self.create_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
            self.tree.setMaximumHeight(200 if compact else 16777215)
            self.tree.setMinimumHeight(160 if compact else 420)

    # ---------------- templates ----------------
    def load(self):
        self.fill_templates()
        self.load_records()

    shown = lambda self: self.load()
    on_status = lambda self: self.load_records()

    def fill_templates(self, select=None):
        from house_status import ROOT
        from task_catalog import catalog
        owner = self.house.current_qq
        if not owner:
            self.tree.clear()
            return
        previous = select or (self.current or {}).get('key')
        self.items = {item['key']: item for item in catalog(ROOT, owner, self.house.friend_dir)}
        self.tree.blockSignals(True)
        self.tree.clear()
        groups, chosen = {}, None
        for item in self.items.values():
            if item['group'] not in groups:
                groups[item['group']] = QTreeWidgetItem([item['group']])
                groups[item['group']].setFlags(Qt.ItemIsEnabled)
                self.tree.addTopLevelItem(groups[item['group']])
            child = QTreeWidgetItem([item['title'] + ('' if item['available'] else '（不可用）')])
            child.setData(0, Qt.UserRole, item['key'])
            if not item['available']:
                child.setForeground(0, Qt.gray)
            groups[item['group']].addChild(child)
            if item['key'] == previous:
                chosen = child
            elif chosen is None and item['available'] and previous not in self.items:
                chosen = child
        self.tree.expandAll()
        self.tree.blockSignals(False)
        if chosen is not None:
            self.tree.setCurrentItem(chosen)
        self.choose_template()

    def choose_template(self):
        selected = self.tree.currentItem()
        key = selected.data(0, Qt.UserRole) if selected else None
        if not key or key not in self.items:
            return
        item = self.current = self.items[key]
        self.title.setText(item['title'] + ('　·　长期目标' if item['kind'] == 'goal' else ''))
        self.about.setText(item['description'] + (('\n' + item['notes']) if item['notes'] else ''))
        self.warning.setText('还不能创建：' + item['reason'] if not item['available'] else '')
        self.warning.setVisible(not item['available'])
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs = {}
        from task_catalog import FIELDS
        for name in item['fields']:
            label, widget, placeholder = FIELDS[name]
            default = item['defaults'].get(name, '')
            if widget == 'text':
                edit = PlainTextEdit()
                edit.setFixedHeight(96)
                edit.setPlaceholderText(item['description'])
                # 2026-10-26 user: 框里只有灰色说明，点「创建」却提示"请填写要做什么"，看起来像按钮坏了。
                # 没有默认值时直接把模板说明填进去，用户改一改就能建。
                edit.setPlainText(str(default) or item['description'])
            elif widget == 'workspace':
                edit = ComboBox()
                from session_manager import list_workspaces
                for space in list_workspaces(self.house.current_qq):
                    edit.addItem(space['title'] + ('' if space.get('is_default') else f"（{space['path']}）"),
                                 userData=space['id'])
                index = next((i for i in range(edit.count()) if edit.itemData(i) == default), 0)
                edit.setCurrentIndex(index)
            elif widget == 'int':
                edit = QSpinBox()
                edit.setRange(4, 600)
                edit.setValue(int(default or 8))
                edit.setStyleSheet(SPIN_STYLE)
            else:
                edit = LineEdit()
                edit.setPlaceholderText(placeholder)
                edit.setText(', '.join(default) if isinstance(default, list) else str(default))
            self.inputs[name] = edit
            self.form.addRow(BodyLabel(label), edit)
        self.auto_start.setVisible(item['kind'] == 'goal')
        for widget in (self.submit, self.save_template):
            widget.setEnabled(item['available'])
        self.delete_template.setVisible(item['custom'])

    def values(self):
        values = {}
        for name, edit in self.inputs.items():
            if isinstance(edit, PlainTextEdit):
                values[name] = edit.toPlainText().strip()
            elif isinstance(edit, ComboBox):
                values[name] = edit.currentData() or 'default'
            elif isinstance(edit, QSpinBox):
                values[name] = edit.value()
            else:
                values[name] = edit.text().strip()
        return values

    def study_action(self, operation):
        from topic_library import call_tool
        owner, directory = self.house.current_qq, self.house.friend_dir
        if not owner or not directory:
            return toast(self.house, 'warning', '请先选择好友。')
        args = {'operation': operation}
        if operation == 'import':
            args['path'] = self.study_path.text().strip()
        elif operation == 'search':
            args['query'] = self.study_query.text().strip()
        self.study_result.setPlainText('正在读取学习资料…')
        def done(result):
            if owner != self.house.current_qq:
                return
            from study_tools import describe
            self.study_result.setPlainText(describe(result))
        def failed(error):
            if owner == self.house.current_qq:
                self.study_result.setPlainText('学习操作未完成：' + str(error))
        run_async(lambda _: call_tool(directory, 'study_write' if operation == 'import' else 'study_read', args), done, failed)

    def create(self):
        from house_status import ROOT
        from task_catalog import create
        owner, item = self.house.current_qq, self.current
        if not owner or not item:
            return toast(self.house, 'warning', '请先选择好友和任务类型。')
        values, directory = self.values(), self.house.friend_dir
        options = dict(max_calls=self.calls.value(), token_budget=self.tokens.value(),
                       notify_qq=self.notify.isChecked(), auto_start=self.auto_start.isChecked())
        self.submit.setEnabled(False)

        def done(result):
            self.submit.setEnabled(True)
            toast(self.house, 'success', result['message'])
            self.load_records(select=result['task_id'])

        def failed(error):
            self.submit.setEnabled(True)
            toast(self.house, 'error', str(error), '未创建')
        run_async(lambda _: create(ROOT, owner, directory, item['key'], values, **options), done, failed)

    def save_as_template(self):
        from house_status import ROOT
        from task_catalog import save_custom
        item = self.current
        if not item:
            return
        dialog = InputDialog(self.house, '保存为我的模板', '模板名称（当前填写的内容会成为它的默认值）')
        if not dialog.exec():
            return
        try:
            key = save_custom(ROOT, self.house.current_qq, dialog.edit.text(), item['base'], self.values())
            self.fill_templates(select=key)
            toast(self.house, 'success', '已保存到「我的模板」。')
        except Exception as exc:
            toast(self.house, 'error', str(exc), '未保存')

    def remove_template(self):
        from house_status import ROOT
        from task_catalog import delete_custom
        item = self.current
        if not item or not item['custom'] or not confirm(self.house, '删除模板', f"删除「{item['title']}」？已创建的任务不受影响。"):
            return
        delete_custom(ROOT, self.house.current_qq, item['key'])
        self.current = None
        self.fill_templates()

    # ---------------- records ----------------
    def load_records(self, select=None):
        owner = self.house.current_qq
        if self.records_owner != owner:
            self.records, self.shown_records = [], []
            self.records_owner = None
            self.table.setRowCount(0)
            self.show_detail()
        if self.busy or not owner:
            return
        self.busy = True
        chosen = self.selected()
        select = select or (chosen and chosen[1].get('task_id' if chosen[0] == 'task' else 'goal_id'))

        def read(_):
            from task_manager import TaskManager
            manager = TaskManager.get_instance()
            goals = manager.list_goals(owner)
            stage_ids = {task['task_id'] for goal in goals for task in goal['tasks']}
            records = [('goal', goal) for goal in goals]
            for task in manager.list_tasks(owner_id=owner, limit=300):
                record = task.to_dict()
                record['goal_stage'] = task.task_id in stage_ids
                records.append(('task', record))
            records.sort(key=lambda pair: pair[1].get('updated_at') or pair[1].get('created_at') or '', reverse=True)
            return records

        def done(records):
            self.busy = False
            if owner != self.house.current_qq:
                return self.load_records()
            self.records_owner, self.records = owner, records
            self.show_records(select=select)

        def failed(error):
            self.busy = False
            self.status.setText('读取任务失败：' + str(error))
        run_async(read, done, failed)

    def visible_records(self):
        allowed = FILTERS[max(0, self.filter.currentIndex())][1]
        return [pair for pair in self.records if allowed is None or pair[1]['status'] in allowed]

    def show_records(self, *_, select=None):
        records = self.visible_records()
        self.shown_records = records
        self.table.blockSignals(True)
        self.table.setRowCount(len(records))
        selected_row = 0
        for index, (kind, record) in enumerate(records):
            identifier = record['task_id' if kind == 'task' else 'goal_id']
            if identifier == select:
                selected_row = index
            undone = kind == 'task' and (record.get('checkpoint') or {}).get('undone')
            progress = (f"{record['progress']:.0%}" if kind == 'goal'
                        else f"{record.get('call_count', 0)} 次调用")
            values = (record_type(kind, record), tr(record.get('title') or record.get('description', '')),
                      '已撤销' if undone else STATUS.get(record['status'], record['status']), progress,
                      str(record.get('updated_at') or record.get('created_at') or '')[:16].replace('T', ' '))
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        self.table.blockSignals(False)
        if records:
            self.table.selectRow(selected_row)
        total = len(self.records)
        self.status.setText(f'当前好友共 {total} 条，显示 {len(records)} 条' if total else '还没有任务，从上方模板创建一个。')
        self.show_detail()

    def selected(self):
        index = self.table.currentRow()
        records = getattr(self, 'shown_records', [])
        if self.records_owner == self.house.current_qq and 0 <= index < len(records):
            return records[index]

    def show_detail(self):
        selected = self.selected()
        if not selected:
            return set_text_keep(self.detail, '选择一条记录查看阶段、日志、产物和可撤销的文件改动。')
        kind, record = selected
        owner = self.house.current_qq
        identifier = record['task_id' if kind == 'task' else 'goal_id']
        def read(_):
            from house_status import ROOT
            from task_catalog import undo_preview
            from task_manager import TaskManager
            manager = TaskManager.get_instance()
            logs, research, result = [], None, dict(record)
            if kind == 'task':
                task = manager.get_task(identifier)
                if not task or task.owner_id != owner:
                    raise PermissionError('任务归属已改变')
                result, logs = task.to_dict(), manager.get_task_logs(identifier)[-30:]
                project = task.checkpoint.get('research_project_id')
                if project:
                    from research_tracker import ResearchTracker
                    research = ResearchTracker.get_instance().get_project_summary(project)
            else:
                result = manager.get_goal(identifier) or result
            text = describe_record(kind, result, logs, research)
            preview = undo_preview(ROOT, owner, kind, identifier)
            lines = ['\n撤销', preview['text']]
            for name, label in (('created', '新建'), ('modified', '修改'), ('deleted', '删除')):
                names = (preview.get('changes') or {}).get(name) or []
                if names:
                    lines.append(f'{label}：' + '、'.join(names[:12]) + (f' 等 {len(names)} 个' if len(names) > 12 else ''))
            return text + '\n'.join(lines)

        def done(text):
            current = self.selected()
            if current and current[1].get('task_id' if kind == 'task' else 'goal_id') == identifier:
                set_text_keep(self.detail, text)
        run_async(read, done, lambda error: set_text_keep(self.detail, f'读取详情失败：{error}'))

    def control(self, action):
        if action == 'refresh':
            return self.load_records()
        selected = self.selected()
        if not selected:
            return toast(self.house, 'warning', '请先选择一条记录。')
        kind, record = selected
        owner = self.house.current_qq
        identifier = record['task_id' if kind == 'task' else 'goal_id']
        if action == 'learn':
            try:
                from task_manager import TaskManager
                from task_workflows import WorkflowStore
                flow=WorkflowStore(TaskManager.get_instance()).learn(owner,kind,identifier)
                self.house.show_friend(owner,'workflowPage')
                self.house.pages['workflowPage'].load(select=flow)
                return
            except Exception as error:
                return toast(self.house,'error',str(error))
        if action == 'undo':
            return self.undo(kind, identifier)
        body = {'action': action, 'task_id': record.get('task_id'), 'goal_id': record.get('goal_id')}
        if action == 'accept':
            if kind != 'task' or record['category'] != 'goal_plan':
                return toast(self.house, 'warning', '请选择「拆分长期目标」的规划记录。')
            body['action'] = 'accept_plan'
        elif action == 'approve' and kind == 'goal':
            review = InputDialog(self.house, '总体验收', record.get('completion_criteria', ''))
            if not review.exec():
                return
            body.update(action='complete', review=review.edit.text())
        elif action == 'reject':
            if kind == 'goal':
                return toast(self.house, 'warning', '长期目标请用「撤销」终止。')
            reason = InputDialog(self.house, '拒绝结果', '原因（会记录在任务日志里）')
            if not reason.exec():
                return
            body['reason'] = reason.edit.text()

        def work(_):
            from house_web import post_agent_goal_action, post_agent_task_action
            handler = post_agent_goal_action if kind == 'goal' or action == 'accept' else post_agent_task_action
            return handler(owner, body)
        run_async(work, lambda result: (toast(self.house, 'success', result.get('message', '已更新')),
                                        self.load_records()),
                  lambda error: toast(self.house, 'error', str(error)))

    def undo(self, kind, identifier):
        from house_status import ROOT
        from task_catalog import undo, undo_preview
        owner = self.house.current_qq
        try:
            preview = undo_preview(ROOT, owner, kind, identifier)
        except Exception as exc:
            return toast(self.house, 'error', str(exc))
        if preview['action'] == 'none':
            return toast(self.house, 'info', preview['text'], '无需撤销')
        title = '终止' if preview['action'] == 'stop' else '撤销文件改动'
        if not confirm(self.house, title, preview['text'] + '？', yes=title):
            return
        run_async(lambda _: undo(ROOT, owner, kind, identifier),
                  lambda result: (toast(self.house, 'success', result['message']), self.load_records()),
                  lambda error: toast(self.house, 'error', str(error), '撤销失败'))
