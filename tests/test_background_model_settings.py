"""Real Qt/file configuration checks; no model, CLI, QQ or download calls."""
import json
import os
import subprocess

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

import codex_settings
from house_qt.background_model_settings import BackgroundModelSettings
from house_qt.pages_friend import ModelPage


@pytest.fixture
def settings(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'fast': ['low'], 'deep': ['high']})
    def no_process(*args, **kwargs):
        raise AssertionError('读取设置不能发起 CLI、模型或下载进程')
    monkeypatch.setattr(subprocess, 'Popen', no_process)
    profile = {'qq_id': '12345', 'style': '保留原人设', 'other_setting': {'keep': True},
               'model_api': {'provider': 'codex', 'codex_model': 'fast', 'codex_effort': 'low',
                             'quota_reply': '原回复', 'dsh_settings': {'model': 'DeepSeek-V41-Flash', 'effort': 'low'}},
               'agent_adapters': {'opencode': {'model': 'go/flash', 'variant': 'low',
                    'model_options': {'go/flash': ['low', 'high'], 'go/pro': ['high']}},
                    'zcode': {'model': 'configured-zcode-model'}}}
    path = tmp_path / '12345' / 'friend.json'
    path.parent.mkdir()
    path.write_text(json.dumps(profile, ensure_ascii=False), 'utf-8')
    house = QMainWindow()
    house.current_qq, house.profile_path, house.friend_dir = '12345', path, path.parent
    panel = BackgroundModelSettings(house, ModelPage.PROVIDER_TITLES)
    house.setCentralWidget(panel)
    house.resize(420, 700)
    panel.set_collapsed(False)
    panel.load()
    house.show()
    app.processEvents()
    yield app, house, panel, path
    house.close()
    app.processEvents()


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


def test_background_choices_reuse_each_backend_and_keep_unknown_efforts_disabled(settings):
    _, _, panel, _ = settings
    assert panel.provider.currentData() == ''
    assert not panel.model.isEnabled() and not panel.effort.isEnabled()
    choose(panel.provider, 'codex')
    choose(panel.model, 'deep')
    assert panel.effort.currentData() == 'high'
    choose(panel.provider, 'dsh')
    assert panel.model.findData('DeepSeek-V41-Flash') >= 0
    assert panel.effort.findData('max') == -1
    choose(panel.provider, 'opencode')
    assert panel.model.findData('go/pro') >= 0
    choose(panel.model, 'go/pro')
    assert panel.effort.currentData() == 'high'
    choose(panel.provider, 'zcode')
    assert panel.model.count() == 1 and panel.model.currentData() == 'configured-zcode-model'
    assert not panel.effort.isEnabled() and panel.effort.currentData() == ''


def test_save_changes_only_this_friends_two_settings_and_preserves_literal_reply(settings):
    _, _, panel, path = settings
    choose(panel.provider, 'opencode')
    choose(panel.effort, 'high')
    text = '额度用完了。\n{task_id} 按原文保留。'
    panel.quota_reply.setPlainText(text)
    fresh = json.loads(path.read_text('utf-8'))
    fresh['other_setting']['added_elsewhere'] = True
    path.write_text(json.dumps(fresh, ensure_ascii=False), 'utf-8')
    assert panel.save()
    saved = json.loads(path.read_text('utf-8'))
    assert saved['background_model'] == {'provider': 'opencode', 'model': 'go/flash', 'effort': 'high'}
    assert saved['model_api']['quota_reply'] == text
    assert saved['model_api']['provider'] == 'codex' and saved['model_api']['codex_model'] == 'fast'
    assert saved['agent_adapters'] == fresh['agent_adapters']
    assert saved['other_setting'] == fresh['other_setting'] and saved['style'] == fresh['style']


def test_inherit_and_blank_reply_are_saved_without_automatic_model_selection(settings):
    _, _, panel, path = settings
    panel.quota_reply.setPlainText(' \n\t ')
    assert panel.save()
    saved = json.loads(path.read_text('utf-8'))
    assert saved['background_model'] == {} and saved['model_api']['quota_reply'] == ''
    assert saved['model_api']['codex_model'] == 'fast'


