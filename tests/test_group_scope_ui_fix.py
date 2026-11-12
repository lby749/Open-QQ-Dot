import json
import os
import sys
import time
import threading
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QWidget
from house_qt.agent_group import AgentGroupPage
import agent_group
import pytest


@pytest.mark.parametrize('switch_friend', [True, False])
def test_switch_friend_and_room_during_running_job(tmp_path, monkeypatch, switch_friend):
    app = QApplication.instance() or QApplication([])
    house = QWidget()
    house.current_qq = '123'
    for owner in ('123', '456'):
        directory = tmp_path/'friends'/owner
        directory.mkdir(parents=True)
        (directory/'friend.json').write_text(json.dumps({'qq_id': owner, 'model_api': {'provider': 'codex'}}), 'utf-8')
    house.friend_dir = tmp_path/'friends/123'
    house.profile_path = house.friend_dir/'friend.json'
    monkeypatch.setattr(AgentGroupPage, '_base', lambda self: tmp_path)
    room_a = agent_group.create_room(tmp_path, '123', 'A')
    second_owner = '456' if switch_friend else '123'
    room_b = agent_group.create_room(tmp_path, second_owner, 'B')
    for owner, room in [('123', room_a), (second_owner, room_b)]:
        agent_group.add_member(tmp_path, owner, room['id'], 'codex', '成员', model='chosen-codex')
    page = AgentGroupPage(house)
    page.open_room(room_a['id'])
    submitted = []
    monkeypatch.setattr('house_qt.agent_group.run_async', lambda job, done, failed, **kw: submitted.append((job, done)))
    page.input.setText('A任务')
    page.send()
    page.input.setText('A排队消息')
    page.send()
    assert len(page.pending) == 1
    # Real widgets switch before the delayed worker reads its profile and memory.
    if switch_friend:
        house.current_qq = '456'
        house.friend_dir = tmp_path/'friends/456'
        house.profile_path = house.friend_dir/'friend.json'
        page.refresh()
    else:
        page.open_room(room_b['id'])
    assert page.room_id == room_b['id']
    assert not page.pending and not page.input.text()
    calls = []
    def model(prompt, directory, **kwargs):
        calls.append((prompt, directory, kwargs['profile']))
        return 'A的结果', None
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(codex_text=model))
    job, done = submitted[0]
    done(job(lambda text: None))
    app.processEvents()
    assert len(calls) == 1
    assert calls[0][1] == tmp_path/'friends/123'
    assert calls[0][2]['model_api']['codex_model'] == 'chosen-codex'
    assert calls[0][2]['qq_id'] == '123'
    assert page.room_id == room_b['id']
    assert 'A的结果' not in page.log.toPlainText()
    assert not agent_group.load_room(tmp_path, second_owner, room_b['id'])['messages']
    a = agent_group.load_room(tmp_path, '123', room_a['id'])
    assert [m['content'] for m in a['messages']] == ['A任务', 'A的结果']
    page.close()
    house.close()


@pytest.mark.parametrize('mode', ['sequential', 'parallel'])
def test_real_async_pool_host_workers_summary_and_queue(tmp_path, monkeypatch, mode):
    from house_qt import common
    app = QApplication.instance() or QApplication([])
    common.init_ui_thread()
    house = QWidget()
    house.current_qq = '123'
    house.friend_dir = tmp_path/'friends/123'
    house.friend_dir.mkdir(parents=True)
    house.profile_path = house.friend_dir/'friend.json'
    house.profile_path.write_text('{"qq_id":"123"}', encoding='utf-8')
    monkeypatch.setattr(AgentGroupPage, '_base', lambda self: tmp_path)
    room = agent_group.create_room(tmp_path, '123', mode=mode)
    host = agent_group.add_member(tmp_path, '123', room['id'], 'codex', '主持')
    worker = agent_group.add_member(tmp_path, '123', room['id'], 'codex', '执行')
    agent_group.update_room(tmp_path, '123', room['id'], host_id=host['id'])
    page = AgentGroupPage(house)
    release = threading.Event()
    calls = []
    def model(prompt, *args, **kwargs):
        assert release.wait(3), 'worker not released'
        calls.append(prompt)
        if '候选成员：' in prompt:
            return json.dumps({'summary': '分工结果', 'assignments': [
                {'member_id': worker['id'], 'task': '核对本轮任务', 'model': 'gpt-6-luna', 'effort': 'low'}]}), None
        if '请汇总' in prompt: return '汇总结果', None
        return '成员结果', None
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(codex_text=model))
    page.input.setText('第一任务')
    page.send()
    page.input.setText('第二任务')
    page.send()
    assert page.busy and len(page.pending) == 1
    release.set()
    deadline = time.monotonic()+8
    while (page.busy or page.pending) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not page.busy and not page.pending
    saved = agent_group.load_room(tmp_path, '123', room['id'])
    assert [m['content'] for m in saved['messages'] if m['role'] == 'user'] == ['第一任务','第二任务']
    assert len(calls) == 6
    assert sum(m['content'] == '【汇总】汇总结果' for m in saved['messages']) == 2
    page.close()
    house.close()
