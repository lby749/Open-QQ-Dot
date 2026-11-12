"""Owner-scoped controls for the existing goal, research and browser workers."""
import json

from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QListWidget, QListWidgetItem, QSpinBox,
                               QTableWidgetItem)
from qfluentwidgets import CheckBox, LineEdit, PlainTextEdit, PrimaryPushButton, PushButton, TableWidget

from .common import Card, Page, InputDialog, hint, row, run_async, set_text_keep, toast


STATUS = {'PENDING': '等待执行', 'RUNNING': '正在执行', 'VERIFYING': '独立验证中',
          'WAITING_APPROVAL': '等待确认', 'WAITING_REVIEW': '等待总体验收',
          'WAITING_RESOURCE': '等待资源', 'COMPLETED': '已完成', 'FAILED': '执行失败',
          'PAUSED': '已暂停', 'CANCELLED': '已终止', 'ACTIVE': '自主推进中',
          'BLOCKED': '需要处理阻碍', 'BLOCKED_BY_DEPENDENCY': '前置阶段未完成',
          'BUDGET_EXHAUSTED': '预算已用尽', 'WAITING_VERIFICATION': '等待研究证据核验'}

CATEGORY = {'general': '通用任务', 'code': '代码', 'file': '文件', 'data': '数据', 'research': '研究',
            'browser': '网页操作', 'video': '视频', 'ecommerce': '电商（模拟）', 'goal_plan': '拆分长期目标',
            'goal_patrol': '目标巡检', 'config_check': '配置巡检', 'agent_message': '智能体消息'}

# 2026-10-26 user: the oversized empty box and the loose button row did not match the rest of the panel.
# Native spin buttons stay native on purpose: overriding their sub-controls draws the arrows outside the
# rounded frame, so only the field frame is styled here.
SPIN_STYLE = ('QSpinBox {color:#243656;background:white;border:1px solid #DDE8F7;border-radius:6px;'
              'padding:6px 8px;min-height:20px;}')
ENTRY_STYLE = ('QListWidget {background:#FBFDFF;border:1px solid #E2E8F0;border-radius:8px;'
               'color:#243656;padding:4px;}'
               'QListWidget::item {padding:6px 8px;border-radius:6px;}'
               'QListWidget::item:selected {background:#EAF3FF;color:#1E3A8A;}')


def describe_record(kind, record, logs=(), research=None):
    lines = [record.get('title') or record.get('description', ''),
             '状态：' + STATUS.get(record['status'], record['status'])]
    if kind == 'goal':
        lines += [f"已独立验收 {record['progress']:.0%} · 模型调用 {record['call_count']}/{record['max_calls']}",
                  '授权范围：' + record['scope'], '总体验收标准：' + record['completion_criteria'], '\n阶段计划']
        for index, stage in enumerate(record['tasks']):
            lines += [f"{index + 1}. {stage['title']} · {STATUS.get(stage['status'], stage['status'])}",
                      stage.get('error') or stage.get('result') or stage['description']]
        lines += ['\n推进记录'] + [f"{event['created_at'][:19]} {event['message']}" for event in record['events']]
    else:
        lines += ['任务说明：' + record['description']]
        if record.get('error'):
            lines += ['\n当前阻碍：' + record['error']]
        result = record.get('result')
        if result and record.get('category') == 'goal_plan':
            try:
                stages = json.loads(result)['milestones']
                lines += ['\nAI 生成的阶段计划'] + [f"{i + 1}. {s['title']}\n{s['description']}" for i, s in enumerate(stages)]
            except (ValueError, KeyError, TypeError):
                lines += [result]
        elif result:
            lines += ['\n执行结果', result]
        if record.get('verification_info'):
            lines += ['\n独立验收：' + str(record['verification_info'].get('details', '尚未验收'))]
        lines += ['\n产物'] + (record.get('artifacts') or ['尚无已提交产物'])
    if research:
        lines += ['\n研究进度', f"假设 {research['hypotheses_count']} · 实验 {research['experiments_count']}",
                  '研究状态：' + STATUS.get(research.get('status'), '等待核查'),
                  f"累计用量（含估算）：{research.get('used_tokens', 0)}/{research.get('budget_tokens', 0)} 词元",
                  '已记录发现（以证据和适用范围为准）：' + ('；'.join(research.get('verified_findings') or []) or '暂无，待核验的假设不记为突破')]
        tool_states = {'NOT_STARTED': '未开始', 'RUNNING': '运行中或结束待确认', 'SUCCEEDED': '工具执行成功',
                       'FAILED': '工具执行失败', 'TIMED_OUT': '执行超时', 'CANCELLED': '已取消'}
        for exp in research.get('recent_experiments', []):
            state = exp.get('tool_status', 'NOT_STARTED')
            finding = '数据支持，仅限验收范围' if exp.get('claim_supported') else '尚无可核验的结论支持'
            lines += [f"实验 {exp['experiment_id']}：{tool_states.get(state, '状态待确认')}；{finding}",
                      '验收范围：' + str(exp.get('scope') or '尚未指定')]
            if state == 'RUNNING':
                lines += ['已保存执行收据；结束状态未确认前，续作不会自动重复执行。']
    if logs:
        from ui_locale import tr
        lines += ['\n最近执行日志'] + [f"{log.get('created_at', '')[:19]} {tr(log.get('message', ''))}" for log in logs]
    return '\n'.join(lines)


