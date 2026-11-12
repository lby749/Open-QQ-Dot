import json

import pytest

import workspace_tools


@pytest.mark.parametrize('config',[{'mode':'read_only'},{'mode':'workspace','read_only':True}])
def test_read_only_does_not_advertise_write_or_command_tools(tmp_path,config):
    (tmp_path/'friend.json').write_text(json.dumps({'computer_tools':{'enabled':True,'commands':True,**config}}),'utf-8')
    names={tool['name'] for tool in workspace_tools.tools(tmp_path)}
    assert {'desk_list_files','desk_read_file','desk_open_url'} <= names
    assert not names & {'desk_write_file', 'desk_run_command'}
    assert 'terminal command execution enabled' not in workspace_tools.instructions(tmp_path)
    for tool in ('desk_write_file','desk_run_command'):
        with pytest.raises(ValueError,match='read-only mode'):
            workspace_tools.call(tmp_path,tool,{'path':'example.txt','text':'x','command':'echo x'})
    assert not (tmp_path/'example.txt').exists()


def test_write_mode_keeps_its_existing_capabilities(tmp_path):
    (tmp_path/'friend.json').write_text('{"computer_tools":{"enabled":true,"commands":true}}','utf-8')
    names={tool['name'] for tool in workspace_tools.tools(tmp_path)}
    assert 'desk_write_file' in names and 'desk_run_command' in names


def test_work_tasks_already_exclude_phone_and_chat_tools(tmp_path,monkeypatch):
    monkeypatch.setenv('QQ_TASK_KIND','work')
    (tmp_path/'friend.json').write_text('{"computer_tools":{"enabled":true}}','utf-8')
    import api_backend
    names={tool['name'] for tool in api_backend._tools(tmp_path)}
    assert 'search_friend_memory' not in names and 'get_today_chat' not in names
    assert 'phone_now' not in names and 'phone_screen_text' not in names
    assert 'desk_read_file' in names and 'desk_write_file' in names
