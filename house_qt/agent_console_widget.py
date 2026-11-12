"""Antigravity & DeepSeek Harness style standalone Agent Console Page.

Provides per-friend isolated multi-project tree view (Projects -> Sessions),
dedicated QQ real-time private chat stream, real Codex CLI model options & reasoning efforts,
Codex-style slash commands (/), mentions (@), pure reply extraction (no leaked prompts/templates),
collapsible thinking process cards (CoT), and real-time execution workbench.
"""

from __future__ import annotations
from ui_locale import tr

import os
import re
import re as _re
import html as _html
from datetime import datetime
from pathlib import Path

import time
import threading
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPalette, QTextCursor
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QListWidgetItem, QScrollArea, QSizePolicy, QStyle, QVBoxLayout, QWidget)
from PySide6.QtWidgets import QTreeWidgetItem
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon,
                            IconWidget, IndeterminateProgressRing, ListWidget, PlainTextEdit, PrimaryPushButton,
                            PushButton, RoundMenu, SubtitleLabel,
                            TransparentToolButton, TreeWidget)

import codex_settings
import agent_service
import session_manager
import skill_registry
from store import FriendStore
from .common import LONG_POOL, InputDialog, confirm, run_async, toast


def _linkify(text):
    """Plain message text -> safe HTML with clickable http(s) links."""
    escaped = _html.escape(str(text))
    return _re.sub(r'(https?://[^\s<&]+)',
                   lambda match: f'<a href="{match.group(1)}">{match.group(1)}</a>', escaped)


def menu_item(menu, icon, text, slot):
    """RoundMenu.addAction takes only an Action (an icon, text and slot raise TypeError, so the menu never opened);
    the slot is called without Qt's `checked` flag, so `lambda k=key: ...` keeps its key."""
    action = Action(icon, text, menu)
    action.triggered.connect(lambda *_: slot())
    menu.addAction(action)
    return action


