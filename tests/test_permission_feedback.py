"""Denied operations must be explicit; tests use no models or real QQ."""
from unittest.mock import Mock
import pytest
import notification_chat
from notification_manager import NotificationMessage
from tool_registry import ToolRegistry, ToolDefinition, ToolPermissionLevel


@pytest.mark.parametrize('level,config,expected', [
    (ToolPermissionLevel.SENSITIVE_EXEC, {'enabled': True, 'commands': False}, '命令执行'),
    (ToolPermissionLevel.WORKSPACE_WRITE, {'enabled': True, 'mode': 'read_only'}, '写入'),
])
def test_denied_tool_names_the_missing_permission_without_running(level, config, expected):
    handler = Mock()
    definition = ToolDefinition('qa_denied', 'test', {'type': 'object'}, level, handler=handler)
    result = ToolRegistry().execute_tool('qa_denied', {}, computer_config=config, definition=definition)
    assert not result.success and '没有' in result.error and expected in result.error
    handler.assert_not_called()


@pytest.mark.parametrize('error', ['PermissionError: secret path', 'Permission denied: C:/private',
    '没有权限修改文件', 'Owner has not authorized external agent execution'])
def test_permission_failure_notice_keeps_the_denial_not_the_private_error(error):
    message = NotificationMessage('test', '任务失败：写入文件', error, category='task',
        metadata={'task_status': 'failed', 'task_title': '写入文件'})
    facts = notification_chat.event_facts(message)
    assert facts.get('permission_denied') is True
    assert '权限' in facts['reason'] and '未执行' in facts['reason']
    assert 'private' not in str(facts) and 'secret' not in str(facts)


def test_custom_denial_is_friend_scoped_and_does_not_call_a_model():
    facts = {'permission_denied': True, 'status': 'failed'}
    first = notification_chat.render_event(None, {'permission_denied_reply': '先把项目权限打开，再叫我继续。'}, None, facts)
    second = notification_chat.render_event(None, {}, None, facts)
    assert '没有所需权限' in first[0] and '先把项目权限打开' in first[0]
    assert second == ['没有所需权限，受限操作没有继续执行。']


def test_denial_is_structured_and_never_retryable():
    from agent_errors import classify_error, AgentErrorType
    from provider_permissions import permission_denied
    error = classify_error('codex', exc=PermissionError('private details'))
    assert error.error_type == AgentErrorType.PERMISSION_DENIED and not error.retryable
    assert permission_denied(error) and permission_denied(PermissionError())
    assert 'private details' not in str(error)


def test_private_chat_denial_uses_the_literal_friend_reply_once(tmp_path, monkeypatch):
    import json, main
    from store import FriendStore
    monkeypatch.setattr('message_classification.enqueue_for_database', Mock())
    directory = tmp_path/'12345';directory.mkdir()
    profile = main.default_profile('12345','测试好友',False)
    profile['permission_denied_reply'] = '权限没有开，这次先停下。'
    (directory/'friend.json').write_text(json.dumps(profile),'utf-8')
    bridge = object.__new__(main.Bridge)
    bridge.log = Mock()
    sends=[]
    def send(record, store, text, **options):
        sends.append(text);store.set_reply_attempt(options['reply_id'],'sent');return 'CONFIRMED'
    bridge.send_text=send
    monkeypatch.setattr(main, 'make_reply', Mock(side_effect=PermissionError('secret path')))
    record={'qq_id':'12345','directory':str(directory),'self_id':'34567'}
    store=FriendStore(directory)
    try:
        store.add_message('in-1','friend','做个任务')
        bridge.process_reply(record,profile,store,'in-1')
        bridge.process_reply(record,profile,store,'in-1')
        assert len(sends)==1 and '没有所需权限' in sends[0] and profile['permission_denied_reply'] in sends[0]
        assert 'secret' not in sends[0]
    finally:store.close()
