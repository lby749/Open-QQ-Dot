"""Qt destruction may deregister a sibling while Fluent snapshots its style registry."""
import os
import weakref

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import delete, isValid
from qfluentwidgets.common.style_sheet import FluentStyleSheet, StyleSheetManager
import house_qt.common


def test_style_snapshot_survives_widget_destruction_during_lookup():
    app = QApplication.instance() or QApplication([])
    manager = StyleSheetManager()
    first, sibling = QWidget(), QWidget()
    manager.register(FluentStyleSheet.BUTTON, first)
    manager.register(FluentStyleSheet.BUTTON, sibling)
    first_ref = weakref.ref(first)
    source = manager.widgets.data.pop(first_ref)
    # Put the first widget first, so its lookup destroys another registered widget.
    sibling_ref = weakref.ref(sibling)
    sibling_source = manager.widgets.data.pop(sibling_ref)
    class DeleteSiblingRef(weakref.ref):
        def __call__(self):
            if isValid(sibling):
                delete(sibling)
            return super().__call__()
    manager.widgets.data[DeleteSiblingRef(first)] = source
    manager.widgets.data[sibling_ref] = sibling_source
    try:
        items = list(manager.items())
        assert items == [(first, source)]
    finally:
        if isValid(first):
            delete(first)
        if isValid(sibling):
            delete(sibling)
        app.processEvents()
