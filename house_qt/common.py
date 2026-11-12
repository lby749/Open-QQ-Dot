"""Shared pieces of the Qt panel: background jobs, toasts, cards, rows and simple dialogs."""
from ui_locale import tr

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from shiboken6 import isValid
from qfluentwidgets import (BodyLabel, CaptionLabel, InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            MessageBoxBase, PlainTextEdit, PushButton, ScrollArea, SimpleCardWidget,
                            StrongBodyLabel, SubtitleLabel)
from qfluentwidgets import SegmentedWidget
from qfluentwidgets.common.style_sheet import StyleSheetManager


def _style_items_snapshot(self):
    # QObject destruction deregisters widgets synchronously, including during GC.
    # Copy the underlying dict before dereferencing keys; WeakKeyDictionary.items()
    # can otherwise be resized by that signal while a theme update iterates it.
    return [(widget, source) for ref, source in self.widgets.data.copy().items()
            if (widget := ref()) is not None and isValid(widget)]


StyleSheetManager.items = _style_items_snapshot

ACCENT = '#2563EB'  # blue/white release palette
GREEN, RED, MUTED = '#3E8E5E', '#C0533A', '#64748B'


class SectionSwitcher(SegmentedWidget):
    """One compact appearance for every section switch, preserving Fluent routing."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.hBoxLayout.setContentsMargins(4, 4, 4, 4)
        self.hBoxLayout.setSpacing(4)
        self.setStyleSheet('''SegmentedItem {background:transparent;border:none;
            padding:4px 14px;color:#61728A;}
            SegmentedItem:hover {color:#1D4ED8;}
            SegmentedItem[isSelected=true] {color:#1D4ED8;font-weight:600;}''')

    def addItem(self, routeKey, text, onClick=None, icon=None):
        item = super().addItem(routeKey, text, onClick, icon)
        if item is not None:
            item.setFixedHeight(36)
            item.ensurePolished()
            font = item.font()
            font.setBold(True)  # Selected labels are heavier than the container font.
            item.setMinimumWidth(QFontMetrics(font).horizontalAdvance(text)+36)
            self.hBoxLayout.setStretchFactor(item, 0)
            self.setMaximumWidth(sum(w.minimumWidth() for w in self.items.values()) +
                                 4*max(0, len(self.items)-1)+8)
        return item

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor('#DCE7F5'))
        painter.setBrush(QColor('#F1F5FC'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 9, 9)
        item = self.currentItem()
        if item is not None:
            painter.setPen(QColor('#D2E1FA'))
            painter.setBrush(QColor('#FFFFFF'))
            painter.drawRoundedRect(QRectF(float(self.slideAni.value()), 4, item.width(), 36), 7, 7)


class _Invoker(QObject):
    invoke = Signal(object)

    def __init__(self):
        super().__init__()
        self.invoke.connect(lambda fn: fn(), Qt.QueuedConnection)


_invoker = None
# Friend database reads/writes queue on one thread: source snapshots check with a zero lock timeout, so two
# panel threads touching the same friend database at once failed with "database is locked".
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='house-db')
LONG_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='house-long')  # model calls, network, file builds


def init_ui_thread():
    """Call once on the UI thread after QApplication exists."""
    global _invoker
    _invoker = _Invoker()


def on_ui(fn):
    """Run fn on the UI thread (callable from any thread)."""
    _invoker.invoke.emit(fn)


def run_async(job, done=None, error=None, progress=None, pool=POOL):
    """job(progress) runs off the UI thread; done(value), error(exc) and progress(text) run on the UI thread."""
    def report(text):
        if progress is not None:
            on_ui(lambda text=text: progress(text))

    def work():
        try:
            value = job(report)
        except Exception as exc:  # delivered to the UI; nothing is swallowed
            on_ui(lambda failure=exc: (error or _unhandled)(failure))
        else:
            if done is not None:
                on_ui(lambda value=value: done(value))
    return pool.submit(work)


def _unhandled(exc):
    toast(None, 'error', str(exc))


def quiet(_exc):
    """For automatic refreshes: a failed read is retried by the next status check, no popup."""


_window = None


def set_window(window):
    global _window
    _window = window


def toast(parent, kind, text, title=''):
    """Short feedback at the top right of the window: kind is success / info / warning / error."""
    parent = parent or _window
    show = {'success': InfoBar.success, 'info': InfoBar.info, 'warning': InfoBar.warning, 'error': InfoBar.error}[kind]
    show(title=tr(title), content=tr(text), orient=Qt.Horizontal, isClosable=True, position=InfoBarPosition.TOP_RIGHT,
         duration=6000 if kind in ('warning', 'error') else 2500, parent=parent)


def notify(parent, title, text):
    """Desktop notification for work that completed while the window was in the background."""
    win = parent.window() if parent is not None else _window
    tray = getattr(win, 'tray', None) if win is not None else None
    if tray is not None:
        try:
            tray.showMessage(str(title), str(text))
        except Exception:
            pass
    try:
        toast(parent, 'success', text, title)
    except Exception:
        pass


def confirm(parent, title, text, yes='确定', no='Cancel'):
    box = MessageBox(tr(title), tr(text), parent or _window)
    box.yesButton.setText(tr(yes))
    box.cancelButton.setText(tr(no))
    return bool(box.exec())


def notice(parent, title, text):
    box = MessageBox(tr(title), tr(text), parent or _window)
    box.yesButton.setText('知道了')
    box.cancelButton.hide()
    box.exec()


class TextDialog(MessageBoxBase):
    """Read-only text in a large modal panel (prompt inputs, memory sources, usage)."""

    def __init__(self, parent, title, text, caption=''):
        super().__init__(parent or _window)
        self.viewLayout.addWidget(SubtitleLabel(tr(title)))
        if caption:
            self.viewLayout.addWidget(hint(caption))
        self.body = PlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setPlainText(text)
        self.body.setMinimumSize(760, 480)
        self.viewLayout.addWidget(self.body)
        self.yesButton.setText(tr('Close'))
        self.cancelButton.hide()


class InputDialog(MessageBoxBase):
    def __init__(self, parent, title, label):
        super().__init__(parent or _window)
        self.viewLayout.addWidget(SubtitleLabel(tr(title)))
        self.viewLayout.addWidget(BodyLabel(label))
        self.edit = LineEdit()
        self.edit.setMinimumWidth(360)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText('确定')
        self.cancelButton.setText(tr('Cancel'))


def hint(text):
    label = CaptionLabel(tr(text))
    label.setWordWrap(True)
    label.setTextColor(MUTED, '#94A3B8')
    return label


def body(text=''):
    label = BodyLabel(tr(text))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def row(*widgets, stretch_at=None, spacing=8):
    """A horizontal line of widgets; None inserts the stretch."""
    box = QHBoxLayout()
    box.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            box.addStretch(1)
        elif isinstance(widget, QHBoxLayout):
            box.addLayout(widget)
        else:
            box.addWidget(widget)
    return box


class Card(SimpleCardWidget):
    """A titled card; add widgets/layouts with add().

    2026-10-26 user: some pages stacked every section open (the model page had sixteen). A card can now
    fold down to its title bar — `collapsible` adds the button, `collapsed` starts folded — so a long page
    reads as a list of sections and the user opens only what they came for.
    """

    def __init__(self, title='', parent=None, actions=(), collapsible=True, collapsed=False):
        super().__init__(parent)
        title = tr(title)
        self.setStyleSheet('Card { background: #FFFFFF; border: 1px solid #DBEAFE; border-radius: 8px; }')
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(22, 18, 22, 18)
        self.layout_.setSpacing(10)
        self.collapsed = False
        self._fold_button = None
        self._actions = list(actions)
        if title or actions or collapsible:
            head = QHBoxLayout()
            if title:
                head.addWidget(StrongBodyLabel(title) if len(title) > 18 else SubtitleLabel(title))
            head.addStretch(1)
            for action in self._actions:
                head.addWidget(action)
            if collapsible:
                self._fold_button = PushButton('收起')
                self._fold_button.setFixedWidth(64)
                self._fold_button.clicked.connect(self.toggle)
                head.addWidget(self._fold_button)
            self.layout_.addLayout(head)
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        self.layout_.addWidget(self.body)
        if collapsed:
            self.set_collapsed(True)

    def add(self, *items):
        for item in items:
            if isinstance(item, QHBoxLayout | QVBoxLayout):
                self.body_layout.addLayout(item)
            else:
                self.body_layout.addWidget(item)
        return self

    def toggle(self):
        self.set_collapsed(not self.collapsed)

    def set_collapsed(self, collapsed):
        self.collapsed = bool(collapsed)
        self.body.setVisible(not self.collapsed)
        # A folded card hides the buttons that act on the hidden fields, so nothing looks clickable but inert.
        for action in self._actions:
            action.setVisible(not self.collapsed)
        if self._fold_button is not None:
            self._fold_button.setText('展开' if self.collapsed else '收起')


class Page(ScrollArea):
    """A scrollable page of cards. Friend pages implement load() (friend changed) and shown() (navigated to)."""

    def __init__(self, house, name):
        super().__init__()
        self.house = house
        self.setObjectName(name)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.content = QWidget()
        self.content.setObjectName('pageContent')
        self.column = QVBoxLayout(self.content)
        self.column.setContentsMargins(28, 12, 28, 28)
        self.column.setSpacing(14)
        self.column.setAlignment(Qt.AlignTop)
        self.setWidget(self.content)
        self.setStyleSheet('Page, #pageContent { background: #EFF6FF; border: none; }')
        self.loaded_for = None

    def add(self, widget):
        self.column.addWidget(widget)
        return widget

    def load(self):
        """Fill the page for house.current_qq."""

    def shown(self):
        if self.loaded_for != self.house.current_qq:
            self.loaded_for = self.house.current_qq
            self.load()

    def on_status(self):
        """Periodic refresh while visible (every bridge status check)."""


def set_text_keep(edit, text):
    """Replace a read-only text box's content only when it changed (keeps scroll position otherwise)."""
    if edit.toPlainText() != text:
        edit.setPlainText(text)
