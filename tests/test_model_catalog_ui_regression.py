"""UI response contracts; live tool/account checks remain separate acceptance evidence."""
import json
import os

import pytest


@pytest.fixture
def model_page(tmp_path, monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QMainWindow
    from house_qt import pages_friend
    from house_qt.common import init_ui_thread
    import codex_settings
    app = QApplication.instance() or QApplication([])
    init_ui_thread()
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.setattr(pages_friend, 'ROOT', tmp_path)
    monkeypatch.setattr(codex_settings, 'model_options', lambda *args: {'configured-model': ['low']})
    monkeypatch.setattr(pages_friend, 'toast', lambda *args: None)
    directory = tmp_path/'friends/12345'
    directory.mkdir(parents=True)
    profile = directory/'friend.json'
    profile.write_text(json.dumps({'model_api': {'provider': 'codex'}}), encoding='utf-8')
    house = QMainWindow()
    house.friend_dir, house.profile_path = directory, profile
    house.current_qq, house.records = '12345', {}
    page = pages_friend.ModelPage(house)
    house.setCentralWidget(page)
    page.load()
    yield page, house
    house.close()
    house.deleteLater()
    app.processEvents()


def immediate_query(job, done=None, error=None, **kwargs):
    try:
        result = job(None)
    except Exception as exc:
        if error:
            error(exc)
    else:
        if done:
            done(result)


def test_install_button_does_not_start_duplicate_jobs_and_can_retry(model_page, monkeypatch):
    from house_qt import pages_friend
    page, _ = model_page
    pending = []
    monkeypatch.setattr(pages_friend, 'run_async', lambda job, done, error, **kwargs: pending.append((done,error)))
    page.prepare_agent('codex')
    page.prepare_agent('codex')
    assert len(pending) == 1
    pending[0][1](RuntimeError('离线'))
    page.prepare_agent('codex')
    assert len(pending) == 2


def test_dsh_button_uses_the_tool_default_and_selected_models_own_efforts(model_page, monkeypatch):
    import dsh_backend
    from house_qt import pages_friend
    page, _ = model_page
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *args: 'synthetic-dsh')
    monkeypatch.setattr(dsh_backend, 'fetch_model_catalog', lambda *args, **kwargs: {
        'models': {'small-from-tool': ['low'], 'deep-from-tool': ['high']},
        'default_model': 'deep-from-tool', 'source': 'synthetic tool catalogue'})
    monkeypatch.setattr(pages_friend, 'run_async', immediate_query)
    page.fetch_dsh_models()
    assert [page.dsh_model.itemText(i) for i in range(page.dsh_model.count())] == ['small-from-tool', 'deep-from-tool']
    assert page.dsh_model.currentText() == 'deep-from-tool'
    assert [page.dsh_effort.itemData(i) for i in range(page.dsh_effort.count())] == ['', 'high']
    page.dsh_model.setCurrentText('small-from-tool')
    assert [page.dsh_effort.itemData(i) for i in range(page.dsh_effort.count())] == ['', 'low']


def test_old_friend_model_response_and_error_cannot_update_the_current_friend(model_page, tmp_path, monkeypatch):
    import dsh_backend
    from house_qt import pages_friend
    page, house = model_page
    callbacks, messages = {}, []
    monkeypatch.setattr(dsh_backend, 'cli_path', lambda *args: 'synthetic-dsh')
    monkeypatch.setattr(pages_friend, 'run_async', lambda job, **kwargs: callbacks.update(kwargs))
    monkeypatch.setattr(pages_friend, 'toast', lambda *args: messages.append(args))
    page.fetch_dsh_models()
    done, error = callbacks['done'], callbacks['error']
    directory = tmp_path/'friends/67890'
    directory.mkdir()
    (directory/'friend.json').write_text(json.dumps({'model_api': {'provider': 'codex',
        'dsh_settings': {'model': 'current-friend-model'}}}), encoding='utf-8')
    house.friend_dir, house.profile_path, house.current_qq = directory, directory/'friend.json', '67890'
    page.load()
    messages.clear()
    done({'models': {'old-friend-model': ['high']}, 'source': 'old source'})
    error(RuntimeError('old friend connection failed'))
    assert page.dsh_model.currentText() == 'current-friend-model'
    assert page.dsh_catalog == {} and not messages


def test_api_button_populates_selectable_models_instead_of_only_showing_a_toast(model_page, monkeypatch):
    from house_qt import pages_friend
    from qfluentwidgets import EditableComboBox
    page, _ = model_page
    page.key.setText('synthetic-api-key')
    page.base_url.setText('https://example.invalid/v1')
    monkeypatch.setattr(pages_friend, 'discover_models', lambda *args: ['listed-one', 'listed-two'])
    monkeypatch.setattr(pages_friend, 'run_async', immediate_query)
    page.fetch_api_models()
    assert isinstance(page.deepseek_model, EditableComboBox)
    assert [page.deepseek_model.itemText(i) for i in range(page.deepseek_model.count())] == ['listed-one', 'listed-two']
    page.deepseek_model.setCurrentIndex(1)
    assert page.deepseek_model.text() == 'listed-two'


def test_changed_api_connection_rejects_the_previous_catalogue(model_page, monkeypatch):
    from house_qt import pages_friend
    page, _ = model_page
    page.key.setText('synthetic-api-key')
    page.base_url.setText('https://one.invalid/v1')
    callbacks = {}
    monkeypatch.setattr(pages_friend, 'run_async', lambda job, **kwargs: callbacks.update(kwargs))
    page.fetch_api_models()
    page.base_url.setText('https://two.invalid/v1')
    page.deepseek_model.setText('current-manual-model')
    callbacks['done'](['previous-account-model'])
    assert page.deepseek_model.text() == 'current-manual-model'
    assert 'previous-account-model' not in [page.deepseek_model.itemText(i) for i in range(page.deepseek_model.count())]


def test_antigravity_model_selection_does_not_advertise_conflicting_efforts(model_page, monkeypatch):
    import antigravity_backend
    from house_qt import pages_friend
    page, _ = model_page
    monkeypatch.setattr(antigravity_backend, 'fetch_available_models', lambda *args: [
        ('gemini-test-high', 'High'), ('gemini-test-low', 'Low')])
    monkeypatch.setattr(pages_friend, 'run_async', immediate_query)
    page.fetch_antigravity_models()
    assert [page.ag_effort.itemData(i) for i in range(page.ag_effort.count())] == ['high']
    page.ag_model.setCurrentText('gemini-test-low')
    assert [page.ag_effort.itemData(i) for i in range(page.ag_effort.count())] == ['low']
    assert page.selected_antigravity_effort() == 'low'
