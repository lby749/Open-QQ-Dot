import json
from pathlib import Path

import account_service
import task_session_service


def test_task_session_keeps_account_when_ui_switches(tmp_path):
    friend = tmp_path / 'friends' / '12345'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text('{}', 'utf-8')
    first = account_service.add(tmp_path, {'label': 'first', 'provider': 'opencode',
                                          'model': 'opencode-go/mimo-v2.6-flash'})
    second = account_service.add(tmp_path, {'label': 'second', 'provider': 'opencode',
                                           'model': 'opencode-go/iceman-contributor-free'})
    account_service.bind(tmp_path, '12345', first['id'])
    snapshot = task_session_service.freeze(tmp_path, 'task_1', '12345')
    assert snapshot['model'] == 'opencode-go/mimo-v2.6-flash'
    account_service.activate(tmp_path, 'opencode', second['id'])
    profile = {'model_api': {'provider': 'opencode', 'model': 'opencode-go/iceman-contributor-free'}}
    applied = task_session_service.apply(tmp_path, 'task_1', profile)
    assert applied['model_api']['model'] == 'opencode-go/mimo-v2.6-flash'
    assert applied['agent_adapters']['opencode']['model'] == 'opencode-go/mimo-v2.6-flash'
