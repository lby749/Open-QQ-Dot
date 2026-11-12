"""DotLink page: start the desktop QQ link, quick-login, in-app QR and exports.

No WebUI browser tab is opened.  The page talks to the local runtime through
:mod:`dotlink`; the QR image is rendered by Qt from the PNG the runtime
produced, so scanning happens on the desktop window itself.
"""
from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QLabel, QLineEdit, QTableWidgetItem
from qfluentwidgets import (BodyLabel, ComboBox, LineEdit, MessageBoxBase, PlainTextEdit, PushButton,
                            SubtitleLabel, TableWidget)

from house_status import ROOT
import diagnostics

from .common import LONG_POOL, Card, Page, confirm, hint, row, run_async, toast
import dotlink as link


class DotLinkPage(Page):
    def __init__(self, house):
        super().__init__(house, "dotlinkPage")
        self.paths = link.Paths(ROOT)
        self.state = {}
        self.qr_refresh_attempts = 0
        self.status = BodyLabel("正在读取 QQ 直连状态…")
        self.detail = BodyLabel("")
        self.detail.setWordWrap(True)
        self.accounts = ComboBox()
        self.accounts.setMinimumWidth(240)
        self.log = PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.qr = QLabel("扫码登录时，二维码会显示在这里。不需要打开网页。")
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setMinimumSize(260, 260)
        self.qr.setStyleSheet("QLabel { background: #FFFFFF; border: 1px solid #DBEAFE; border-radius: 12px; color: #64748B; }")
        self.login_timer = QTimer(self)
        self.login_timer.setInterval(2500)
        self.login_timer.timeout.connect(self.poll_login)
        self.refresh_qr_button = self.button("刷新二维码", self.refresh_qr)
        self.connect_button = self.button("连接并启动桥接", self.connect_bridge)
        self.add(Card("QQ 登录与连接").add(
            hint("登录后自动启动桥接。优先快速登录；也可以扫码或使用 QQ 号 + 密码。二维码可随时刷新。"),
            self.status, self.detail,
            row(BodyLabel("快速登录"), self.accounts, self.button("快速登录", self.quick_login),
                self.button("扫码登录", self.qr_login), self.button("密码登录", self.password_login)),
            row(self.refresh_qr_button, self.connect_button, None),
            self.qr))
        self.add(Card("好友").add(
            hint("登录后可以导入当前 QQ 账号的好友；已有好友不会重复添加。"),
            row(self.button("前往好友总览", self.open_friends),
                self.button("从 QQ 导入好友", self.import_friends), None)))
        self.add(Card("高级操作", collapsible=True, collapsed=True).add(
            row(self.button("启动 / 修复直连", self.start_runtime), self.button("刷新状态", self.refresh),
                self.button("停止直连", self.stop_runtime), self.button("一键诊断", self.diagnose),
                self.button("导出诊断包", self.export_diagnostics), None)))
        self.add(Card("聊天记录导出", collapsible=True, collapsed=True).add(
            hint("把当前 QQ 账号的好友与群聊天记录导出成迁移用的 JSON。不经过网页，也不会上传；"
                 "可选择是否包含群聊。导出文件包含原聊天文本与媒体地址，请妥善保管。"),
            row(self.button("导出全部好友与群聊 JSON", self.export_all_chats),
                self.button("导出当前好友", self.export_current_friend), None)))
        self.add(Card("运行日志", collapsible=True, collapsed=True).add(
            hint("只保留最近的本地日志，用于排查登录或端口问题。"), self.log))

    def button(self, title, action):
        button = PushButton(title)
        button.clicked.connect(action)
        return button

    def load(self):
        self.refresh()

    def shown(self):
        self.refresh()

    def on_status(self):
        self.refresh(quiet=True)

    def refresh(self, quiet=False):
        run_async(lambda _: link.state(self.paths), self.apply,
                  (lambda exc: None) if quiet else (lambda exc: toast(self.house, "error", str(exc), "QQ 直连")),
                  pool=LONG_POOL)

    def apply(self, value):
        self.state = value or {}
        running = bool(self.state.get("running"))
        login = self.state.get("login") or {}
        accounts = self.state.get("accounts") or []
        selected_account = self.accounts.currentData()
        self.accounts.clear()
        for account in accounts:
            label = f"{account.get('nickname') or account['uin']} · {account['uin']}"
            self.accounts.addItem(label, userData=account["uin"])
        if not accounts:
            if login.get("is_login"):
                self.accounts.addItem(f"当前已登录 · {login.get('uin') or ''}", userData=str(login.get("uin") or ""))
            else:
                self.accounts.addItem("本机没有快速登录记录", userData="")
        selected_index = self.accounts.findData(selected_account)
        if selected_index >= 0:
            self.accounts.setCurrentIndex(selected_index)
        if login.get("is_login"):
            self.login_timer.stop()
            self.qr.clear()
            self.qr.setVisible(False)
            self.refresh_qr_button.setVisible(False)
        online = bool(running and login.get('is_login') and login.get('online'))
        self.connect_button.setVisible(not online)
        if online:
            self.status.setText(f"QQ 已在线：{login.get('nickname') or login.get('uin')}（{login.get('uin')}）")
            self.status.setTextColor("#3E8E5E", "#4ADE80")
            mode = "消息接口已就绪" if self.state.get("onebot_listening") else "消息接口尚未就绪（稍等或点击“连接并启动桥接”）"
            self.detail.setText(f"{mode}；管理端口 {self.state.get('webui_port') or '-'}。")
        elif running and login.get('is_login'):
            self.status.setText("QQ 已登录，正在连接")
            self.status.setTextColor("#B45309", "#FBBF24")
            self.detail.setText("尚未确认在线，消息接口存在不代表群消息可以送达。")
        elif running:
            self.status.setText("QQ 直连正在运行，等待登录")
            self.status.setTextColor("#B45309", "#FBBF24")
            self.detail.setText(f"登录阶段：{login.get('phase') or '等待扫码'}"
                                + (f"；{login.get('error')}" if login.get("error") else ""))
        else:
            self.status.setText("QQ 直连未运行" + (f"：{self.state['error']}" if self.state.get("error") else ""))
            self.status.setTextColor("#64748B", "#94A3B8")
            self.detail.setText("点击“启动 / 修复直连”即可在本机准备好配置并启动，无需网页。")
        self.log.setPlainText(self.state.get("logs") or "")
        if not login.get("is_login"):
            self.show_qr(self.state.get("qr") or {})

    def show_qr(self, qr):
        data_url = (qr or {}).get("data_url") or ""
        if not data_url:
            self.qr.setVisible(False)
            return
        self.qr.setVisible(True)
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1])
            image = QPixmap()
            if image.loadFromData(raw):
                self.qr.setPixmap(image.scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self.qr.setText("")
        except Exception:
            pass

    def _ensure_runtime(self):
        if link.runtime_ready(self.paths):
            return True
        existing = link.find_existing_runtimes(self.paths.home)
        if existing:
            source = str(existing[0])
            if confirm(self, "检测到已有 NapCat",
                       "本机已有 QQ 直连运行时（NapCat）：\n\n"
                       f"{source}\n\n"
                       "可以直接使用它，不会复制、不会修改其 JS 文件，也不会重新下载。\n"
                       "只有你选择“下载官方版本”时才会重新下载。\n\n"
                       "NapCat 是独立项目，使用 Limited Redistribution License，"
                       "版权归 Copyright © 2024 Mlikiowa；会补充许可证和来源记录。",
                       "使用已有 NapCat"):
                try:
                    link.adopt_runtime(self.paths.home, existing[0])
                    toast(self.house, "success", "已使用本机已有 NapCat，未重新下载。", "DotLink")
                    return True
                except Exception as exc:
                    toast(self.house, "error", f"无法使用已有 NapCat：{exc}", "DotLink")
        if not confirm(self, "需要 QQ 直连运行时",
                       "本机没有检测到可用的 QQ 直连运行时（NapCat）。\n\n"
                       "安装器不会预先下载它。只有在你点击确认后，程序才会从 NapCat 官方 GitHub "
                       "下载并自动配置。\n\n"
                       "NapCat 是独立项目，使用 Limited Redistribution License，"
                       "版权归 Copyright © 2024 Mlikiowa；下载后会附完整许可证、来源和版本记录。",
                       "从 NapCat 官方下载"):
            return False
        from .runtime_download_dialog import RuntimeDownloadDialog
        return bool(RuntimeDownloadDialog(ROOT, self).exec())

    def start_runtime(self):
        if not self._ensure_runtime():
            return
        def done(_value):
            self.refresh()
            toast(self.house, "success", "QQ 直连已启动，正在读取登录态。", "DotLink")
        run_async(lambda _: link.start(self.paths), done,
                  lambda exc: toast(self.house, "error", str(exc), "QQ 直连启动失败"), pool=LONG_POOL)
        self.status.setText("正在启动 QQ 直连…")

    def quick_login(self):
        uin = self.accounts.currentData() if hasattr(self.accounts, "currentData") else ""
        if not uin:
            return toast(self.house, "warning", "没有可用的快速登录账号，请使用扫码登录。")
        self.begin(uin)

    def qr_login(self):
        if (self.state.get("login") or {}).get("is_login"):
            if not confirm(self, "重新扫码", "当前已登录。要断开当前登录并重新扫码吗？", "断开并扫码"):
                return

            def stopped(_):
                self.state = {**(self.state or {}), "login": {}}
                self.begin("", prefer_quick=False)

            return run_async(lambda _: link.stop(self.paths), stopped,
                             lambda exc: toast(self.house, "error", str(exc), "停止失败"), pool=LONG_POOL)
        self.begin("", prefer_quick=False)

    def refresh_qr(self, auto=False):
        if (self.state.get("login") or {}).get("is_login"):
            return toast(self.house, "info", "当前已登录；换号请先停止直连。", "二维码")
        if not auto:
            self.status.setText("正在刷新二维码…")
        self.qr_refresh_attempts += 1

        def done(qr):
            if not qr:
                self.status.setText("二维码暂不可用")
                return
            self.show_qr(qr)
            self.status.setText("二维码已刷新，请用手机 QQ 扫描")
            self.login_timer.start()

        run_async(lambda _: link.refresh_qr_image(self.paths), done,
                  lambda exc: toast(self.house, "error", str(exc), "刷新二维码"), pool=LONG_POOL)

    def password_login(self):
        login = self.state.get("login") or {}
        if login.get("is_login") and not confirm(
                self, "切换登录", "密码登录会先断开当前 QQ 登录，确定继续？", "继续"):
            return
        dialog = PasswordLoginDialog(self.house)
        if not dialog.exec():
            return
        uin, password = dialog.values
        self.status.setText("正在使用账号密码登录…")
        self.detail.setText("密码只在内存中转为 MD5，不会保存；如果 QQ 要求验证码会提示你。")

        def done(value):
            self.apply(value)
            toast(self.house, "success", "QQ 密码登录成功。", "DotLink")
            self.auto_connect()

        run_async(lambda _: link.password_login(self.paths, uin, password), done,
                  lambda exc: (self.status.setText("密码登录未完成"),
                               self.detail.setText(str(exc)),
                               toast(self.house, "error", str(exc), "登录未完成")), pool=LONG_POOL)

    def begin(self, account, prefer_quick=True):
        if not self._ensure_runtime():
            return
        self.qr_refresh_attempts = 0
        self.status.setText("正在准备登录…")
        self.detail.setText("有快速登录记录会优先免扫码；否则二维码会显示在本页。")

        def done(value):
            if value.get("mode") == "qr":
                self.show_qr(value.get("qr") or {})
                self.status.setText("请用手机 QQ 扫描本页二维码")
                self.detail.setText("扫码后无需在网页确认，登录成功会自动连接。")
                self.login_timer.start()
            else:
                self.login_timer.stop()
                self.apply(value)
                toast(self.house, "success", "QQ 已免扫码登录。", "DotLink")
                self.auto_connect()
        run_async(lambda _: link.begin_login(self.paths, account=account, prefer_quick=prefer_quick), done,
                  lambda exc: toast(self.house, "error", str(exc), "登录未完成"), pool=LONG_POOL)

    def auto_connect(self):
        """After a successful login, connect the bridge without another click."""
        if getattr(self.house, "running", False):
            return
        self.house.request("start")

    def poll_login(self):
        def done(value):
            if value.get("is_login"):
                self.login_timer.stop()
                self.refresh()
                toast(self.house, "success", f"QQ 登录成功：{value.get('nickname') or value.get('uin')}", "DotLink")
                self.auto_connect()
            elif value.get("error"):
                self.login_timer.stop()
                error = str(value["error"])
                if (("过期" in error or "expired" in error.lower()) and self.qr_refresh_attempts < 2):
                    self.detail.setText("二维码已过期，正在自动刷新…")
                    self.refresh_qr(auto=True)
                    return
                self.detail.setText("登录未完成：" + error)
        run_async(lambda _: link.poll_login(self.paths), done, lambda _exc: None, pool=LONG_POOL)

    def stop_runtime(self):
        run_async(lambda _: link.stop(self.paths),
                  lambda value: (self.refresh(), toast(self.house, "info", "QQ 直连已停止。", "DotLink")),
                  lambda exc: toast(self.house, "error", str(exc), "停止失败"), pool=LONG_POOL)

    def connect_bridge(self):
        self.house.request("start")

    def diagnose(self):
        self.detail.setText("正在诊断 QQ 直连…")
        def done(result):
            lines = ["连接诊断："]
            for item in result.get("checks", []):
                mark = "✓" if item.get("ok") else "✗"
                lines.append(f"{mark} {item['name']}：{item['detail']}")
                if not item.get("ok") and item.get("fix"):
                    lines.append(f"   → {item['fix']}")
            lines.append("")
            lines.append("全部正常。" if result.get("ok") else "按上面的修复提示处理后，再点“刷新状态”。")
            self.detail.setText("\n".join(lines))
            if result.get("ok"):
                toast(self.house, "success", "QQ 直连诊断通过。", "诊断")
            else:
                toast(self.house, "warning", "诊断发现未就绪项，详见本页。", "诊断")

        run_async(lambda _: link.diagnose(self.paths), done,
                  lambda exc: toast(self.house, "error", str(exc), "诊断失败"), pool=LONG_POOL)

    def export_diagnostics(self):
        import time
        default = ROOT / "exports" / f"OpenQQDot-diagnostic-{time.strftime('%Y%m%d-%H%M')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "保存诊断包", str(default), "Zip (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"

        def done(result):
            toast(self.house, "success", f"诊断包已保存：{result['target']}", "诊断")
            self.detail.setText(self.detail.text() + f"\n\n诊断包：{result['target']}")

        run_async(lambda _: diagnostics.export_bundle(ROOT, Path(path)), done,
                  lambda exc: toast(self.house, "error", str(exc), "诊断包导出失败"), pool=LONG_POOL)

    def open_friends(self):
        overview = getattr(self.house, "overview", None)
        if overview is not None:
            self.house.switchTo(overview)

    def import_friends(self):
        login = self.state.get("login") or {}
        if not login.get("is_login"):
            return toast(self.house, "warning", "请先登录 QQ，再导入好友。")

        def fetch(_):
            from qq_link import QQClient
            client = QQClient()
            try:
                return client.friend_list()
            finally:
                client.close()

        run_async(fetch, self.choose_friends,
                  lambda exc: toast(self.house, "error", str(exc), "读取 QQ 好友失败"), pool=LONG_POOL)

    def choose_friends(self, friends):
        if not friends:
            return toast(self.house, "info", "当前 QQ 账号没有可导入的好友。")
        dialog = FriendImportDialog(self.house, friends, set(self.house.records))
        if not dialog.exec() or not dialog.selected:
            return
        chosen = [{"uin": uin, "name": name} for uin, name in dialog.selected]
        self.status.setText(f"正在导入 {len(chosen)} 位好友…")

        def work(_):
            from house_status import invoke_bridge
            added, errors = [], []
            for item in chosen:
                code, output = invoke_bridge(f"bind -QQ {item['uin']}")
                if code:
                    errors.append(f"{item['name']}（{item['uin']}）：{output}")
                else:
                    added.append(item["uin"])
            return added, errors

        def done(result):
            added, errors = result
            self.house.reload_friends()
            self.status.setText("好友导入完成。")
            if added:
                toast(self.house, "success", f"已导入 {len(added)} 位好友，请逐个设置聊天风格。", "QQ 好友")
                self.open_friends()
            if errors:
                toast(self.house, "warning", f"{len(errors)} 位好友未导入：" + errors[0][:200], "好友导入")

        run_async(work, done, lambda exc: toast(self.house, "error", str(exc), "导入失败"), pool=LONG_POOL)

    def export_all_chats(self):
        if not (self.state.get("login") or {}).get("is_login"):
            return toast(self.house, "warning", "请先登录 QQ，再导出聊天记录。")
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录", str(ROOT))
        if not folder:
            return
        include_groups = confirm(self, "导出范围", "同时导出群聊吗？\n选择「确定」包含群聊，「取消」只导出好友。",
                                 "包含群聊", "只导出好友")
        self.export_all(folder, include_groups)

    def export_current_friend(self):
        login = self.state.get("login") or {}
        if not login.get("is_login"):
            return toast(self.house, "warning", "请先登录 QQ，再导出聊天记录。")
        qq = self.house.current_qq
        if not qq:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录", str(ROOT))
        if not folder:
            return
        self.export_all(folder, False, friend=qq, title=self.house.records.get(qq, {}).get("display_name", qq))

    def export_all(self, folder, include_groups, friend="", title=""):
        self.status.setText("正在导出聊天记录…")

        def work(_):
            from qq_history_export import HistoryClient, export_all, export_conversation, safe_name
            client, raw = HistoryClient.from_local(ROOT)
            try:
                if friend:
                    name = safe_name(title or friend)
                    return export_conversation(client, friend=friend, title=title,
                                               output=Path(folder) / f"{name}（{friend}）.json")
                return export_all(client, Path(folder), include_groups=include_groups)
            finally:
                raw.close()

        def done(result):
            self.status.setText("聊天记录已导出。")
            if "files" in result:
                toast(self.house, "success", f"已导出 {len(result['files'])} 个会话到 {folder}", "聊天记录")
            else:
                toast(self.house, "success", f"已导出 {result.get('messages', 0)} 条消息到 {folder}", "聊天记录")

        run_async(work, done, lambda exc: toast(self.house, "error", str(exc), "导出失败"), pool=LONG_POOL)


class FriendImportDialog(MessageBoxBase):
    """Pick QQ friends to bind; already-bound accounts are shown disabled."""

    def __init__(self, parent, friends, existing):
        super().__init__(parent)
        self.friends = []
        for item in friends:
            uin = str(item.get("user_id") or item.get("uin") or "")
            if not uin:
                continue
            name = str(item.get("remark") or item.get("nickname") or item.get("card") or uin)
            self.friends.append({"uin": uin, "name": name, "exists": uin in existing})
        self.selected = []
        self.viewLayout.addWidget(SubtitleLabel("从当前 QQ 导入好友"))
        self.viewLayout.addWidget(BodyLabel("勾选后添加为 Open QQ Dot 好友；已有好友不会重复添加。"))
        self.search = LineEdit()
        self.search.setPlaceholderText("按昵称或 QQ 号筛选")
        self.search.textChanged.connect(self.fill)
        self.viewLayout.addWidget(self.search)
        self.table = TableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["选择", "昵称", "QQ"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumSize(560, 360)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.viewLayout.addWidget(self.table)
        toggle = PushButton("全选 / 全不选")
        toggle.clicked.connect(self.toggle_all)
        self.viewLayout.addWidget(toggle)
        self.yesButton.setText("导入所选")
        self.cancelButton.setText("取消")
        self.fill()

    def fill(self):
        keyword = self.search.text().strip().lower()
        rows = [item for item in self.friends
                if not keyword or keyword in item["name"].lower() or keyword in item["uin"]]
        self.table.setRowCount(len(rows))
        self._rows = rows
        for index, item in enumerate(rows):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked if (item["exists"] or len(rows) <= 20) and not item["exists"] else Qt.Unchecked)
            if item["exists"]:
                check.setCheckState(Qt.Unchecked)
                check.setFlags(Qt.ItemIsEnabled)
                check.setToolTip("已经添加")
            self.table.setItem(index, 0, check)
            self.table.setItem(index, 1, QTableWidgetItem(item["name"] + ("（已添加）" if item["exists"] else "")))
            self.table.setItem(index, 2, QTableWidgetItem(item["uin"]))

    def toggle_all(self):
        rows = getattr(self, "_rows", [])
        should_check = any(self.table.item(index, 0).checkState() != Qt.Checked
                           for index in range(len(rows))
                           if self.table.item(index, 0).flags() & Qt.ItemIsUserCheckable)
        for index, item in enumerate(rows):
            cell = self.table.item(index, 0)
            if item["exists"]:
                continue
            cell.setCheckState(Qt.Checked if should_check else Qt.Unchecked)

    def accept(self):
        self.selected = []
        for index, item in enumerate(getattr(self, "_rows", [])):
            cell = self.table.item(index, 0)
            if not item["exists"] and cell is not None and cell.checkState() == Qt.Checked:
                self.selected.append((item["uin"], item["name"]))
        super().accept()


class PasswordLoginDialog(MessageBoxBase):
    """QQ number + password login.  The password stays in memory and is sent as MD5."""

    def __init__(self, parent):
        super().__init__(parent)
        self.values = ("", "")
        self.viewLayout.addWidget(SubtitleLabel("QQ 账号密码登录"))
        self.viewLayout.addWidget(BodyLabel("密码不会保存到磁盘；如果 QQ 要求验证码，会提示验证地址。"))
        self.uin = LineEdit()
        self.uin.setPlaceholderText("QQ 号（5–12 位数字）")
        self.password = LineEdit()
        self.password.setPlaceholderText("QQ 密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = BodyLabel("")
        self.error.setTextColor("#DC2626", "#F87171")
        self.viewLayout.addWidget(self.uin)
        self.viewLayout.addWidget(self.password)
        self.viewLayout.addWidget(self.error)
        self.yesButton.setText("登录")
        self.cancelButton.setText("取消")

    def accept(self):
        import re
        uin = self.uin.text().strip()
        password = self.password.text()
        if not re.fullmatch(r"\d{5,12}", uin):
            self.error.setText("请输入 5–12 位数字 QQ 号。")
            return
        if not password:
            self.error.setText("请输入密码。")
            return
        self.values = (uin, password)
        super().accept()