class GeneratingCard(QFrame):
    """Dynamic indicator card showing model is actively reasoning, searching and formulating reply."""
    def __init__(self, agent_name: str = "Agent", parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            GeneratingCard {
                background: #F8FAFC;
                border: 1px dashed #93C5FD;
                border-radius: 10px;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        self.spinner = IndeterminateProgressRing(self)
        self.spinner.setFixedSize(18, 18)
        self.spinner.setStrokeWidth(2.5)

        self.label = CaptionLabel(f"🧠 {agent_name} 正在分析上下文、检索记忆并生成回复…", self)
        self.label.setStyleSheet("color: #0284C7; font-size: 13px; font-weight: 500;")

        layout.addWidget(self.spinner)
        layout.addWidget(self.label)
        layout.addStretch(1)


def format_relative_time(timestamp_str: str) -> str:
    """Format an ISO timestamp to compact human-readable relative time like 1m, 2m, 38m, 1h."""
    if not timestamp_str:
        return ""
    try:
        t = str(timestamp_str).replace('Z', '+00:00')
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is not None:
            now = datetime.now(dt.tzinfo)
        else:
            now = datetime.now()
        diff = (now - dt).total_seconds()
        if diff < 0:
            diff = 0
        if diff < 60:
            return tr("Just now")
        elif diff < 3600:
            return f"{int(diff // 60)}m"
        elif diff < 86400:
            return f"{int(diff // 3600)}h"
        elif diff < 86400 * 30:
            return f"{int(diff // 86400)}d"
        else:
            return dt.strftime('%m-%d')
    except Exception:
        return ""


from reply_sanitizer import extract_clean_reply


class CollapsibleThoughtCard(QFrame):
    """Collapsible card displaying model thinking/reasoning chain (Chain-of-Thought)."""
    def __init__(self, thought_text: str, parent=None):
        super().__init__(parent)
        self.thought_text = thought_text.strip()
        self.expanded = False

        self.setStyleSheet("""
            CollapsibleThoughtCard {
                background: #F1F5F9;
                border: 1px solid #CBD5E1;
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QWidget()
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(6)

        self.icon = IconWidget(FluentIcon.MESSAGE, self)
        self.icon.setFixedSize(14, 14)
        words_count = len(self.thought_text)
        self.title_lbl = CaptionLabel(f"💭 深度思考过程（{words_count} 字符）· 点击展开", self)
        self.title_lbl.setStyleSheet("color: #475569; font-weight: bold;")

        self.toggle_btn = TransparentToolButton(FluentIcon.CHEVRON_RIGHT, self)
        self.toggle_btn.setFixedSize(20, 20)
        self.toggle_btn.clicked.connect(self.toggle)

        h_layout.addWidget(self.icon)
        h_layout.addWidget(self.title_lbl)
        h_layout.addStretch(1)
        h_layout.addWidget(self.toggle_btn)

        self.content_lbl = QLabel(self.thought_text, self)
        self.content_lbl.setWordWrap(True)
        self.content_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_lbl.setStyleSheet("color: #475569; font-family: monospace; font-size: 12px; line-height: 1.4;")
        self.content_lbl.setVisible(False)

        layout.addWidget(header)
        layout.addWidget(self.content_lbl)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(event)

    def toggle(self):
        self.expanded = not self.expanded
        self.content_lbl.setVisible(self.expanded)
        self.toggle_btn.setIcon(FluentIcon.CHEVRON_DOWN if self.expanded else FluentIcon.CHEVRON_RIGHT)
        words_count = len(self.thought_text)
        action_text = tr("Click to collapse") if self.expanded else tr("Click to expand")
        self.title_lbl.setText(f"💭 深度思考过程（{words_count} 字符）· {action_text}")


class ActionStepRow(QFrame):
    """Ultra-clean single-line action step indicator matching Codex execution steps."""
    def __init__(self, action_text: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)

        icon_char = "⚡"
        act_lower = action_text.lower()
        if any(k in act_lower for k in ("image", "图像", "图片", "jpg", "png")):
            icon_char = "⚯"
        elif any(k in act_lower for k in ("edit", "file", "编辑", "文件", "write")):
            icon_char = "✎"
        elif any(k in act_lower for k in ("command", "run", "命令", "运行", "exec", "powershell")):
            icon_char = "🖵"
        elif any(k in act_lower for k in ("search", "web", "搜索", "网页")):
            icon_char = "🌐"
        elif any(k in act_lower for k in ("think", "思考")):
            icon_char = "💭"

        lbl = CaptionLabel(f"{icon_char} {action_text}", self)
        lbl.setStyleSheet("color: #64748B; font-size: 12px; font-weight: 500;")
        layout.addWidget(lbl)
        layout.addStretch(1)


class MessageCard(QWidget):
    """Refined stream message row: Borderless fluent layout matching Codex / Antigravity desktop clients."""
    def __init__(self, msg: dict, agent_name: str = "Agent", parent=None):
        super().__init__(parent)
        self.msg = msg
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        thinking = msg.get('thinking', '')
        timestamp = msg.get('timestamp', '')

        if not thinking and '<think>' in content and '</think>' in content:
            m = re.search(r'<think>([\s\S]*?)</think>', content)
            if m:
                thinking = m.group(1).strip()
                content = content.replace(m.group(0), '').strip()

        is_user = (role == 'user')
        self.setStyleSheet("background: transparent; border: none;")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(36, 6, 36, 6)
        outer_layout.setSpacing(6)

        time_str = ""
        if timestamp:
            try:
                t = str(timestamp).replace('Z', '+00:00')
                dt = datetime.fromisoformat(t)
                time_str = dt.strftime('%H:%M:%S')
            except Exception:
                time_str = str(timestamp)[:8]

        if is_user:
            bubble_container = QWidget(self)
            b_layout = QHBoxLayout(bubble_container)
            b_layout.setContentsMargins(0, 0, 0, 0)
            b_layout.setSpacing(8)

            # 2026-10-26 user: "改成长条这横着的,不要先这样缩着" — a user message is a full-width strip,
            # not a narrow block that wraps a one-sentence prompt into four lines.
            card_frame = QFrame(bubble_container)
            card_frame.setObjectName('userBubble')
            card_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            # Scoped to the frame: a bare QFrame selector also borders every QLabel inside it.
            card_frame.setStyleSheet("""
                QFrame#userBubble {
                    background: #EFF6FF;
                    border: 1px solid #BFDBFE;
                    border-radius: 12px;
                }
            """)
            card_layout = QVBoxLayout(card_frame)
            card_layout.setContentsMargins(14, 10, 14, 10)
            card_layout.setSpacing(4)

            hdr = QHBoxLayout()
            hdr.setContentsMargins(0, 0, 0, 0)
            role_lbl = BodyLabel(tr("You"), card_frame)
            role_lbl.setStyleSheet("font-weight: bold; color: #1D4ED8; font-size: 12px;")
            hdr.addWidget(role_lbl)
            if time_str:
                time_lbl = CaptionLabel(time_str, card_frame)
                time_lbl.setStyleSheet("color: #60A5FA; font-size: 11px;")
                hdr.addWidget(time_lbl)
            hdr.addStretch(1)  # name and time hug the left edge of the strip
            card_layout.addLayout(hdr)

            body_lbl = QLabel(_linkify(content), card_frame)
            body_lbl.setWordWrap(True)
            body_lbl.setTextFormat(Qt.RichText)
            body_lbl.setOpenExternalLinks(True)
            body_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
            body_lbl.setStyleSheet("font-size: 13.5px; line-height: 1.5; color: #1E293B;")
            card_layout.addWidget(body_lbl)

            b_layout.addWidget(card_frame, 1)
            outer_layout.addWidget(bubble_container)

        else:
            stream_container = QWidget(self)
            s_layout = QVBoxLayout(stream_container)
            s_layout.setContentsMargins(0, 0, 0, 0)
            s_layout.setSpacing(6)

            hdr = QHBoxLayout()
            hdr.setContentsMargins(0, 0, 0, 0)
            hdr.setSpacing(8)

            role_lbl = BodyLabel(f"● {agent_name}", stream_container)
            role_lbl.setStyleSheet("font-weight: bold; color: #0F172A; font-size: 13px;")
            hdr.addWidget(role_lbl)

            if time_str:
                time_lbl = CaptionLabel(time_str, stream_container)
                time_lbl.setStyleSheet("color: #94A3B8; font-size: 11px;")
                hdr.addWidget(time_lbl)

            hdr.addStretch(1)

            btn_copy = TransparentToolButton(FluentIcon.COPY, stream_container)
            btn_copy.setToolTip(tr("Copy content"))
            btn_copy.setFixedSize(22, 22)
            btn_copy.clicked.connect(lambda: self.copy_to_clipboard(content))
            hdr.addWidget(btn_copy)
            s_layout.addLayout(hdr)

            if thinking:
                thought_card = CollapsibleThoughtCard(thinking, stream_container)
                s_layout.addWidget(thought_card)

            action_lines = []
            regular_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if (stripped.startswith(('⚯', '✎', '🖵', '🌐', '已查看', '编辑了文件', '运行了命令', '已搜索网页', '正在思考'))
                        or stripped.startswith(('- [x]', '- [ ]', 'Tool Call:'))):
                    action_lines.append(stripped)
                else:
                    regular_lines.append(line)

            if action_lines and len(regular_lines) <= 2:
                for act in action_lines:
                    act_widget = ActionStepRow(act, stream_container)
                    s_layout.addWidget(act_widget)
                if regular_lines and "".join(regular_lines).strip():
                    rem_lbl = QLabel(_linkify("\n".join(regular_lines).strip()), stream_container)
                    rem_lbl.setWordWrap(True)
                    rem_lbl.setTextFormat(Qt.RichText)
                    rem_lbl.setOpenExternalLinks(True)
                    rem_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                    rem_lbl.setStyleSheet("font-size: 13.5px; line-height: 1.6; color: #0F172A;")
                    s_layout.addWidget(rem_lbl)
            else:
                body_lbl = QLabel(_linkify(content), stream_container)
                body_lbl.setWordWrap(True)
                body_lbl.setTextFormat(Qt.RichText)
                body_lbl.setOpenExternalLinks(True)
                body_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
                body_lbl.setStyleSheet("font-size: 13.5px; line-height: 1.6; color: #0F172A; background: transparent;")
                s_layout.addWidget(body_lbl)

            outer_layout.addWidget(stream_container)

    def copy_to_clipboard(self, text: str):
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        if cb:
            cb.setText(text)


class SuggestionRowWidget(QWidget):
    """Custom row widget for slash commands and mentions matching Codex UI."""
    def __init__(self, title: str, desc: str, right_hint: str = "", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(10)

        title_lbl = BodyLabel(tr(title), self)
        title_lbl.setStyleSheet("font-weight: bold; color: #0F172A; font-size: 13px;")

        desc_lbl = CaptionLabel(tr(desc), self)
        desc_lbl.setStyleSheet("color: #64748B; font-size: 12px;")

        layout.addWidget(title_lbl)
        if desc:
            layout.addWidget(desc_lbl)
        layout.addStretch(1)

        if right_hint:
            hint_lbl = CaptionLabel(right_hint, self)
            hint_lbl.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: bold;")
            layout.addWidget(hint_lbl)


class CommandSuggestionWidget(QFrame):
    """Floating-like suggestion panel above input bar for Slash Commands (/) and Mentions (@).
    Matches Codex desktop client UX shown in user reference images."""
    def __init__(self, console_page, parent=None):
        super().__init__(parent)
        self.console = console_page
        self.current_mode = None  # '/' or '@'
        self.items_data = []

        self.setStyleSheet("""
            CommandSuggestionWidget {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 12px;
            }
        """)
        self.setFixedHeight(240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self.title_lbl = CaptionLabel("", self)
        self.title_lbl.setStyleSheet("color: #64748B; font-weight: bold; margin-left: 8px;")

        self.list_widget = ListWidget(self)
        self.list_widget.setStyleSheet("""
            ListWidget {
                border: none;
                background: transparent;
            }
            ListWidget::item {
                border-radius: 6px;
                color: #1E293B;
            }
            ListWidget::item:hover {
                background: #F1F5F9;
            }
            ListWidget::item:selected {
                background: #E2E8F0;
                color: #0F172A;
            }
        """)
        self.list_widget.itemClicked.connect(self.on_item_clicked)

        layout.addWidget(self.title_lbl)
        layout.addWidget(self.list_widget, 1)
        self.hide()

    def show_slash_commands(self, filter_text: str = ""):
        self.current_mode = '/'
        self.title_lbl.setText(tr("快捷命令 (Slash Commands)"))
        provider_text = self.console.provider_combo.currentText() if hasattr(self.console, 'provider_combo') else "Codex CLI"
        effort_text = self.console.get_effort_short_label() if hasattr(self.console, 'get_effort_short_label') else ""
        commands = [
            ("🔄 Engine", f"切换智能体引擎（当前：{provider_text})", ">", "action_switch_provider"),
            ("👥 Team Orchestration", "跨智能体协作编排、交接顺位与额度守护", ">", "action_team_status"),
            ("🧠 Reasoning", "Adjust Reasoning Effort", f"{effort_text} >" if effort_text else ">", "action_effort_toggle"),
            ("➕ New Chat", "Start Empty Chat Session", "", "action_new_chat"),
            ("📦 Model", "Switch Active Model (Linked with CLI Engine)", ">", "action_switch_model"),
            ("⚡ Fast 模式", "Toggle Fast Mode Acceleration", "", "action_fast_toggle"),
            ("📊 运行状态", "查看当前会话 ID、上下文与网络", "", "action_status"),
            ("💡 计划模式", "查看与排班智能体自主任务与规划", "", "action_plan"),
            ("🧠 长期记忆", "检索好友长期事实记忆", "", "insert_mention_memory"),
            ("📚 话题知识", "检索并关联深度话题研究库", "", "insert_mention_topic"),
            ("🎭 设定模板", "切换机前参照的设定模板口吻", "", "insert_mention_template"),
            ("📌 沉淀记忆", "提炼当前会话核心事实至长期记忆", "", "action_extract_memory"),
            ("✏️ 重命名", "重命名当前聊天会话", "", "action_rename"),
            ("🗑️ Clear", "清空当前会话中的所有消息记录", "", "action_clear"),
            ("➕ 安装 Skill…", "选择文件夹，加到当前选中的智能体", "", "action_install_skill"),
        ]
        try:
            for skill in skill_registry.list_skills():
                commands.append((f"/{skill.slug}", f"{skill.description} · {skill.provider}", "技能", f"insert_skill:{skill.slug}"))
        except Exception:
            pass
        self.populate_items(commands, filter_text)

    def show_mentions(self, filter_text: str = ""):
        self.current_mode = '@'
        self.title_lbl.setText(tr("添加与上下文引用 (Mentions)"))
        mentions = [
            ("📎 文件和文件夹", "浏览并引用工作区中的代码或文档", "", "insert_file"),
            ("📁 文件夹…", "引用一个文件夹路径，模型可据此读取", "", "insert_folder"),
            ("🧠 长期记忆", "关联好友长期事实记忆与画像", "", "insert_mention_memory"),
            ("📚 Topic Knowledge Library", "关联深度话题研究知识库", "", "insert_mention_topic"),
            ("🎭 设定模板", "引用设定模板说话口吻与范例", "", "insert_mention_template"),
            ("💡 主动计划", "引用智能体自主任务与规划", "", "insert_mention_plan"),
        ]
        try:
            for plugin in skill_registry.list_plugins():
                mentions.append((f"🔌 {plugin.name}", f"引用 {plugin.provider} 插件", "插件", f"insert_plugin:{plugin.name}"))
        except Exception:
            pass
        self.populate_items(mentions, filter_text)

    def populate_items(self, items: list[tuple[str, str, str, str]], filter_text: str = ""):
        self.items_data = []
        self.list_widget.clear()
        filter_lower = filter_text.strip().lower()

        for title, desc, right_hint, tag in items:
            searchable = f"{title} {desc} {tr(title)} {tr(desc)} {tag}".lower()
            if not filter_lower or filter_lower in searchable:
                w_item = QListWidgetItem(self.list_widget)
                w_item.setData(Qt.UserRole, tag)
                row_widget = SuggestionRowWidget(title, desc, right_hint, parent=self.list_widget)
                w_item.setSizeHint(row_widget.sizeHint())
                self.list_widget.addItem(w_item)
                self.list_widget.setItemWidget(w_item, row_widget)
                self.items_data.append((tr(title), tr(desc), right_hint, tag))

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)
            self.show()
        else:
            self.hide()

    def move_selection(self, step: int):
        cnt = self.list_widget.count()
        if cnt == 0:
            return
        curr = self.list_widget.currentRow()
        nxt = (curr + step) % cnt
        self.list_widget.setCurrentRow(nxt)

    def activate_current(self) -> bool:
        curr_item = self.list_widget.currentItem()
        if not curr_item:
            return False
        self.on_item_clicked(curr_item)
        return True

    def on_item_clicked(self, item: QListWidgetItem):
        tag = item.data(Qt.UserRole)
        self.hide()
        self.console.handle_suggestion_action(self.current_mode, tag)


class AgentConsolePage(QWidget):
    """Standalone full-screen Agent Console page with hierarchical Projects tree view,
    dedicated QQ message stream, real Codex CLI model choices, slash commands and mentions."""
    def __init__(self, house, parent=None):
        super().__init__(parent)
        self.house = house
        self.setObjectName('workbenchPage')
        self.active_ws_id = 'default'
        self.active_session_id = None
        self.is_generating = False
        self.loaded_for = None
        self.trigger_mode = None
        self.trigger_start = 0
        self.trigger_end = 0
        self._profile_mtime = 0.0
        self._profile_sync = QTimer(self)
        self._profile_sync.setInterval(2000)
        self._profile_sync.timeout.connect(self._sync_profile_changes)
        self._profile_sync.start()

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ================= Left: Projects & Sessions Tree Drawer (290px) =================
        self.drawer = QFrame(self)
        self.drawer.setFixedWidth(290)
        self.drawer.setStyleSheet("""
            QFrame#sessionDrawer {
                background: #F8FAFD;
                border: none;
                border-right: 1px solid #DBEAFE;
            }
        """)
        self.drawer.setObjectName("sessionDrawer")
        drawer_layout = QVBoxLayout(self.drawer)
        drawer_layout.setContentsMargins(14, 16, 14, 16)
        drawer_layout.setSpacing(10)

        # 1. Projects Tree Header
        proj_hdr = QHBoxLayout()
        proj_hdr.setContentsMargins(2, 0, 2, 0)
        proj_title = SubtitleLabel(tr("Projects"), self.drawer)
        proj_title.setStyleSheet("font-weight: bold; color: #0F172A; font-size: 15px;")

        self.btn_add_proj = TransparentToolButton(FluentIcon.FOLDER_ADD, self.drawer)
        self.btn_add_proj.setToolTip(tr("Add local project folder"))
        self.btn_add_proj.setFixedSize(28, 28)
        self.btn_add_proj.clicked.connect(self.add_project_dialog)

        self.btn_add_sess = TransparentToolButton(FluentIcon.ADD, self.drawer)
        self.btn_add_sess.setToolTip(tr("New chat under active project"))
        self.btn_add_sess.setFixedSize(28, 28)
        self.btn_add_sess.clicked.connect(self.create_new_session)

        proj_hdr.addWidget(proj_title)
        proj_hdr.addStretch(1)
        proj_hdr.addWidget(self.btn_add_proj)
        proj_hdr.addWidget(self.btn_add_sess)
        drawer_layout.addLayout(proj_hdr)

        # 2. Prominent [+ New Chat] Button matching Codex client
        self.btn_new_chat = PushButton(tr("+ New Chat"), self.drawer)
        self.btn_new_chat.setFixedHeight(34)
        self.btn_new_chat.setStyleSheet("""
            QPushButton {
                background: #EFF6FF;
                border: 1px solid #BFDBFE;
                border-radius: 8px;
                color: #1D4ED8;
                font-weight: 600;
                font-size: 13px;
                padding-left: 14px;
                text-align: left;
            }
            QPushButton:hover {
                background: #DBEAFE;
                border-color: #93C5FD;
                color: #1E40AF;
            }
            QPushButton:pressed {
                background: #BFDBFE;
            }
        """)
        self.btn_new_chat.clicked.connect(self.create_new_session)
        drawer_layout.addWidget(self.btn_new_chat)

        # 3. Hierarchical Projects & Sessions Tree
        self.tree = TreeWidget(self.drawer)
        self.tree.setColumnCount(1)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_context_menu)
        self.tree.itemClicked.connect(self.on_tree_item_clicked)
        self.tree.itemDoubleClicked.connect(self.on_tree_item_clicked)
        self.tree.setStyleSheet("""
            TreeWidget {
                border: none;
                background: transparent;
                font-size: 13px;
                color: #0F172A;
            }
            TreeWidget::item {
                height: 32px;
                min-height: 32px;
                padding: 0 8px;
                margin: 2px 0;
                border-radius: 6px;
                color: #334155;
            }
            TreeWidget::item:hover {
                background: #EFF6FF;
                color: #1D4ED8;
            }
            TreeWidget::item:selected {
                background: #DBEAFE;
                color: #1E40AF;
                font-weight: 600;
            }
            TreeWidget::branch {
                background: transparent;
            }
        """)
        drawer_layout.addWidget(self.tree, 1)

        # Retain tpl_hint compatibility stub
        self.tpl_hint = CaptionLabel("", self)
        self.tpl_hint.hide()

        # ================= Right: Chat & Execution Canvas =================
        self.chat_canvas = QFrame(self)
        self.chat_canvas.setStyleSheet("""
            QFrame#chatCanvas {
                background: #FFFFFF;
                border: none;
            }
        """)
        self.chat_canvas.setObjectName("chatCanvas")
        chat_layout = QVBoxLayout(self.chat_canvas)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        # Header Info Bar: Spaciously Arranged with Top Border & Zero Clutter
        top_bar = QWidget(self.chat_canvas)
        top_bar.setObjectName("chatTopBar")
        top_bar.setStyleSheet("""
            QWidget#chatTopBar {
                background: #FFFFFF;
                border-bottom: 1px solid #E2E8F0;
            }
        """)
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(28, 12, 28, 12)
        tb_layout.setSpacing(10)

        self.sess_title_lbl = SubtitleLabel(tr("Chat"), top_bar)
        self.sess_title_lbl.setStyleSheet("font-weight: bold; font-size: 15px; color: #0F172A;")
        
        self.ws_badge = CaptionLabel(tr("Workspace: Default"), top_bar)
        self.ws_badge.setStyleSheet("color: #1D4ED8; background: #EFF6FF; border: 1px solid #BFDBFE; padding: 4px 10px; border-radius: 6px; font-weight: bold;")

        # Template selector placed cleanly on top bar instead of clogging left drawer
        self.template_badge = CaptionLabel(tr("Template: None"), top_bar)
        self.template_badge.setStyleSheet("color: #7C3AED; background: #EDE9FE; border: 1px solid #DDD6FE; padding: 4px 10px; border-radius: 6px; font-weight: bold;")

        self.tpl_combo = ComboBox(top_bar)
        self.tpl_combo.setMinimumWidth(180)
        self.tpl_combo.setStyleSheet("""
            ComboBox {
                background: #EFF6FF;
                border: 1px solid #BFDBFE;
                border-radius: 6px;
                color: #1D4ED8;
                font-weight: 500;
                font-size: 12px;
                padding: 2px 8px;
            }
        """)
        self.tpl_combo.currentIndexChanged.connect(self.on_template_changed)

        # Generating dynamic indicator (Spinner + Text + Stop button)
        self.working_widget = QWidget(top_bar)
        ww_layout = QHBoxLayout(self.working_widget)
        ww_layout.setContentsMargins(6, 2, 8, 2)
        ww_layout.setSpacing(6)
        self.working_widget.setStyleSheet("""
            QWidget {
                background: #F0F9FF;
                border: 1px solid #BAE6FD;
                border-radius: 6px;
            }
        """)
        self.working_spinner = IndeterminateProgressRing(self.working_widget)
        self.working_spinner.setFixedSize(14, 14)
        self.working_spinner.setStrokeWidth(2)
        self.working_label = CaptionLabel(tr("Reasoning..."), self.working_widget)
        self.working_label.setStyleSheet("color: #0284C7; font-weight: bold; font-size: 11px;")
        self.btn_stop_top = TransparentToolButton(FluentIcon.CANCEL, self.working_widget)
        self.btn_stop_top.setToolTip(tr("Stop generation"))
        self.btn_stop_top.setFixedSize(20, 20)
        self.btn_stop_top.clicked.connect(self.stop_generation)
        ww_layout.addWidget(self.working_spinner)
        ww_layout.addWidget(self.working_label)
        ww_layout.addWidget(self.btn_stop_top)
        self.working_widget.setVisible(False)

        self.btn_refresh_qq = PushButton(tr("🔄 Refresh"), top_bar)
        self.btn_refresh_qq.clicked.connect(self.refresh_current_view)
        self.btn_refresh_qq.setVisible(False)

        # Flush message queue quick action button
        self.btn_flush_queue = PushButton(tr("⚡ Flush Queue"), top_bar)
        self.btn_flush_queue.setToolTip(tr("Drain and flush any stagnant message queues or backlog"))
        self.btn_flush_queue.setFixedHeight(28)
        self.btn_flush_queue.setStyleSheet("""
            QPushButton {
                background: #EFF6FF;
                border: 1px solid #BFDBFE;
                border-radius: 6px;
                color: #1D4ED8;
                font-size: 12px;
                font-weight: 600;
                padding: 0 10px;
            }
            QPushButton:hover {
                background: #DBEAFE;
                border-color: #93C5FD;
            }
        """)
        self.btn_flush_queue.clicked.connect(self.flush_message_queue)

        # Three Action Buttons Group matching Codex Client
        self.btn_pin_sess = TransparentToolButton(FluentIcon.PIN, top_bar)
        self.btn_pin_sess.setToolTip(tr("Save to Long-Term Memory (Pin)"))
        self.btn_pin_sess.setFixedSize(28, 28)
        self.btn_pin_sess.clicked.connect(self.save_conversation_to_memory)

        self.btn_rename_sess = TransparentToolButton(FluentIcon.EDIT, top_bar)
        self.btn_rename_sess.setToolTip(tr("Rename current session"))
        self.btn_rename_sess.setFixedSize(28, 28)
        self.btn_rename_sess.clicked.connect(self.rename_current_session)

        self.btn_more_sess = TransparentToolButton(FluentIcon.MORE, top_bar)
        self.btn_more_sess.setToolTip(tr("More options"))
        self.btn_more_sess.setFixedSize(28, 28)
        self.btn_more_sess.clicked.connect(self.show_more_session_menu)

        tb_layout.addWidget(self.sess_title_lbl)
        tb_layout.addWidget(self.ws_badge)
        tb_layout.addWidget(self.template_badge)
        tb_layout.addWidget(self.tpl_combo)
        tb_layout.addWidget(self.working_widget)
        tb_layout.addStretch(1)
        tb_layout.addWidget(self.btn_refresh_qq)
        tb_layout.addWidget(self.btn_flush_queue)
        tb_layout.addWidget(self.btn_pin_sess)
        tb_layout.addWidget(self.btn_rename_sess)
        tb_layout.addWidget(self.btn_more_sess)
        chat_layout.addWidget(top_bar)

        # Chat Scroll Container
        self.scroll = QScrollArea(self.chat_canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("background: transparent;")

        self.messages_container = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_container)
        self.messages_layout.setContentsMargins(0, 16, 0, 16)
        self.messages_layout.setSpacing(14)
        self.messages_layout.addStretch(1)

        self.scroll.setWidget(self.messages_container)
        chat_layout.addWidget(self.scroll, 1)

        # Floating Command Suggestion Panel (Slash Commands / and Mentions @)
        self.suggestion_panel = CommandSuggestionWidget(self, self.chat_canvas)
        chat_layout.addWidget(self.suggestion_panel)

        # Bottom Input Area Container (Codex Desktop Client Floating Style)
        input_container = QWidget(self.chat_canvas)
        ic_layout = QVBoxLayout(input_container)
        ic_layout.setContentsMargins(36, 0, 36, 18)
        ic_layout.setSpacing(0)

        self.input_card = QFrame(input_container)
        self.input_card.setObjectName("inputCard")
        self.input_card.installEventFilter(self)
        self.input_card.setStyleSheet("""
            QFrame#inputCard {
                background: #FFFFFF;
                border: 1px solid #BFDBFE;
                border-radius: 14px;
            }
        """)
        input_layout = QVBoxLayout(self.input_card)
        input_layout.setContentsMargins(12, 8, 12, 8)
        input_layout.setSpacing(6)

        self.input_edit = PlainTextEdit(self.input_card)
        self.input_edit.setPlaceholderText("输入消息，Enter 发送，Shift+Enter 换行")
        self.input_edit.setFixedHeight(62)
        self.input_edit.setStyleSheet("border: none; background: transparent; font-size: 13.5px; "
                                     "line-height: 1.5; color: #0F172A; padding: 2px 2px 0 2px;")
        # 2026-10-26 user: the placeholder read like typed text and could be mistaken for overlapping input.
        palette = self.input_edit.palette()
        palette.setColor(QPalette.PlaceholderText, QColor('#94A3B8'))
        self.input_edit.setPalette(palette)
        self.input_edit.textChanged.connect(self.on_input_text_or_cursor_changed)
        self.input_edit.cursorPositionChanged.connect(self.on_input_text_or_cursor_changed)
        self.input_edit.installEventFilter(self)

        # Bottom bars: actions on the first row, model choice on the second (see below).
        self.btn_attach = TransparentToolButton(FluentIcon.ADD, self.input_card)
        self.btn_attach.setToolTip(tr("Attach project file reference"))
        self.btn_attach.setFixedSize(26, 26)
        self.btn_attach.clicked.connect(self.choose_and_attach_file)
        self.voice_btn = PushButton('语音', self.input_card)
        self.voice_btn.setIcon(FluentIcon.MICROPHONE.qicon())
        self.voice_btn.setFixedHeight(30)
        self.voice_btn.setToolTip('本地语音输入：转写后确认发送')
        self.voice_btn.clicked.connect(self.voice_input)
        self._voice_preparing = False
        self._voice_capture, self._voice_scope = None, None
        self._voice_cancel = threading.Event()
        self.house.destroyed.connect(lambda:self._voice_cancel.set())
        self.options_btn = PushButton('更多', self.input_card)
        self.options_btn.setFixedHeight(30)
        self.options_btn.clicked.connect(self.input_options)

        # Approval / Access Badge (Clickable to switch permissions)
        self.access_badge = PushButton(tr("Full Access"), self.input_card)
        self.access_badge.setFixedHeight(26)
        self.access_badge.setStyleSheet("""
            PushButton {
                color: #EA580C;
                background: #FFF7ED;
                border: 1px solid #FED7AA;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
                padding: 0 8px;
            }
            PushButton:hover {
                background: #FDBA74;
                color: #9A3412;
            }
        """)
        self.access_badge.setToolTip(tr("Switch CLI permission and approval policy"))
        self.access_badge.clicked.connect(self.on_access_badge_clicked)

        # CLI Provider Selector (Codex / Claude / Antigravity / DSH / API)
        self.provider_combo = ComboBox(self.input_card)
        self.provider_combo.setMinimumWidth(125)
        self.provider_combo.currentIndexChanged.connect(self.on_provider_changed)
        self.input_mode_combo = ComboBox(self.input_card)
        # 2026-10-26 user: "你好" was submitted as a task. Automatic routing is the default; the two
        # explicit entries stay for when the user really wants one or the other.
        self.input_mode_combo.addItem('自动判断', userData='auto')
        self.input_mode_combo.addItem('执行任务', userData='task')
        self.input_mode_combo.addItem('直接对话', userData='chat')
        self.input_mode_combo.setToolTip('默认按内容判断：像任务就提交到任务系统，普通对话只生成回复。'
                                        '也可以固定成「执行任务」或「直接对话」。QQ 消息流发送你输入的原文。')
        self.session_input_modes = {}
        self.input_mode_combo.currentIndexChanged.connect(self.remember_input_mode)

        # Model Selector (Dynamically linked to active CLI)
        self.model_combo = ComboBox(self.input_card)
        self.model_combo.setMinimumWidth(130)
        self.model_combo.hide()
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)

        # Reasoning Effort Selector (Dynamically linked to active CLI)
        self.effort_combo = ComboBox(self.input_card)
        self.effort_combo.setMinimumWidth(110)
        self.effort_combo.hide()
        self.effort_combo.currentIndexChanged.connect(self.on_effort_changed)

        # Unified Model & Effort Pill Button (Matches Codex & Claude Code client UX)
        self.model_btn = PushButton(tr("Model & Effort ⌄"), self.input_card)
        self.model_btn.setObjectName("modelEffortPillBtn")
        self.model_btn.setToolTip(tr("Choose model or adjust reasoning effort"))
        self.model_btn.setStyleSheet("""
            QPushButton#modelEffortPillBtn {
                background: #F1F5F9;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 12px;
                color: #0F172A;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#modelEffortPillBtn:hover {
                background: #E2E8F0;
                border-color: #94A3B8;
            }
            QPushButton#modelEffortPillBtn:pressed {
                background: #CBD5E1;
            }
        """)
        # 2026-10-26 user: 箭头图标压在文字上（"GPT-6.1 Sol 中"被盖住）。胶囊本身就是按钮，
        # 不再叠图标，靠悬停高亮表示可点。
        self.model_btn.clicked.connect(self.popup_model_menu)

        # Fast Mode Checkbox (Codex / API Fast Tier)
        self.fast_chk = CheckBox("快速模式", self.input_card)
        self.fast_chk.setToolTip(tr("Enable Fast mode acceleration"))
        self.fast_chk.stateChanged.connect(self.on_fast_changed)

        self.send_btn = PrimaryPushButton(tr("Send (Enter)"), self.input_card)
        self.send_btn.setStyleSheet("""
            PrimaryPushButton {
                background: #2563EB;
                border: 1px solid #1D4ED8;
                border-radius: 8px;
                font-weight: bold;
                padding: 6px 18px;
                color: #FFFFFF;
            }
            PrimaryPushButton:hover {
                background: #1D4ED8;
            }
        """)
        self.send_btn.clicked.connect(self.send_message)

        # Keep one compact row. Less-frequent controls remain available in the overflow menu.
        row_actions = QHBoxLayout()
        row_actions.setContentsMargins(0, 0, 0, 0)
        row_actions.setSpacing(8)
        row_actions.addWidget(self.btn_attach)
        row_actions.addWidget(self.voice_btn)
        row_actions.addWidget(self.provider_combo)
        row_actions.addWidget(self.model_btn)
        row_actions.addWidget(self.access_badge)
        row_actions.addStretch(1)
        row_actions.addWidget(self.options_btn)
        row_actions.addWidget(self.send_btn)
        self.fast_chk.hide();self.input_mode_combo.hide()

        input_layout.addWidget(self.input_edit)
        input_layout.addLayout(row_actions)
        ic_layout.addWidget(self.input_card)
        chat_layout.addWidget(input_container)

        # Bottom runtime metrics: rounds/steps, throughput, token cache and context usage.
        self.metrics_bar = QWidget(self.chat_canvas)
        self.metrics_bar.setObjectName("chatMetricsBar")
        self.metrics_bar.setStyleSheet("""
            QWidget#chatMetricsBar {
                background: #F8FAFC;
                border-top: 1px solid #E2E8F0;
            }
            QLabel#metricText { color: #64748B; font-size: 11px; padding: 0 4px; }
            QLabel#metricSep { color: #CBD5E1; font-size: 11px; padding: 0 6px; }
        """)
        metrics_layout = QHBoxLayout(self.metrics_bar)
        metrics_layout.setContentsMargins(36, 5, 36, 5)
        metrics_layout.setSpacing(4)
        self.metric_rounds = QLabel("0 轮 0 步", self.metrics_bar)
        self.metric_speed = QLabel("-- tok/s", self.metrics_bar)
        self.metric_tokens = QLabel("0 tok · 缓存命中 --", self.metrics_bar)
        self.metric_context = QLabel("0%", self.metrics_bar)
        for label in (self.metric_rounds, self.metric_speed, self.metric_tokens, self.metric_context):
            label.setObjectName("metricText")
        dot = QLabel("●", self.metrics_bar)
        dot.setObjectName("metricSep")
        metrics_layout.addWidget(dot)
        metrics_layout.addWidget(self.metric_rounds)
        for label in (self.metric_speed, self.metric_tokens, self.metric_context):
            sep = QLabel("|", self.metrics_bar)
            sep.setObjectName("metricSep")
            metrics_layout.addWidget(sep)
            metrics_layout.addWidget(label)
        metrics_layout.addStretch(1)
        chat_layout.addWidget(self.metrics_bar)
        self._metric_usage = None
        self._metric_usage_key = None
        self._metric_usage_at = 0.0
        self._last_tok_s = None
        self._generation_started = None

        main_layout.addWidget(self.drawer)
        main_layout.addWidget(self.chat_canvas, 1)

    # ================= Text Changed & Key Filter for / and @ =================

    @staticmethod
    def detect_trigger(text: str, pos: int) -> tuple[str | None, int, str]:
        """Detect slash command (/, , , ) or mention (@, ) before cursor pos."""
        before_cursor = text[:pos]
        if not before_cursor:
            return None, 0, ''

        # 1. Check mentions (@ or ) anywhere in current line
        last_at = max(before_cursor.rfind('@'), before_cursor.rfind(''))
        if last_at != -1:
            between = before_cursor[last_at + 1:]
            if '\n' not in between and ' ' not in between:
                if last_at == 0 or before_cursor[last_at - 1].isspace() or before_cursor[last_at - 1] in ' , ,. , ':
                    return '@', last_at, between

        # 2. Check slash commands (/, , , ) at start of line
        current_line_start = before_cursor.rfind('\n') + 1
        current_line = before_cursor[current_line_start:]
        stripped_line = current_line.lstrip()
        if stripped_line:
            first_char = stripped_line[0]
            if first_char in ('/', '／', '、'):
                lead_spaces = len(current_line) - len(stripped_line)
                trigger_pos = current_line_start + lead_spaces
                between = before_cursor[trigger_pos + 1:]
                if ' ' not in between:
                    return '/', trigger_pos, between

        return None, 0, ''

    def on_input_text_or_cursor_changed(self):
        """Monitor text and cursor changes to trigger / or @ suggestions like Codex."""
        text = self.input_edit.toPlainText()
        cursor = self.input_edit.textCursor()
        pos = cursor.position()

        mode, trigger_pos, filter_text = self.detect_trigger(text, pos)
        if mode == '/':
            self.trigger_mode = '/'
            self.trigger_start = trigger_pos
            self.trigger_end = pos
            self.suggestion_panel.show_slash_commands(filter_text)
        elif mode == '@':
            self.trigger_mode = '@'
            self.trigger_start = trigger_pos
            self.trigger_end = pos
            self.suggestion_panel.show_mentions(filter_text)
        else:
            self.trigger_mode = None
            self.suggestion_panel.hide()

    def eventFilter(self, obj, event):
        if obj is getattr(self,'input_card',None) and event.type()==event.Type.Resize:
            self.fit_input_controls()
        if obj is getattr(self,'input_edit',None):
            if event.type() == event.Type.InputMethod:
                self._input_composing = bool(event.preeditString())
            elif event.type() == event.Type.FocusOut:
                self._input_composing = False
        if (obj is getattr(self,'input_edit',None) and event.type() == event.Type.KeyPress
                and not getattr(self, '_input_composing', False)):
            if self.suggestion_panel.isVisible():
                if event.key() == Qt.Key_Down:
                    self.suggestion_panel.move_selection(1)
                    return True
                elif event.key() == Qt.Key_Up:
                    self.suggestion_panel.move_selection(-1)
                    return True
                elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                    if self.suggestion_panel.activate_current():
                        return True
                elif event.key() == Qt.Key_Escape:
                    self.suggestion_panel.hide()
                    return True
            else:
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    if not (event.modifiers() & Qt.ShiftModifier):
                        self.send_message()
                        return True
        return super().eventFilter(obj, event)

    def replace_trigger_range(self, replacement: str):
        """Replace the typed trigger token with replacement string cleanly preserving other text."""
        cursor = self.input_edit.textCursor()
        start = getattr(self, 'trigger_start', None)
        end = getattr(self, 'trigger_end', None)
        if start is not None and end is not None and start <= end:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.insertText(replacement)
            self.input_edit.setTextCursor(cursor)
        else:
            if replacement:
                cursor.insertText(replacement)
                self.input_edit.setTextCursor(cursor)
        self.input_edit.setFocus()

    def handle_suggestion_action(self, mode: str, tag: str):
        """Handle selection from suggestion panel."""
        # 1. Mention insertions
        if tag.startswith("insert_"):
            if tag == "insert_file":
                self.replace_trigger_range("")
                self.choose_and_attach_file()
            elif tag == "insert_folder":
                self.replace_trigger_range("")
                self.choose_folder_mention()
            elif tag.startswith("insert_skill:"):
                self.replace_trigger_range(f"/{tag.split(':', 1)[1]} ")
            elif tag.startswith("insert_plugin:"):
                self.replace_trigger_range(f"@plugin:{tag.split(':', 1)[1]} ")
            elif tag == "insert_mention_memory":
                self.replace_trigger_range("@Long-Term Memory ")
            elif tag == "insert_mention_topic":
                self.replace_trigger_range("@Topic Knowledge Library ")
            elif tag == "insert_mention_template":
                self.replace_trigger_range("@设定模板 ")
            elif tag == "insert_mention_plan":
                self.replace_trigger_range("@主动计划 ")
            return

        # 2. Slash command actions
        self.replace_trigger_range("")

        if tag == "action_clear":
            self.clear_current_session()
        elif tag == "action_install_skill":
            self.install_skill_dialog()
        elif tag == "action_new_chat":
            self.create_new_session()
        elif tag == "action_switch_provider":
            self.provider_combo.showPopup()
        elif tag == "action_switch_model":
            self.model_combo.showPopup()
        elif tag == "action_effort_toggle":
            cnt = self.effort_combo.count()
            if cnt > 0:
                nxt = (self.effort_combo.currentIndex() + 1) % cnt
                self.effort_combo.setCurrentIndex(nxt)
        elif tag == "action_fast_toggle":
            if self.fast_chk.isVisible():
                self.fast_chk.setChecked(not self.fast_chk.isChecked())
            else:
                toast(self.house, "info", "当前智能体渠道不支持快速模式。")
        elif tag == "action_status":
            self.show_status_dialog()
        elif tag == "action_team_status":
            self.show_team_status_dialog()
        elif tag == "action_plan":
            self.house.show_friend(self.house.current_qq, 'proactivePage')
        elif tag == "action_extract_memory":
            self.save_conversation_to_memory()
        elif tag == "action_rename":
            if self.active_session_id and self.active_session_id != '__qq_stream__':
                self.rename_session_dialog(self.active_ws_id, self.active_session_id, self.sess_title_lbl.text())

    def show_team_status_dialog(self):
        qq = self.house.current_qq
        from house_status import load_profile
        profile = load_profile(self.house.profile_path)
        api = profile.get('model_api') or {}
        multi = api.get('multi_agent') or {}
        enabled = bool(multi.get('enabled') or api.get('multi_agent_enabled'))
        active = multi.get('active_providers') or api.get('active_providers') or [api.get('provider', 'codex')]
        order = multi.get('handover_order') or api.get('handover_order') or active
        master = multi.get('master') or api.get('master_provider') or api.get('provider', 'codex')

        PROVIDER_TITLES = {
            'codex': ('Codex CLI', '💻 代码实现与终端'),
            'claude': ('Claude Code', '🧠 架构与安全审查 [英文协议]'),
            'antigravity': ('Antigravity CLI', '🌐 深度推理与全网检索'),
            'dsh': ('DeepSeek Harness', '⚡ 算法与代码推导'),
            'custom': ('自定义 API', '🔧 领域与微调接口'),
        }

        mode_str = "👥 跨智能体协同协作（已启用）" if enabled else "⚪ 单智能体独立模式"
        master_title = PROVIDER_TITLES.get(master, (master,))[0]
        order_str = " ➔ ".join([f"[{i + 1}] {PROVIDER_TITLES.get(p, (p,))[0]}" for i, p in enumerate(order)])

        info = (
            f"• 协作状态：{mode_str}\n"
            f"• 👑 任务分配主智能体：{master_title}\n"
            f"• 🔄 编排交接顺位：{order_str}\n"
            f"• ⚠️ 额度保护机制：额度用完或剩余 ≤ 5% 时自动顺序交接 (开启)\n"
            f"• 🌐 协议对接规范：交接至 Claude Code 时采用高精度标准英文协议\n"
            f"• 🎯 能力自省委托：干不了的活（如联网检索/执行终端命令）自动寻找具备能力的智能体接替\n\n"
            f"提示：可在左侧导航「模型设置」页自由多选智能体并上下调整编排顺位。"
        )
        toast(self.house, "info", info, "多智能体协同与交接编排")

    def show_status_dialog(self):
        """Show runtime status dialog inspired by Codex."""
        qq = self.house.current_qq
        provider_name = self.provider_combo.currentText()
        model_name = self.model_combo.currentText()
        effort_name = self.effort_combo.currentText()
        approval_name = self.access_badge.text()
        fast_state = ("开启" if self.fast_chk.isChecked() else "关闭") if self.fast_chk.isVisible() else "不适用"
        ws_info = next((w for w in session_manager.list_workspaces(qq) if w['id'] == self.active_ws_id), None)
        ws_name = ws_info['title'] if ws_info else "默认主空间"
        sess_title = self.sess_title_lbl.text()
        from house_status import ROOT
        from store import FriendStore
        store = FriendStore(ROOT / 'friends' / qq)
        try:
            from memory_management import entries
            from topic_research import items
            mem_count = sum(bool(item['active']) for item in entries(store))
            top_count = len(items(ROOT / 'friends' / qq))
        finally:
            store.close()

        status_text = (
            f"• 好友 QQ：{qq}\n"
            f"•  CLI Engine: {provider_name}\n"
            f"• Model: {model_name}\n"
            f"• Reasoning Effort: {effort_name}\n"
            f"• 审批权限：{approval_name}\n"
            f"• 快速模式：{fast_state}\n"
            f"• 当前工作区：{ws_name}\n"
            f"• 当前会话：{sess_title} ({self.active_session_id})\n"
            f"• 长期事实：{mem_count} 条\n"
            f"• 话题知识：{top_count} 个"
        )
        toast(self.house, "info", status_text, "智能体工作台运行状态")

    def install_skill_dialog(self):
        from house_status import load_profile
        folder = QFileDialog.getExistingDirectory(self, "选择包含 SKILL.md 的文件夹", "D:\\")
        if not folder:
            return
        profile = load_profile(self.house.profile_path)
        api = profile.get("model_api") or {}
        multi = api.get("multi_agent") or {}
        providers = list(multi.get("active_providers") or [])
        if not providers:
            providers = [api.get("provider") or "codex"]
        try:
            targets = skill_registry.install(Path(folder), providers)
        except Exception as error:
            toast(self.house, "error", str(error))
            return
        toast(self.house, "success", "已安装到：" + "、".join(str(path) for path in targets))

    def choose_folder_mention(self):
        folder = QFileDialog.getExistingDirectory(self, "选择要引用的文件夹", "D:\\")
        if not folder:
            return
        self.replace_trigger_range(f'@folder:"{folder}" ')
        toast(self.house, "success", f"已引用文件夹：{folder}")

    def choose_and_attach_file(self):
        qq = self.house.current_qq
        if self.active_session_id == '__qq_stream__':
            return self.choose_qq_attachment(qq)
        workspaces = session_manager.list_workspaces(qq)
        ws_info = next((w for w in workspaces if w['id'] == self.active_ws_id), None)
        base_dir = ws_info['path'] if ws_info and os.path.isdir(ws_info.get('path', '')) else "D:\\"
        file_path, _ = QFileDialog.getOpenFileName(self, "选择要引用的项目文件", base_dir, "所有文件 (*.*)")
        if file_path:
            p = Path(file_path)
            ref_str = f"@{p.name} "
            cursor = self.input_edit.textCursor()
            cursor.insertText(ref_str)
            self.input_edit.setTextCursor(cursor)
            toast(self.house, "success", f"已引用项目文件: {p.name}")

    def choose_qq_attachment(self, qq):
        if not qq or qq not in self.house.records:
            return toast(self.house, 'warning', '请先选择已绑定的好友。')
        path, _ = QFileDialog.getOpenFileName(self, '发送 QQ 文件、图片或视频', '', '所有文件 (*.*)')
        if not path:
            return
        self.btn_attach.setEnabled(False)

        def done(receipt):
            self.btn_attach.setEnabled(True)
            toast(self.house, 'success' if receipt.get('local_saved') else 'warning',
                  receipt.get('warning') or f'附件已发送：{receipt["name"]}')
            if self.house.current_qq == qq and self.active_session_id == '__qq_stream__':
                self.load_session_ui('__qq_stream__')

        def failed(error):
            self.btn_attach.setEnabled(True)
            toast(self.house, 'error', str(error), '发送失败或结果未确认')
        import agent_service
        run_async(lambda _: agent_service.send_friend_attachment(qq, path), done, failed, pool=LONG_POOL)

    # ================= Lifecycle & Loading =================

    def load(self):
        self.reload()

    def shown(self):
        self.reload()

    def on_status(self):
        """Handle background bridge status updates gracefully."""
        pass

    def get_agent_name(self) -> str:
        """Display name for the assistant, never the persona prompt itself."""
        try:
            from house_status import load_profile
            profile = load_profile(self.house.profile_path)
            explicit = str(profile.get('agent_name') or '').strip()
            if explicit:
                return explicit[:24]
            import persona_template
            active_tpl = persona_template.active(profile)
            if active_tpl:
                source = active_tpl.get('source') if isinstance(active_tpl.get('source'), dict) else {}
                name = str(source.get('speaker') or active_tpl.get('name') or '').strip()
                if name:
                    return name[:24]
            provider = str((profile.get('model_api') or {}).get('provider') or '').strip()
            from agent_tool_adapters import BUILTINS
            labels = {
                'webchat_chatgpt': 'ChatGPT 网页',
                'webchat_gemini': 'Gemini 网页',
                'webchat_deepseek': 'DeepSeek 网页',
                'custom': '自定义 API',
                'multi_agent': '多智能体协同',
            }
            if provider in BUILTINS:
                return BUILTINS[provider].name
            return labels.get(provider, 'Dot Agent')
        except Exception:
            return 'Dot Agent'

    def refresh_current_view(self):
        """Refresh current session UI (re-fetch QQ stream or reload messages)."""
        if self.active_session_id:
            self.load_session_ui(self.active_session_id)

    def _sync_profile_changes(self):
        """Pick up model/persona edits made from the phone without restarting."""
        qq = self.house.current_qq
        self.fit_input_controls()
        if not qq or self.loaded_for != qq:
            return
        if self.isVisible() and self.active_session_id == '__qq_stream__' and not getattr(self, '_qq_sync_pending', False):
            from .common import run_async, LONG_POOL
            self._qq_sync_pending = True
            def done(data):
                from shiboken6 import isValid
                if not isValid(self):
                    return
                self._qq_sync_pending = False
                if self.house.current_qq != qq or self.active_session_id != '__qq_stream__':
                    return
                signature = [(m['id'], m['content']) for m in data['messages']]
                if signature != getattr(self, '_qq_stream_signature', None):
                    self._qq_stream_signature = signature
                    self.load_session_ui('__qq_stream__')
            def failed(_error):
                self._qq_sync_pending = False
            run_async(lambda _: agent_service.qq_stream(qq), done, failed, pool=LONG_POOL)
        try:
            mtime = self.house.profile_path.stat().st_mtime
        except OSError:
            return
        if self._profile_mtime and mtime != self._profile_mtime:
            self._profile_mtime = mtime
            self.reload()
        else:
            self._profile_mtime = mtime

    def fit_input_controls(self):
        if not hasattr(self,'send_btn'):return
        width=self.input_card.width()
        self.provider_combo.setVisible(width>=720)
        self.access_badge.setVisible(width>=790)
        self.fast_chk.hide();self.input_mode_combo.hide()

    def input_options(self):
        menu=RoundMenu(parent=self)
        for index in range(self.input_mode_combo.count()):
            action=Action(self.input_mode_combo.itemText(index),menu)
            action.setCheckable(True);action.setChecked(index==self.input_mode_combo.currentIndex())
            action.triggered.connect(lambda _=False,index=index:self.input_mode_combo.setCurrentIndex(index))
            action.setEnabled(self.active_session_id!='__qq_stream__');menu.addAction(action)
        fast=Action('快速模式',menu);fast.setCheckable(True);fast.setChecked(self.fast_chk.isChecked())
        fast.setEnabled(getattr(self,'_fast_supported',False));fast.triggered.connect(lambda checked:self.fast_chk.setChecked(checked));menu.addAction(fast)
        menu.addSeparator()
        for index in range(self.provider_combo.count()):
            action=Action('智能体：'+self.provider_combo.itemText(index),menu)
            action.triggered.connect(lambda _=False,index=index:self.provider_combo.setCurrentIndex(index));menu.addAction(action)
        menu.exec(self.options_btn.mapToGlobal(self.options_btn.rect().bottomLeft()))

    def prepare_voice(self):
        if self._voice_preparing:return
        from house_settings import root
        import voice_input
        self._voice_preparing=True;self.voice_btn.setToolTip('正在准备小型语音识别模型，界面可继续使用')
        def done(result):
            from shiboken6 import isValid
            if not isValid(self):return
            if not result.get('ready'):return failed(RuntimeError('模型校验未通过'))
            self._voice_preparing=False
            self.voice_btn.setToolTip('语音输入已就绪；在本机识别，不自动发送')
        def failed(error):
            from shiboken6 import isValid
            if not isValid(self):return
            self._voice_preparing=False
            self.voice_btn.setToolTip('语音模型准备未完成，点击语音重试：'+str(error)[:180])
        def progress(value):
            from shiboken6 import isValid
            if isValid(self):self.voice_btn.setToolTip('正在准备语音：'+str(value[1]))
        run_async(lambda report:voice_input.ensure_default(root(),progress=lambda percent,text:report((percent,text)),
            cancel_event=self._voice_cancel),done,failed,progress,pool=LONG_POOL)

    def voice_input(self):
        from house_settings import root
        from .voice_capture import VoiceRecorder
        import voice_input
        if self._voice_capture and self._voice_capture.state == 'recording':
            self._voice_capture.finish_recording()
            return
        if self._voice_capture and self._voice_capture.state == 'permission':
            self._voice_capture.cancel()
            return
        if self._voice_capture and self._voice_capture.state == 'transcribing':
            return
        if not self.house.current_qq:
            return toast(self.house,'info','请先添加并选择好友，再使用语音输入。')
        if self._voice_preparing:
            return toast(self.house,'info','小型语音模型正在准备，完成后即可录音。')
        from house_status import load_profile
        tier=voice_input.settings(load_profile(self.house.profile_path))['tier']
        if not voice_input.local_ready({'tier':tier},root()):
            if tier == 'small':self.prepare_voice()
            else:return toast(self.house,'info','所选语音模型尚未下载，请在语音识别设置中一键下载。')
            return toast(self.house,'info','正在准备小型语音模型，已有文件会复用。')
        if not self._voice_capture:
            self._voice_capture = VoiceRecorder(self, root(), tier)
            self._voice_capture.stateChanged.connect(self.update_voice_button)
            self._voice_capture.textReady.connect(self.insert_voice_text)
            from PySide6.QtWidgets import QApplication
            QApplication.instance().aboutToQuit.connect(self._voice_capture.cancel)
        self._voice_capture.root, self._voice_capture.tier = root(), tier
        self._voice_scope = (self.house.current_qq,self.active_ws_id,self.active_session_id)
        self._voice_capture.start()

    def update_voice_button(self, state, message):
        recording = state == 'recording'
        self.voice_btn.setText('停止录音' if recording else '转写中' if state == 'transcribing' else '语音')
        self.voice_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop) if recording else FluentIcon.MICROPHONE.qicon())
        self.voice_btn.setEnabled(state != 'transcribing')
        self.voice_btn.setToolTip(message or '点击开始录音，再点一次停止并转写')
        self.fit_input_controls()
        if state == 'error':
            toast(self.house, 'warning', message, '语音输入')

    def insert_voice_text(self, text):
        if self._voice_scope == (self.house.current_qq,self.active_ws_id,self.active_session_id):
            self.input_edit.insertPlainText(text)

    def hideEvent(self, event):
        if getattr(self, '_voice_capture', None):
            self._voice_capture.cancel()
        super().hideEvent(event)

    def reload(self):
        """Reload Projects tree, persona templates, and real Codex model controls."""
        qq = self.house.current_qq
        if self._voice_capture and self._voice_scope != (qq,self.active_ws_id,self.active_session_id):
            self._voice_capture.cancel()
        if not qq:
            return

        from house_status import load_profile
        try:
            profile = load_profile(self.house.profile_path)
        except Exception:
            profile = {}

        # 1. Update Models & Efforts from real Codex CLI options
        self.reload_models_ui(profile)

        # 2. Reload Projects Tree
        self.reload_projects_tree(qq)

        # 3. Reload Persona Template selector
        self.reload_templates_ui(profile)
        self.loaded_for = qq

    def get_current_provider(self, profile: dict) -> str:
        api = (profile or {}).get('model_api') or {}
        multi = api.get('multi_agent') or {}
        if multi.get('enabled') or api.get('multi_agent_enabled'):
            return 'multi_agent'
        p = api.get('provider', 'codex')
        if p in ('dsh', 'deepseek_harness'):
            return 'dsh'
        if p in ('custom', 'deepseek', 'api'):
            return 'custom'
        from agent_tool_adapters import AGENT_BACKENDS
        if p in ('claude', 'antigravity', *AGENT_BACKENDS):
            return p
        if isinstance(p, str) and p.startswith('webchat_'):
            # 2026-10-26: 网页对话来源也要能被识别，否则一选就被下面的 codex 兜底冲掉。
            return p
        return 'codex'

    def get_effective_provider_and_master(self, profile: dict) -> tuple[str, str]:
        """Return (active_provider, master_provider). If active_provider != 'multi_agent', master == active."""
        api = (profile or {}).get('model_api') or {}
        multi = api.get('multi_agent') or {}
        is_multi = bool(multi.get('enabled') or api.get('multi_agent_enabled'))
        if is_multi:
            master = multi.get('master') or api.get('master_provider') or 'codex'
            return 'multi_agent', master
        p = api.get('provider', 'codex')
        if p in ('dsh', 'deepseek_harness'):
            return 'dsh', 'dsh'
        if p in ('custom', 'deepseek', 'api'):
            return 'custom', 'custom'
        from agent_tool_adapters import AGENT_BACKENDS
        if p in ('claude', 'antigravity', *AGENT_BACKENDS):
            return p, p
        if isinstance(p, str) and p.startswith('webchat_'):
            return p, p
        return 'codex', 'codex'

    def update_access_badge(self, profile: dict):
        provider, actual_provider = self.get_effective_provider_and_master(profile)
        tools = profile.get('computer_tools') or {}
        api = profile.get('model_api') or {}
        approvals = api.get('provider_approvals') if isinstance(api.get('provider_approvals'), dict) else {}
        approval = approvals.get(actual_provider) or tools.get('approval_mode', '')

        badge_text = "完全访问"
        from agent_tool_adapters import AGENT_BACKENDS
        if actual_provider in AGENT_BACKENDS:
            allowed = (tools.get('enabled') and tools.get('commands') and tools.get('full_computer_access')
                       and not tools.get('read_only') and tools.get('mode') != 'read_only')
            badge_text = '外部工具已授权' if allowed else '外部工具尚未授权'
        if actual_provider == 'codex':
            mapping = {'full-access': '完全访问', 'on-request': '按需批准', 'request-review': '请求批准'}
            badge_text = mapping.get(approval, '完全访问')
        elif actual_provider == 'claude':
            mapping = {'bypass': 'Bypass 跳过', 'auto': 'Auto 自动', 'accept-edits': 'Accept 接受', 'plan': 'Plan 方案先审', 'manual': 'Manual 手动'}
            badge_text = mapping.get(approval, 'Bypass 跳过')
        elif actual_provider == 'antigravity':
            mapping = {'turbo': 'Turbo 无限制', 'default': 'Default 默认', 'request-review': 'Review 严格'}
            badge_text = mapping.get(approval, 'Turbo 无限制')
        elif actual_provider == 'dsh':
            mapping = {'danger-full-access': '全自动执行', 'workspace-write': '工作区写', 'read-only': '严格只读'}
            badge_text = mapping.get(approval, '全自动执行')
        elif actual_provider == 'custom':
            mapping = {'never': '无需审批', 'on-request': '按需确认', 'request-review': '严格审批'}
            badge_text = mapping.get(approval, '无需审批')

        access = ('仅聊天' if not tools.get('enabled') else '只读查看' if tools.get('mode') == 'read_only' or tools.get('read_only') else
                  '完全访问' if tools.get('full_computer_access') and tools.get('commands') else
                  '全盘读写' if tools.get('full_computer_access') else '工作区读写')
        self.access_badge.setText(access)
        self.access_badge.setToolTip('电脑权限：'+access+'；智能体审批：'+badge_text)

    def on_access_badge_clicked(self):
        qq = self.house.current_qq
        if not qq:
            toast(self.house, "info", "先在左上角选择一位好友，再切换审批权限。")
            return
        now = time.time()
        if now - getattr(self, '_access_menu_closed_at', 0) < 0.25:
            return

        from house_status import load_profile
        from .pages_friend import APPROVAL_MODES
        profile = load_profile(self.house.profile_path)
        _, actual_provider = self.get_effective_provider_and_master(profile)
        options = APPROVAL_MODES.get(actual_provider, APPROVAL_MODES['codex'])

        menu = RoundMenu(parent=self)
        api = profile.get('model_api') or {}
        approvals = api.get('provider_approvals') if isinstance(api.get('provider_approvals'), dict) else {}
        current_approval = approvals.get(actual_provider) or (
            (profile.get('computer_tools') or {}).get('approval_mode') or options[0][1])

        for label, key in options:
            menu_item(menu, FluentIcon.ACCEPT.qicon() if current_approval == key else FluentIcon.CHECKBOX.qicon(),
                                    label, lambda k=key: self.set_approval_mode(k))

        menu.aboutToHide.connect(lambda: setattr(self, '_access_menu_closed_at', time.time()))
        menu.exec(self.access_badge.mapToGlobal(self.access_badge.rect().bottomLeft()))

    def rename_current_session(self):
        sid = self.active_session_id
        ws_id = self.active_ws_id
        if not sid or sid == '__qq_stream__':
            toast(self.house, "info", "QQ 消息流通道不可重命名。")
            return
        curr_title = self.sess_title_lbl.text()
        self.rename_session_dialog(ws_id, sid, curr_title)

    def show_more_session_menu(self):
        now = time.time()
        if now - getattr(self, '_more_menu_closed_at', 0) < 0.25:
            return
        menu = RoundMenu(parent=self)
        sid = self.active_session_id
        ws_id = self.active_ws_id
        if sid == '__qq_stream__':
            menu_item(menu, FluentIcon.SYNC.qicon(), "🔄 Refresh QQ ", self.refresh_current_view)
        else:
            menu_item(menu, FluentIcon.EDIT.qicon(), "✏️ 重命名对话", self.rename_current_session)
            menu_item(menu, FluentIcon.PIN.qicon(), "📌 保存到长期记忆", self.save_conversation_to_memory)
            menu_item(menu, FluentIcon.FOLDER.qicon(), "📁 打开所在项目文件夹", lambda: self.open_in_explorer(ws_id))
            menu.addSeparator()
            menu_item(menu, FluentIcon.DELETE.qicon(), "🧹 清空本会话记录", lambda: self.clear_specific_session(ws_id, sid))
            menu_item(menu, FluentIcon.CANCEL.qicon(), "🗑️ 删除此对话", lambda: self.delete_specific_session(ws_id, sid))

        menu.aboutToHide.connect(lambda: setattr(self, '_more_menu_closed_at', time.time()))
        menu.exec(self.btn_more_sess.mapToGlobal(self.btn_more_sess.rect().bottomLeft()))

    def stop_generation(self):
        if not self.is_generating:
            return
        self.is_generating = False
        self.send_btn.setEnabled(True)
        self.send_btn.setText('发送')
        self.send_btn.setStyleSheet("")
        if hasattr(self, 'working_widget'):
            self.working_widget.setVisible(False)
        if getattr(self, 'generating_card', None):
            self.generating_card.deleteLater()
            self.generating_card = None
        toast(self.house, "info", "已停止当前生成。")

    def set_approval_mode(self, key: str):
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            _, actual_provider = self.get_effective_provider_and_master(profile)
            api = profile.setdefault('model_api', {})
            approvals = api.setdefault('provider_approvals', {})
            approvals[actual_provider] = key
            write_profile(self.house.profile_path, profile)
            self.update_access_badge(profile)
            toast(self.house, "success", f"「{actual_provider}」审批已切换为：{self.access_badge.text()}")
        except Exception as exc:
            toast(self.house, "error", str(exc), "修改审批权限失败")

    def reload_models_ui(self, profile: dict):
        """Populate provider, model, and effort selectors from currently active CLI."""
        api = (profile or {}).get('model_api') or {}
        current_provider = self.get_current_provider(profile)

        # 1. Update Provider Selector
        providers = [
            ("Codex CLI", "codex"),
            ("Claude Code", "claude"),
            ("Antigravity CLI", "antigravity"),
            ("DeepSeek Harness", "dsh"),
            ("自定义 API", "custom"),
            # 2026-10-26 user: 有些活用网页端更省——本地不跑模型、不占额度也不进缓存。
            ("🌐 网页对话 · ChatGPT", "webchat_chatgpt"),
            ("🌐 网页对话 · Gemini", "webchat_gemini"),
            ("🌐 网页对话 · DeepSeek", "webchat_deepseek"),
            ("👥 跨智能体协同网络", "multi_agent"),
        ]
        from agent_tool_adapters import AGENT_BACKENDS, BUILTINS
        providers[-1:-1] = [(BUILTINS[key].name, key) for key in AGENT_BACKENDS]
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for title, key in providers:
            self.provider_combo.addItem(title, userData=key)

        for i in range(self.provider_combo.count()):
            if self.provider_combo.itemData(i) == current_provider:
                self.provider_combo.setCurrentIndex(i)
                break
        self.provider_combo.blockSignals(False)

        # 2. Populate Models based on active Provider
        self.populate_provider_models(current_provider, profile)

        # 3. Update Fast Mode
        self._fast_supported = current_provider in ('codex','custom')
        if current_provider in ('codex', 'custom'):
            self.fast_chk.setVisible(True)
            curr_fast = api.get('codex_fast', False)
            self.fast_chk.blockSignals(True)
            self.fast_chk.setChecked(bool(curr_fast))
            self.fast_chk.blockSignals(False)
        else:
            self.fast_chk.setVisible(False)

        # 4. Update Access / Approval Badge
        self.update_access_badge(profile)
        self.fit_input_controls()

    def get_provider_model_structure(self, actual_provider: str):
        """Return (header_title, main_models, overflow_models) matching Codex and Claude Code client UX."""
        if isinstance(actual_provider, str) and actual_provider.startswith('webchat_'):
            # 2026-10-26 user: 网页端也能在控制台选模型；这里给的是该站点提供的档位。
            import web_chat

            source = actual_provider[len('webchat_'):]
            items = [(name, name, '') for name in web_chat.models_for(source)]
            return '网页端模型', items, []
        from agent_tool_adapters import AGENT_BACKENDS
        if actual_provider in AGENT_BACKENDS:
            return '工具模型配置', [('沿用工具配置', '沿用工具配置', '')], []
        if actual_provider in ('claude', 'codex'):
            if actual_provider == 'claude':
                import claude_backend
                models = claude_backend.fetch_available_models()
            else:
                models = list(codex_settings.model_options())
            items = [(model, self.get_short_model_name(model), str(i + 1) if i < 3 else '')
                     for i, model in enumerate(models)]
            return '本轮使用的模型', items[:4], [(m, label) for m, label, _ in items[4:]]
        if actual_provider == 'dsh':
            header_title = "DeepSeek 账号"
            main_models = [
                ('DeepSeek-V41-Flash', 'DeepSeek-V41-Flash', ''),
                ('DeepSeek-V4-Pro', 'DeepSeek-V4-Pro', ''),
            ]
            overflow_models = []
            return header_title, main_models, overflow_models

        elif actual_provider == 'antigravity':
            header_title = "本轮使用的模型"
            main_models = [
                ('gemini-3.8-flash-high', 'Gemini 3.8 Flash High', '1'),
                ('gemini-3.8-flash-medium', 'Gemini 3.8 Flash Medium', '2'),
                ('gemini-3.8-flash-low', 'Gemini 3.8 Flash Low', '3'),
            ]
            overflow_models = [
                ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
                ('claude-opus-4-6-thinking', 'Claude Opus 4.6 Thinking'),
                ('gpt-oss-120b-medium', 'GPT-OSS 120B Medium'),
            ]
            return header_title, main_models, overflow_models

        else:  # custom
            header_title = "API 服务账号"
            main_models = [
                ('deepseek-chat', 'DeepSeek V3 (Chat)', ''),
                ('deepseek-reasoner', 'DeepSeek R1 (Reasoner)', ''),
                ('gpt-4o', 'GPT-4o', ''),
                ('claude-3-5-sonnet', 'Claude 3.5 Sonnet', ''),
            ]
            overflow_models = []
            return header_title, main_models, overflow_models

    def get_model_efforts(self, actual_provider: str, model_id: str) -> list[tuple[str, str]]:
        """Return the precise reasoning efforts supported by the specific model according to official specs."""
        if isinstance(actual_provider, str) and actual_provider.startswith('webchat_'):
            # 2026-10-26 user: 网页端的思考强度（ChatGPT 的高/中/低、Gemini 档位、DeepSeek 深度思考）。
            import web_chat

            source = actual_provider[len('webchat_'):]
            return [(name, name) for name in web_chat.efforts_for(source)]
        from agent_tool_adapters import AGENT_BACKENDS
        if actual_provider in AGENT_BACKENDS:
            return [('沿用工具配置', '沿用工具配置')]
        model_clean = (model_id or '').lower()

        if actual_provider == 'antigravity':
            from antigravity_backend import EFFORTS
            fixed = next((level for level in EFFORTS if model_id.endswith('-' + level)), None)
            if fixed:
                return [(fixed, self.get_effort_short_label(fixed) + '（由模型名称指定）')]
            if model_clean.startswith('claude-'):
                return [('low', '由智能体管理')]
            return [(level, self.get_effort_short_label(level)) for level in ('low', 'medium', 'high')]

        # 1. Google Gemini (Antigravity): Absolutely NO Extra, NO Max!
        if actual_provider == 'antigravity' or 'gemini' in model_clean:
            if 'gemini' in model_clean or actual_provider == 'antigravity' and not ('claude' in model_clean or 'thinking' in model_clean):
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]
            elif 'opus-4-6' in model_clean or 'thinking' in model_clean:
                return [
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('max', 'Max (最大)'),
                ]
            else:
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]

        # 2. Codex CLI (OpenAI)
        elif actual_provider == 'codex' or 'gpt' in model_clean:
            if 'gpt-4o' in model_clean or '4o' in model_clean:
                return [('none', 'Off (不支持思考)')]
            try:
                options = codex_settings.model_options()
                if model_id in options:
                    raw_levels = options[model_id]
                    level_names = {
                        'none': 'Off (Off)',
                        'off': 'Off (Off)',
                        'low': 'Low (低)',
                        'medium': 'Medium (中)',
                        'high': 'High (高)',
                        'xhigh': 'Extra (超高)',
                        'max': 'Max (最大)',
                        'ultra': 'Ultra (极致)',
                    }
                    return [(lvl, level_names.get(lvl, lvl.capitalize())) for lvl in raw_levels]
            except Exception:
                pass

            if any(m in model_clean for m in ('6.1-sol', '6-astra', '6-sol', '5.6-sol', '5.6-terra')):
                return [
                    ('none', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('xhigh', 'Extra (超高)'),
                    ('max', 'Max (最大)'),
                    ('ultra', 'Ultra (极致)'),
                ]
            elif any(m in model_clean for m in ('6-luna', '5.6-luna')):
                return [
                    ('none', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('xhigh', 'Extra (超高)'),
                    ('max', 'Max (最大)'),
                ]
            else:
                return [
                    ('none', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]

        # 3. Claude Code (Anthropic)
        elif actual_provider == 'claude' or 'claude' in model_clean:
            if 'haiku-4-5' in model_clean:
                return [('off', 'Off (不支持思考)')]
            elif '4-6' in model_clean:
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('max', 'Max (最大)'),
                ]
            else:  # 5.5 and 5 series
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('extra', 'Extra (超高)'),
                    ('max', 'Max (最大)'),
                ]

        # 4. DeepSeek (DSH / DeepSeek )
        elif actual_provider == 'dsh':
            if 'flash' in model_clean:
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]
            else:  # Pro / Reasoner
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                    ('max', 'Max (深度思考)'),
                ]

        # 5. Custom API
        else:
            if 'reasoner' in model_clean or 'r1' in model_clean:
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]
            elif any(k in model_clean for k in ('chat', '4o', 'qwen', 'kimi')):
                return [('off', 'Off (不支持思考)')]
            else:
                return [
                    ('off', 'Off (Off)'),
                    ('low', 'Low (低)'),
                    ('medium', 'Medium (中)'),
                    ('high', 'High (高)'),
                ]

    def get_short_model_name(self, model_id: str) -> str:
        short_names = {
            'claude-opus-5-5': 'Opus 5.5',
            'claude-sonnet-5-5': 'Sonnet 5.5',
            'claude-fable-5-1': 'Fable 5.1',
            'claude-haiku-5-5': 'Haiku 5.5',
            'claude-haiku-4-5-20251001': 'Haiku 4.5',
            'claude-sonnet-5': 'Sonnet 5',
            'claude-opus-5': 'Opus 5',
            'claude-fable-5': 'Fable 5',
            'claude-opus-4-8': 'Opus 4.8',
            'claude-opus-4-7': 'Opus 4.7',
            'claude-opus-4-6': 'Opus 4.6',
            'claude-sonnet-4-6': 'Sonnet 4.6',
            'gpt-6.1-sol': 'GPT-6.1 Sol',
            'gpt-6-astra': 'GPT-6 Astra',
            'gpt-6-sol': 'GPT-6 Sol',
            'gpt-6-luna': 'GPT-6 Luna',
            'gpt-5.6-sol': 'GPT-5.6 Sol',
            'gpt-5.6-terra': 'GPT-5.6 Terra',
            'gpt-5.6-luna': 'GPT-5.6 Luna',
            'DeepSeek-V41-Flash': 'DeepSeek-V41-Flash',
            'DeepSeek-V4-Pro': 'DeepSeek-V4-Pro',
            'gemini-3.8-flash-high': 'Gemini 3.8 Flash',
            'gemini-3.8-flash-medium': 'Gemini 3.8 Med',
            'gemini-3.8-flash-low': 'Gemini 3.8 Low',
            'deepseek-chat': 'DeepSeek Chat',
            'deepseek-reasoner': 'DeepSeek R1',
        }
        return short_names.get(model_id, model_id or 'Model')

    def get_effort_short_label(self, effort_id: str = "") -> str:
        if not effort_id:
            effort_id = self.effort_combo.currentData() if hasattr(self, 'effort_combo') and self.effort_combo.count() > 0 else 'medium'
        effort_clean = str(effort_id or '').lower().strip()
        labels = {
            'off': '关闭',
            'none': '关闭',
            'low': '低',
            'medium': '中',
            'high': '高',
            'extra': '更高',
            'xhigh': '更高',
            'max': '最高',
            'ultra': '极高',
        }
        return labels.get(effort_clean, effort_clean or '中')

    def refresh_model_button_text(self):
        curr_m = self.model_combo.currentData() if hasattr(self, 'model_combo') and self.model_combo.count() > 0 else ''
        curr_e = self.effort_combo.currentData() if hasattr(self, 'effort_combo') and self.effort_combo.count() > 0 else 'medium'
        m_label = self.get_short_model_name(curr_m)
        e_label = self.get_effort_short_label(curr_e)
        if hasattr(self, 'model_btn'):
            self.model_btn.setText(f"{m_label}  {e_label}")

    def popup_model_menu(self):
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile
        profile = load_profile(self.house.profile_path)
        provider, actual_provider = self.get_effective_provider_and_master(profile)

        menu = RoundMenu(parent=self)

        header_title, main_models, overflow_models = self.get_provider_model_structure(actual_provider)

        # 1. Category Header (Muted non-interactive label)
        header_act = Action(header_title, menu)
        header_act.setEnabled(False)
        menu.addAction(header_act)

        # 2. Main Models
        curr_model = self.model_combo.currentData() or ''
        curr_effort = self.effort_combo.currentData() or 'medium'
        efforts = self.get_model_efforts(actual_provider, curr_model)

        for model_id, model_label, shortcut in main_models:
            is_selected = (curr_model == model_id)
            if is_selected:
                text = f"{model_label}  ✓"
            elif shortcut:
                text = f"{model_label}    {shortcut}"
            else:
                text = model_label
            act = Action(text, menu)
            act.triggered.connect(lambda checked, m=model_id: self.select_model_from_menu(m))
            menu.addAction(act)

        menu.addSeparator()

        # 3. Effort Submenu (Model-specific reasoning levels)
        if len(efforts) <= 1 and efforts and efforts[0][0] in ('none', 'off'):
            # Model does not support reasoning effort (e.g. gpt-4o, haiku-4-5)
            no_effort_act = Action('当前模型不支持调整思考强度', menu)
            no_effort_act.setEnabled(False)
            menu.addAction(no_effort_act)
        else:
            effort_short = self.get_effort_short_label(curr_effort)
            effort_menu = RoundMenu(f"思考强度    {effort_short} >", menu)
            for eff_id, eff_label in efforts:
                is_selected = (curr_effort == eff_id)
                eff_text = f"{eff_label}  ✓" if is_selected else eff_label
                act = Action(eff_text, effort_menu)
                act.triggered.connect(lambda checked, e=eff_id: self.select_effort_from_menu(e))
                effort_menu.addAction(act)
            menu.addMenu(effort_menu)

        # 4. Overflow models (More models >)
        if overflow_models:
            menu.addSeparator()
            more_menu = RoundMenu("更多模型", menu)
            for model_id, model_label in overflow_models:
                is_selected = (curr_model == model_id)
                text = f"{model_label}  ✓" if is_selected else model_label
                act = Action(text, more_menu)
                act.triggered.connect(lambda checked, m=model_id: self.select_model_from_menu(m))
                more_menu.addAction(act)
            menu.addMenu(more_menu)

        from PySide6.QtCore import QPoint
        from qfluentwidgets.components.widgets.menu import MenuAnimationType
        pos = self.model_btn.mapToGlobal(QPoint(0, 0))
        menu.exec(pos, aniType=MenuAnimationType.PULL_UP)

    def select_model_from_menu(self, model_id: str):
        found = False
        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == model_id:
                unchanged = self.model_combo.currentIndex() == i
                self.model_combo.setCurrentIndex(i)
                if unchanged:
                    self.on_model_changed(i)
                found = True
                break
        if not found:
            self.model_combo.addItem(self.get_short_model_name(model_id), userData=model_id)
            self.model_combo.setCurrentIndex(self.model_combo.count() - 1)
        self.refresh_model_button_text()

    def select_effort_from_menu(self, effort_id: str):
        found = False
        for i in range(self.effort_combo.count()):
            if self.effort_combo.itemData(i) == effort_id:
                unchanged = self.effort_combo.currentIndex() == i
                self.effort_combo.setCurrentIndex(i)
                if unchanged:
                    self.on_effort_changed(i)
                found = True
                break
        if not found:
            self.effort_combo.addItem(self.get_effort_short_label(effort_id), userData=effort_id)
            self.effort_combo.setCurrentIndex(self.effort_combo.count() - 1)
        self.refresh_model_button_text()

    def populate_provider_models(self, provider: str, profile: dict):
        api = (profile or {}).get('model_api') or {}
        provider, actual_provider = self.get_effective_provider_and_master(profile)
        header_title, main_models, overflow_models = self.get_provider_model_structure(actual_provider)[:3]

        models = [m[0] for m in main_models] + [m[0] for m in overflow_models]

        curr_model = ""
        if actual_provider == 'codex':
            curr_model = api.get('codex_model') or api.get('model') or 'gpt-6.1-sol'
        elif actual_provider == 'claude':
            raw = api.get('claude_model') or 'claude-haiku-5-5'
            curr_model = {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5', 'haiku': 'claude-haiku-5-5', 'fable': 'claude-fable-5-1'}.get(raw, raw)
        elif actual_provider == 'antigravity':
            curr_model = api.get('antigravity_model') or 'gemini-3.8-flash-high'
        elif actual_provider == 'dsh':
            dsh_settings = api.get('dsh_settings') or {}
            curr_model = dsh_settings.get('model') or api.get('model') or 'DeepSeek-V41-Flash'
        elif actual_provider == 'custom':
            curr_model = api.get('model') or 'deepseek-chat'

        if curr_model not in models and models:
            curr_model = models[0]

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in models:
            label = self.get_short_model_name(m)
            if provider == 'multi_agent':
                label = f"👑 {label}"
            self.model_combo.addItem(label, userData=m)

        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == curr_model:
                self.model_combo.setCurrentIndex(i)
                break
        self.model_combo.blockSignals(False)

        self.update_efforts_ui(provider, curr_model, profile)
        self.refresh_model_button_text()

    def update_efforts_ui(self, provider: str, model: str, profile: dict):
        provider, actual_provider = self.get_effective_provider_and_master(profile)
        api = (profile or {}).get('model_api') or {}
        efforts = self.get_model_efforts(actual_provider, model)

        curr_effort = 'medium'
        if actual_provider == 'codex':
            curr_effort = api.get('codex_effort', 'medium')
        elif actual_provider == 'claude':
            curr_effort = api.get('claude_effort', 'medium')
        elif actual_provider == 'antigravity':
            curr_effort = api.get('antigravity_effort', 'medium')
        elif actual_provider == 'dsh':
            dsh_settings = api.get('dsh_settings') or {}
            curr_effort = dsh_settings.get('effort') or api.get('api_effort') or 'medium'
        elif actual_provider == 'custom':
            curr_effort = api.get('api_effort', 'medium')

        self.effort_combo.blockSignals(True)
        self.effort_combo.clear()
        for e_id, e_label in efforts:
            self.effort_combo.addItem(tr(e_label), userData=e_id)

        matched = False
        for i in range(self.effort_combo.count()):
            if self.effort_combo.itemData(i) == curr_effort:
                self.effort_combo.setCurrentIndex(i)
                matched = True
                break

        if not matched and self.effort_combo.count() > 0:
            # Fallback to a valid effort supported by this model
            fallback_idx = min(len(efforts) - 1, 2)
            self.effort_combo.setCurrentIndex(fallback_idx)

        self.effort_combo.blockSignals(False)
        self.refresh_model_button_text()

    def on_provider_changed(self, index):
        if index < 0:
            return
        provider_val = self.provider_combo.itemData(index)
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            api = profile.setdefault('model_api', {})
            if provider_val == 'multi_agent':
                api['multi_agent_enabled'] = True
                api.setdefault('multi_agent', {})['enabled'] = True
            else:
                api['multi_agent_enabled'] = False
                api.setdefault('multi_agent', {})['enabled'] = False
                api['provider'] = provider_val
            write_profile(self.house.profile_path, profile)
            self.house.mark_stale('modelPage')
            self.reload_models_ui(profile)
            toast(self.house, "success", f"已切换到 {self.provider_combo.currentText()}。")
        except Exception as exc:
            toast(self.house, "error", str(exc), " CLI Failed")

    def on_model_changed(self, index):
        if index < 0:
            return
        model_val = self.model_combo.itemData(index)
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            api = profile.setdefault('model_api', {})
            provider, actual_provider = self.get_effective_provider_and_master(profile)
            if actual_provider == 'codex':
                api['codex_model'] = model_val
                api['model'] = model_val
            elif actual_provider == 'claude':
                api['claude_model'] = model_val
            elif actual_provider == 'antigravity':
                api['antigravity_model'] = model_val
            elif actual_provider == 'dsh':
                api.setdefault('dsh_settings', {})['model'] = model_val
                api['model'] = model_val
            elif actual_provider == 'custom':
                api['model'] = model_val

            levels = [value for value, _ in self.get_model_efforts(actual_provider, model_val)]
            effort_key = {'codex': 'codex_effort', 'claude': 'claude_effort',
                          'antigravity': 'antigravity_effort', 'custom': 'api_effort'}.get(actual_provider)
            if effort_key and levels and api.get(effort_key) not in levels:
                api[effort_key] = 'medium' if 'medium' in levels else levels[0]

            if provider == 'multi_agent':
                api.setdefault('multi_agent', {})['model'] = model_val

            write_profile(self.house.profile_path, profile)
            self.update_efforts_ui(provider, model_val, profile)
            self.refresh_model_button_text()
            prefix = "主智能体模型" if provider == 'multi_agent' else "模型"
            toast(self.house, "success", f"{prefix}已切换为：{model_val}")
        except Exception as exc:
            toast(self.house, "error", str(exc), "模型切换失败")

    def on_effort_changed(self, index):
        if index < 0:
            return
        effort_val = self.effort_combo.itemData(index)
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            api = profile.setdefault('model_api', {})
            provider, actual_provider = self.get_effective_provider_and_master(profile)
            if actual_provider == 'codex':
                api['codex_effort'] = effort_val
                api['antigravity_effort'] = effort_val
            elif actual_provider == 'claude':
                api['claude_effort'] = effort_val
            elif actual_provider == 'antigravity':
                api['antigravity_effort'] = effort_val
            elif actual_provider == 'dsh':
                api.setdefault('dsh_settings', {})['effort'] = effort_val
                api['api_effort'] = effort_val
            elif actual_provider == 'custom':
                api['api_effort'] = effort_val

            if provider == 'multi_agent':
                api.setdefault('multi_agent', {})['effort'] = effort_val

            write_profile(self.house.profile_path, profile)
            self.refresh_model_button_text()
            prefix = "主智能体思考强度" if provider == 'multi_agent' else "思考强度"
            toast(self.house, "success", f"{prefix}已切换为：{self.effort_combo.currentText()}")
        except Exception as exc:
            toast(self.house, "error", str(exc), "思考强度切换失败")

    def on_fast_changed(self, state):
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            api = profile.setdefault('model_api', {})
            is_fast = bool(self.fast_chk.isChecked())
            api['codex_fast'] = is_fast
            write_profile(self.house.profile_path, profile)
            status_str = "已开启" if is_fast else "已关闭"
            toast(self.house, "success", f"Fast 快速模式{status_str}")
        except Exception as exc:
            toast(self.house, "error", str(exc), "切换快速模式失败")

    # ================= Hierarchical Projects Tree (with QQ Stream) =================

    def reload_projects_tree(self, qq: str):
        """Populate the hierarchical Projects tree with clean titles and no truncation."""
        self.tree.blockSignals(True)
        self.tree.clear()

        workspaces = session_manager.list_workspaces(qq)
        active_ws_id = self.active_ws_id or session_manager.get_active_workspace_id(qq)
        active_sid = self.active_session_id or session_manager.get_active_session_id(qq, active_ws_id)

        target_item = None
        for ws in workspaces:
            ws_id = ws['id']
            ws_item = QTreeWidgetItem(self.tree)
            is_def = ws.get('is_default', False)
            title = "📁 默认主空间" if is_def else f"📁 {ws['title']}"
            ws_item.setText(0, title)
            ws_item.setData(0, Qt.UserRole, ('workspace', ws_id))
            ws_item.setToolTip(0, f"items: {ws.get('path', '')}")
            ws_item.setSizeHint(0, QSize(280, 36))
            ws_item.setForeground(0, QColor("#0F172A"))
            ws_font = ws_item.font(0)
            ws_font.setBold(True)
            ws_item.setFont(0, ws_font)

            # If default workspace, add pinned QQ Stream channel
            if is_def:
                qq_stream_item = QTreeWidgetItem(ws_item)
                qq_stream_item.setText(0, "📱 QQ 消息流")
                qq_stream_item.setData(0, Qt.UserRole, ('session', 'default', '__qq_stream__'))
                qq_stream_item.setToolTip(0, "当前好友在 QQ 上的真实双向聊天记录镜像与消息流通道")
                qq_stream_item.setSizeHint(0, QSize(280, 32))
                qq_stream_item.setForeground(0, QColor("#0284C7"))
                if active_ws_id == 'default' and active_sid == '__qq_stream__':
                    target_item = qq_stream_item

            # Child sessions
            sessions = session_manager.list_sessions(qq, ws_id)
            for s in sessions:
                s_item = QTreeWidgetItem(ws_item)
                s_title = s['title']
                rel_time = format_relative_time(s.get('updated_at', s.get('created_at', '')))
                display_text = f"💬 {s_title}" + (f"  ·  {rel_time}" if rel_time else "")
                s_item.setText(0, display_text)
                s_item.setData(0, Qt.UserRole, ('session', ws_id, s['id']))
                s_item.setToolTip(0, f"会话: {s_title}\n时间: {s.get('updated_at', s.get('created_at', ''))}")
                s_item.setSizeHint(0, QSize(280, 32))
                s_item.setForeground(0, QColor("#334155"))

                if ws_id == active_ws_id and s['id'] == active_sid:
                    target_item = s_item

        self.tree.expandAll()
        self.tree.blockSignals(False)

        if target_item:
            self.tree.setCurrentItem(target_item)
            self.active_session_id = active_sid
            self.active_ws_id = active_ws_id
            self.load_session_ui(active_sid)
        elif workspaces:
            first_ws = workspaces[0]['id']
            sessions = session_manager.list_sessions(qq, first_ws)
            first_sid = sessions[0]['id'] if sessions else '__qq_stream__'
            self.active_ws_id = first_ws
            self.active_session_id = first_sid
            self.load_session_ui(first_sid)

        # Update workspace badge
        ws_info = next((w for w in workspaces if w['id'] == self.active_ws_id), None)
        self.ws_badge.setText(f"工作区: {ws_info['title'] if ws_info else '默认主空间'}")

    def on_tree_item_clicked(self, item: QTreeWidgetItem, column: int):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        item_type = data[0]
        qq = self.house.current_qq
        if item_type == 'workspace':
            ws_id = data[1]
            self.active_ws_id = ws_id
            session_manager.set_active_workspace_id(qq, ws_id)
            sessions = session_manager.list_sessions(qq, ws_id)
            if sessions:
                sid = sessions[0]['id']
                session_manager.set_active_session_id(qq, sid, ws_id)
                self.active_session_id = sid
                self.load_session_ui(sid)
            elif ws_id == 'default':
                self.active_session_id = '__qq_stream__'
                self.load_session_ui('__qq_stream__')
            ws_info = next((w for w in session_manager.list_workspaces(qq) if w['id'] == ws_id), None)
            self.ws_badge.setText(f"工作区: {ws_info['title'] if ws_info else '默认主空间'}")
        elif item_type == 'session':
            ws_id, sid = data[1], data[2]
            self.active_ws_id = ws_id
            self.active_session_id = sid
            session_manager.set_active_workspace_id(qq, ws_id)
            if sid != '__qq_stream__':
                session_manager.set_active_session_id(qq, sid, ws_id)
            self.load_session_ui(sid)
            ws_info = next((w for w in session_manager.list_workspaces(qq) if w['id'] == ws_id), None)
            self.ws_badge.setText(f"工作区: {ws_info['title'] if ws_info else '默认主空间'}")

    def show_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos) or self.tree.currentItem()
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        item_type = data[0]
        menu = RoundMenu(parent=self)

        if item_type == 'workspace':
            ws_id = data[1]
            menu_item(menu, FluentIcon.ADD.qicon(), "➕ 在此项目下新建对话", lambda: self.create_session_for_workspace(ws_id))
            menu_item(menu, FluentIcon.FOLDER.qicon(), "📁 在资源管理器中打开", lambda: self.open_in_explorer(ws_id))
            if ws_id != 'default':
                menu.addSeparator()
                menu_item(menu, FluentIcon.CLOSE.qicon(), "❌ 从工作台移除该项目", lambda: self.remove_workspace_by_id(ws_id))
        elif item_type == 'session':
            ws_id, sid = data[1], data[2]
            if sid == '__qq_stream__':
                menu_item(menu, FluentIcon.SYNC.qicon(), "🔄 Refresh QQ ", self.refresh_current_view)
            else:
                menu_item(menu, FluentIcon.EDIT.qicon(), "✏️ 重命名对话", lambda: self.rename_session_dialog(ws_id, sid, item.text(0)))
                menu_item(menu, FluentIcon.PIN.qicon(), "📌 保存到长期记忆", self.save_conversation_to_memory)
                menu_item(menu, FluentIcon.FOLDER.qicon(), "📁 打开所在项目文件夹", lambda: self.open_in_explorer(ws_id))
                menu.addSeparator()
                menu_item(menu, FluentIcon.DELETE.qicon(), "🧹 清空该对话记录", lambda: self.clear_specific_session(ws_id, sid))
                menu_item(menu, FluentIcon.CLOSE.qicon(), "🗑️ 删除该对话", lambda: self.delete_specific_session(ws_id, sid))

        menu.exec(self.tree.mapToGlobal(pos))

    def open_in_explorer(self, ws_id: str):
        qq = self.house.current_qq
        workspaces = session_manager.list_workspaces(qq)
        ws_info = next((w for w in workspaces if w['id'] == ws_id), None)
        if ws_info and os.path.isdir(ws_info.get('path', '')):
            os.startfile(ws_info['path'])
        else:
            toast(self.house, "warning", "工作区目录不存在。")

    def add_project_dialog(self):
        qq = self.house.current_qq
        path = QFileDialog.getExistingDirectory(self, "选择要添加到 Projects 的项目文件夹", "D:\\")
        if not path:
            return
        try:
            ws = session_manager.add_workspace(qq, path)
            self.active_ws_id = ws['id']
            self.reload_projects_tree(qq)
            toast(self.house, "success", f"已添加项目「{ws['title']}」，已自动归纳到 Projects 树。")
        except Exception as exc:
            toast(self.house, "error", str(exc), "itemsFailed")

    def remove_workspace_by_id(self, ws_id: str):
        qq = self.house.current_qq
        if ws_id == 'default':
            toast(self.house, "warning", "默认主文件夹无法移除。")
            return
        if not confirm(self.house, "项目", "确定从项目列表中移除当前项目吗？\n\n磁盘上的本地文件会保留。"):
            return
        session_manager.remove_workspace(qq, ws_id)
        self.active_ws_id = 'default'
        self.reload_projects_tree(qq)
        toast(self.house, "success", "项目已从工作台移除。")

    def create_new_session(self):
        self.create_session_for_workspace(self.active_ws_id)

    def create_session_for_workspace(self, ws_id: str):
        qq = self.house.current_qq
        s = session_manager.create_session(qq, ws_id)
        self.active_ws_id = ws_id
        self.active_session_id = s['id']
        self.reload_projects_tree(qq)
        toast(self.house, "success", f"已新建对话「{s['title']}」")

    def rename_session_dialog(self, ws_id: str, sid: str, old_title: str):
        qq = self.house.current_qq
        clean_name = re.sub(r'^\s*💬\s*|\s*·.*$', '', old_title).strip()
        dialog = InputDialog(self.house, "重命名对话", "请输入新的对话名称：")
        dialog.edit.setText(clean_name)
        if not dialog.exec():
            return
        new_title = dialog.edit.text().strip()
        if new_title:
            session_manager.rename_session(qq, sid, new_title, ws_id)
            self.reload_projects_tree(qq)

    def clear_specific_session(self, ws_id: str, sid: str):
        qq = self.house.current_qq
        if sid == '__qq_stream__':
            toast(self.house, "info", "QQ 实时私聊流为真实聊天同步通道，不可在控制台清空。")
            return
        if not confirm(self.house, "Clear Chat", "确定清空该会话中的所有消息记录吗？"):
            return
        session_manager.clear_session(qq, sid, ws_id)
        self.reload_projects_tree(qq)
        toast(self.house, "success", "Session history cleared. ")

    def clear_current_session(self):
        if self.active_session_id:
            self.clear_specific_session(self.active_ws_id, self.active_session_id)

    def delete_specific_session(self, ws_id: str, sid: str):
        qq = self.house.current_qq
        if sid == '__qq_stream__':
            toast(self.house, "warning", "QQ 实时私聊通道为固定通道，不可删除。")
            return
        if not confirm(self.house, "删除对话", "确定删除该会话吗？"):
            return
        if session_manager.delete_session(qq, sid, ws_id):
            self.reload_projects_tree(qq)
            toast(self.house, "success", "会话已删除。")
        else:
            toast(self.house, "info", "唯一的会话已清空保留。")

    # ================= Persona Template Selector =================

    def flush_message_queue(self):
        try:
            from queue_governor import QueueGovernor
            count = QueueGovernor.get_instance().flush_all()
            toast(self.house, "success", f"消息队列已疏通，已处理 {count} 条待处理消息。", "Queue Drained")
        except Exception as exc:
            toast(self.house, "info", f"队列状态正常（{exc})")

    def reload_templates_ui(self, profile: dict):
        from persona_template import list_templates
        templates = list_templates()
        current_tpl_id = profile.get('persona_template')

        self.tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        self.tpl_combo.addItem("不使用模板（仅按常驻人设）", userData=None)

        selected_idx = 0
        for idx, t in enumerate(templates):
            speaker = t.get('source', {}).get('speaker', '')
            display_str = f"{t['name']} · {speaker}" if speaker else t['name']
            self.tpl_combo.addItem(f"🎭 {display_str}", userData=t['id'])
            if t['id'] == current_tpl_id:
                selected_idx = idx + 1

        self.tpl_combo.setCurrentIndex(selected_idx)
        self.tpl_combo.blockSignals(False)
        self.update_template_display(profile)

    def update_template_display(self, profile: dict):
        import persona_template
        active_tpl = persona_template.active(profile)
        if active_tpl:
            name = active_tpl.get('name', '模板')
            self.template_badge.setText(f"模板: {name}")
            self.tpl_hint.setText(f"参照: {name} 对话范例与习惯")
            self.tpl_hint.setToolTip(active_tpl.get('summary', ''))
        else:
            self.template_badge.setText("模板: 未选用")
            self.tpl_hint.setText("对话参照: 常驻人设与指令")
            self.tpl_hint.setToolTip("未选用模板，按照「常驻指令与设定」中的设定运行")

    def on_template_changed(self, index):
        if index < 0:
            return
        tpl_id = self.tpl_combo.itemData(index)
        qq = self.house.current_qq
        if not qq:
            return
        from house_status import load_profile, write_profile
        try:
            profile = load_profile(self.house.profile_path)
            if tpl_id:
                profile['persona_template'] = tpl_id
            else:
                profile.pop('persona_template', None)
            write_profile(self.house.profile_path, profile)
            self.house.mark_stale('templatePage')
            self.house.mark_stale('stylePage')
            self.update_template_display(profile)
            if self.active_session_id:
                self.load_session_ui(self.active_session_id)
            toast(self.house, "success", f"已切换设定模板：{self.tpl_combo.currentText()}，机前对话参照已更新。")
        except Exception as exc:
            toast(self.house, "error", str(exc), "切换模板失败")

    # ================= Chat Canvas UI (Sessions vs QQ Stream) =================

    def _metric_friend_dir(self):
        try:
            from house_status import ROOT
            return ROOT / 'friends' / str(self.house.current_qq)
        except Exception:
            return None

    def _metric_usage_fields(self):
        now = time.time()
        directory = self._metric_friend_dir()
        if (self._metric_usage_key == directory and self._metric_usage is not None
                and now - self._metric_usage_at < 30):
            return self._metric_usage
        self._metric_usage_key = directory
        if directory is None or not directory.is_dir():
            self._metric_usage = {}
        else:
            try:
                from usage_accounting import summary
                data = summary(directory)
                tokens = data.get('tokens') or {}
                self._metric_usage = {
                    'total': int(tokens.get('input_tokens', 0) or 0) + int(tokens.get('output_tokens', 0) or 0),
                    'input': int(tokens.get('input_tokens', 0) or 0),
                    'cached': int(tokens.get('cached_input_tokens', 0) or 0),
                }
            except Exception:
                self._metric_usage = {}
        self._metric_usage_at = now
        return self._metric_usage

    def refresh_metrics(self, context_chars=None):
        if not hasattr(self, 'metric_rounds'):
            return
        qq, ws_id = self.house.current_qq, self.active_ws_id
        sid = self.active_session_id
        rounds = steps = 0
        text_chars = 0
        try:
            if qq:
                data = session_manager.load_session(qq, sid, ws_id)
                messages = data.get('messages') or []
                rounds = sum(1 for m in messages if m.get('role') == 'user')
                steps = sum(len(m.get('tools') or []) for m in messages)
                text_chars = sum(len(str(m.get('content') or '')) for m in messages)
        except Exception:
            pass
        self.metric_rounds.setText(f"{rounds} 轮 {steps} 步")
        self.metric_speed.setText(f"{self._last_tok_s:.0f} tok/s" if self._last_tok_s else "-- tok/s")
        usage = self._metric_usage_fields()
        total = int(usage.get('total') or 0)
        cached = int(usage.get('cached') or 0)
        input_tokens = int(usage.get('input') or 0)
        total_text = f"{total / 1_000_000:.1f}M tok" if total >= 1_000_000 else f"{total:,} tok"
        cache_text = f"缓存命中 {cached / input_tokens:.0%}" if input_tokens else "缓存命中 --"
        self.metric_tokens.setText(f"{total_text} · {cache_text}")
        try:
            budget = int(context_chars or 128_000)
            profile = None
            from house_status import load_profile
            profile = load_profile(self.house.profile_path)
            budget = int((profile.get('runtime_context') or {}).get('max_context_tokens') or budget)
        except Exception:
            budget = int(context_chars or 128_000)
        used = max(0, int(text_chars / 2))
        percent = min(100, round(used * 100 / max(1, budget)))
        self.metric_context.setText(f"{percent}%")

    def load_session_ui(self, session_id: str):
        if self._voice_capture and self._voice_scope != (self.house.current_qq,self.active_ws_id,session_id):
            self._voice_capture.cancel()
        self.active_session_id = session_id
        self.btn_attach.setToolTip('发送 QQ 文件、图片或视频' if session_id == '__qq_stream__' else '引用项目文件')
        self.input_mode_combo.setVisible(False)
        key = (self.house.current_qq, self.active_ws_id, session_id)
        self.input_mode_combo.blockSignals(True)
        self.input_mode_combo.setCurrentIndex(
            self.input_mode_combo.findData(self.session_input_modes.get(key, 'auto')))
        self.input_mode_combo.blockSignals(False)
        qq = self.house.current_qq
        friend_name = self.house.records.get(qq, {}).get('display_name', qq)

        # Clear existing bubbles
        while self.messages_layout.count() > 1:
            child = self.messages_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # Mode A: Dedicated QQ Private Chat Stream ()
        if session_id == '__qq_stream__':
            self.sess_title_lbl.setText(f"📱 QQ 私聊实时流 · {friend_name}")
            self.ws_badge.setText("通道: QQ 实时私聊")
            self.btn_refresh_qq.setVisible(True)
            self.input_edit.setPlaceholderText(f"输入要通过 QQ 发送给「{friend_name}」的私聊消息（Enter 发送）…")
            self.send_btn.setText('发送 QQ')

            from house_status import ROOT
            store = FriendStore(ROOT / 'friends' / qq)
            try:
                qq_msgs = store.recent_messages(80)
            finally:
                store.close()

            if not qq_msgs:
                card = MessageCard({
                    'role': 'assistant',
                    'content': f"暂无与好友「{friend_name}」的 QQ 聊天记录。\n当好友在 QQ 发送来信或机器人自动回复时，将自动在此消息流中同步汇聚。",
                    'timestamp': ""
                }, agent_name="系统提示", parent=self.messages_container)
                self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)
            else:
                agent_display = self.get_agent_name()
                for item in qq_msgs:
                    is_friend = (item.get('direction') == 'friend')
                    msg_data = {
                        'role': 'user' if is_friend else 'assistant',
                        'content': item.get('body', ''),
                        'timestamp': item.get('created_at', '')
                    }
                    role_tag = f"{friend_name}（QQ 来信）" if is_friend else (
                        f"{agent_display}（QQ 回复）" if item.get('identity') == 'model' else '我（QQ 发出）')
                    card = MessageCard(msg_data, agent_name=role_tag, parent=self.messages_container)
                    self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)

        # Mode B: Independent Agent Project / Workbench Session (OnAgent)
        else:
            self.btn_refresh_qq.setVisible(False)
            self.send_btn.setText('发送')
            self.input_edit.setPlaceholderText("输入任务指令，Enter 发送，Shift+Enter 换行")

            data = session_manager.load_session(qq, session_id, self.active_ws_id)
            self.sess_title_lbl.setText(data.get('title', '对话'))
            ws_info = next((w for w in session_manager.list_workspaces(qq) if w['id'] == self.active_ws_id), None)
            self.ws_badge.setText(f"工作区: {ws_info['title'] if ws_info else '默认主空间'}")

            agent_name = self.get_agent_name()
            for msg in data.get('messages', []):
                card = MessageCard(msg, agent_name=agent_name, parent=self.messages_container)
                self.messages_layout.insertWidget(self.messages_layout.count() - 1, card)

        self.scroll_to_bottom()
        self.refresh_metrics()

    def scroll_to_bottom(self):
        QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    # ================= Send Message & Agent Execution =================

    def remember_input_mode(self, _index):
        if self.active_session_id and self.active_session_id != '__qq_stream__':
            key = (self.house.current_qq, self.active_ws_id, self.active_session_id)
            self.session_input_modes[key] = self.input_mode_combo.currentData()

    def resolve_input_mode(self, text):
        """What this message is: 'auto' reads the text, the two explicit choices are respected as-is.

        2026-10-26 user: typing 你好 created task_20261026_141126_31727e — a greeting is conversation,
        and only a real delegation (or 任务：/`/task` prefix) should reach the task engine.
        """
        mode = self.input_mode_combo.currentData() or 'auto'
        if mode == 'auto':
            import task_spec
            return 'task' if task_spec.input_intent(text, 'workbench-auto') == 'task' else 'chat'
        return mode

    def submit_task_message(self, qq, ws_id, sid, text):
        if getattr(self, 'task_submitting', False):
            return
        self.task_submitting = True
        self.send_btn.setEnabled(False)
        provider = self.provider_combo.currentData()
        def done(result):
            self.task_submitting = False
            self.send_btn.setEnabled(True)
            if self.house.current_qq == qq and self.active_ws_id == ws_id and self.active_session_id == sid:
                self.input_edit.clear()
                self.load_session_ui(sid)
            toast(self.house, 'success', '任务已提交，可在任务中心查看进度。')
        def failed(exc):
            self.task_submitting = False
            self.send_btn.setEnabled(True)
            toast(self.house, 'error', str(exc), '任务未提交')
        from agent_service import execute_agent_turn
        run_async(lambda _: execute_agent_turn(qq, ws_id, sid, text, provider=provider, mode='task'),
                  done, failed, pool=LONG_POOL)

    def send_message(self):
        text = self.input_edit.toPlainText().strip()
        if not text:
            return

        qq = self.house.current_qq
        if not qq or qq not in self.house.records:
            toast(self.house, 'warning', '请先在好友总览中添加并选择好友。')
            return
        ws_id = self.active_ws_id
        sid = self.active_session_id or '__qq_stream__'

        # ---------------- Handler 1: Dedicated QQ Private Chat Send ----------------
        if sid == '__qq_stream__':
            self.input_edit.clear()
            self.send_qq_message(qq, text)
            return

        # ---------------- Handler 2: AI Agent Execution Session ----------------
        if self.is_generating:
            self.stop_generation()
            return
        mode = self.resolve_input_mode(text)
        if mode == 'task':
            self.submit_task_message(qq, ws_id, sid, text)
            return

        # Find current workspace directory
        workspaces = session_manager.list_workspaces(qq)
        ws_item = next((w for w in workspaces if w['id'] == ws_id), None)
        cwd_dir = Path(ws_item['path']) if ws_item else session_manager._friend_dir(qq)

        agent_name = self.get_agent_name()
        u_msg = session_manager.append_message(qq, sid, 'user', text, ws_id=ws_id)
        u_card = MessageCard(u_msg, agent_name=agent_name, parent=self.messages_container)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, u_card)
        self.input_edit.clear()
        self.scroll_to_bottom()

        self.is_generating = True
        self._generation_started = time.time()
        self.send_btn.setEnabled(True)
        # 2026-10-26 user: the stop state was a bare red box with an emoji square. Same shape as the send
        # state, red, white bold text, no emoji.
        self.send_btn.setText('停止生成')
        self.send_btn.setStyleSheet("""
            PrimaryPushButton {
                background: #DC2626;
                border: 1px solid #B91C1C;
                border-radius: 8px;
                font-weight: bold;
                padding: 6px 18px;
                color: #FFFFFF;
            }
            PrimaryPushButton:hover {
                background: #B91C1C;
            }
            PrimaryPushButton:pressed {
                background: #991B1B;
            }
        """)
        if hasattr(self, 'working_widget'):
            self.working_widget.setVisible(True)

        self.generating_card = GeneratingCard(agent_name=agent_name, parent=self.messages_container)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, self.generating_card)
        self.scroll_to_bottom()

        # Capture selected model and effort from actual options
        selected_model = self.model_combo.itemData(self.model_combo.currentIndex())
        selected_effort = self.effort_combo.itemData(self.effort_combo.currentIndex())
        selected_provider = self.provider_combo.currentData()
        selected_fast = self.fast_chk.isChecked()

        def run_call(_):
            from house_status import ROOT, load_profile
            from main import codex_text
            import persona_template

            friend_dir = ROOT / 'friends' / qq
            profile = load_profile(friend_dir / 'friend.json')
            persona = profile.get('persona', '')
            basic = profile.get('basic_info', '')
            style = profile.get('style', '')

            s_data = session_manager.load_session(qq, sid, ws_id)
            # 2026-10-26 user: 最近六句太多，两句就够；再把两口归档进记忆，不必每轮重发。
            history = s_data.get('messages', [])[-2:]

            # 1. Persona Template ()
            active_tpl = persona_template.active(profile)
            agent_role_name = "Dot Agent"
            template_instruction = ""

            if active_tpl:
                tpl_name = active_tpl.get('name', '未命名模板')
                speaker = active_tpl.get('source', {}).get('speaker', '')
                agent_role_name = speaker or tpl_name
                # 2026-10-26 user: 范例不能砍（砍了就不像模板了），但也不该每条都塞五六段。
                # 固定 2 段范例 + 对范例的概括，每条消息都带，长度只有原来的四成左右。
                tpl_block = persona_template.prompt_block(
                    profile, friend_dir, text, scene='workbench', kind='reply', max_samples=2,
                )
                template_instruction = (
                    f"【机前参照的设定模板：{tpl_name}】\n"
                    f"角色：{agent_role_name}。模板概括：{active_tpl.get('summary', '')}\n"
                    f"{tpl_block}\n"
                    f"【核心要求】：以「{agent_role_name}」的口吻回复，照着上面范例的语气、句长、用词和表情习惯；不要复述这些说明。"
                )
            else:
                agent_role_name = agent_name

            # Thinking effort instruction
            effort_prompt = ""
            if selected_effort in ('high', 'xhigh', 'max', 'ultra'):
                effort_prompt = "【深度思考推理要求】：请充分展开逻辑推导与代码分析，完整输出深度推演逻辑（可包含 <think>...</think> 思考链）。"
            elif selected_effort == 'none':
                effort_prompt = "【极速响应模式】：请简明扼要直接给出结论或结果，不进行冗长发散。"

            prompt_parts = [
                f" [Agent Workspace Session] ",
                f"你是一个高效专业的智能体助手（代号/角色：{agent_role_name}）。",
                f"当前执行项目工作区为：{ws_item['title'] if ws_item else '默认'}（工作目录：{cwd_dir}）。",
            ]

            if effort_prompt:
                prompt_parts.append(effort_prompt)

            if template_instruction:
                prompt_parts.append(template_instruction)
            elif persona:
                prompt_parts.append(f"【角色设定与常驻指令】\n{persona}")

            if style:
                prompt_parts.append(f"【交互与回复偏好】\n{style}")

            if basic:
                prompt_parts.append(
                    f"【当前使用者的背景与用户画像】\n"
                    f"以下是正在与你对话的用户画像信息，供你更好理解对方需求：\n{basic}"
                )

            # Auto inject Second Brain memory & Topics knowledge
            from store import FriendStore
            store = FriendStore(friend_dir)
            try:
                from memory_management import core_blocks
                memories = core_blocks(store)
                if memories:
                    mems = '\n'.join(f'{key}: {value}' for key, value in memories.items())
                    prompt_parts.append(f"【长期事实记忆】\n{mems}")

                from topic_research import items
                topic_rows = [item for item in items(friend_dir) if item.get('summary')][:4]
                if topic_rows:
                    tops = "\n".join(f"- <{r['title']}>: {r['summary'][:120]}" for r in topic_rows)
                    prompt_parts.append(f" [Topic Knowledge Library] \n{tops}")
            finally:
                store.close()

            conv_lines = []
            # 2026-10-26 user: 输入 token 太多。历史每轮都整段重发，长对话里它比人设还大；
            # 这里只带最近 10 条、每条最多 400 字，旧内容留给记忆模块。
            for h in history[-11:-1]:
                prefix = "用户" if h['role'] == 'user' else agent_role_name
                body = (h['content'] or '').strip()
                if len(body) > 400:
                    body = body[:400] + '…'
                conv_lines.append(f"{prefix}: {body}")
            if conv_lines:
                prompt_parts.append("【近期会话历史】\n" + "\n".join(conv_lines))

            # Strict output constraint ensuring clean final reply only
            prompt_parts.append(
                f"【输出铁律（极其重要）】：\n"
                f"1. 直接输出对用户最后一条指令的最终回复正文。\n"
                f"2. 绝对严禁复读、引用或输出本提示词中的任何背景说明、模板名称、设定说明、范例示例或历史上下文！\n"
                f"3. 绝对严禁添加诸如“好的，根据模板回复如下：”或“以下是我的回答：”等前置废话！"
            )

            prompt_parts.append(f"用户：{text}\n{agent_role_name}：")
            full_prompt = "\n\n".join(prompt_parts)

            # Ensure model and effort override in profile during execution
            call_profile = dict(profile)
            call_api = dict(call_profile.get('model_api') or {})
            provider = selected_provider
            if provider == 'multi_agent':
                # 2026-10-26 user: a chat line under 跨智能体协作网络 came back as a submitted task. The
                # collaboration pipeline runs tasks; a plain reply goes to the configured master agent.
                provider = (call_api.get('multi_agent') or {}).get('master') or 'codex'
            call_api['provider'] = provider
            call_api['multi_agent_enabled'] = provider == 'multi_agent'
            call_api['multi_agent'] = {**(call_api.get('multi_agent') or {}), 'enabled': provider == 'multi_agent'}

            if provider == 'codex':
                if selected_model:
                    call_api['codex_model'] = selected_model
                    call_api['model'] = selected_model
                if selected_effort:
                    call_api['codex_effort'] = selected_effort
                    call_api['antigravity_effort'] = selected_effort
                call_api['codex_fast'] = selected_fast
            elif provider == 'claude':
                if selected_model:
                    call_api['claude_model'] = selected_model
                if selected_effort:
                    call_api['claude_effort'] = selected_effort
            elif provider == 'antigravity':
                if selected_model:
                    call_api['antigravity_model'] = selected_model
                if selected_effort:
                    call_api['antigravity_effort'] = selected_effort
            elif provider in ('dsh', 'deepseek_harness'):
                if selected_model:
                    call_api['model'] = selected_model
                    call_api.setdefault('dsh_settings', {})['model'] = selected_model
                if selected_effort:
                    call_api['api_effort'] = selected_effort
                    call_api.setdefault('dsh_settings', {})['effort'] = selected_effort
            elif provider in ('custom', 'deepseek', 'api'):
                if selected_model:
                    call_api['model'] = selected_model
                if selected_effort:
                    call_api['api_effort'] = selected_effort
                call_api['codex_fast'] = selected_fast
            elif provider == 'multi_agent':
                call_api['multi_agent_enabled'] = True
                call_api.setdefault('multi_agent', {})['enabled'] = True
                multi_cfg = call_api.get('multi_agent') or {}
                actual_master = multi_cfg.get('master') or call_api.get('master_provider') or 'codex'
                if actual_master == 'codex':
                    if selected_model:
                        call_api['codex_model'] = selected_model
                        call_api['model'] = selected_model
                    if selected_effort:
                        call_api['codex_effort'] = selected_effort
                elif actual_master == 'claude':
                    if selected_model:
                        call_api['claude_model'] = selected_model
                    if selected_effort:
                        call_api['claude_effort'] = selected_effort
                elif actual_master == 'antigravity':
                    if selected_model:
                        call_api['antigravity_model'] = selected_model
                    if selected_effort:
                        call_api['antigravity_effort'] = selected_effort
                elif actual_master == 'dsh':
                    if selected_model:
                        call_api.setdefault('dsh_settings', {})['model'] = selected_model
                        call_api['model'] = selected_model
                    if selected_effort:
                        call_api.setdefault('dsh_settings', {})['effort'] = selected_effort
                        call_api['api_effort'] = selected_effort
                elif actual_master == 'custom':
                    if selected_model:
                        call_api['model'] = selected_model
                    if selected_effort:
                        call_api['api_effort'] = selected_effort

            call_profile['model_api'] = call_api

            if str(selected_provider).startswith('webchat_'):
                # 2026-10-26 user: 网页对话直连——把整理好的提示词交给已登录的网页，本地一行模型都不跑，
                # 不占本地额度、不进本地缓存、也不带上下文（网页那边是一次性会话）。
                # 网页只需要「你是谁 + 刚聊了什么 + 这次的问题」，整份内部提示词塞过去既贵又会被当成指令。
                import web_chat

                brief = [f"你是「{agent_role_name}」。用中文简短自然地回答，不要复述这段说明。"]
                if template_instruction:
                    # 网页端同样要范例，否则它不知道该怎么说话。
                    brief.append(template_instruction)
                else:
                    persona_text = (persona or '').strip()
                    if persona_text:
                        brief.append("人设要点：" + persona_text[:400])
                if conv_lines:
                    brief.append("最近对话：\n" + "\n".join(conv_lines[-2:]))
                brief.append("用户：" + text)
                return web_chat.ask(str(selected_provider)[len('webchat_'):], "\n".join(brief),
                                    model=str(selected_model or ""), effort=str(selected_effort or ""))

            reply_text, _ = codex_text(full_prompt, cwd_dir if cwd_dir.is_dir() else friend_dir, ephemeral=True, profile=call_profile)
            return reply_text or ""

        def on_done(result_text):
            if not self.is_generating:
                return
            self.is_generating = False
            self.send_btn.setEnabled(True)
            self.send_btn.setText('发送')
            self.send_btn.setStyleSheet("")
            if hasattr(self, 'working_widget'):
                self.working_widget.setVisible(False)
            card = getattr(self, 'generating_card', None)
            self.generating_card = None
            if card is not None:
                try:
                    card.deleteLater()
                except RuntimeError:
                    # The page may have been torn down while a long model call was in flight.
                    pass

            agent_name = self.get_agent_name()
            # Clean prompt echoes and extract thinking content
            clean_reply, thinking_part = extract_clean_reply(result_text, agent_name)

            if not clean_reply:
                clean_reply = result_text.strip() or "（模型未输出正文内容）"

            a_msg = session_manager.append_message(qq, sid, 'assistant', clean_reply, thinking=thinking_part, ws_id=ws_id)
            if self._generation_started:
                elapsed = max(0.5, time.time() - self._generation_started)
                self._last_tok_s = max(1.0, len(clean_reply) / 2) / elapsed
            self._generation_started = None
            self.refresh_metrics()
            a_card = MessageCard(a_msg, agent_name=agent_name, parent=self.messages_container)
            self.messages_layout.insertWidget(self.messages_layout.count() - 1, a_card)
            self.scroll_to_bottom()
            self.reload_projects_tree(qq)

        def on_error(exc):
            if not self.is_generating:
                return
            self.is_generating = False
            self.send_btn.setEnabled(True)
            self.send_btn.setText('发送')
            self.send_btn.setStyleSheet("")
            if hasattr(self, 'working_widget'):
                self.working_widget.setVisible(False)
            card = getattr(self, 'generating_card', None)
            self.generating_card = None
            if card is not None:
                try:
                    card.deleteLater()
                except RuntimeError:
                    # The page may have been torn down while a long model call was in flight.
                    pass

            agent_name = self.get_agent_name()
            err_msg = session_manager.append_message(qq, sid, 'assistant', f"⚠️ Failed: {exc}", ws_id=ws_id)
            err_card = MessageCard(err_msg, agent_name=agent_name, parent=self.messages_container)
            self.messages_layout.insertWidget(self.messages_layout.count() - 1, err_card)
            self.scroll_to_bottom()
            toast(self.house, "error", str(exc), "Failed")

        run_async(run_call, on_done, on_error, pool=LONG_POOL)

    def send_qq_message(self, qq: str, text: str):
        """Send a real private message to friend via QQ OneBot API and log to FriendStore."""
        if not qq or qq not in self.house.records:
            toast(self.house, 'warning', '请先在好友总览中添加并选择好友。')
            return
        toast(self.house, "info", "正在向对方 QQ 发送消息…")

        def do_send(_):
            import agent_service
            return agent_service.send_friend_message(qq, text)

        def on_send_done(receipt):
            if receipt.get('local_saved'):
                toast(self.house, "success", "QQ 消息已成功发出并同步。")
            else:
                toast(self.house, 'warning', receipt.get('warning', 'QQ 已确认发送，本地同步尚未完成。'))
            if self.house.current_qq == qq:
                self.load_session_ui('__qq_stream__')
                self.reload_projects_tree(qq)

        def failed(exc):
            if self.house.current_qq == qq and not self.input_edit.toPlainText():
                self.input_edit.setPlainText(text)
            toast(self.house, 'error', str(exc), '发送失败或结果未确认')
        run_async(do_send, on_send_done, failed, pool=LONG_POOL)

    def save_conversation_to_memory(self):
        qq = self.house.current_qq
        sid = self.active_session_id
        if not sid or sid == '__qq_stream__':
            toast(self.house, "warning", "请选择普通智能体对话进行记忆提炼。")
            return
        s_data = session_manager.load_session(qq, sid, self.active_ws_id)
        msgs = s_data.get('messages', [])
        if not msgs:
            toast(self.house, "warning", "当前对话尚无记录可沉淀。")
            return

        toast(self.house, "info", "正在将当前会话要点提炼沉淀至长期记忆…")

        def do_extract(_):
            from house_status import ROOT, load_profile
            from main import codex_text
            from store import FriendStore
            friend_dir = ROOT / 'friends' / qq
            profile = load_profile(self.house.profile_path)
            conv_text = "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-10:])
            prompt = (
                f"从以下会话中提炼出关于用户的 1~3 条核心事实或喜好偏好（每条一句话，不超过30字，不包含敏感信息）。"
                f"若无实质信息则返回空。\n\n会话内容：\n{conv_text}"
            )
            summary_facts, _ = codex_text(prompt, friend_dir, ephemeral=True, profile=profile)
            saved_count = 0
            if summary_facts:
                store = FriendStore(friend_dir)
                try:
                    for line in summary_facts.splitlines():
                        line = line.strip().lstrip('-*0123456789. ')
                        if 4 <= len(line) <= 80:
                            store.db.execute("INSERT OR IGNORE INTO memories (body, created_at) VALUES (?, datetime('now'))", (line,))
                            saved_count += 1
                    store.db.commit()
                finally:
                    store.close()
            return saved_count

        def on_done(count):
            toast(self.house, "success", f"已成功保存 {count} 项到长期记忆。")

        run_async(do_extract, on_done, lambda exc: toast(self.house, "error", str(exc)), pool=LONG_POOL)


# Backward compatibility alias
AgentConsoleWorkbench = AgentConsolePage
