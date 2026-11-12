import hashlib
import json
from pathlib import Path
import time
from unittest.mock import Mock, patch

import pytest
import main
import notification_chat
from notification_manager import NotificationManager, NotificationMessage, NotificationDeferred, NotificationStatus
from store import FriendStore
from task_spec import input_intent


def test_file_notice_uses_complete_target_name_instead_of_clipped_task_title():
    message = NotificationMessage('test', '任务已完成：创建文件 qq_followup_20261', '', category='task',
        metadata={'task_status': 'completed', 'task_title': '创建文件 qq_followup_20261',
                  'target_names': ['qq_followup_20261110b.txt']})
    assert notification_chat.event_facts(message)['subject'] == '文件任务：qq_followup_20261110b.txt'


@pytest.fixture
def friend(tmp_path, monkeypatch):
    monkeypatch.setenv('QQ_BRIDGE_HOME', str(tmp_path))
    monkeypatch.setattr('session_manager.ROOT', tmp_path)
    directory = tmp_path / 'friends' / '12345'
    directory.mkdir(parents=True)
    profile = main.default_profile('12345', '测试好友', False)
    profile['style'] = '你是小柚，称呼对方队长。表达冷静简洁，不使用表情。'
    profile['basic_info'] = {'nickname': '队长'}
    (directory / 'friend.json').write_text(json.dumps(profile), 'utf-8')
    (tmp_path / 'friends.json').write_text(json.dumps({'friends': {'12345': {
        'qq_id': '12345', 'directory': str(directory), 'self_id': '77777'}}}), 'utf-8')
    with patch('message_classification.enqueue_for_database'):
        store = FriendStore(directory)
        yield directory, profile, store
        store.close()


def notice(manager):
    manager.publish('Task completed: 打开页面', '{"page":{"content":"INTERNAL_RESULT"}}\nTask ID: task_12345678',
                    category='task', recipient_id='12345', metadata={'task_status': 'completed', 'task_title': '打开页面'})
    return current_notice(manager)


def current_notice(manager):
    return NotificationMessage(**manager.list_recent(recipient_id='12345')[0])


def test_notification_reuses_current_friend_persona_and_template_examples(friend):
    directory, profile, store = friend
    profile['persona_template'] = 'voice-template'
    with patch('persona_template.prompt_block', return_value='角色范例：队长，我把资料放好了。') as template, \
            patch('multi_agent_orchestrator.call_single_agent',
                  return_value=(json.dumps({'messages':['队长，网页已经看过了。']}), None)) as model:
        bubbles = notification_chat.render_event(store, profile, directory,
                                                  {'kind':'task','status':'completed','subject':'查看网页'})
    assert bubbles == ['队长，网页已经看过了。']
    prompt = model.call_args.args[1]
    assert profile['style'] in prompt
    assert '角色范例：队长，我把资料放好了。' in prompt
    assert template.called
    assert model.call_args.kwargs['base_profile'] is profile


def test_two_friend_reply_styles_are_not_replaced_by_one_fixed_tone(friend):
    directory, profile, store = friend
    with patch('multi_agent_orchestrator.call_single_agent', return_value=('{"messages":["已处理"]}', None)) as model:
        for style in ('称呼对方老许，轻松短句', '使用正式称呼，完整陈述，不加语气词'):
            current = {**profile, 'style':style}
            notification_chat.render_event(store, current, directory, {'kind':'task','status':'queued'})
            assert style in model.call_args.args[1]


def test_notification_does_not_replay_other_tasks_from_old_context(friend):
    directory, profile, store = friend
    store.set('context_summary', 'OLD_TASK_FROM_SUMMARY 仓库维护暂停')
    store.set('handoff_summary', 'OLD_TASK_FROM_HANDOFF')
    store.add_message('old-1', 'friend', 'OLD_TASK_FROM_CHAT 仓库维护')
    with patch('multi_agent_orchestrator.call_single_agent', return_value=(
            '{"messages":["正在检查本次收发"]}', None)) as model:
        notification_chat.render_event(store, profile, directory,
            {'kind': 'task', 'status': 'in_progress', 'subject': '本次QQ收发'})
    prompt = model.call_args.args[1]
    assert 'OLD_TASK_FROM_' not in prompt
    assert '本次QQ收发' in prompt and profile['style'] in prompt


