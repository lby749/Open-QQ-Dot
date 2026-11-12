"""OpenAI Dots & Muse style consolidated pages for Open-QQ-Dot.

Compresses the 12 fragmented legacy pages into 3 high-level multi-tab workstations:
1. AgentProfilePage (Standing Instructions, Persona, Interaction Style, User Context)
2. KnowledgeMemoryPage (Second Brain, Persistent Memory, Deep Web Research)
3. AuditLogPage (Chat History, Context & Tool Calls Audit, Activity Logs)
"""
from ui_locale import tr

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget
from .common import SectionSwitcher

from .pages_friend import BasicPage, StylePage, TopicPage
from .pages_library import ContextPage, StickerPage, TemplatePage
from .pages_more import EmotionPage, HistoryPage, MemoryPage


class AgentProfilePage(QWidget):
    """Consolidated page for Agent Profile & Standing Instructions."""
    def __init__(self, house):
        super().__init__()
        self.house = house
        self.setObjectName('profilePage')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 18)
        layout.setSpacing(10)

        self.seg = SectionSwitcher(self)
        self.stacked = QStackedWidget(self)

        self.template_page = TemplatePage(house)
        self.style_page = StylePage(house)
        self.basic_page = BasicPage(house)
        self.sticker_page = StickerPage(house)

        self.stacked.addWidget(self.template_page)
        self.stacked.addWidget(self.style_page)
        self.stacked.addWidget(self.basic_page)
        self.stacked.addWidget(self.sticker_page)

        self._tab_mapping = {
            'template': self.template_page,
            'style': self.style_page,
            'basic': self.basic_page,
            'stickers': self.sticker_page,
        }

        self.seg.addItem('template', tr('常驻指令与角色 (Standing Goals)'), onClick=lambda: self.switch_tab(self.template_page))
        self.seg.addItem('style', tr('交互与回复偏好 (Preferences)'), onClick=lambda: self.switch_tab(self.style_page))
        self.seg.addItem('basic', tr('用户画像与背景 (User Context)'), onClick=lambda: self.switch_tab(self.basic_page))
        self.seg.addItem('stickers', tr('Stickers & Multimodal'), onClick=lambda: self.switch_tab(self.sticker_page))
        self.seg.setCurrentItem('template')

        layout.addWidget(self.seg)
        layout.addWidget(self.stacked)
        self.loaded_for = None

    def switch_tab(self, widget):
        self.stacked.setCurrentWidget(widget)
        for key, w in self._tab_mapping.items():
            if w == widget:
                self.seg.setCurrentItem(key)
                break
        if hasattr(widget, 'shown'):
            widget.shown()

    def select_tab(self, key):
        if key in self._tab_mapping:
            self.seg.setCurrentItem(key)
            self.switch_tab(self._tab_mapping[key])

    def load(self):
        self.template_page.load()
        self.style_page.load()
        self.basic_page.load()
        self.sticker_page.load()
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    def shown(self):
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    on_status = shown

    def __getattr__(self, name):
        if 'template_page' in self.__dict__:
            return getattr(self.template_page, name)
        raise AttributeError(name)



class KnowledgeMemoryPage(QWidget):
    """Consolidated page for Second Brain, Persistent Memory & Deep Research."""
    def __init__(self, house):
        super().__init__()
        self.house = house
        self.setObjectName('memoryPage')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 18)
        layout.setSpacing(10)

        self.seg = SectionSwitcher(self)
        self.stacked = QStackedWidget(self)

        self.memory_page = MemoryPage(house)
        self.topic_page = TopicPage(house)

        self.stacked.addWidget(self.memory_page)
        self.stacked.addWidget(self.topic_page)

        self._tab_mapping = {'memory': self.memory_page, 'topic': self.topic_page}

        self.seg.addItem('memory', tr('长期事实与记忆 (Persistent Memory)'), onClick=lambda: self.switch_tab(self.memory_page))
        self.seg.addItem('topic', tr('深度研究与资料库 (Knowledge & Topics)'), onClick=lambda: self.switch_tab(self.topic_page))
        self.seg.setCurrentItem('memory')

        layout.addWidget(self.seg)
        layout.addWidget(self.stacked)
        self.loaded_for = None

    def switch_tab(self, widget):
        self.stacked.setCurrentWidget(widget)
        for key, w in self._tab_mapping.items():
            if w == widget:
                self.seg.setCurrentItem(key)
                break
        if hasattr(widget, 'shown'):
            widget.shown()

    def select_tab(self, key):
        if key in self._tab_mapping:
            self.seg.setCurrentItem(key)
            self.switch_tab(self._tab_mapping[key])

    def unsaved(self):
        return self.memory_page.unsaved()

    def load(self):
        self.memory_page.load()
        self.topic_page.load()
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    def shown(self):
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    on_status = shown

    def __getattr__(self, name):
        if 'memory_page' in self.__dict__:
            return getattr(self.memory_page, name)
        raise AttributeError(name)


class AuditLogPage(QWidget):
    """Consolidated page for Chat History, Context Audit & Activity logs."""
    def __init__(self, house):
        super().__init__()
        self.house = house
        self.setObjectName('historyPage')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 18)
        layout.setSpacing(10)

        self.seg = SectionSwitcher(self)
        self.stacked = QStackedWidget(self)

        self.history_page = HistoryPage(house)
        self.context_page = ContextPage(house)
        self.emotion_page = EmotionPage(house)

        self.stacked.addWidget(self.history_page)
        self.stacked.addWidget(self.context_page)
        self.stacked.addWidget(self.emotion_page)

        self._tab_mapping = {'history': self.history_page, 'context': self.context_page, 'emotion': self.emotion_page}

        self.seg.addItem('history', tr('会话历史记录 (Chat History)'), onClick=lambda: self.switch_tab(self.history_page))
        self.seg.addItem('context', tr('本轮输入与推理审计 (Context Audit)'), onClick=lambda: self.switch_tab(self.context_page))
        self.seg.addItem('emotion', tr('活动与心情日志 (Activity & Emotion)'), onClick=lambda: self.switch_tab(self.emotion_page))
        self.seg.setCurrentItem('history')

        layout.addWidget(self.seg)
        layout.addWidget(self.stacked)
        self.loaded_for = None

    def switch_tab(self, widget):
        self.stacked.setCurrentWidget(widget)
        for key, w in self._tab_mapping.items():
            if w == widget:
                self.seg.setCurrentItem(key)
                break
        if hasattr(widget, 'shown'):
            widget.shown()

    def select_tab(self, key):
        if key in self._tab_mapping:
            self.seg.setCurrentItem(key)
            self.switch_tab(self._tab_mapping[key])

    def load(self):
        self.history_page.load()
        self.context_page.load()
        self.emotion_page.load()
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    def shown(self):
        current = self.stacked.currentWidget()
        if hasattr(current, 'shown'):
            current.shown()

    on_status = shown

    def __getattr__(self, name):
        if 'history_page' in self.__dict__:
            return getattr(self.history_page, name)
        raise AttributeError(name)

