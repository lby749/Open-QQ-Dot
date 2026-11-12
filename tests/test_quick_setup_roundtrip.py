"""Wizard round trips use isolated profiles, not real contacts, models or downloads."""
import json
import os
from itertools import product
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QWidget
import house_qt.quick_setup as setup


def test_reopening_keeps_the_saved_configuration(tmp_path, monkeypatch):
    import house_status
    monkeypatch.setattr(house_status, 'ROOT', tmp_path)
    monkeypatch.setattr(setup, 'detect_agents', lambda: [
        {'id':'codex','label':'Codex CLI','model':'gpt-6-luna','effort':'low','rank':0}])
    friend = tmp_path/'friends/12345'
    friend.mkdir(parents=True)
    _, _, preset = setup.build_prompts('冷静理性','好朋友','不透露','中性','both','关闭主动')
    (friend/'friend.json').write_text(json.dumps({
        'qq_id':'12345','purpose':'both','performance_tier':'中占用',
        'prompt_presets':preset,'model_api':{'provider':'codex','codex_model':'gpt-6.1-sol'},
        'agent_name':'配置回读','reply_bubbles_max':5,'continuation_max':0,
        'voice_reply':{'mode':'sometimes'},'computer_tools':{'enabled':True,'mode':'read_only'}}),'utf-8')
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    dialog = setup.QuickSetupDialog(parent,'12345')
    try:
        values = dialog.choices()
        assert values == {'purpose':'聊天 + 工作','agent':'codex','model':'gpt-6.1-sol','effort':'low',
            'persona':'冷静理性','relation':'好朋友','friend_gender':'不透露','assistant_gender':'中性',
            'frequency':'关闭主动','performance':'中占用','permission':'只读查看（可看文件，不改）',
            'agent_name':'配置回读','reply_bubbles_max':5,'continuation_max':0,'voice_mode':'sometimes'}
    finally:
        dialog.reject()
        parent.close()


def test_all_prepared_choices_have_consistent_examples_and_usage_modes():
    """All 12,960 mappings, not 12,960 model calls or UI screenshots."""
    import quick_setup_examples
    count = 0
    for persona, relation, gender, assistant, frequency in product(
            setup.PERSONAS, setup.RELATIONS, ('女','男','不透露'), ('女','男','中性'), setup.FREQUENCY):
        for purpose, performance in product(setup.PURPOSES.values(), setup.PERFORMANCE):
            identity, style, preset = setup.build_prompts(persona,relation,gender,assistant,purpose,frequency)
            assert identity and style and preset['prepared_key'] == setup._prepared_key(persona,relation,gender,assistant,frequency)
            assert len(quick_setup_examples.examples({'prompt_presets':preset})) == 12
            from model_downloader import MODEL_CATALOG
            assert all(tier in MODEL_CATALOG[kind] for kind, tier in setup.PERFORMANCE[performance][1])
            count += 1
    assert count == 12960


def test_model_extraction_uses_real_catalog_contract_and_ignores_late_source(tmp_path, monkeypatch):
    import house_status, agent_model_selection
    monkeypatch.setattr(house_status, 'ROOT', tmp_path)
    directory = tmp_path/'friends/12345';directory.mkdir(parents=True)
    profile = {'qq_id':'12345','model_api':{'provider':'codex','codex_model':'saved-model','codex_effort':'high'}}
    (directory/'friend.json').write_text(json.dumps(profile),'utf-8')
    monkeypatch.setattr(setup, 'detect_agents', lambda: [
        {'id':'codex','label':'Codex CLI','model':'saved-model','effort':'low','rank':0},
        {'id':'dsh','label':'DSH','model':'dsh-saved','effort':'low','rank':1}])
    jobs=[]
    monkeypatch.setattr(setup,'run_async',lambda job,done,error,**_:jobs.append((job,done,error)))
    monkeypatch.setattr(agent_model_selection,'discover',lambda p,provider,d: {
        'models':{'actual-model':['medium','high']},'source':'local test catalogue',
        'signature':agent_model_selection.signature(p,provider)})
    app=QApplication.instance() or QApplication([]);parent=QWidget();dialog=setup.QuickSetupDialog(parent,'12345')
    try:
        assert dialog.model.count()==1 and dialog.choices()['effort']=='high'
        dialog.fetch_models_button.click()
        job,done,_=jobs.pop();entry=job(None)
        dialog.agent.setCurrentIndex(1)
        done(entry)
        assert dialog.model.currentText()=='dsh-saved' and 'codex' not in dialog.catalogs
        dialog.agent.setCurrentIndex(0);dialog.fetch_models_button.click()
        job,done,_=jobs.pop();done(job(None))
        assert dialog.model.currentText()=='actual-model' and dialog.model.count()==1
        assert [dialog.effort.itemData(i) for i in range(dialog.effort.count())]==['medium','high']
        assert dialog.choices()['effort'] in ('medium','high')
    finally:
        dialog.reject();parent.close()
