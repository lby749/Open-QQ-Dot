"""First-run guide: shown once, and the flag it leaves behind."""
from __future__ import annotations

import json

import pytest

from house_qt import first_run


def test_guide_shows_only_until_it_is_marked_done(tmp_path):
    assert first_run.should_show(root=tmp_path) is True
    first_run.mark_done(root=tmp_path)
    assert first_run.should_show(root=tmp_path) is False


def test_existing_setting_that_is_not_a_bool_does_not_break(tmp_path):
    (tmp_path / 'house.local.json').write_text('{"onboarding_done": "yes"}', 'utf-8')
    assert first_run.should_show(root=tmp_path) is False


def test_mark_done_keeps_other_settings(tmp_path):
    (tmp_path / 'house.local.json').write_text('{"inference_device": "cuda"}', 'utf-8')
    first_run.mark_done(root=tmp_path)
    saved = json.loads((tmp_path / 'house.local.json').read_text('utf-8'))
    assert saved['inference_device'] == 'cuda'
    assert saved['onboarding_done'] is True


def test_steps_cover_the_four_first_actions():
    assert [step[1] for step in first_run.STEPS] == [
        '连接 QQ', '添加好友', '选智能体', '本地模型按需下载']


@pytest.fixture(scope='module')
def qt_app():
    pytest.importorskip('PySide6.QtWidgets')
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def test_dialog_marks_the_house_when_accepted(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    guide = first_run.FirstRunGuide()
    assert guide.windowTitle() == '开始使用 Open QQ Dot'
    guide.accept()
    assert first_run.should_show(root=tmp_path) is False
    guide.deleteLater()