def test_switching_friends_clears_draft_and_late_save_never_writes_either_profile(settings, tmp_path):
    _, house, panel, original = settings
    choose(panel.provider, 'codex')
    choose(panel.model, 'deep')
    panel.quota_reply.setPlainText('旧好友未保存的私密草稿')
    second = tmp_path / '67890' / 'friend.json'
    second.parent.mkdir()
    second.write_text(json.dumps({'qq_id': '67890', 'model_api': {'quota_reply': '新好友的回复'}}, ensure_ascii=False), 'utf-8')
    before = original.read_bytes(), second.read_bytes()
    house.current_qq, house.profile_path, house.friend_dir = '67890', second, second.parent
    assert not panel.save()
    assert (original.read_bytes(), second.read_bytes()) == before
    assert panel.quota_reply.toPlainText() == '' and panel.model.count() == 0
    panel.load()
    assert panel.provider.currentData() == '' and panel.quota_reply.toPlainText() == '新好友的回复'


def test_refresh_uses_saved_configuration_without_discarding_quota_draft(settings):
    _, _, panel, path = settings
    choose(panel.provider, 'opencode')
    panel.quota_reply.setPlainText('未保存的原文')
    fresh = json.loads(path.read_text('utf-8'))
    fresh['agent_adapters']['opencode']['model_options']['go/new'] = ['medium']
    path.write_text(json.dumps(fresh), 'utf-8')
    panel.refresh_choices()
    assert panel.model.findData('go/new') >= 0 and panel.quota_reply.toPlainText() == '未保存的原文'


def test_invalid_model_or_effort_does_not_overwrite_existing_settings(settings):
    _, _, panel, path = settings
    choose(panel.provider, 'codex')
    panel.effort.addItem('不支持的强度', userData='unsupported')
    choose(panel.effort, 'unsupported')
    before = path.read_bytes()
    assert not panel.save() and path.read_bytes() == before
    assert '保存未完成' in panel.status.text()


def test_narrow_card_keeps_editable_controls_inside_the_visible_body(settings):
    app, house, panel, _ = settings
    house.resize(360, 720)
    app.processEvents()
    assert panel.minimumSizeHint().width() <= 360
    for field in (panel.provider, panel.model, panel.effort, panel.quota_reply, panel.refresh_button):
        assert field.isVisible()
        assert field.mapTo(panel, field.rect().topRight()).x() <= panel.width()


def test_one_malformed_backend_does_not_blank_all_background_settings(settings):
    _, _, panel, path = settings
    fresh = json.loads(path.read_text('utf-8'))
    fresh['agent_adapters']['opencode']['model_options'] = {'go/invalid': None}
    path.write_text(json.dumps(fresh), 'utf-8')
    panel.load()
    choose(panel.provider, 'opencode')
    assert not panel.model.isEnabled() and '不可用' in panel.status.text()
    choose(panel.provider, 'codex')
    choose(panel.model, 'deep')
    assert panel.save()


def test_source_conflict_is_reported_without_internal_error_or_partial_write(settings, monkeypatch):
    from house_qt import background_model_settings
    from persona_store import Conflict
    _, _, panel, path = settings
    before = path.read_bytes()
    def conflicting(*args):
        raise Conflict('source snapshot changed or legacy token is missing')
    monkeypatch.setattr(background_model_settings, 'write_profile', conflicting)
    assert not panel.save() and path.read_bytes() == before
    assert '保存未完成' in panel.status.text() and 'legacy token' not in panel.status.text()


def test_quota_reply_limit_refuses_overlong_text_without_cropping_or_writing(settings):
    _, _, panel, path = settings
    before = path.read_bytes()
    panel.quota_reply.setPlainText('字' * 1201)
    assert not panel.save() and path.read_bytes() == before
    assert len(panel.quota_reply.toPlainText()) == 1201 and '最多 1200 字' in panel.status.text()
    panel.quota_reply.setPlainText('字' * 1200)
    assert panel.save()
    assert json.loads(path.read_text('utf-8'))['model_api']['quota_reply'] == '字' * 1200
