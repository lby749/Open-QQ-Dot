import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock, patch

import pytest

import main
from ui_dashboard_data import usage_snapshot


def test_explicit_natural_qq_task_is_persisted_rather_than_inline_chat():
    import task_spec
    import lexicon
    request = '帮我做一个任务：在你的本地工作区创建proof.txt，内容写成OK，完成后实际读取文件。'
    assert task_spec.input_intent(request, 'qq') == 'task'
    assert lexicon.task_category(request) == 'file'
    for message in ('帮我做一个任务是什么意思？', '他说帮我做一个任务：创建文件。',
                    '如果帮我做一个任务：写文件，你会怎么做？', '今天做了一个任务，好累'):
        assert task_spec.input_intent(message, 'qq') == 'chat'


def test_gpu_memory_error_evidence_is_shared_across_chinese_and_english():
    from retrieval_core import diagnostic_terms
    for text in ('显存不够，加载模型失败', 'CUDA out of memory', 'GPU OOM'):
        assert 'cuda_oom' in diagnostic_terms(text)
    for text in ('内存条应该怎么买', '今天的天气很好', 'room service'):
        assert 'cuda_oom' not in diagnostic_terms(text)


def test_installer_reuses_the_existing_release_folder_for_login_state(tmp_path, monkeypatch):
    import windows_installer
    import installer_ui
    executable = tmp_path/'发布验收版'/'Setup.exe'
    old = tmp_path/'Open-QQ-Dot'
    old.mkdir()
    (old/'friends.json').write_text('{}', encoding='utf-8')
    (old/'launcher.py').write_text('# existing installation', encoding='utf-8')
    monkeypatch.setattr(windows_installer.sys, 'executable', str(executable))
    assert windows_installer.default_destination() == old
    assert installer_ui.InstallerApp.default_destination(None) == old


def test_missing_compaction_log_does_not_block_qq_or_clear_session():
    store = Mock()
    store.get.return_value = 'old-session'
    with patch.object(main, 'completed_compactions', side_effect=FileNotFoundError('missing log')), \
         patch.object(main, 'codex_text') as model:
        main.handoff_if_due(store, {'model_api': {'provider': 'codex'}}, Path.cwd())
    store.set.assert_not_called()
    model.assert_not_called()


def test_compaction_log_permission_error_is_not_a_missing_session():
    store = Mock()
    store.get.return_value = 'old-session'
    with patch.object(main, 'completed_compactions', side_effect=PermissionError('denied')):
        with pytest.raises(PermissionError):
            main.handoff_if_due(store, {}, Path.cwd())


def test_invalid_friend_scope_cannot_fall_back_to_house_usage(tmp_path):
    directory = tmp_path/'friends/12345'
    directory.mkdir(parents=True)
    with sqlite3.connect(directory/'history.sqlite3') as db:
        db.execute('CREATE TABLE usage_calls(seq INTEGER, model TEXT, usage TEXT)')
        db.execute('INSERT INTO usage_calls VALUES(1,?,?)', ('private-model', json.dumps(
            {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 10})))
    assert usage_snapshot(tmp_path/'data')['calls'] == 1
    assert usage_snapshot(tmp_path/'data', '12345')['calls'] == 1
    for invalid in ('', '../12345', 'invalid', 12345):
        result = usage_snapshot(tmp_path/'data', invalid)
        assert result['calls'] == 0
        assert result['by_friend'] == result['by_model'] == []
