"""Task capsule page: export a learned workflow, import a capsule, run it as a tool."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QHeaderView, QTableWidgetItem
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PlainTextEdit, PushButton, TableWidget

from house_status import ROOT

from .common import LONG_POOL, Card, Page, confirm, hint, row, run_async, toast
from .autonomy import STATUS, describe_record
from .task_center import CATEGORY
import task_capsule


class CapsulePage(Page):
    def __init__(self, house):
        super().__init__(house, "capsulePage")
        self.owner = ""
        self.records = []
        self.skills: list[str] = []
        self.busy = False
        self.runs = []
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["名称", "版本", "阶段", "所需智能体", "样例"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumHeight(140)
        self.table.setMaximumHeight(240)
        self.table.itemSelectionChanged.connect(self.show_selected)
        self.detail = PlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(170)
        self.input = PlainTextEdit()
        self.input.setPlaceholderText('填写本次要处理的材料、网址、文件位置或具体要求。请同时在智能体设置中授予所需项目权限。')
        self.input.setMaximumHeight(125)
        self.current_agent = CheckBox('各阶段使用当前好友所选的智能体（保留原有验收与权限）')
        self.run_button = self.button('开始本次任务', self.run_capsule)
        self.add(Card("已安装的任务胶囊").add(
            hint("任务胶囊把一个可学习工作流打包成文件：工作流阶段、所需模型与权限、技能说明、测试样例和自动化示教。"
                 "导入后作为工具调用，但仍走现有审批、预算与验收。"),
            self.table,
            row(self.button("刷新", self.load), self.button("导入胶囊文件", self.import_capsule),
                self.button("检查胶囊", self.test_capsule),
                self.button("卸载", self.remove_capsule))))
        self.add(Card('阶段、权限与样例说明', collapsed=True).add(self.detail))
        self.add(Card('本次运行').add(hint('可保留胶囊原来的分工，也可勾选使用当前智能体。预算与阶段验收沿用工作流；外部智能体按启动次数记账，内部请求由工具自身管理。'),
            self.input, self.current_agent, row(self.run_button,
                self.button('检查模型与项目权限', lambda: self.house.switchTo(self.house.pages['modelPage'])))))
        self.run_selector = ComboBox()
        self.run_selector.currentIndexChanged.connect(self.show_run)
        self.run_detail = PlainTextEdit()
        self.run_detail.setReadOnly(True)
        self.run_detail.setMinimumHeight(210)
        self.resume_button = self.button('继续已暂停的任务', lambda: self.goal_action('resume'))
        self.pause_button = self.button('暂停这一轮', lambda: self.goal_action('pause'))
        self.artifacts = ComboBox()
        self.open_artifact = self.button('打开已验收产物', self.open_result)
        self.add(Card('进度与结果').add(row(self.run_selector, self.button('刷新进度', self.load_runs)),
            self.run_detail, row(self.pause_button, self.resume_button,
            self.button('到任务中心处理阻碍', lambda: self.house.switchTo(self.house.pages['taskCenterPage']))),
            row(self.artifacts, self.open_artifact)))
        self.workflow = LineEdit()
        self.workflow.setReadOnly(True)
        self.workflow.setPlaceholderText("选择要导出的可学习工作流（在任务中心 → 可学习工作流中创建）")
        self.title = LineEdit()
        self.title.setPlaceholderText("胶囊名称（留空使用工作流名称）")
        self.description = LineEdit()
        self.description.setPlaceholderText("一句话说明这个流程解决什么问题")
        self.output = LineEdit()
        self.output.setPlaceholderText("保存为 .oqtc 任务胶囊")
        self.skills_label = BodyLabel("未附加技能文件")
        self.export_workflow = None
        self.add(Card("从可学习工作流导出胶囊", collapsible=True, collapsed=True).add(
            hint("导出为 .oqtc 任务胶囊（内部是标准 ZIP，可直接重新导入；同时兼容 .zip 文件）。不包含账号、令牌或私聊原文。"),
            row(self.workflow, self.button("选择工作流", self.pick_workflow)),
            row(self.title, self.description),
            row(self.output, self.button("选择保存位置", self.pick_output), self.button("附加技能 SKILL.md", self.pick_skill)),
            self.skills_label,
            self.button("导出胶囊", self.export_capsule)))

    def button(self, title, action):
        button = PushButton(title)
        button.clicked.connect(action)
        return button

    def load(self):
        owner = self.house.current_qq
        if self.owner != owner:
            self.owner = owner
            self.records, self.runs = [], []
            self.table.setRowCount(0)
            self.detail.clear()
            self.input.clear()
            self.current_agent.setChecked(False)
            self.export_workflow = None
            self.workflow.clear()
            self.title.clear()
            self.description.clear()
            self.output.clear()
            self.skills = []
            self.skills_label.setText('未附加技能文件')
            self.show_runs([])
        if not owner:
            self.detail.setPlainText('请先在顶部选择好友或机器人会话。')
            return
        run_async(lambda _: task_capsule.list_capsules(ROOT, owner=owner),
                  lambda records: self.fill(records) if self.house.current_qq == owner else None,
                  lambda exc: toast(self.house, "error", str(exc), "任务胶囊") if self.house.current_qq == owner else None,
                  pool=LONG_POOL)

    def shown(self):
        self.load()

    def fill(self, records):
        selected = self.selected_id()
        self.table.blockSignals(True)
        self.records = records or []
        self.table.setRowCount(len(self.records))
        for index, item in enumerate(self.records):
            try:
                capsule = task_capsule.load_capsule(ROOT, item["capsule_id"], owner=self.owner)
                stages = len(capsule["definition"].get("steps") or [])
                providers = "、".join('沿用当前智能体' if value == 'current' else value
                    for value in capsule["requirements"].get("models", {}).get("providers", []))
                samples = len(capsule["samples"])
            except Exception:
                stages, providers, samples = "?", "", 0
            values = [item.get("title", item["capsule_id"]), str(item.get("version", 1)),
                      str(stages), providers or "沿用当前", str(samples)]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        if self.records:
            index = next((i for i, item in enumerate(self.records) if item['capsule_id'] == selected), 0)
            self.table.selectRow(index)
            self.show_selected()
        else:
            self.detail.setPlainText("还没有安装任务胶囊。可以先从可学习工作流导出一个 .oqtc 胶囊，再导入；也兼容 .zip。")
            self.show_runs([])

    def selected_id(self):
        row_index = self.table.currentRow()
        if not (0 <= row_index < len(self.records)):
            return ""
        return self.records[row_index]["capsule_id"]

    def show_selected(self):
        identifier = self.selected_id()
        if not identifier:
            return
        try:
            capsule = task_capsule.load_capsule(ROOT, identifier, owner=self.owner)
        except Exception as exc:
            return self.detail.setPlainText(str(exc))
        lines = [f"{capsule['title']}  ·  v{capsule['version']}", ""]
        lines.append("阶段：")
        for index, step in enumerate(capsule["definition"].get("steps") or []):
            lines.append(f"  {index + 1}. {step.get('title')} [{CATEGORY.get(step.get('category'), '通用任务')}]"
                         + (f" · {step['backend']}" if step.get("backend") else ""))
            lines.append(f"     {step.get('description', '')[:300]}")
        lines.append("")
        requirements = capsule['requirements']
        lines.append('所需智能体：' + ('、'.join('沿用当前智能体' if value == 'current' else value
            for value in requirements.get('models', {}).get('providers', [])) or '沿用当前'))
        labels = {'writes_workspace': '项目文件读写', 'runs_commands': '命令执行', 'uses_browser': '浏览器操作'}
        lines.append('所需权限：' + ('、'.join(label for key, label in labels.items()
                       if requirements.get('permissions', {}).get(key)) or '按阶段使用已有权限'))
        lines.append("")
        lines.append("测试样例：")
        for sample in capsule["samples"]:
            expect = sample['expect']
            descriptions = []
            if expect.get('keys'):
                descriptions.append('包含字段：' + '、'.join(expect['keys']))
            if expect.get('contains'):
                descriptions.append('包含指定文字')
            if 'equals' in expect:
                descriptions.append('与指定值一致')
            if expect.get('completed'):
                descriptions.append('完成并通过阶段验收')
            lines.append(f"  - {sample['name']}：" + '；'.join(descriptions))
        lines.append("")
        kinds = {'stage_plan': '阶段计划', 'browser_steps': '浏览器步骤', 'verified_run': '已验收流程'}
        lines.append("自动化示范：" + kinds.get(capsule['demo'].get('kind'), '流程说明') + " · " + str(capsule["demo"].get("note", "")))
        self.detail.setPlainText("\n".join(lines))
        self.load_runs()

    def on_status(self):
        self.load_runs()

    def load_runs(self):
        owner, identifier = self.house.current_qq, self.selected_id()
        if not owner or not identifier:
            self.show_runs([])
            return
        run_async(lambda _: task_capsule.capsule_runs(ROOT, owner, identifier),
                  lambda runs: self.show_runs(runs) if self.house.current_qq == owner and self.selected_id() == identifier else None,
                  lambda exc: self.run_detail.setPlainText(str(exc)) if self.house.current_qq == owner else None)

    def show_runs(self, runs):
        selected = self.run_selector.currentData()
        self.runs = [run for run in runs if run and run.get('owner_id') == self.house.current_qq]
        self.run_selector.blockSignals(True)
        self.run_selector.clear()
        for run in self.runs:
            created = run.get('created_at', '')
            if isinstance(created, (int, float)):
                created = datetime.fromtimestamp(created).strftime('%m-%d %H:%M')
            self.run_selector.addItem(str(created)[:19] + ' · ' + STATUS.get(run['status'], '待检查'), userData=run['goal_id'])
        index = self.run_selector.findData(selected)
        if index >= 0:
            self.run_selector.setCurrentIndex(index)
        self.run_selector.blockSignals(False)
        self.show_run()

    def show_run(self, *_):
        record = next((item for item in self.runs if item['goal_id'] == self.run_selector.currentData()), None)
        remaining = record and record['call_count'] < record['max_calls'] and (
            not record['token_budget'] or record['consumed_tokens'] < record['token_budget'])
        self.resume_button.setEnabled(bool(record and record['status'] == 'PAUSED' and remaining))
        self.pause_button.setEnabled(bool(record and record['status'] in ('ACTIVE', 'PENDING', 'RUNNING', 'WAITING_REVIEW')))
        self.artifacts.clear()
        if not record:
            self.run_detail.setPlainText('尚未运行。选择上方胶囊，填写本次输入后开始；执行记录会保存在当前好友下。')
        else:
            self.run_detail.setPlainText(describe_record('goal', record))
            paths = list(dict.fromkeys(str(path) for result in record.get('results', []) for path in result.get('artifacts', [])))
            for path in paths:
                self.artifacts.addItem(Path(path).name, userData=path)
        self.open_artifact.setEnabled(self.artifacts.count() > 0)

    def open_result(self):
        value = self.artifacts.currentData()
        if not value:
            return
        path = Path(value)
        if not path.is_absolute():
            from task_manager import TaskManager
            path = TaskManager.get_instance().workspace / path
        if not path.is_file():
            return toast(self.house, 'warning', '产物文件已不存在，请到任务中心查看验收记录。')
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def goal_action(self, action):
        owner, identifier = self.house.current_qq, self.run_selector.currentData()
        if self.busy or not owner or not identifier:
            return
        from task_manager import TaskManager
        manager = TaskManager.get_instance()
        def work(_):
            if not manager.goal_action(identifier, owner, action):
                raise ValueError('任务状态已变化，请刷新后重试。')
            if action == 'resume':
                manager.start_worker()
        run_async(work, lambda _: self.load_runs(), lambda exc: toast(self.house, 'error', str(exc)))

    def import_capsule(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择任务胶囊压缩包", str(ROOT),
                                                "任务胶囊压缩包 (*.zip *.oqtc);;所有文件 (*)")
        if path:
            self.install_paths([Path(path)])

    def install_paths(self, paths):
        """Install dropped/double-clicked capsule files (one or several)."""
        owner = self.house.current_qq
        if not owner:
            return toast(self.house, "warning", "请先选择好友，再导入任务胶囊。")

        def work(_):
            imported, errors = [], []
            for path in paths:
                try:
                    imported.append(task_capsule.install_capsule(ROOT, Path(path), owner=owner))
                except Exception as exc:
                    errors.append(f"{Path(path).name}：{exc}")
            return imported, errors

        def done(result):
            imported, errors = result
            self.load()
            if imported:
                titles = "、".join(item.get("title", "") for item in imported[:3])
                toast(self.house, "success", f"已导入 {len(imported)} 个任务胶囊：{titles}", "任务胶囊")
            if errors:
                toast(self.house, "warning", "部分胶囊未导入：" + errors[0][:240], "任务胶囊")

        run_async(work, done, lambda exc: toast(self.house, "error", str(exc), "胶囊导入失败"), pool=LONG_POOL)

    def run_capsule(self):
        if self.busy:
            return
        owner = self.house.current_qq
        identifier = self.selected_id()
        if not identifier or not owner:
            return toast(self.house, "warning", "请先选择一个胶囊。")
        if not confirm(self, "运行任务胶囊", "按胶囊内的工作流创建一轮任务？仍会使用当前好友的权限、预算和审批。", "运行"):
            return

        text, current = self.input.toPlainText(), self.current_agent.isChecked()
        self.busy = True
        self.run_button.setEnabled(False)
        def done(result=None, error=None):
            self.busy = False
            self.run_button.setEnabled(True)
            if self.house.current_qq != owner:
                return
            if error:
                toast(self.house, 'error', str(error), '胶囊未能运行')
            else:
                toast(self.house, "success", '已开始；可在下方查看阶段进度和产物。', "任务胶囊")
                self.load_runs()

        run_async(lambda _: task_capsule.run_capsule(ROOT, owner, identifier, input_text=text, use_current_agent=current),
                  done, lambda exc: done(error=exc), pool=LONG_POOL)

    def test_capsule(self):
        owner = self.house.current_qq
        identifier = self.selected_id()
        if not identifier:
            return toast(self.house, "warning", "请先选择一个胶囊。")

        def done(result):
            if self.house.current_qq != owner or self.selected_id() != identifier:
                return
            text = "\n".join(f"{'✓' if item['passed'] else '✗'} {item['name']}：{item['reason']}"
                             for item in result["results"])
            self.detail.setPlainText(f"完整性与样例结构检查 {result['passed']}/{result['total']}（未调用模型）\n\n" + text)
            toast(self.house, "success" if result["ok"] else "warning",
                  f"结构检查：{result['passed']}/{result['total']}，未调用模型", "任务胶囊")

        run_async(lambda _: task_capsule.test_capsule(ROOT, identifier, owner=owner), done,
                  lambda exc: toast(self.house, "error", str(exc), "测试失败"), pool=LONG_POOL)

    def remove_capsule(self):
        owner = self.house.current_qq
        identifier = self.selected_id()
        if not identifier:
            return
        record = next(item for item in self.records if item['capsule_id'] == identifier)
        shared = not record.get('owner')
        message = ('这是共享胶囊，卸载此版本会影响所有好友。' if shared else '卸载当前好友所选的胶囊版本。')
        if not confirm(self, "卸载任务胶囊", message + "文件会移到回收目录；已经创建的工作流仍保留。", "卸载"):
            return
        run_async(lambda _: task_capsule.remove_capsule(ROOT, identifier, version=record['version'], owner=None if shared else owner),
                  lambda value: (self.load(), toast(self.house, "success", "已卸载任务胶囊。")),
                  lambda exc: toast(self.house, "error", str(exc), "卸载失败"), pool=LONG_POOL)

    def pick_workflow(self):
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        owner = self.house.current_qq
        if not owner:
            return
        try:
            flows = WorkflowStore(TaskManager.get_instance()).list(owner)
        except Exception as exc:
            return toast(self.house, "error", str(exc), "读取工作流失败")
        if not flows:
            return toast(self.house, "warning", "还没有可学习工作流，请先在任务中心学习或创建。")
        from qfluentwidgets import ComboBox, MessageBoxBase, SubtitleLabel
        box = MessageBoxBase(self.house)
        box.viewLayout.addWidget(SubtitleLabel("选择要打包的工作流"))
        selector = ComboBox()
        for flow in flows:
            selector.addItem(f"{flow['title']}（第 {flow['version']} 版）", userData=flow["workflow_id"])
        box.viewLayout.addWidget(selector)
        box.yesButton.setText("选择")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        self.export_workflow = selector.currentData()
        self.workflow.setText(selector.currentText())
        if not self.title.text().strip():
            self.title.setText(selector.currentText().split("（第", 1)[0])

    def pick_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存任务胶囊", str(ROOT / "capsules"),
                                               "任务胶囊 (*.oqtc);;ZIP 兼容包 (*.zip)")
        if path:
            if not path.lower().endswith((".oqtc", ".zip")):
                path += ".oqtc"
            self.output.setText(path)

    def pick_skill(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择技能说明", str(ROOT), "技能说明 (SKILL.md *.md);;所有文件 (*)")
        if path:
            self.skills.append(path)
            self.skills_label.setText(f"已附加 {len(self.skills)} 个技能文件：" +
                                      "、".join(Path(item).parent.name or Path(item).stem for item in self.skills))

    def export_capsule(self):
        if not self.export_workflow:
            return toast(self.house, "warning", "请先选择要导出的工作流。")
        target = self.output.text().strip()
        if not target:
            self.pick_output()
            target = self.output.text().strip()
            if not target:
                return
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        store = WorkflowStore(TaskManager.get_instance())
        owner = self.house.current_qq
        title, description, skills = self.title.text().strip(), self.description.text().strip(), list(self.skills)
        workflow = self.export_workflow

        def done(record):
            self.load()
            toast(self.house, "success", f"已导出：{Path(record['path']).name}", "任务胶囊")

        run_async(lambda _: task_capsule.export_capsule(
            ROOT, store, owner, workflow, Path(target),
            title=title, description=description, skills=skills),
            done, lambda exc: toast(self.house, "error", str(exc), "导出失败"), pool=LONG_POOL)