class AutonomyPage(Page):
    def __init__(self, house, mode):
        super().__init__(house, mode + 'AutomationPage')
        self.mode = mode
        self.busy = False
        self.records = []
        self.records_owner = None
        # 2026-10-26 user: "这些应该按条创建" — one line per task, each line becomes its own task,
        # instead of one oversized text box that could only ever make a single task.
        self.entries = []
        self.entry = LineEdit()
        self.entry.setPlaceholderText({'goal': '一条一个目标，例如：修好这个仓库的登录报错并通过测试',
            'research': '一条一个研究方向或问题，例如：检索长上下文检索论文并整理可核查的证据',
            'browser': '一条一个网址，例如：https://example.com'}[mode])
        self.entry.returnPressed.connect(self.add_entry)
        self.add_entry_button = PushButton('添加一条')
        self.add_entry_button.clicked.connect(self.add_entry)
        self.entry_list = QListWidget()
        self.entry_list.setStyleSheet(ENTRY_STYLE)
        self.entry_list.setMinimumHeight(64)
        self.entry_list.setMaximumHeight(120)
        self.entry_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.entry_list.itemSelectionChanged.connect(self.sync_entry_state)
        self.entry_count = hint('还没有条目')
        self.remove_entry = PushButton('删除选中')
        self.remove_entry.clicked.connect(self.remove_selected)
        self.clear_entry = PushButton('全部清空')
        self.clear_entry.clicked.connect(self.clear_entries)
        self.scope = LineEdit()
        self.scope.setPlaceholderText('授权范围，例如仅当前好友工作目录；不发布、不付款')
        self.criteria = LineEdit()
        self.criteria.setPlaceholderText('验收标准，例如指定文件存在并通过测试')
        self.calls = QSpinBox()
        self.calls.setRange(1, 200)
        self.calls.setValue(12)
        self.tokens = QSpinBox()
        self.tokens.setRange(1000, 1000000)
        self.tokens.setValue(100000)
        for field in (self.calls, self.tokens):
            field.setMinimumWidth(130)
            field.setStyleSheet(SPIN_STYLE)
        self.auto_start = CheckBox('授权 AI 在上述范围内生成计划后自动开始执行')
        self.auto_start.setChecked(True)
        title = {'goal': '自主规划与持续执行', 'research': '自主研究智能体', 'browser': '浏览器自动化'}[mode]
        self.submit = PrimaryPushButton({'goal': '规划目标', 'research': '创建研究任务', 'browser': '启动浏览器任务'}[mode])
        self.submit.clicked.connect(self.create)
        form = Card(title).add(
            hint('使用当前好友的模型、记忆与工具权限；过程、阻碍、日志和产物会保留。'),
            hint('按条创建：每一条单独成为一个任务，互不合并；输入后按回车或点“添加一条”。'),
            row(self.entry, self.add_entry_button),
            self.entry_list,
            row(None, self.entry_count, self.remove_entry, self.clear_entry))
        if mode == 'goal':
            form.add(self.scope, self.criteria, self.auto_start)
            self.auto_start.toggled.connect(lambda checked: self.sync_entry_state())
        if mode == 'browser':
            self.criteria.setPlaceholderText('期望页面包含的文字；留空时复杂操作需人工验收')
            form.add(self.criteria, hint('使用真实浏览器；遇到登录、验证码或权限阻碍时如实停止，不伪造成功。'))
        form.add(row(hint('最多模型调用'), self.calls, hint('令牌预算'), self.tokens, None, self.submit))
        self.sync_entry_state()
        self.add(form)
        self.status = hint('选择好友后显示该好友的目标、任务与执行状态。')
        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(['类型', '目标 / 任务', '状态', '进度 / 调用'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self.show_detail)
        refresh = PushButton('刷新执行进度')
        refresh.clicked.connect(self.load)
        controls = row(refresh)
        for action, label in [('pause', '暂停'), ('resume', '继续'), ('cancel', '终止'),
                              ('accept', '确认规划并执行'), ('approve', '确认结果验收')]:
            button = PushButton(label)
            button.clicked.connect(lambda _, action=action: self.control(action))
            controls.addWidget(button)
        self.add(Card('正在发生的工作').add(self.status, self.table, controls))
        self.detail = PlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(240)
        self.add(Card('规划阶段、研究证据、执行日志与产物').add(self.detail))

    # ---------- entries: one task per line ----------

    def add_entry(self):
        text = self.entry.text().strip()
        if not text:
            return
        if text in self.entries:
            self.entry.clear()
            return toast(self.house, 'info', '这一条已经在列表里了。')
        if self.mode == 'browser':
            from urllib.parse import urlsplit
            parsed = urlsplit(text)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                return toast(self.house, 'warning', '请填写有效的 HTTP/HTTPS 网页地址，例如 https://example.com')
        self.entries.append(text)
        self.entry_list.addItem(QListWidgetItem(text))
        self.entry.clear()
        self.entry.setFocus()
        self.sync_entry_state()

    def remove_selected(self):
        rows = sorted((self.entry_list.row(item) for item in self.entry_list.selectedItems()), reverse=True)
        for row_index in rows:
            self.entry_list.takeItem(row_index)
            del self.entries[row_index]
        self.sync_entry_state()

    def clear_entries(self):
        self.entries.clear()
        self.entry_list.clear()
        self.sync_entry_state()

    def sync_entry_state(self):
        count = len(self.entries)
        verb = {'goal': '规划', 'research': '创建', 'browser': '启动'}[self.mode]
        noun = {'goal': '个目标', 'research': '条研究任务', 'browser': '个浏览器任务'}[self.mode]
        base = {'goal': '规划目标', 'research': '创建研究任务', 'browser': '启动浏览器任务'}[self.mode]
        self.submit.setText(f'{verb} {count} {noun}' if count else base)
        self.entry_count.setText(f'已添加 {count} 条' if count else '还没有条目')
        self.remove_entry.setEnabled(bool(count) and bool(self.entry_list.selectedItems()))
        self.clear_entry.setEnabled(bool(count))

    def build_body(self, description):
        body = {'description': description, 'max_calls': self.calls.value(),
                'token_budget': self.tokens.value()}
        if self.mode == 'goal':
            body.update(action='propose', scope=self.scope.text().strip(),
                        completion_criteria=self.criteria.text().strip(),
                        auto_start=self.auto_start.isChecked())
        else:
            body.update(action='create', title=description[:60], category=self.mode)
            if self.mode == 'browser':
                body.update(url=description, browser_autoplan=True,
                            expected_text=self.criteria.text().strip())
        return body

    def create(self):
        owner = self.house.current_qq
        if not owner:
            return toast(self.house, 'warning', '请先添加并选择一个好友。')
        pending = list(self.entries)
        if not pending and self.entry.text().strip():
            self.add_entry()
            pending = list(self.entries)
        if not pending:
            return toast(self.house, 'warning', '请先添加至少一条；每一条会单独创建一个任务。')
        if self.mode == 'goal' and (not self.scope.text().strip() or not self.criteria.text().strip()):
            return toast(self.house, 'warning', '请明确授权范围和验收标准。')
        bodies = [self.build_body(text) for text in pending]
        self.submit.setEnabled(False)

        def work(_):
            from house_web import post_agent_goal_action, post_agent_task_action
            handler = post_agent_goal_action if self.mode == 'goal' else post_agent_task_action
            created, failures = 0, []
            for body in bodies:
                try:
                    handler(owner, body)
                    created += 1
                except Exception as exc:  # one bad entry must not drop the others
                    failures.append(f"{body['description'][:20]}：{exc}")
            return created, failures

        def done(result):
            self.submit.setEnabled(True)
            created, failures = result
            if created:
                message = f'已创建 {created} 条'
                if failures:
                    message += f'，{len(failures)} 条没成功（{failures[0]}）'
                toast(self.house, 'warning' if failures else 'success', message)
                self.clear_entries()
            else:
                toast(self.house, 'error', '创建失败：' + (failures[0] if failures else '未知原因'))
            self.load()

        def failed(error):
            self.submit.setEnabled(True)
            toast(self.house, 'error', str(error))
        run_async(work, done, failed)

    def load(self):
        owner = self.house.current_qq
        if self.records_owner != owner:
            self.records = []
            self.table.setRowCount(0)
            self.records_owner = None
            self.show_detail()
        if self.busy:
            return
        if not owner:
            self.records = []
            self.table.setRowCount(0)
            return
        self.busy = True
        selected = self.selected()
        selected_id = selected[1]['task_id' if selected[0] == 'task' else 'goal_id'] if selected else None

        def read(_):
            from task_manager import TaskManager
            manager = TaskManager.get_instance()
            goals = [('goal', goal) for goal in manager.list_goals(owner)] if self.mode == 'goal' else []
            tasks = [('task', task.to_dict()) for task in manager.list_tasks(owner_id=owner, limit=100)
                     if self.mode == 'goal' or task.category == self.mode]
            return goals + tasks

        def done(records):
            self.busy = False
            if owner != self.house.current_qq:
                self.load()
                return
            self.records_owner = owner
            self.records = records
            self.table.blockSignals(True)
            self.table.setRowCount(len(records))
            selected_row = 0
            for index, (kind, record) in enumerate(records):
                identifier = record['task_id' if kind == 'task' else 'goal_id']
                if identifier == selected_id:
                    selected_row = index
                values = ('长期目标' if kind == 'goal' else CATEGORY.get(record['category'], record['category']),
                          record.get('title') or record.get('description'),
                          STATUS.get(record['status'], record['status']),
                          f"{record['progress']:.0%}" if kind == 'goal' else f"{record.get('call_count', 0)} 次调用")
                for column, value in enumerate(values):
                    self.table.setItem(index, column, QTableWidgetItem(str(value)))
            self.table.blockSignals(False)
            if records:
                self.table.selectRow(selected_row)
            self.status.setText(f'当前好友 {owner} · {len(records)} 条记录 · 每次后台状态更新时刷新')
            self.show_detail()

        def failed(error):
            self.busy = False
            self.status.setText('读取执行状态失败：' + str(error))
        run_async(read, done, failed)

    def selected(self):
        index = self.table.currentRow()
        if self.records_owner == self.house.current_qq and 0 <= index < len(self.records):
            return self.records[index]

    def show_detail(self):
        selected = self.selected()
        if not selected:
            return set_text_keep(self.detail, '尚无任务。提交目标后，这里显示真实阶段、日志和产物。')
        kind, record = selected
        owner = self.house.current_qq
        identifier = record['task_id' if kind == 'task' else 'goal_id']

        def read(_):
            from task_manager import TaskManager
            manager = TaskManager.get_instance()
            result = dict(record)
            logs = []
            research = None
            if kind == 'task':
                task = manager.get_task(identifier)
                if not task or task.owner_id != owner:
                    raise PermissionError('任务归属已改变')
                result = task.to_dict()
                logs = manager.get_task_logs(identifier)[-30:]
                project_id = task.checkpoint.get('research_project_id')
                if project_id:
                    from research_tracker import ResearchTracker
                    research = ResearchTracker.get_instance().get_project_summary(project_id)
            return describe_record(kind, result, logs, research)

        def done(text):
            current = self.selected()
            if owner == self.house.current_qq and current and current[1].get('task_id' if kind == 'task' else 'goal_id') == identifier:
                set_text_keep(self.detail, text)
        run_async(read, done, lambda error: self.status.setText(str(error)))

    def control(self, action):
        selected = self.selected()
        if not selected:
            return toast(self.house, 'warning', '请先选择一条记录。')
        kind, record = selected
        owner = self.house.current_qq
        body = {'action': action, 'task_id': record.get('task_id'), 'goal_id': record.get('goal_id')}
        if kind == 'goal' and action == 'approve':
            review = InputDialog(self.house, '总体验收', record['completion_criteria'])
            if not review.exec():
                return
            body.update(action='complete', review=review.edit.text())
        elif action == 'accept':
            if kind != 'task' or record['category'] != 'goal_plan':
                return toast(self.house, 'warning', '请选择已生成的规划草案。')
            body['action'] = 'accept_plan'
        def work(_):
            from house_web import post_agent_goal_action, post_agent_task_action
            handler = post_agent_goal_action if kind == 'goal' or action == 'accept' else post_agent_task_action
            return handler(owner, body)
        run_async(work, lambda result: (toast(self.house, 'success', result.get('message', '已更新')), self.load()),
                  lambda error: toast(self.house, 'error', str(error)))

    shown = lambda self: self.load()
    on_status = shown
