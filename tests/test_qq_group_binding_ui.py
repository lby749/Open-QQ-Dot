import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget

import agent_group
import qq_official
from house_qt.qq_group_binding import QQGroupBindingDialog


def test_bot_change_clears_other_app_openid_and_late_owner_cannot_save(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    base = tmp_path / 'data'
    room = agent_group.create_room(base, '12345')
    host = agent_group.add_member(base, '12345', room['id'], 'codex', '主持人')
    member = agent_group.add_member(base, '12345', room['id'], 'codex', '研究员')
    agent_group.update_room(base, '12345', room['id'], host_id=host['id'])
    monkeypatch.setattr(qq_official, 'bots', lambda _: [
        {'app_id': '11111', 'name': '机器人一'}, {'app_id': '22222', 'name': '机器人二'}])
    monkeypatch.setattr(qq_official.ContactStore, 'list', lambda _, identifier: [])
    house = QWidget()
    house.current_qq = '12345'
    house.pages = {'dotlinkPage': SimpleNamespace(state={'login': {'uin': '23456'}})}
    dialog = QQGroupBindingDialog(house, base, '12345', room['id'])
    bot, channel = dialog.workers[member['id']]
    bot.setCurrentIndex(bot.findData('11111'))
    channel.setCurrentText('ONLY_BOT_ONE')
    bot.setCurrentIndex(bot.findData('22222'))
    assert channel.currentText() != 'ONLY_BOT_ONE'
    path = base / 'agent_groups/12345' / (room['id'] + '.json')
    before = path.read_bytes()
    house.current_qq = '67890'
    dialog.save()
    assert '好友已切换' in dialog.error.text()
    assert path.read_bytes() == before
    dialog.close()
    house.close()
    app.processEvents()


def test_six_bot_binding_scrolls_without_hiding_save_buttons(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    base = tmp_path / 'data'
    room = agent_group.create_room(base, '12345')
    host = agent_group.add_member(base, '12345', room['id'], 'codex', '主持人')
    for i in range(6):
        agent_group.add_member(base, '12345', room['id'], 'codex', '成员' + str(i))
    agent_group.update_room(base, '12345', room['id'], host_id=host['id'])
    monkeypatch.setattr(qq_official, 'bots', lambda _: [])
    house = QWidget()
    house.current_qq, house.pages = '12345', {}
    dialog = QQGroupBindingDialog(house, base, '12345', room['id'])
    dialog.resize(800, 650)
    dialog.show()
    app.processEvents()
    button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save)
    assert dialog.height() <= 650
    assert dialog.rect().contains(button.mapTo(dialog, button.rect().center()))
    assert button.isVisible()
    dialog.close()
    house.close()
    app.processEvents()


def test_real_record_widgets_show_qq_round_and_clear_on_friend_switch(tmp_path, monkeypatch):
    """Qt + SQLite integration with synthetic records; no transport/model calls."""
    from house_qt.agent_group import AgentGroupPage
    import qq_group_team
    app = QApplication.instance() or QApplication([])
    base = tmp_path/'data'
    room = agent_group.create_room(base, '12345')
    host = agent_group.add_member(base, '12345', room['id'], 'codex', '主持人')
    worker = agent_group.add_member(base, '12345', room['id'], 'codex', '检查员')
    worker2 = agent_group.add_member(base, '12345', room['id'], 'codex', '复核员')
    room = agent_group.load_room(base, '12345', room['id'])
    room['host_id'] = host['id']
    room['qq_group'] = {'enabled':True,'self_id':'23456','group_id':'34567',
        'bots':{worker['id']:{'app_id':'11111','group_openid':'GROUP_A'},
                worker2['id']:{'app_id':'22222','group_openid':'GROUP_B'}}}
    agent_group.save_room(base, '12345', room)
    qq_group_team.initialize(base)
    with qq_group_team.database(base) as db:
        db.execute('INSERT INTO runs(id,owner,room,status,detail,created,dispatch,summary) VALUES(?,?,?,?,?,?,?,?)',
            ('TEST_RUN','12345',room['id'],'FINISHED','已取得回执',1,'本轮QQ分工','本轮QQ汇总'))
        db.execute('INSERT INTO outbox(id,run,member,app,channel,content,state,receipt) VALUES(?,?,?,?,?,?,?,?)',
            ('TEST_DELIVERY','TEST_RUN',worker['id'],'11111','GROUP_A','<独立检查结果>','SENT','SYNTHETIC_RECEIPT'))
        db.execute('INSERT INTO outbox(id,run,member,app,channel,content,state,receipt) VALUES(?,?,?,?,?,?,?,?)',
            ('TEST_DELIVERY_2','TEST_RUN',worker2['id'],'22222','GROUP_B','后续复核结果','SENT','SYNTHETIC_RECEIPT_2'))
    monkeypatch.setattr(AgentGroupPage, '_base', lambda _: base)
    house = QWidget()
    house.current_qq = '12345'
    house.profile_path = tmp_path / 'friend.json'
    house.profile_path.write_text(json.dumps({'qq_id': '12345', 'model_api': {'provider': 'codex'}}), 'utf-8')
    page = AgentGroupPage(house)
    assert page.send_qq_btn.isEnabled()
    text = page.log.toPlainText()
    assert all(value in text for value in ('本轮QQ分工','<独立检查结果>','本轮QQ汇总','已送达'))
    assert text.index('<独立检查结果>') < text.index('后续复核结果') < text.index('本轮QQ汇总')
    house.profile_path.write_text(json.dumps({'qq_id': '12345', 'cooperation_enabled': False}), 'utf-8')
    page.refresh()
    assert not page.send_qq_btn.isEnabled() and '好友未允许协作' in page.qq_status.text()
    house.current_qq = '67890'
    page.refresh()
    assert not page.log.toPlainText() and not page.send_qq_btn.isEnabled()
    page.close()
    house.close()
    app.processEvents()
