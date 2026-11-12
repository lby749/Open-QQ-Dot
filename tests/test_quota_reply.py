import json
from contextlib import closing
from types import SimpleNamespace
import pytest

from agent_errors import AgentError, AgentErrorType
import quota_reply
from store import FriendStore


def test_only_real_quota_errors_trigger_the_configured_notice():
    assert quota_reply.exhausted(RuntimeError('Codex 模型账号额度不足'))
    assert quota_reply.exhausted(AgentError(AgentErrorType.QUOTA_EXHAUSTED, message='out of credit'))
    assert not quota_reply.exhausted(AgentError(AgentErrorType.RATE_LIMIT, message='temporarily rate limited'))
    assert not quota_reply.exhausted(RuntimeError('network timeout'))
    assert not quota_reply.exhausted(AgentError(AgentErrorType.AUTH_ERROR, message='not logged in'))


def test_literal_notice_is_once_per_friend_until_normal_reply_recovers(tmp_path):
    calls = []
    def send(record, store, text, **options):
        assert options['voice_decision'] is False and options['reply_plan'].bubbles == (text,)
        calls.append((record['qq_id'], text))
        store.set_reply_attempt(options['reply_id'], 'sent')
        return 'RECEIPT'
    bridge = SimpleNamespace(send_text=send)
    profile = {'model_api': {'quota_reply': '我现在有点忙，晚点再回你～'}}
    with closing(FriendStore(tmp_path/'alice')) as store:
        store.add_message('1', 'friend', '你好')
        store.set_reply_attempt('1', 'processing')
        assert quota_reply.send_notice(bridge, {'qq_id':'alice'}, profile, store, '1', None)
        assert not quota_reply.send_notice(bridge, {'qq_id':'alice'}, profile, store, '1', None)
        quota_reply.recovered(store)
        store.set_reply_attempt('1', 'processing')
        assert quota_reply.send_notice(bridge, {'qq_id':'alice'}, profile, store, '1', None)
    with closing(FriendStore(tmp_path/'bob')) as store:
        store.add_message('2', 'friend', '你好')
        store.set_reply_attempt('2', 'processing')
        assert quota_reply.send_notice(bridge, {'qq_id':'bob'}, profile, store, '2', None)
    assert [owner for owner, _ in calls] == ['alice','alice','bob']


def test_uncertain_send_is_not_retried_and_blank_configuration_is_silent(tmp_path):
    calls = []
    def send(record, store, text, **options):
        calls.append(text)
        store.set_reply_attempt(options['reply_id'], 'sending')
        raise TimeoutError('uncertain')
    bridge = SimpleNamespace(send_text=send)
    with closing(FriendStore(tmp_path)) as store:
        store.add_message('1', 'friend', '你好')
        store.set_reply_attempt('1', 'processing')
        assert not quota_reply.send_notice(bridge, {}, {}, store, '1', None)
        with pytest.raises(TimeoutError):
            quota_reply.send_notice(bridge, {}, {'model_api':{'quota_reply':'稍后再聊'}}, store, '1', None)
        assert json.loads(store.get(quota_reply.KEY))['state'] == 'uncertain'
        assert not quota_reply.send_notice(bridge, {}, {'model_api':{'quota_reply':'稍后再聊'}}, store, '1', None)
    assert calls == ['稍后再聊']
