import json
from unittest.mock import Mock
import pytest
import local_persona_analyzer as analyzer
import persona_template


@pytest.mark.parametrize('provider,settings,adapters,expected,effort',[
    ('codex',{'codex_model':'current-codex','codex_effort':'high'},{},'current-codex','high'),
    ('dsh',{'dsh_settings':{'model':'current-dsh','effort':'medium'}},{},'current-dsh','medium'),
    ('opencode',{}, {'opencode':{'model':'go/current','variant':'high'}},'go/current','high'),
    ('zcode',{}, {'zcode':{'model':'current-zcode','effort':'low'}},'current-zcode','low'),
])
def test_template_analysis_uses_current_model_once_without_tools_or_fallback(tmp_path,monkeypatch,provider,settings,adapters,expected,effort):
    import multi_agent_orchestrator,dsh_backend
    monkeypatch.setattr(dsh_backend,'cli_path',lambda *_:None)
    profile={'qq_id':'12345','model_api':{'provider':provider,**settings},'agent_adapters':adapters,
             'computer_tools':{'enabled':True,'full_computer_access':True,'commands':True}}
    (tmp_path/'friend.json').write_text(json.dumps(profile),'utf-8')
    model=Mock(return_value=('{"ok":true}',None));monkeypatch.setattr(multi_agent_orchestrator,'call_single_agent',model)
    assert json.loads(analyzer.call_model('data',{'type':'object'},'cloud',tmp_path))=={'ok':True}
    args=model.call_args.args;options=model.call_args.kwargs
    from agent_model_selection import current
    assert args[0]==provider and current(args[3],provider)==(expected,effort)
    assert args[3]['computer_tools']=={'enabled':False,'commands':False}
    assert options['text_only'] and options['max_retries']==0 and options['local_ok'] is False
    assert analyzer.model_name('cloud',tmp_path)==f'{provider} · {expected} · {effort}'
    assert json.loads((tmp_path/'friend.json').read_text('utf-8'))==profile


def test_trait_context_is_bounded_and_scene_labels_never_call_a_model(tmp_path,monkeypatch):
    pairs=[{'user':['消息'+str(i)],'target':['回复'+str(i)]} for i in range(6000)]
    model=Mock(return_value='{"summary":"根据片段","traits":[]}')
    monkeypatch.setattr(persona_template,'call_model',model)
    persona_template.analyze_traits(pairs,'cloud',tmp_path)
    assert model.call_args.args[0].count('对方：')==persona_template.TRAIT_SAMPLE
    model.reset_mock()
    assert len(persona_template.tag_scenes(['你好','今天不错'],tmp_path))==2
    model.assert_not_called()
