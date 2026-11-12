"""Migration page: import ChatGPT/Claude/DeepSeek/JSONL/Markdown conversations and projects."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QTableWidgetItem
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PlainTextEdit, PushButton, TableWidget

from house_status import ROOT

from .common import LONG_POOL, Card, Page, confirm, hint, row, run_async, toast
import app_backup
import platform_migration


class MigrationPage(Page):
    def __init__(self, house):
        super().__init__(house, "migrationPage")
        self.mode = ComboBox()
        self.mode.addItem('平台导出（对话 + 项目）', userData='export')
        self.mode.addItem('CLI 项目目录（只迁目录，不带聊天上下文）', userData='cli')
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.source = LineEdit()
        self.source.setPlaceholderText("选择 ChatGPT / Claude 导出的 conversations.json（或 .zip、目录、.jsonl、.md）")
        self.project = LineEdit()
        self.project.setPlaceholderText("项目名称（留空自动命名）")
        self.desktop_table = TableWidget()
        self.desktop_table.setColumnCount(4)
        self.desktop_table.setHorizontalHeaderLabels(['应用', '项目', '目录', '会话'])
        self.desktop_table.horizontalHeader().setStretchLastSection(True)
        self.desktop_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.desktop_table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.desktop_table.setMinimumHeight(170)
        self.report = PlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(180)
        self.imports = TableWidget()
        self.imports.setColumnCount(4)
        self.imports.setHorizontalHeaderLabels(["时间", "平台", "项目", "状态"])
        self.imports.horizontalHeader().setStretchLastSection(True)
        self.imports.setMinimumHeight(150)
        self.records = []
        self.add(Card("从其他平台迁移对话与项目").add(
            hint("只读取你选择的导出文件，不联网、不改原文件。迁移会创建可继续使用的桌面项目和对话，并在回溯中心保留撤销记录。"),
            row(self.mode, None),
            row(self.source, self.button("选择文件", self.pick_file), self.button("选择目录", self.pick_folder)),
            row(self.project, None),
            row(self.button("先检查（不写入）", self.inspect), self.button("开始迁移", self.run_import)),
            self.report))
        self.add(Card("从本机 CLI / 桌面应用迁移项目", collapsible=True).add(
            hint("扫描 Codex、Claude Code、DeepSeek Harness 的默认数据目录，只读取会话首行里的项目路径；"
                 "不导入聊天上下文、账号或令牌，只复制项目目录并在控制台建立空会话入口。"),
            row(self.button("扫描本机应用", self.scan_desktop),
                self.button("迁移选中项目", self.migrate_desktop), None),
            self.desktop_table))
        self.add(Card("最近的迁移", collapsible=True, collapsed=True).add(self.imports,
            row(self.button("刷新列表", self.load), self.button("撤销最近一次", self.undo_last))))
        self.backup_note = BodyLabel("备份会包含好友、任务、迁移记录、胶囊和 DotLink 设置；不包含 QQ 登录数据库和日志。")
        self.backup_note.setWordWrap(True)
        self.add(Card("数据备份与恢复").add(
            hint("换电脑或重装前先创建一个 .oqdbackup；恢复会保留旧数据到 _before_restore/，不会直接删除。"),
            self.backup_note,
            row(self.button("创建数据备份", self.create_backup),
                self.button("从备份恢复", self.restore_backup), None)))

    def update_mode(self):
        cli = self.mode.currentData() == 'cli'
        self.source.setPlaceholderText('选择本地 CLI 项目目录（只复制目录，不导入对话/上下文）' if cli
                                         else '选择 ChatGPT / Claude 导出的 conversations.json（或 .zip、目录、.jsonl、.md）')

    def button(self, title, action):
        button = PushButton(title)
        button.clicked.connect(action)
        return button

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择平台导出文件", str(ROOT),
            "对话导出 (*.json *.jsonl *.zip *.md);;所有文件 (*)")
        if path:
            self.source.setText(path)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择导出目录", str(ROOT))
        if path:
            self.source.setText(path)

    def load(self):
        owner = self.house.current_qq
        if not owner:
            return
        run_async(lambda _: platform_migration.list_imports(ROOT, owner), self.fill,
                  lambda exc: toast(self.house, "error", str(exc), "迁移记录"), pool=LONG_POOL)

    def shown(self):
        self.load()

    def fill(self, records):
        self.records = records or []
        self.imports.setRowCount(len(self.records))
        import time
        for index, item in enumerate(self.records):
            values = [time.strftime("%m-%d %H:%M", time.localtime(item.get("created_at") or 0)),
                      item.get("platform", ""), item.get("project_title", ""),
                      "已撤销" if item.get("status") == "reverted" else "已导入"]
            for column, value in enumerate(values):
                self.imports.setItem(index, column, QTableWidgetItem(str(value)))
        if self.records:
            self.imports.selectRow(0)

    def inspect_paths(self, paths):
        """Preview dropped/double-clicked exports; the first one is shown."""
        paths = [Path(item) for item in paths if Path(item).exists()]
        if not paths:
            return toast(self.house, "warning", "没有可读取的文件。")
        self.source.setText(str(paths[0]))
        self.inspect()
        if len(paths) > 1:
            toast(self.house, "info", f"已载入第一个文件；其余 {len(paths) - 1} 个可逐个拖入检查。", "迁移助手")

    def inspect(self):
        path = self.source.text().strip()
        if not path:
            return toast(self.house, "warning", "请先选择导出文件或目录。")
        if self.mode.currentData() == 'cli':
            if not Path(path).is_dir():
                return toast(self.house, "error", "CLI 迁移需要选择一个项目目录。")
            self.report.setPlainText('CLI 迁移模式\n\n只会复制这个项目目录，并创建一个空的会话入口。\n'
                                     '不会导入原 CLI 的对话、上下文、登录信息或账号令牌。\n\n'
                                     f'目录：{path}')
            return

        def done(report):
            lines = [f"平台：{report['platform']}", f"对话：{report['conversations']} 个",
                     f"消息：{report['messages']} 条", f"建议项目名：{report['project']}"]
            if report.get("projects"):
                lines.append("涉及项目：" + "、".join(report["projects"][:10]))
            if report.get("earliest"):
                import time
                lines.append("时间范围：" + time.strftime("%Y-%m-%d", time.localtime(report["earliest"])) + " 起")
            lines += ["", "对话预览："] + [f"  - {title}" for title in report.get("titles", [])]
            lines += ["", "确认后会写入新的项目目录和会话，可在回溯中心撤销。"]
            self.report.setPlainText("\n".join(lines))
            if not self.project.text().strip():
                self.project.setText(report["project"])
            toast(self.house, "success", "检查完成，尚未写入任何内容。", "迁移助手")

        run_async(lambda _: platform_migration.inspect_report(Path(path)), done,
                  lambda exc: toast(self.house, "error", str(exc), "无法读取导出"), pool=LONG_POOL)

    def run_import(self):
        path, owner = self.source.text().strip(), self.house.current_qq
        if not path:
            return toast(self.house, "warning", "请先选择导出文件或目录。")
        if not owner:
            return toast(self.house, "warning", "请先选择好友。")
        cli = self.mode.currentData() == 'cli'
        message = ("只复制这个项目目录并创建空会话入口；不会迁移 CLI 的对话或上下文。继续？" if cli
                   else "导入会创建新的项目与会话；原文件不会被修改。继续？")
        if not confirm(self, "开始迁移", message, "开始迁移"):
            return

        def done(manifest):
            if manifest.get("duplicate"):
                toast(self.house, "info", "这个导出已经迁移过，未重复写入。")
            else:
                toast(self.house, "success",
                      f"迁移完成：{manifest['stats']['conversations']} 个对话 / {manifest['stats']['messages']} 条消息")
            self.report.setPlainText(f"迁移完成。导入编号：{manifest['import_id']}\n"
                                     f"项目：{manifest['project_title']}\n"
                                     f"可在回溯中心撤销这次迁移。")
            self.load()
            self.open_console(manifest)

        if cli:
            runner = lambda _: platform_migration.run_cli_import(ROOT, owner, Path(path),
                                                                 project_title=self.project.text().strip())
        else:
            runner = lambda _: platform_migration.run_import(ROOT, owner, Path(path),
                                                             project_title=self.project.text().strip())
        run_async(runner, done, lambda exc: toast(self.house, "error", str(exc), "迁移失败"), pool=LONG_POOL)

    def open_console(self, manifest):
        """Select the imported workspace/session and show it in the agent console."""
        project = (manifest.get("projects") or [{}])[0]
        ws_id = project.get("workspace_id")
        session_id = project.get("session_id") or next(
            (item.get("session_id") for item in manifest.get("conversations", []) if item.get("session_id")), "")
        try:
            import session_manager
            qq = self.house.current_qq
            if ws_id:
                session_manager.set_active_workspace_id(qq, ws_id)
            if ws_id and session_id:
                session_manager.set_active_session_id(qq, session_id, ws_id)
            console = self.house.pages.get('workbenchPage')
            if console:
                console.reload_projects_tree(qq)
        except Exception:
            pass
        if self.house.current_qq:
            self.house.show_friend(self.house.current_qq, 'workbenchPage')

    def scan_desktop(self):
        def done(items):
            self.desktop_table.setRowCount(len(items))
            for index, item in enumerate(items):
                values = [item.get("app", ""), item.get("title", ""), item.get("path", ""),
                          str(item.get("sessions", 0))]
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(Qt.UserRole, item.get("path", ""))
                    self.desktop_table.setItem(index, column, cell)
            toast(self.house, "info", f"找到 {len(items)} 个可迁移项目。", "本机应用迁移")

        run_async(lambda _: platform_migration.discover_desktop_projects(), done,
                  lambda exc: toast(self.house, "error", str(exc), "扫描失败"), pool=LONG_POOL)

    def migrate_desktop(self):
        owner = self.house.current_qq
        if not owner:
            return toast(self.house, "warning", "请先选择好友。")
        rows = sorted({index.row() for index in self.desktop_table.selectedIndexes()})
        paths = []
        for row in rows:
            cell = self.desktop_table.item(row, 0)
            if cell and cell.data(Qt.UserRole):
                paths.append(Path(cell.data(Qt.UserRole)))
        if not paths:
            return toast(self.house, "warning", "请先扫描并选中要迁移的项目。")
        if not confirm(self, "迁移本机项目", f"将复制 {len(paths)} 个项目目录，不导入聊天/上下文。继续？", "开始迁移"):
            return

        def job(report):
            results = []
            for index, path in enumerate(paths):
                report(f"复制 {index + 1}/{len(paths)}：{path.name}…")
                results.append(platform_migration.run_cli_import(ROOT, owner, path))
            return results

        def done(results):
            self.report.setPlainText("\n".join(
                f"已迁移：{item['project_title']} → {item['projects'][0]['path']}" for item in results))
            self.load()
            if results:
                self.open_console(results[-1])
            toast(self.house, "success", f"已迁移 {len(results)} 个项目。", "本机应用迁移")

        run_async(job, done, lambda exc: toast(self.house, "error", str(exc), "迁移失败"), pool=LONG_POOL)

    def undo_last(self):
        owner = self.house.current_qq
        if not owner:
            return
        record = next((item for item in self.records if item.get("status") != "reverted"), None)
        if not record:
            return toast(self.house, "info", "没有可撤销的迁移。")
        if not confirm(self, "撤销迁移", f"撤销「{record.get('project_title')}」及其会话？导入包会移到回收目录。", "撤销"):
            return
        run_async(lambda _: platform_migration.undo_import(ROOT, owner, record["import_id"]),
                  lambda report: (self.load(), toast(self.house, "success", "已撤销这次迁移。")),
                  lambda exc: toast(self.house, "error", str(exc), "撤销失败"), pool=LONG_POOL)

    def create_backup(self):
        import time
        default = ROOT / "exports" / f"OpenQQDot-{time.strftime('%Y%m%d-%H%M')}.oqdbackup"
        path, _ = QFileDialog.getSaveFileName(self, "保存数据备份", str(default),
                                              "Open QQ Dot 备份 (*.oqdbackup)")
        if not path:
            return
        if not path.lower().endswith(app_backup.SUFFIX):
            path += app_backup.SUFFIX

        def done(report):
            self.backup_note.setText(f"最近备份：{report['files']} 个文件，"
                                     f"{report['bytes'] / (1024 ** 2):.1f} MB → {report['target']}")
            toast(self.house, "success", "数据备份已创建。", "备份")

        run_async(lambda _: app_backup.create_backup(ROOT, Path(path)), done,
                  lambda exc: toast(self.house, "error", str(exc), "备份失败"), pool=LONG_POOL)

    def restore_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择数据备份", str(ROOT),
                                              "Open QQ Dot 备份 (*.oqdbackup);;所有文件 (*)")
        if not path:
            return
        if not confirm(self, "恢复数据", "恢复会覆盖好友、任务、迁移记录和设置；旧数据会保留到 _before_restore/。继续？",
                       "恢复"):
            return

        def done(report):
            self.house.reload_friends()
            self.load()
            self.backup_note.setText(f"已恢复 {report['files']} 个文件；旧数据在 {report['previous'] or '（无）'}。")
            toast(self.house, "success", "数据已恢复，建议重启 Open QQ Dot 让所有页面重新加载。", "备份")

        run_async(lambda _: app_backup.restore_backup(ROOT, Path(path)), done,
                  lambda exc: toast(self.house, "error", str(exc), "恢复失败"), pool=LONG_POOL)

