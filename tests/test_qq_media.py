import json
from unittest.mock import patch

import pytest

import qq_media


@pytest.fixture
def home(tmp_path):
    directory=tmp_path/'friends/12345'; directory.mkdir(parents=True)
    (directory/'friend.json').write_text('{}','utf-8')
    (tmp_path/'friends.json').write_text(json.dumps({'friends':{'12345':{'self_id':'77777'}}}),'utf-8')
    path=tmp_path/'交付.txt'; path.write_text('这是测试产物','utf-8')
    return tmp_path,path


@pytest.fixture(autouse=True)
def isolated_approval_store(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    from approval_manager import ApprovalManager
    ApprovalManager._instance = None
    yield
    ApprovalManager._instance = None


class Client:
    sends=[]
    account='77777'
    fail=False
    def call(self,*args): return {'user_id':self.account}
    def close(self): pass
    def send_private_segments(self,owner,segments,timeout):
        self.sends.append((owner,segments))
        if self.fail: raise TimeoutError('发送结果未确认')
        return {'message_id':34567}


@pytest.mark.parametrize('kind,suffix',[('file','.txt'),('image','.png'),('video','.mp4')])
def test_media_has_real_typed_segments_and_receipt_deduplication(home,kind,suffix):
    root,path=home; path=path.rename(path.with_suffix(suffix)); Client.sends=[]
    with patch('qq_link.QQClient',Client):
        receipt=qq_media.send(root,'12345',path,delivery_key='same')
        again=qq_media.send(root,'12345',path,delivery_key='same')
    assert receipt['kind']==kind and receipt['local_saved']
    assert again['deduplicated'] and len(Client.sends)==1
    assert Client.sends[0][1][0]['data']['file']==path.resolve().as_uri()


def test_account_mismatch_and_unknown_receipt_never_send_again(home):
    root,path=home; Client.sends=[]
    with patch('qq_link.QQClient',Client),patch.object(Client,'account','88888'):
        with pytest.raises(PermissionError): qq_media.send(root,'12345',path,delivery_key='mismatch')
    assert not Client.sends
    with patch('qq_link.QQClient',Client):
        assert qq_media.send(root,'12345',path,delivery_key='mismatch')['local_saved']
    assert len(Client.sends)==1  # A pre-send account error did not consume the delivery key.
    Client.sends=[]
    with patch('qq_link.QQClient',Client),patch.object(Client,'fail',True):
        with pytest.raises(TimeoutError): qq_media.send(root,'12345',path,delivery_key='unknown')
        with pytest.raises(RuntimeError,match='避免重复'): qq_media.send(root,'12345',path,delivery_key='unknown')
    assert len(Client.sends)==1


def test_attachment_tool_cannot_escape_workspace_even_with_full_computer_access(home):
    root,path=home
    base=root/'project'; base.mkdir()
    with patch('workspace_tools.workspace',return_value=base),patch('qq_media.send') as send:
        with pytest.raises(PermissionError): qq_media.call(root/'friends/12345',{'path':str(path)})
        send.assert_not_called()


def test_attachment_approval_is_separate_from_file_read_permission(home):
    root,_=home
    from approval_manager import ApprovalCategory,ApprovalManager
    import topic_library
    with (patch.object(ApprovalManager,'check_and_request_approval',return_value=(False,'未授权')) as check,
            patch('qq_media.send') as send):
        with pytest.raises(topic_library.ToolRefused):
            topic_library.call_tool(root/'friends/12345','qq_send_attachment',{'path':'test.txt'})
    assert check.call_args.kwargs['category']==ApprovalCategory.SEND_MESSAGE
    send.assert_not_called()
