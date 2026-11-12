from pathlib import Path
from store import FriendStore
import workspace_tools


def test_search_chat_history_and_files(tmp_path, monkeypatch):
    friend = tmp_path / 'friend'
    friend.mkdir()
    (friend / 'friend.json').write_text('{"computer_tools":{"enabled":true}}', encoding='utf-8')
    store = FriendStore(friend)
    store.add_message('10001', 'friend', '我们上次说过要去海边', '2026-10-01T10:00:00+00:00')
    store.add_message('10001', 'me', '记得，是下周三', '2026-10-01T10:01:00+00:00')
    store.close()
    result = workspace_tools.call(friend, 'desk_search_chat_history', {'query': '海边'})
    assert result['matches'][0]['text'] == '我们上次说过要去海边'
    work = tmp_path / 'workspace'
    work.mkdir()
    (work / 'notes.txt').write_text('project roadmap\nnext step', encoding='utf-8')
    monkeypatch.setattr(workspace_tools, 'workspace', lambda directory: work)
    found = workspace_tools.call(friend, 'desk_search_files', {'query': 'roadmap'})
    assert found['matches'][0]['path'] == 'notes.txt'
