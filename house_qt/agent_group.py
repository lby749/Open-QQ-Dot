"""Multi-agent group chat subpage (first tab of 智能体协作)."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)
from qfluentwidgets import CheckBox, ComboBox, PushButton, MessageBoxBase, LineEdit, SubtitleLabel

from house_settings import data_dir
from house_status import load_profile
from store import FriendStore
from .common import LONG_POOL, confirm, hint, run_async, toast
from agent_tool_adapters import AGENT_BACKENDS


class MemberDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent.window() if parent else None)
        self.viewLayout.addWidget(SubtitleLabel('添加智能体'))
        self.widget.setMinimumWidth(440)
        form = QFormLayout()
        self.viewLayout.addLayout(form)
        self.provider = ComboBox()
        from .pages_friend import ModelPage
        for key in ("codex", "claude", "antigravity", "dsh", "api", *AGENT_BACKENDS):
            title = ModelPage.PROVIDER_TITLES.get('custom' if key == 'api' else key, (key,))[0]
            self.provider.addItem(title, userData=key)
        self.name = LineEdit()
        self.name.setPlaceholderText("留空自动生成，如 codex-执行者")
        self.role = LineEdit()
        self.role.setPlaceholderText("例如：审查者，检查代码和事实")
        self.model = LineEdit()
        self.model.setPlaceholderText("可选：该 Agent 的模型名")
        self.template = ComboBox()
        self.template.addItem("不使用模板", userData="")
        try:
            from persona_template import list_templates
            for item in list_templates():
                self.template.addItem(f"{item['name']} · {item['source']['speaker']}", userData=item["id"])
        except Exception:
            pass
        form.addRow("智能体工具", self.provider)
        form.addRow("名字", self.name)
        form.addRow("角色", self.role)
        form.addRow("模型", self.model)
        form.addRow("人格模板", self.template)
        self.yesButton.setText('添加')
        self.cancelButton.setText('取消')


class AgentGroupPage(QWidget):
    """A group room where every member is a real agent backend."""

    def __init__(self, house):
        super().__init__(house)
        self.house = house
        self.setObjectName("agentGroupPage")
        self.room_id = None
        self.room = None
        self.loaded_for = None
        self.busy = False
        self.pending = []
        self.setStyleSheet("""
            QWidget#agentGroupPage {background:#F5F8FC;}
            QLabel#agTitle {font-size:20px;font-weight:700;color:#1c2d48;}
            QListWidget {background:white;border:1px solid #E2E8F0;border-radius:8px;padding:2px;}
            QTextEdit {background:white;border:1px solid #E2E8F0;border-radius:8px;color:#243656;padding:6px;}
            QLineEdit {background:white;border:1px solid #CBD5E1;border-radius:7px;padding:7px;}
        """)
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(12)

        left = QVBoxLayout()
        title = QLabel("多智能体群聊")
        title.setObjectName("agTitle")
        left.addWidget(title)
        left.addWidget(hint("一个 Agent 可拆成多个角色；不同 Agent 也能进同一个群。"
                            "成员共享群聊长期记忆和好友记忆，但可以各自选择人格模板。"))
        self.rooms = QListWidget()
        self.rooms.currentItemChanged.connect(lambda item, _old: self.open_room(item.data(Qt.UserRole) if item else None))
        left.addWidget(self.rooms, 1)
        buttons = QHBoxLayout()
        new = PushButton("新建群")
        new.clicked.connect(self.new_room)
        delete = PushButton("删除")
        delete.clicked.connect(self.delete_room)
        buttons.addWidget(new)
        buttons.addWidget(delete)
        buttons.addStretch(1)
        left.addLayout(buttons)
        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(260)

        right = QVBoxLayout()
        top = QHBoxLayout()
        self.room_title = QLabel("选择或新建一个协作群")
        self.room_title.setStyleSheet("font-size:16px;font-weight:700;color:#1E293B;")
        add = PushButton("添加成员")
        add.clicked.connect(self.add_member)
        split = PushButton("拆分单个 Agent")
        split.clicked.connect(self.split_agent)
        top.addWidget(self.room_title, 1)
        bind_qq = PushButton('接入 QQ 群')
        bind_qq.clicked.connect(self.bind_qq_group)
        top.addWidget(bind_qq)
        top.addWidget(add)
        top.addWidget(split)
        right.addLayout(top)

        controls = QHBoxLayout()
        self.mode = ComboBox()
        self.mode.addItem("顺序发言", userData="sequential")
        self.mode.addItem("并行调用", userData="parallel")
        self.mode.setMinimumWidth(120)
        self.host = ComboBox()
        self.host.setMinimumWidth(160)
        self.summary = CheckBox("主持人分发后汇总")
        self.summary.setChecked(True)
        controls.addWidget(QLabel("模式"))
        controls.addWidget(self.mode)
        controls.addSpacing(8)
        controls.addWidget(QLabel("主持人"))
        controls.addWidget(self.host)
        controls.addWidget(self.summary)
        controls.addStretch(1)
        save = PushButton("保存群设置")
        save.clicked.connect(self.save_settings)
        controls.addWidget(save)
        right.addLayout(controls)

        self.members = QLabel("")
        self.members.setWordWrap(True)
        self.members.setStyleSheet("color:#64748B;font-size:12px;")
        right.addWidget(self.members)
        self.qq_status = hint('本地协作群；可将当前 QQ 设为主账号，多个官方机器人设为成员。')
        right.addWidget(self.qq_status)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        right.addWidget(self.log, 1)
        send_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("在群里说点什么；成员回应时也可以继续输入，命令会排队…")
        self.input.returnPressed.connect(self.send)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self.send)
        self.send_qq_btn = QPushButton('发送到 QQ 群协作')
        self.send_qq_btn.clicked.connect(self.send_to_qq)
        send_row.addWidget(self.input, 1)
        send_row.addWidget(self.send_btn)
        send_row.addWidget(self.send_qq_btn)
        right.addLayout(send_row)
        self.status = hint("")
        right.addWidget(self.status)
        right_box = QWidget()
        right_box.setLayout(right)
        root.addWidget(left_box)
        root.addWidget(right_box, 1)
        self.refresh()

    def _owner(self):
        return str(getattr(self.house, "current_qq", "") or "")

    def _base(self):
        return data_dir()

    def shown(self):
        self.refresh()

    def refresh(self):
        import agent_group
        self.send_qq_btn.setEnabled(False)
        owner = self._owner()
        if owner != self.loaded_for:
            self.room_id = None
            self.room = None
            self.pending.clear()
            self.input.clear()
            self.log.clear()
            self.members.clear()
            self.qq_status.setText('本地协作群；可将当前 QQ 设为主账号，多个官方机器人设为成员。')
            self.room_title.setText("选择或新建一个协作群")
        current = self.room_id
        self.rooms.blockSignals(True)
        self.rooms.clear()
        if not owner:
            self.status.setText("先在好友总览选择一位好友；群聊按好友隔离。")
            self.rooms.blockSignals(False)
            return
        self.loaded_for = owner
        rooms = agent_group.list_rooms(self._base(), owner)
        for room in rooms:
            item = QListWidgetItem(f"{room['title']}　·　{room['members']} 人 · {room['messages']} 条")
            item.setData(Qt.UserRole, room["id"])
            self.rooms.addItem(item)
        self.rooms.blockSignals(False)
        if current and any(r["id"] == current for r in rooms):
            self.open_room(current)
        elif rooms:
            self.rooms.setCurrentRow(0)
            self.open_room(rooms[0]["id"])
        else:
            self.room_id = None
            self.room = None
            self.room_title.setText("还没有协作群，点「新建群」开始")
            self.members.setText("")
            self.log.clear()

    def new_room(self):
        import agent_group
        owner = self._owner()
        if not owner:
            return toast(self, "warning", "请先选择好友。")
        title, ok = QInputDialog.getText(self, "新建多智能体群", "群名称：", text="新协作群")
        if not ok:
            return
        room = agent_group.create_room(self._base(), owner, title)
        self.room_id = room["id"]
        self.refresh()
        self.open_room(room["id"])

    def delete_room(self):
        if not self.room_id or not confirm(self, "删除协作群", "删除后不可恢复，确定吗？"):
            return
        path = Path(self._base()) / "agent_groups" / self._owner() / f"{self.room_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return toast(self, "error", str(exc))
        self.room_id = None
        self.refresh()

    def open_room(self, room_id):
        import agent_group
        if not room_id or not self._owner():
            return
        if room_id != self.room_id:
            self.pending.clear()
            self.input.clear()
        try:
            self.room = agent_group.load_room(self._base(), self._owner(), room_id)
        except (OSError, ValueError):
            return
        self.room_id = room_id
        self.room_title.setText(self.room.get("title", "协作群"))
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.room.get("mode", "sequential"))))
        self.mode.blockSignals(False)
        self.summary.setChecked(bool(self.room.get("summary", True)))
        members = self.room.get("members") or []
        self.members.setText("成员：" + ("　".join(
            f"{m['name']}（{m['provider']} · {m.get('role', '')[:16]}"
            + (f" · 模板 {m.get('template', '')[:6]}" if m.get("template") else "") + "）"
            for m in members) if members else "还没有成员"))
        self.host.blockSignals(True)
        self.host.clear()
        self.host.addItem("无主持人", userData="")
        for member in members:
            self.host.addItem(member["name"], userData=member["id"])
        self.host.setCurrentIndex(max(0, self.host.findData(self.room.get("host_id", ""))))
        self.host.blockSignals(False)
        self.refresh_qq_status()
        self.render_log()

    def bind_qq_group(self):
        if not self.room_id:
            return toast(self, 'warning', '请先新建协作群、添加成员并选择主持人。')
        from .qq_group_binding import QQGroupBindingDialog
        dialog = QQGroupBindingDialog(self.house, self._base(), self._owner(), self.room_id, self)
        if dialog.exec():
            self.open_room(self.room_id)

    def refresh_qq_status(self):
        self.send_qq_btn.setEnabled(False)
        if not self.room_id or not self._owner():
            self.qq_status.setText('本地协作群；可将当前 QQ 设为主账号，多个官方机器人设为成员。')
            return
        import agent_group
        import qq_group_team
        try:
            room = agent_group.load_room(self._base(), self._owner(), self.room_id)
            config = room.get('qq_group') or {}
            if not config.get('enabled'):
                self.qq_status.setText('仅本地协作，未启用真实 QQ 群接入。')
                return
            profile = load_profile(self.house.profile_path)
            available, reason = agent_group.node_states(room, profile).get(room.get('host_id'), (False, '未设置主智能体'))
            if not available:
                self.qq_status.setText('此流程当前不可执行：' + reason)
                return
            self.send_qq_btn.setEnabled(not self.busy)
            state = qq_group_team.snapshot(self._base(), self._owner(), self.room_id)
        except Exception:
            self.send_qq_btn.setEnabled(False)
            self.qq_status.setText('群接入状态暂不可读，请检查群设置和本地记录。')
            return
        pending = sum(row['state'] == 'PENDING' for row in state['deliveries'])
        uncertain = sum(row['state'] == 'UNCERTAIN' for row in state['deliveries'])
        latest = state['runs'][0]['detail'] if state['runs'] else '尚未发起协作'
        self.qq_status.setText(f"主 QQ {config['self_id']} · QQ 群 {config['group_id']} · "
            f"{len(config['bots'])} 名机器人成员 · 待发送 {pending} · 未确认 {uncertain}；"
            '可从此页发起；群内仅允许指定成员 @ 主账号或发送“协作：内容”来发起。\n' + latest)

    on_status = refresh_qq_status

    def send_to_qq(self):
        """Submit the desktop user's command to the bound real group, without faking QQ events."""
        import uuid
        text, owner, room_id, base = self.input.text().strip(), self._owner(), self.room_id, self._base()
        if not text or self.busy or not room_id:
            return
        self.busy = True
        self.send_qq_btn.setEnabled(False)
        operation = uuid.uuid4().hex
        self.status.setText('正在向已绑定的 QQ 群发起协作…')
        def job(_):
            from qq_link import QQClient
            import qq_group_team
            client = QQClient()
            team = None
            try:
                team = qq_group_team.QQGroupTeam(base, base.parent, lambda: client,
                                               lambda: str(client.call('get_login_info')['user_id']))
                team.start()
                key = team.start_local(owner, room_id, text, operation)
                team.pool.shutdown(wait=True)
                with qq_group_team.database(base) as db:
                    return dict(db.execute('SELECT status,detail FROM runs WHERE id=?', (key,)).fetchone())
            finally:
                if team: team.close()
                client.close()
        def done(result):
            self.busy = False
            if self._owner() == owner and self.room_id == room_id:
                if self.input.text().strip() == text:
                    self.input.clear()
                self.status.setText(result['detail'])
                self.refresh_qq_status()
        def failed(exc):
            self.busy = False
            if self._owner() == owner and self.room_id == room_id:
                self.status.setText(str(exc))
                self.refresh_qq_status()
        run_async(job, done, failed, pool=LONG_POOL)

    def render_log(self):
        if not self.room:
            self.log.clear()
            return
        rows = []
        for message in self.room.get("messages") or []:
            name = "你" if message.get("role") == "user" else message.get("name", "成员")
            color = "#2563EB" if message.get("role") == "user" else (
                "#B45309" if message.get("role") == "host" else "#0F766E")
            text = str(message.get("content") or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
            rows.append(f"<div style='margin:8px 0'><b style='color:{color}'>{name}</b>"
                        f"<div style='color:#243656'>{text}</div></div>")
        if (self.room.get('qq_group') or {}).get('enabled'):
            import html
            import qq_group_team
            try:
                state = qq_group_team.snapshot(self._base(), self._owner(), self.room_id)
                names = {m['id']: m['name'] for m in self.room.get('members', [])}
                states = {'SENT':'已送达', 'PENDING':'等待发送', 'SENDING':'正在发送',
                          'UNCERTAIN':'发送未确认', 'REVOKED':'接入已撤销'}
                def display(label, content):
                    if content:
                        rows.append("<div style='margin:8px 0'><b style='color:#0F766E'>" + html.escape(label) +
                            "</b><div>" + html.escape(content).replace('\n','<br>') + '</div></div>')
                for run in reversed(state['runs']):
                    display('QQ 群 · 主持人', run.get('dispatch', ''))
                    for item in reversed(state['deliveries']):
                        if item['run'] == run['id']:
                            route = {'group': '群内', 'private': '私聊主账号', 'mailbox': '智能体邮箱'}.get(item.get('route', 'group'), '协作')
                            display(route + ' · ' + names.get(item['member'], '成员') + ' · ' + states.get(item['state'], '状态未知'), item['content'])
                    display('QQ 群 · 主持人', run.get('summary', ''))
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                rows.append('<div>QQ群记录暂不可读，请查看连接状态。</div>')
        self.log.setHtml("".join(rows) or "<div style='color:#94A3B8'>还没有消息，先发一句。</div>")

    def add_member(self):
        import agent_group
        if not self.room_id:
            return toast(self, "warning", "先新建一个群。")
        dialog = MemberDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        agent_group.add_member(self._base(), self._owner(), self.room_id,
                               dialog.provider.currentData(), dialog.name.text(),
                               dialog.role.text(), dialog.model.text(),
                               dialog.template.currentData() or "")
        self.open_room(self.room_id)

    def split_agent(self):
        import agent_group
        if not self.room_id:
            return toast(self, "warning", "先新建一个群。")
        provider, ok = QInputDialog.getItem(self, "拆分单个 Agent", "选择要拆分的 Agent：",
                                            ["codex", "claude", "antigravity", "dsh", "api", *AGENT_BACKENDS],
                                            0, False)
        if not ok:
            return
        count, ok = QInputDialog.getInt(self, "拆分成员数", "拆成几个角色（2–6）：", 3, 2, 6)
        if not ok:
            return
        agent_group.split_agent(self._base(), self._owner(), self.room_id, provider, count)
        self.open_room(self.room_id)

    def save_settings(self):
        import agent_group
        if not self.room_id:
            return
        agent_group.update_room(self._base(), self._owner(), self.room_id,
                                mode=self.mode.currentData(),
                                host_id=self.host.currentData() or "",
                                summary=self.summary.isChecked())
        self.open_room(self.room_id)
        toast(self, "success", "群设置已保存。")

    def send(self):
        text = self.input.text().strip()
        if not text or not self.room_id:
            return
        if self.busy:
            self.pending.append((self._owner(), self.room_id, text))
            self.input.clear()
            self.status.setText(f"已排队 {len(self.pending)} 条命令；当前一轮完成后会继续。")
            return
        self.input.clear()
        self._start(text)

    def _start(self, text):
        import agent_group
        if self.loaded_for is not None and self.loaded_for != self._owner():
            self.refresh()
            return
        if not self.room:
            return
        members = list(self.room.get("members") or [])
        if not members:
            return toast(self, "warning", "先给群添加至少一个成员。")
        owner, room_id, base = self._owner(), self.room_id, self._base()
        friend_dir = Path(self.house.friend_dir)
        profile = deepcopy(load_profile(self.house.profile_path))
        agent_group.append_message(base, owner, room_id, text, role="user", name="你")
        self.open_room(room_id)
        self.busy = True
        self.send_btn.setEnabled(True)  # keep usable: new text queues instead of waiting
        self.status.setText("成员正在看群聊并回应…")
        mode = self.room.get("mode", "sequential")
        host_id = self.room.get("host_id") or ""
        host = next((m for m in members if m["id"] == host_id), None)
        do_summary = self.summary.isChecked()

        def job(_report):
            def report(message):
                if self._owner() == owner and self.room_id == room_id:
                    _report(message)
            from main import codex_text
            from persona_template import prompt_block
            from concurrent.futures import ThreadPoolExecutor, as_completed

            current_room = agent_group.load_room(base, owner, room_id)
            if current_room.get('flow_version'):
                answer = agent_group.run_configured(base, owner, current_room, text, friend_dir, profile)
                return [(host or members[0], answer, 'host')]

            try:
                store = FriendStore(friend_dir)
                friend_memory = store.memory()
                store.close()
            except Exception:
                friend_memory = ""
            room = agent_group.load_room(base, owner, room_id)
            memory = agent_group.shared_memory(base, owner, room, friend_memory)

            def member_profile(member):
                from agent_model_selection import apply as selected_profile
                value = selected_profile(profile, member['provider'], member.get('model', ''), member.get('effort', ''))
                if member.get("template"):
                    value["persona_template"] = member["template"]
                return value

            def call(member, prompt):
                value = member_profile(member)
                style = ""
                if member.get("template"):
                    try:
                        style = prompt_block(value, friend_dir, prompt, "chat", max_samples=3).strip()
                    except Exception:
                        style = ""
                current = agent_group.load_room(base, owner, room_id)
                extra = style + ('\n你本轮的分派任务与验收要求：\n' + member['assignment'] if member.get('assignment') else '')
                full = agent_group.prompt_for(current, member, extra=extra, memory=memory)
                reply, _ = codex_text(full, friend_dir, ephemeral=True, profile=value)
                return str(reply or "").strip()

            def host_call(host, prompt):
                value = member_profile(host)
                style = ""
                if host.get("template"):
                    try:
                        style = prompt_block(value, friend_dir, prompt, "chat", max_samples=3).strip()
                    except Exception:
                        style = ""
                reply, _ = codex_text(prompt + ("\n\n" + style if style else ""),
                                      friend_dir, ephemeral=True, profile=value)
                return str(reply or "").strip()

            def persist(member, content, role="agent"):
                agent_group.append_message(base, owner, room_id, content,
                                           role=role, name=member["name"], provider=member["provider"])

            replies = []
            selected = members
            if host:
                from agent_model_selection import options
                catalog = options(profile, [m['provider'] for m in members], members=members)
                report(f"主智能体 {host['name']} 选择成员、模型与思考强度…")
                raw = host_call(host, agent_group.dispatch_prompt(room, host, text, catalog, memory=memory))
                plan = agent_group.parse_dispatch(raw, room, host, catalog)
                selected = plan['members']
                if not selected:
                    persist(host, plan['direct_reply'], 'host')
                    return [(host, plan['direct_reply'], 'host')]
                effort_names = {'off': '关闭', 'none': '关闭', 'low': '低', 'medium': '中', 'high': '高',
                                'xhigh': '超高', 'max': '最高', 'ultra': '极高', '': '默认'}
                content = '【分发】' + plan['summary'] + '\n' + '\n'.join(
                    f"{m['name']} · {m['model'] or '工具默认模型'} · 思考强度：{effort_names.get(m['effort'], m['effort'])}\n{m['assignment']}"
                    for m in selected)
                persist(host, content, 'host')
                replies.append((host, content, 'host'))
            if mode == "parallel":
                report("并行调用本轮选中的成员…")
                with ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
                    futures = {pool.submit(call, member, text): member for member in selected}
                    for future in as_completed(futures):
                        member = futures[future]
                        try:
                            content = future.result()
                        except Exception as exc:
                            content = f"（{member['name']} 调用失败：{exc}）"
                        persist(member, content)
                        replies.append((member, content, "agent"))
            else:
                for member in selected:
                    report(f"{member['name']} 正在回应…")
                    try:
                        content = call(member, text)
                    except Exception as exc:
                        content = f"（{member['name']} 调用失败：{exc}）"
                    persist(member, content)
                    replies.append((member, content, "agent"))
            if host and do_summary:
                report("主持人汇总…")
                room = agent_group.load_room(base, owner, room_id)
                try:
                    summary = host_call(host, agent_group.summary_prompt(room, host, text, memory=memory, routing=selected))
                    content = "【汇总】" + str(summary or "").strip()
                except Exception as exc:
                    content = f"（主持人汇总失败：{exc}）"
                persist(host, content, "host")
                replies.append((host, content, "host"))
            return replies

        def drain():
            if self.pending:
                queued_owner, queued_room, queued_text = self.pending.pop(0)
                if self._owner() == queued_owner and self.room_id == queued_room:
                    self._start(queued_text)

        def done(replies):
            self.busy = False
            if self._owner() == owner and self.room_id == room_id:
                self.status.setText("本轮群聊完成。")
                self.open_room(room_id)
            drain()

        def failed(exc):
            self.busy = False
            if self._owner() == owner and self.room_id == room_id:
                self.status.setText(f"群聊失败：{exc}")
            drain()

        run_async(job, done, failed, pool=LONG_POOL)