def test_internal_result_is_not_used_as_qq_message_or_model_instruction(friend, tmp_path):
    manager = NotificationManager(tmp_path / 'notices.db')
    message = notice(manager)
    facts = notification_chat.event_facts(message)
    assert 'INTERNAL_RESULT' not in json.dumps(facts)
    assert 'task_12345678' not in json.dumps(facts)
    assert message.content.startswith('{')  # Full technical evidence still exists for the console.
    directory, profile, store = friend
    with patch('multi_agent_orchestrator.call_single_agent', return_value=(
            '{"messages":["Task ID: task_12345678"]}', None)), pytest.raises(ValueError):
        notification_chat.render_event(store, profile, directory, facts)


def test_deferred_persona_generation_does_not_consume_delivery_retries(tmp_path):
    manager = NotificationManager(tmp_path / 'notices.db')
    manager.register_channel_sink(Mock(side_effect=NotificationDeferred('正在整理')), name='qq')
    message = notice(manager)
    manager.retry_pending_notifications()
    manager.retry_pending_notifications()
    current = current_notice(manager)
    assert current.status == NotificationStatus.PENDING
    assert current.retry_count == 0
    assert current.content == message.content


def test_async_persona_bubbles_are_persisted_and_delivered_without_raw_fallback(friend, tmp_path):
    directory, profile, store = friend
    manager = NotificationManager(tmp_path / 'notices.db')
    message = notice(manager)
    presenter = notification_chat.NotificationChat(manager)
    send = Mock()
    with patch.object(notification_chat, 'render_event', return_value=['队长，弄好了。', '结果放在控制台。']) as render:
        with pytest.raises(NotificationDeferred):
            presenter.deliver(message, profile, directory, send)
        deadline = time.monotonic()+3
        while not presenter.jobs[message.id][1].done() and time.monotonic()<deadline:
            time.sleep(.01)
        assert presenter.deliver(message, profile, directory, send)
        assert render.call_args.args[1]['style'] == profile['style']
    assert [call.args[0] for call in send.call_args_list] == ['队长，弄好了。', '结果放在控制台。']
    persisted = current_notice(manager)
    assert persisted.metadata['qq_chat']['sent'] == 2
    assert persisted.content == message.content
    assert notification_chat.NotificationChat(manager).deliver(persisted, profile, directory, send)
    assert send.call_count == 2


def test_unknown_send_receipt_blocks_automatic_duplicate_bubbles(friend, tmp_path):
    directory, profile, store = friend
    manager = NotificationManager(tmp_path / 'notices.db')
    message = notice(manager)
    signature = hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
    manager.update_metadata(message, {'qq_chat':{'signature':signature,'bubbles':['进展消息'], 'sent':0,'inflight':False}})
    send = Mock(side_effect=TimeoutError('unknown receipt'))
    with pytest.raises(TimeoutError):
        notification_chat.NotificationChat(manager).deliver(message, profile, directory, send)
    persisted = current_notice(manager)
    with pytest.raises(RuntimeError, match='重复发送'):
        notification_chat.NotificationChat(manager).deliver(persisted, profile, directory, send)
    send.assert_called_once()


def test_workbench_notices_stay_local(tmp_path):
    manager = NotificationManager(tmp_path / 'notices.db')
    sink = Mock()
    manager.register_channel_sink(sink, name='qq')
    manager.publish('任务完成','详细日志',category='task',recipient_id='12345',channel='local')
    sink.assert_not_called()
    assert current_notice(manager).status == NotificationStatus.SENT


@pytest.mark.parametrize('text,expected', [
    ('今天任务好多啊','chat'), ('你觉得这个网页怎么样','chat'), ('最近在看论文，有点头大','chat'),
    ('如果帮我开发一个完整的程序，会很难吗','chat'), ('他说帮我开发一个完整的程序','chat'),
    ('帮我想想要不要辞职','chat'), ('任务：整理资料','task'), ('帮我打开 https://example.com 并截图','task'),
    ('请帮我修复这个项目的登录报错','task'), ('能不能帮我运行项目测试','task'),
    ('帮我在项目中修复 calc.py','task'), ('请帮我先在当前文件夹里创建说明文件','task'),
    ('帮我在网页上下载公开文档','task'), ('帮我在项目中聊聊代码风格','chat'),
    ('帮我想想如果在目录里运行会怎样','chat'),
])
def test_qq_distinguishes_delegation_from_ordinary_chat(text, expected):
    assert input_intent(text,'qq') == expected
    assert input_intent(text,'workbench') == 'task'


