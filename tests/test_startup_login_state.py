import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QWidget
from house_qt.dotlink import DotLinkPage
from house_qt.window import HouseWindow


def test_running_runtime_is_not_a_logged_in_account():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    with patch('house_qt.dotlink.run_async'):
        page = DotLinkPage(parent)
    try:
        page.apply({'running': True, 'login': {'is_login': False}})
        assert '等待登录' in page.status.text()
        assert '已登录' not in page.status.text()
        page.apply({'running': True, 'login': {'is_login': True, 'uin': '12345'}})
        assert '已登录' in page.status.text()
    finally:
        page.login_timer.stop()
        page.deleteLater()
        parent.deleteLater()
        app.sendPostedEvents()


def test_uncached_existing_login_keeps_the_homepage_and_does_not_login_again(monkeypatch):
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    page = SimpleNamespace(state={}, apply=Mock(), begin=Mock())
    home = object()
    house = SimpleNamespace(closing=False, exiting=False, pages={'dotlinkPage': page},
        stackedWidget=SimpleNamespace(currentWidget=lambda: home), switchTo=Mock())
    def immediate(job, done, error):
        done(job(None))
    with patch('dotlink.state', return_value={'running': True, 'login': {'is_login': True}}), \
         patch('house_settings.load', return_value={'qq_connection_mode': 'legacy'}), \
         patch('house_qt.window.run_async', side_effect=immediate):
        HouseWindow.auto_open_dotlink(house)
    page.apply.assert_called_once()
    house.switchTo.assert_not_called()
    page.begin.assert_not_called()


def test_official_default_does_not_start_personal_account_login(monkeypatch):
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    house = SimpleNamespace()
    with patch('house_settings.load', return_value={}), patch('dotlink.state') as state:
        HouseWindow.auto_open_dotlink(house)
    state.assert_not_called()
