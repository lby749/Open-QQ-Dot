"""回溯中心: one timeline over task files, goals, migrations, capsules and memory."""
from __future__ import annotations

import time

from PySide6.QtWidgets import QTableWidgetItem
from qfluentwidgets import BodyLabel, PlainTextEdit, PushButton, TableWidget

from house_status import ROOT

from .common import LONG_POOL, Card, Page, TextDialog, confirm, hint, row, run_async, toast
import revert_center

KIND_NAMES = {"task": "任务文件", "goal": "长期目标", "migration": "迁移导入",
              "capsule": "任务胶囊", "memory": "长期记忆"}
STATUS_NAMES = {"applied": "可撤销", "reverted": "已撤销"}


class RevertPage(Page):
    def __init__(self, house):
        super().__init__(house, "revertPage")
        self.records = []
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "名称", "状态", "说明"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(260)
        self.table.itemSelectionChanged.connect(self.show_selected)
        self.detail = PlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(140)
        self.add(Card("回溯中心").add(
            hint("集中列出任务文件改动、长期目标阶段、平台迁移、任务胶囊与记忆恢复。"
                 "撤销不会硬删除：任务文件在 undone/，迁移与胶囊在 _trash/；任务文件撤销后可重做。"),
            self.table,
            row(self.button("刷新", self.load), self.button("预览", self.preview),
                self.button("撤销选中", self.revert), self.button("重做任务文件", self.redo),
                self.button("保存记忆快照", self.snapshot_memory), self.button("清理旧记录", self.prune)),
            self.detail))

    def button(self, title, action):
        button = PushButton(title)
        button.clicked.connect(action)
        return button

    def load(self):
        owner = self.house.current_qq
        if not owner:
            return
        run_async(lambda _: revert_center.timeline(ROOT, owner, 300), self.fill,
                  lambda exc: toast(self.house, "error", str(exc), "回溯中心"), pool=LONG_POOL)

    def shown(self):
        self.load()

    def fill(self, records):
        self.records = records or []
        self.table.setRowCount(len(self.records))
        for index, item in enumerate(self.records):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(item.get("created_at") or 0))
            values = [stamp, KIND_NAMES.get(item["kind"], item["kind"]), item.get("title") or item["target"],
                      STATUS_NAMES.get(item["status"], item["status"]), item.get("summary", "")]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        if self.records:
            self.table.selectRow(0)
        else:
            self.detail.setPlainText("还没有回溯记录。任务执行、迁移导入或安装胶囊后会自动出现在这里。")

    def selected(self):
        row_index = self.table.currentRow()
        if 0 <= row_index < len(self.records):
            return self.records[row_index]
        return None

    def show_selected(self):
        item = self.selected()
        if not item:
            return
        self.detail.setPlainText(f"{KIND_NAMES.get(item['kind'], item['kind'])} · {item.get('title') or item['target']}\n"
                                 f"{item.get('summary', '')}\n\n点击「预览」查看这次撤销会具体做什么。")

    def preview(self):
        item = self.selected()
        if not item:
            return toast(self.house, "warning", "请先选择一条记录。")
        def done(value):
            text = value.get("text", "")
            TextDialog(self.house, "撤销预览", f"{item.get('title') or item['target']}\n\n{text}").exec()
        run_async(lambda _: revert_center.preview(ROOT, item["operation_id"]), done,
                  lambda exc: toast(self.house, "error", str(exc), "无法预览"), pool=LONG_POOL)

    def revert(self):
        item = self.selected()
        if not item:
            return toast(self.house, "warning", "请先选择一条记录。")
        if not confirm(self, "撤销", f"撤销「{item.get('title') or item['target']}」？\n{self.detail.toPlainText()[:400]}", "撤销"):
            return

        def done(result):
            self.load()
            toast(self.house, "success", result.get("message", "已撤销"), "回溯中心")

        run_async(lambda _: revert_center.revert(ROOT, item["operation_id"]), done,
                  lambda exc: toast(self.house, "error", str(exc), "撤销失败"), pool=LONG_POOL)

    def redo(self):
        item = self.selected()
        if not item:
            return toast(self.house, "warning", "请先选择一条记录。")
        if item["kind"] != "task":
            return toast(self.house, "info", "只有任务文件改动支持重做；迁移与胶囊可重新导入。")
        run_async(lambda _: revert_center.redo(ROOT, item["operation_id"]),
                  lambda value: (self.load(), toast(self.house, "success", value.get("message", "已重做"))),
                  lambda exc: toast(self.house, "error", str(exc), "重做失败"), pool=LONG_POOL)

    def snapshot_memory(self):
        owner = self.house.current_qq
        if not owner:
            return toast(self.house, "warning", "请先选择好友。")
        run_async(lambda _: revert_center.snapshot_memory(ROOT, owner),
                  lambda _value: (self.load(), toast(self.house, "success", "已保存记忆快照，可在时间线中恢复")),
                  lambda exc: toast(self.house, "error", str(exc), "保存快照失败"), pool=LONG_POOL)

    def prune(self):
        if not confirm(self, "清理旧记录", "只删除 60 天前且已撤销的索引记录；撤销文件与回收目录保留。", "清理"):
            return
        run_async(lambda _: revert_center.prune(ROOT, keep_days=60),
                  lambda value: (self.load(), toast(self.house, "success", f"已清理 {value['removed_records']} 条索引")),
                  lambda exc: toast(self.house, "error", str(exc), "清理失败"), pool=LONG_POOL)