def test_qq_task_ack_uses_persona_renderer_and_idempotent_engine(friend):
    directory, profile, store = friend
    store.add_message('incoming-1','friend','任务：打开 https://example.com 并截图')
    manager = Mock()
    manager.create_task.return_value = 'task_real'
    with patch('task_manager.TaskManager.get_instance', return_value=manager), \
            patch('main.task_event_reply', return_value=['接到了，做完跟你说。']) as render:
        assert main.make_reply(store,profile,directory) == ['接到了，做完跟你说。']
    assert manager.create_task.call_args.kwargs['category'] == 'browser'
    assert manager.create_task.call_args.kwargs['initial_checkpoint']['input_source'] == 'qq'
    assert manager.create_task.call_args.kwargs['idempotency_key'].startswith('qq:12345:')
    assert render.call_args.args[1] is profile
    assert render.call_args.args[3]['status'] == 'queued'


def test_workbench_task_uses_persistent_engine_and_does_not_notify_qq(friend):
    import agent_service
    import session_manager
    directory, profile, store = friend
    session = session_manager.create_session('12345')
    manager = Mock()
    manager.create_task.return_value = 'task_workbench'
    with patch('task_manager.TaskManager.get_instance', return_value=manager):
        result = agent_service.execute_agent_turn('12345','default',session['id'],'修复登录错误')
    assert result['task_id'] == 'task_workbench'
    spec = manager.create_task.call_args.kwargs['initial_checkpoint']
    assert spec['notify_qq'] is False and spec['input_source'] == 'workbench'
    assert spec['session_id'] == session['id']
    manager.start_worker.assert_called_once()


def test_same_qq_event_cannot_restart_a_terminal_task(tmp_path):
    from task_manager import TaskManager, TaskStatus
    manager = TaskManager(tmp_path / 'tasks.db', tmp_path)
    first = manager.create_task('任务','任务说明',owner_id='12345',idempotency_key='qq:input-1')
    manager.update_task_status(first,TaskStatus.CANCELLED)
    same = manager.create_task('任务','任务说明',owner_id='12345',idempotency_key='qq:input-1')
    other = manager.create_task('任务','任务说明',owner_id='23456',idempotency_key='qq:input-1')
    assert same == first
    assert other != first


def test_workbench_task_is_executed_and_verified_with_a_real_file(friend, tmp_path):
    import agent_service
    import session_manager
    from task_manager import TaskManager, TaskStatus
    directory, profile, store = friend
    profile['computer_tools'] = {'enabled':True,'commands':True,'project_path':str(directory)}
    (directory/'friend.json').write_text(json.dumps(profile),'utf-8')
    manager = TaskManager(tmp_path/'data/tasks.db',tmp_path)
    notices = NotificationManager(tmp_path/'data/notices.db')
    qq = Mock()
    notices.register_channel_sink(qq,name='qq')
    def write_file(task, log):
        (directory/'proof.py').write_text('def answer():\n    return 42\n','utf-8')
        return {'output':'已写入文件','modified_files':['proof.py'],'target_files':['proof.py']}
    manager.register_custom_executor('code',write_file)
    session = session_manager.create_session('12345')
    try:
        with patch('task_manager.TaskManager.get_instance',return_value=manager), \
                patch('notification_manager.NotificationManager.get_instance',return_value=notices):
            result = agent_service.submit_workbench_task('12345','default',session['id'],
                                                         '创建代码文件 proof.py；文件: proof.py')
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                task = manager.get_task(result['task_id'])
                if task.status in (TaskStatus.COMPLETED,TaskStatus.FAILED):
                    break
                time.sleep(.05)
            assert task.status == TaskStatus.COMPLETED, task.error
            assert task.verification_info['success'] is True
            assert (directory/'proof.py').read_text('utf-8').startswith('def answer')
            qq.assert_not_called()
    finally:
        manager.stop_worker()


def test_workbench_tool_approval_stays_in_the_workbench(tmp_path):
    from types import SimpleNamespace
    from approval_manager import ApprovalManager
    from task_runtime import current_execution
    manager = NotificationManager(tmp_path/'notices.db')
    qq = Mock()
    manager.register_channel_sink(qq,name='qq')
    task = SimpleNamespace(checkpoint={'notify_qq':False})
    execution = SimpleNamespace(task_id='task-1',manager=SimpleNamespace(get_task=lambda _:task))
    request = SimpleNamespace(owner_id='12345',task_id='task-1',req_id='approval-1',
                              description='确认项目中的文件操作',timeout_seconds=120)
    token = current_execution.set(execution)
    try:
        with patch('notification_manager.NotificationManager.get_instance',return_value=manager):
            object.__new__(ApprovalManager)._notify_request(request)
    finally:
        current_execution.reset(token)
    qq.assert_not_called()
    assert current_notice(manager).channel == 'local'
