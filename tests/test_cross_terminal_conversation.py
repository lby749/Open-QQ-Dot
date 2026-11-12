"""One shared conversation; protocol substitutes here, actual QQ tested separately."""
import json,time
from unittest.mock import Mock
import pytest
import agent_service, house_web, session_manager
from store import FriendStore


@pytest.fixture
def conversation(tmp_path,monkeypatch):
    monkeypatch.setattr(agent_service,'ROOT',tmp_path)
    monkeypatch.setattr(session_manager,'ROOT',tmp_path)
    monkeypatch.setattr('message_classification.enqueue_for_database',Mock())
    agent_service._qq_sync_at.clear()
    directory=tmp_path/'friends/12345';directory.mkdir(parents=True)
    (directory/'friend.json').write_text(json.dumps({'qq_id':'12345'}),'utf-8')
    (tmp_path/'friends.json').write_text(json.dumps({'friends':{'12345':{'qq_id':'12345','directory':str(directory),'self_id':'34567','display_name':'当前好友'}}}),'utf-8')
    history=[];actions=[]
    class Client:
        account='34567'
        def call(self,action,params=None,**kwargs):
            actions.append((action,params))
            if action=='get_login_info':return {'user_id':self.account}
            assert action=='get_friend_msg_history' and params['user_id']==12345
            return {'messages':history}
        def send_private_msg(self,owner,text):
            assert owner=='12345';return {'message_id':123}
        def close(self):pass
    monkeypatch.setattr('qq_link.QQClient',Client)
    return directory,history,actions,Client


def test_mobile_sends_and_shared_stream_uses_real_ids_without_duplicate_echoes(conversation):
    directory,history,_,_=conversation
    result=house_web.post_agent_send('12345',{'sid':'__qq_stream__','text':'手机发出'})
    assert result['message_id']==123 and result['local_saved']
    history.append({'message_id':123,'user_id':34567,'time':time.time(),'message':[{'type':'text','data':{'text':'手机发出'}}]})
    messages=house_web.agent_session('12345',{'sid':['__qq_stream__']})['messages']
    assert len(messages)==1 and messages[0]['id']=='qq_123' and messages[0]['role']=='user'
    assert messages[0]['delivery_status']=='QQ 已确认'


def test_manual_qq_outgoing_is_backfilled_but_incoming_is_not_acknowledged(conversation):
    directory,history,_,_=conversation
    stamp=time.time()
    history.extend([{'message_id':456,'user_id':34567,'time':stamp,'target_id':12345,'message':[{'type':'text','data':{'text':'QQ 手动发出'}}]},
        {'message_id':789,'user_id':12345,'time':stamp,'message':[{'type':'text','data':{'text':'来信待桥接处理'}}]}])
    data=agent_service.qq_stream('12345')
    assert [m['content'] for m in data['messages']]==['QQ 手动发出']
    store=FriendStore(directory)
    try:assert store.record_inbound('789','来信待桥接处理')[0] is True
    finally:store.close()
    second=agent_service.qq_stream('12345')
    assert [m['id'] for m in second['messages']]==['qq_456','qq_789']
    assert second['messages'][1]['direction']=='friend' and second['messages'][1]['role']=='assistant'


def test_model_identity_is_not_overwritten_and_unconfirmed_messages_are_not_receipts(conversation):
    directory,history,_,_=conversation
    store=FriendStore(directory)
    try:
        store.add_message('456','me','模型已发出',identity='model')
        store.add_local_outbound('尚未确认')
    finally:store.close()
    history.append({'message_id':456,'user_id':34567,'time':time.time(),'message':[{'type':'text','data':{'text':'模型已发出'}}]})
    data=agent_service.qq_stream('12345')
    assert data['messages'][0]['identity']=='model'
    assert data['messages'][1]['delivery_status']=='发送未确认'


def test_unbound_friend_and_other_login_never_use_history_or_cache(conversation):
    _,_,actions,client=conversation
    with pytest.raises(PermissionError):agent_service.qq_stream('99999')
    assert actions==[]
    client.account='88888'
    for _ in range(2):
        with pytest.raises(PermissionError,match='绑定不一致'):agent_service.qq_stream('12345')
    assert all(action=='get_login_info' for action,_ in actions)
