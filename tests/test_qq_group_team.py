"""Local protocol/state integration; transports and models are explicit substitutes."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import sqlite3

import pytest

import agent_group
import qq_group_team as team_module
import qq_official


@pytest.fixture
def setup(tmp_path, monkeypatch):
    base = tmp_path / 'data'
    owner = '12345'
    room = agent_group.create_room(base, owner, '测试协作')
    host = agent_group.add_member(base, owner, room['id'], 'codex', '统筹', '分工与汇总')
    workers = [agent_group.add_member(base, owner, room['id'], 'codex', name, role)
               for name, role in [('研究员', '核对依据'), ('审查员', '检查缺陷')]]
    agent_group.update_room(base, owner, room['id'], host_id=host['id'])
    monkeypatch.setattr(qq_official, 'bots', lambda _: [
        {'app_id': '11111', 'name': '研究机器人'}, {'app_id': '22222', 'name': '审查机器人'}])
    monkeypatch.setattr(qq_official, 'load_secret', lambda *_: 'test-only-secret')
    config = {'enabled': True, 'self_id': '23456', 'group_id': '34567', 'operators': ['45678'],
              'bots': {member['id']: {'app_id': identifier, 'group_openid': channel}
                       for member, identifier, channel in zip(workers, ['11111', '22222'], ['GROUP_A', 'GROUP_B'])}}
    team_module.save_binding(base, owner, room['id'], config)
    import codex_settings
    monkeypatch.setattr(codex_settings, 'model_options', lambda: {'test-cheap': ['low'], 'test-deep': ['high']})
    directory = tmp_path / 'friends' / owner
    directory.mkdir(parents=True)
    (directory / 'friend.json').write_text(json.dumps({'model_api': {
        'codex_model': 'test-cheap', 'codex_effort': 'low'}}), 'utf-8')
    plan = {'summary': '研究员核对、审查员检查', 'assignments': [
        {'member_id': m['id'], 'model': 'test-deep', 'effort': 'high', 'task': m['role']} for m in workers]}
    sent, calls = [], []
    class Personal:
        status = {'online': True, 'good': True}
        def call(self, action, parameters=None):
            if action == 'get_status':
                return self.status
            if action == 'get_login_info':
                return {'user_id': '23456'}
            assert action == 'send_group_msg' and parameters['group_id'] == 34567
            sent.append(('mother', parameters['message'][0]['data']['text']))
            return {'message_id': 'MAIN_RECEIPT'}
    class Official:
        def __init__(self, identifier):
            self.identifier = identifier
        def invitation(self):
            return {'app_id': self.identifier, 'qq_id': '66666' if self.identifier == '11111' else '77777'}
        def send(self, channel, text, message, sequence):
            sent.append((self.identifier, channel, message, sequence, text))
            return 'BOT_RECEIPT'
    def generate(member, prompt):
        assert 'PRIVATE_SENTINEL' not in prompt
        calls.append((member['name'], prompt))
        if '候选成员：' in prompt:
            return json.dumps(plan, ensure_ascii=False)
        return member['name'] + '的本轮结果'
    personal = Personal()
    team = team_module.QQGroupTeam(base, tmp_path, lambda: personal, lambda: '23456',
                                  generate=generate, bot_factory=Official)
    # The queue is drained synchronously here; no real process, SDK or model is started.
    team.pool.shutdown(wait=False)
    team.pool = SimpleNamespace(submit=lambda fn, *args: fn(*args), shutdown=lambda **_: None)
    yield SimpleNamespace(home=tmp_path, base=base, owner=owner, room=room, host=host,
                          workers=workers, config=config, team=team, sent=sent, calls=calls, plan=plan, personal=personal)
    team.close()


def event(identifier='MESSAGE_1', user='45678'):
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 23456, 'group_id': 34567,
            'user_id': user, 'message_id': identifier, 'sender': {'nickname': '群成员'},
            'message': [{'type': 'text', 'data': {'text': '协作：比较两个方案'}}]}


def grant(setup, identifier='11111', channel='GROUP_A', message='GRANT_1'):
    setup.team.grant(identifier, channel, message, datetime.now(timezone.utc).isoformat())


def test_private_and_mailbox_reports_have_separate_authority_and_receipts(setup):
    from qq_group_connection import GroupConnection
    service = GroupConnection(setup.home, setup.owner, '23456', setup.personal)
    with sqlite3.connect(service.path) as db:
        db.execute("INSERT INTO links(nonce,owner,self_id,group_id,app,bot,channel,state,expires,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ('a'*32, setup.owner, '23456', '', '11111', '66666', 'PRIVATE_A', 'VERIFIED', 0, 'c2c'))
    room = agent_group.load_room(setup.base, setup.owner, setup.room['id'])
    for member, route in zip(room['members'][1:], ['private', 'mailbox']):
        member['report'] = route
    agent_group.save_room(setup.base, setup.owner, room)
    setup.team.bot_factory = lambda app: SimpleNamespace(
        invitation=lambda: {'app_id': app, 'qq_id': '66666'},
        send_private=lambda *args: setup.sent.append(('private', *args)) or 'C2C_RECEIPT',
        send=lambda *_: pytest.fail('private grant must never send a group report'))
    now = datetime.now(timezone.utc).isoformat()
    assert not team_module.capture_grant(setup.home, '11111', 'GROUP_A', 'WRONG_SCOPE', now, kind='c2c')
    assert team_module.capture_grant(setup.home, '11111', 'PRIVATE_A', 'PRIVATE_GRANT', now, kind='c2c', used=1)
    setup.team.receive(event('PRIVATE_AND_MAIL'))
    rows = team_module.snapshot(setup.base, setup.owner, room['id'])['deliveries']
    assert {r['route'] for r in rows} == {'private', 'mailbox'}
    assert all(r['state'] == 'SENT' and r['receipt'] for r in rows)
    assert next(r for r in rows if r['route'] == 'private')['receipt'] == 'C2C_RECEIPT'
    assert setup.sent[1][1:4] == ('PRIVATE_A', setup.sent[1][2], 'PRIVATE_GRANT')
    from agent_messaging import AgentMailbox
    mail = next(r for r in rows if r['route'] == 'mailbox')
    with AgentMailbox(setup.base / 'agent_mail.sqlite3')._db() as db:
        assert db.execute('SELECT state FROM recipients WHERE message_id=?', (mail['receipt'],)).fetchone()[0] == 'acked'
    import collaboration_view
    view = collaboration_view.snapshot(setup.base, owner=setup.owner)
    assert view['summary']['agents'] == 3 and view['summary']['threads'] == 1
    assert any(m['kind_label'] == '邮箱汇报 · 已送达' for m in view['threads'][0]['messages'])
    assert team_module.capture_grant(setup.home, '11111', 'PRIVATE_A', 'PRIVATE_GRANT', now, kind='c2c')
    with team_module.database(setup.base) as db:
        assert db.execute('SELECT seq FROM private_grants WHERE app=?', ('11111',)).fetchone()[0] == 2
    setup.team.receive(event('PRIVATE_AND_MAIL'))
    assert len(setup.calls) == 4


def test_auto_route_uses_only_available_channels_and_preserves_fixed_choice(setup):
    room = agent_group.load_room(setup.base, setup.owner, setup.room['id'])
    worker = room['members'][1]
    worker.update(report='auto', available_reports=['group', 'mailbox'])
    catalog = {'codex': {'models': {'test-deep': ['high']}, 'default_model': 'test-deep', 'default_effort': 'high'}}
    plan = dict(setup.plan, assignments=[dict(setup.plan['assignments'][0], report='mailbox')])
    assert agent_group.parse_dispatch(json.dumps(plan), room, setup.host, catalog)['members'][0]['report'] == 'mailbox'
    plan['assignments'][0]['report'] = 'private'
    with pytest.raises(ValueError, match='汇报通道'):
        agent_group.parse_dispatch(json.dumps(plan), room, setup.host, catalog)


def test_group_only_legacy_database_cannot_authorize_private_report(setup):
    path = setup.base / 'qq_group_connections.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE links(app TEXT,channel TEXT,state TEXT)')
        db.execute("INSERT INTO links VALUES('11111','GROUP_A','VERIFIED')")
    assert team_module.private_link(setup.base, setup.owner, setup.config, '11111') == ''
    assert not team_module.capture_grant(setup.home, '11111', 'GROUP_A', 'LEGACY',
                                         datetime.now(timezone.utc).isoformat(), kind='c2c')


def test_nested_group_flow_records_real_parent_edges_in_runtime_projection(setup, monkeypatch):
    from agent_model_selection import options
    monkeypatch.setattr('agent_model_selection.flow_options', lambda room, profile:
                        options(profile, ['codex'], members=room['members']))
    parent, child = setup.workers
    agent_group.update_node(setup.base, setup.owner, setup.room['id'], parent['id'], parent_id=setup.host['id'])
    agent_group.update_node(setup.base, setup.owner, setup.room['id'], child['id'], parent_id=parent['id'])
    def generate(member, prompt):
        if '候选成员：' in prompt:
            candidates = json.loads(prompt.split('候选成员：', 1)[1].split('\n模型目录：', 1)[0])
            return json.dumps({'summary': '逐级核查', 'assignments': [
                {'member_id': m['member_id'], 'model': 'test-deep', 'effort': 'high', 'task': '核对一个边界'} for m in candidates]})
        return '本层核查结果'
    setup.team.generate = generate
    grant(setup)
    grant(setup, '22222', 'GROUP_B', 'GRANT_B')
    assert setup.team.receive(event('NESTED_FLOW'))
    import collaboration_view
    thread = collaboration_view.snapshot(setup.base, owner=setup.owner)['threads'][0]
    assert thread['task_status'] == 'FINISHED'
    handovers = [m for m in thread['messages'] if m['kind_label'] == '分派分支任务']
    assert len(handovers) == 2 and all(m['receipt'] for m in handovers)
    nested = next(m for m in handovers if m['to'][0]['agent_id'].endswith('/' + child['id']))
    parent_call = next(m for m in thread['messages'] if m['message_id'] == nested['reply_to'])
    assert parent_call['from'].endswith('/' + parent['id'])
    assert any(m['message_id'] == parent_call['reply_to'] and m['kind'] == 'handoff' for m in handovers)
    assert all(m['brief'] == '交回' for m in handovers)
    assert sum(m['kind_label'] == '模型调用开始' for m in thread['messages']) == 5


def test_mother_dispatches_and_each_official_bot_uses_its_own_group_grant(setup):
    grant(setup)
    grant(setup, '22222', 'GROUP_B', 'GRANT_2')
    assert setup.team.receive(event())
    assert [row[0] for row in setup.sent] == ['mother', '11111', '22222', 'mother']
    assert setup.sent[1][1:4] == ('GROUP_A', 'GRANT_1', 1)
    assert setup.sent[2][1:4] == ('GROUP_B', 'GRANT_2', 1)
    assert [row[0] for row in setup.calls] == ['统筹', '研究员', '审查员', '统筹']
    setup.team.receive(event())
    assert len(setup.calls) == 4 and len(setup.sent) == 4


def test_unauthorized_members_and_wrong_primary_account_do_not_call_models(setup):
    setup.team.receive(event(user='56789'))
    setup.team.self_id_fn = lambda: '99999'
    setup.team.receive(event())
    assert not setup.sent and not setup.calls


@pytest.mark.parametrize('status', [{'online': False, 'good': True}, {}, {'online': True, 'good': False}])
def test_offline_primary_is_not_inferred_from_cached_identity_or_live_process(setup, status):
    setup.personal.status = status
    assert setup.team.receive(event())
    assert not setup.sent and not setup.calls
    run = team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]
    assert run['status'] == 'FAILED' and '主 QQ 离线' in run['detail']


def test_public_group_requests_disable_tools_resume_fallback_and_retries(setup, monkeypatch):
    import house_status
    import multi_agent_orchestrator
    monkeypatch.setattr(house_status, 'load_profile', lambda _: {
        'model_api': {'provider': 'codex'}, 'persona': 'PRIVATE_SENTINEL'})
    calls = []
    def call(provider, prompt, directory, profile, **options):
        calls.append((profile, options))
        return '公开答复', None
    monkeypatch.setattr(multi_agent_orchestrator, 'call_single_agent', call)
    setup.team.generate = None
    assert setup.team._call(setup.owner, setup.room, setup.config, setup.host, '公开任务') == '公开答复'
    profile, options = calls[0]
    assert profile['computer_tools']['enabled'] is False and 'persona' not in profile
    assert options['text_only'] and options['ephemeral'] and options['max_retries'] == 0
    assert options['local_ok'] is False


def test_mother_selects_one_worker_model_effort_and_specific_task(setup):
    setup.plan['assignments'] = [dict(setup.plan['assignments'][1], task='仅审查事实依据')]
    setup.team.receive(event())
    assert [name for name, _ in setup.calls] == ['统筹', '审查员', '统筹']
    assert '仅审查事实依据' in setup.calls[1][1]
    run = team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]
    saved = json.loads(run['plan'])
    assert saved[0]['model'] == 'test-deep' and saved[0]['effort'] == 'high'


def test_closed_branch_is_excluded_from_planner_and_late_deliveries_are_revoked(setup):
    agent_group.update_node(setup.base, setup.owner, setup.room['id'], setup.workers[0]['id'], enabled=False)
    setup.plan['assignments'] = setup.plan['assignments'][1:]
    setup.team.receive(event())
    assert [name for name, _ in setup.calls] == ['统筹', '审查员', '统筹']
    agent_group.update_node(setup.base, setup.owner, setup.room['id'], setup.host['id'], enabled=False)
    grant(setup, '22222', 'GROUP_B', 'GRANT_2')
    assert not any(row[0] == '22222' for row in setup.sent)
    state = team_module.snapshot(setup.base, setup.owner, setup.room['id'])
    assert state['deliveries'][0]['state'] == 'REVOKED'


def test_disabled_root_rejects_before_scheduling_or_generating_anything(setup):
    agent_group.update_node(setup.base, setup.owner, setup.room['id'], setup.host['id'], enabled=False)
    with pytest.raises(PermissionError):
        setup.team.start_local(setup.owner, setup.room['id'], '不应执行', 'closed-root')
    assert setup.team.receive(event())
    assert setup.calls == setup.sent == []
    assert team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'] == []


def test_invalid_model_plan_stops_before_worker_or_public_dispatch(setup):
    setup.plan['assignments'][0]['model'] = 'unconfigured-model'
    setup.team.receive(event())
    assert len(setup.calls) == 1 and setup.sent == []
    assert team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]['status'] == 'FAILED'


def test_simple_question_is_answered_without_fanout(setup):
    setup.plan.update(assignments=[], direct_reply='你好，今天想做什么？')
    setup.team.receive(event())
    assert len(setup.calls) == 1 and len(setup.sent) == 1
    assert setup.sent[0] == ('mother', '【test-cheap｜思考：低】\n你好，今天想做什么？')


def test_network_reads_real_execution_store_without_mailbox_and_keeps_older_runs(setup):
    import collaboration_view
    assert not (setup.base / 'agent_mail.sqlite3').exists()
    setup.team.start_local(setup.owner, setup.room['id'], '核对活动显示', 'view-one')
    data = collaboration_view.snapshot(setup.base, owner=setup.owner)
    assert data['summary']['threads'] == 1 and data['summary']['agents'] == 3
    thread = data['threads'][0]
    assert thread['status_label'] == '待送达'
    calls = [m for m in thread['messages'] if m['kind_label'] == '模型调用开始']
    returns = [m for m in thread['messages'] if m['kind_label'] == '模型已返回']
    assert len(calls) == len(returns) == 4
    assert all(m['brief'] == '返回' for m in returns)  # output is not artifact acceptance
    assert any('待发送' in m['kind_label'] for m in thread['messages'])
    assert collaboration_view.snapshot(setup.base, owner='99999')['threads'] == []
    later = collaboration_view.snapshot(setup.base, owner=setup.owner, now=thread['last'] + 86401)
    assert len(later['threads']) == 1 and later['summary']['messages_24h'] == 0
    assert later['threads'][0]['messages'] == thread['messages']
    grant(setup)
    grant(setup, '22222', 'GROUP_B', 'GRANT_2')
    sent = collaboration_view.snapshot(setup.base, owner=setup.owner)['threads'][0]
    assert sent['status_label'] == '已完成'
    assert len([m for m in sent['messages'] if m['kind_label'] == '群内汇报 · 已送达']) == 2
    assert len(setup.calls) == 4  # refreshing and later delivery never call the model again


def test_network_keeps_legacy_group_evidence_without_fabricated_model_events(setup):
    import collaboration_view
    setup.team.start_local(setup.owner, setup.room['id'], '旧版记录', 'legacy-view')
    with team_module.database(setup.base) as db:
        db.execute('DROP TABLE events')
    data = collaboration_view.snapshot(setup.base, owner=setup.owner)
    assert data['summary']['threads'] == 1
    items = data['threads'][0]['messages']
    assert any(m['kind_label'] == '群内分工' for m in items)
    assert not any(m['kind_label'] == '模型已返回' for m in items)


def test_desktop_start_is_separate_idempotent_and_rejects_different_text(setup):
    key = setup.team.start_local(setup.owner, setup.room['id'], '比较两个方案', 'desktop-one')
    assert key.startswith('desktop:') and len(setup.calls) == 4
    run = team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]
    assert run['dispatch'].startswith('【test-cheap｜思考：低】\n本轮分工')
    assert run['summary'].startswith('【test-cheap｜思考：低】\n本轮汇总')
    assert run['dispatch'].count('【test-deep｜思考：高】') == 2
    assert run['dispatch_receipt'] == run['summary_receipt'] == 'MAIN_RECEIPT'
    setup.team.start_local(setup.owner, setup.room['id'], '比较两个方案', 'desktop-one')
    assert len(setup.calls) == 4
    with pytest.raises(ValueError, match='另一个协作任务'):
        setup.team.start_local(setup.owner, setup.room['id'], '另一个任务', 'desktop-one')


def test_other_live_engine_does_not_interrupt_active_desktop_run(setup):
    setup.team.pool = SimpleNamespace(submit=lambda *args: None, shutdown=lambda **_: None)
    key = setup.team.start_local(setup.owner, setup.room['id'], '先保持运行', 'desktop-pending')
    other = team_module.QQGroupTeam(setup.base, setup.home, lambda: setup.personal, lambda:'23456')
    try:
        with team_module.database(setup.base) as db:
            assert db.execute('SELECT status FROM runs WHERE id=?', (key,)).fetchone()[0] == 'RUNNING'
        with pytest.raises(RuntimeError, match='正在执行'):
            setup.team.start_local(setup.owner, setup.room['id'], '另一个任务', 'desktop-two')
    finally:
        other.close()


def test_worker_failure_is_partial_not_a_successful_round(setup):
    original = setup.team.generate
    def generate(member, prompt):
        if member['id'] == setup.workers[0]['id']:
            raise RuntimeError('synthetic model failure')
        return original(member, prompt)
    setup.team.generate = generate
    setup.team.receive(event())
    run = team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]
    assert run['status'] == 'PARTIAL' and setup.workers[0]['name'] in run['detail']


def test_completed_delivery_updates_waiting_round_without_regenerating(setup):
    setup.team.receive(event())
    assert team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]['status'] == 'WAITING_DELIVERY'
    grant(setup)
    grant(setup, '22222', 'GROUP_B', 'GRANT_2')
    assert team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]['status'] == 'FINISHED'
    assert len(setup.calls) == 4


def test_grant_absence_waits_and_late_delivery_does_not_regenerate_or_replay(setup):
    room = agent_group.load_room(setup.base, setup.owner, setup.room['id'])
    room['memory'] = [{'content': 'PRIVATE_SENTINEL'}]
    room['messages'] = [{'role': 'user', 'content': 'PRIVATE_SENTINEL', 'name': '私聊'}]
    agent_group.save_room(setup.base, setup.owner, room)
    setup.team.receive(event())
    assert len(setup.calls) == 4
    assert team_module.snapshot(setup.base, setup.owner, setup.room['id'])['runs'][0]['status'] == 'WAITING_DELIVERY'
    assert len([row for row in setup.sent if row[0] != 'mother']) == 0
    grant(setup)
    grant(setup)
    assert len(setup.calls) == 4
    assert len([row for row in setup.sent if row[0] == '11111']) == 1


def test_uncertain_delivery_is_not_automatically_retried(setup):
    setup.team.receive(event())
    class Broken:
        def send(self, *_):
            setup.sent.append(('uncertain',))
            raise TimeoutError('result unknown')
    setup.team.senders['11111'] = Broken()
    grant(setup)
    grant(setup, message='GRANT_LATER')
    assert setup.sent.count(('uncertain',)) == 1
    state = team_module.snapshot(setup.base, setup.owner, setup.room['id'])
    assert any(row['state'] == 'UNCERTAIN' for row in state['deliveries'])


def test_changed_binding_revokes_pending_deliveries(setup):
    setup.team.receive(event())
    room = agent_group.load_room(setup.base, setup.owner, setup.room['id'])
    room['qq_group']['operators'].append('56789')
    agent_group.save_room(setup.base, setup.owner, room)
    grant(setup)
    assert not any(row[0] == '11111' for row in setup.sent)
    assert any(row['state'] == 'REVOKED' for row in
               team_module.snapshot(setup.base, setup.owner, setup.room['id'])['deliveries'])


def test_duplicate_bot_binding_is_rejected(setup):
    setup.config['bots'][setup.workers[1]['id']]['app_id'] = '11111'
    with pytest.raises(ValueError, match='独立'):
        team_module.save_binding(setup.base, setup.owner, setup.room['id'], setup.config)


def test_official_worker_event_only_records_grant_and_does_not_start_standalone_reply(setup):
    session = qq_official.BotSession(setup.home, {'app_id': '11111', 'name': '研究员'})
    session.enqueue('group', 'GROUP_A', 'MEMBER_A', 'GRANT_A', '协作领取',
                    datetime.now(timezone.utc).isoformat())
    assert session.events.empty()
    assert not session.store.list('11111')
    assert not setup.calls


def test_sender_uses_official_endpoint_and_cached_token(setup, monkeypatch):
    sender = qq_official.OfficialGroupSender(setup.home, '11111')
    requests = []
    def request(url, body, headers=None):
        requests.append((url, body, headers))
        return {'access_token': 'test-token', 'expires_in': 7200} if 'getAppAccessToken' in url else {'id': 'RECEIPT'}
    monkeypatch.setattr(sender, '_request', request)
    sender.send('GROUP_A', '研究完成', 'GRANT_A', 1)
    sender.send('GROUP_A', '补充', 'GRANT_A', 2)
    assert len(requests) == 3
    assert requests[1][0] == 'https://api.sgroup.qq.com/v2/groups/GROUP_A/messages'
    assert requests[1][1]['msg_id'] == 'GRANT_A' and requests[1][1]['msg_seq'] == 1
    with pytest.raises(ValueError):
        sender.send('../../private', '拒绝', 'GRANT_A', 1)
