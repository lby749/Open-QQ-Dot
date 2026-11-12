import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import house_status
import house_web
import main
from store import FriendStore


class HouseWebTests(unittest.TestCase):
    """2026-10-11 user: the friend house on the phone, served by the panel over Tailscale with a token."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        self.directory.mkdir(parents=True)
        main.dump_json(self.directory / 'friend.json', main.default_profile('12345', 'testfriend', True))
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': {
            'qq_id': '12345', 'display_name': 'testfriend', 'directory': str(self.directory), 'self_id': '77777'}}})
        store = FriendStore(self.directory)
        store.record_inbound('m1', 'Hello')
        store.close()
        self.bridge = 'not running'
        for item in (patch.object(house_status, 'ROOT', self.root),
                     patch.object(house_status, 'REGISTRY', self.root / 'friends.json'),
                     patch.object(house_web, 'invoke_bridge', lambda action: (0, self.bridge)),
                     patch.dict(house_web._bridge, at=0.0, text='')):
            item.start()
            self.addCleanup(item.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), house_web.Handler)
        self.secret = house_web.friend_token('12345')
        self.other_directory = self.root / 'friends' / '67890'
        self.other_directory.mkdir()
        main.dump_json(self.other_directory / 'friend.json', main.default_profile('67890', 'friend', True))
        registry = json.loads((self.root / 'friends.json').read_text('utf-8'))
        registry['friends']['67890'] = {'directory': str(self.other_directory), 'display_name': 'friend'}
        main.dump_json(self.root / 'friends.json', registry)
        self.other_secret = house_web.friend_token('67890')
        self.server.token = 'secret-token'  # the former server-wide token must have no authority
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, body=None, token=None):
        token = self.secret if token is None else token
        request = Request(f'http://127.0.0.1:{self.server.server_port}{path}',
                          data=None if body is None else json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def profile(self):
        return json.loads((self.directory / 'friend.json').read_text('utf-8'))

    def test_shared_study_http_permissions_and_reference_results(self):
        work = self.root / 'study'
        work.mkdir()
        (work / 'lesson.md').write_text('光合作用主要发生在叶绿体。', 'utf-8')
        profile = self.profile()
        profile['computer_tools'] = {'enabled': True, 'commands': False, 'project_path': str(work)}
        main.dump_json(self.directory / 'friend.json', profile)
        status, result = self.call('/api/friend/12345/study_action', {'operation': 'import', 'path': 'lesson.md'})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.call('/api/friend/12345/study_library')[1]['materials'][0]['id'], result['id'])
        from urllib.parse import quote
        hit = self.call('/api/friend/12345/study_library?operation=search&query=' + quote('光合作用'))[1]['items'][0]
        self.assertIn('叶绿体', hit['text'])
        self.assertEqual(self.call('/api/friend/12345/study_library', token=self.other_secret)[0], 403)
        profile['computer_tools']['mode'] = 'read_only'
        main.dump_json(self.directory / 'friend.json', profile)
        self.assertEqual(self.call('/api/friend/12345/study_action', {'operation': 'import', 'path': 'lesson.md'})[0], 400)

    def test_goal_http_controls_keep_friend_identity(self):
        from task_manager import TaskManager
        manager = TaskManager(self.root/'goals-test.db', self.root)
        with patch.object(TaskManager, 'get_instance', return_value=manager):
            status, response = self.call('/api/friend/12345/agent_goal_action', {
                'action':'create', 'description':'维护项目', 'scope':'测试目录',
                'completion_criteria':'生成文件', 'milestones':[{'title':'stage','description':'检查文件'}]})
            self.assertEqual(status, 200)
            gid = response['goal_id']
            self.assertEqual(self.call('/api/friend/12345/agent_goals')[1]['goals'][0]['owner_id'], '12345')
            self.assertEqual(self.call('/api/friend/67890/agent_goals', token=self.other_secret)[1]['goals'], [])
            self.assertEqual(self.call('/api/friend/67890/agent_goal_action',
                {'action':'cancel','goal_id':gid}, token=self.other_secret)[0], 403)
            self.assertEqual(self.call('/api/friend/12345/agent_goal_action', {'action':'pause','goal_id':gid})[0], 200)
            self.assertEqual(manager.get_goal(gid)['status'], 'PAUSED')

    def test_workflow_http_controls_keep_friend_ownership(self):
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        manager=TaskManager(self.root/'data/tasks.sqlite3',self.root)
        store=WorkflowStore(manager)
        identifier=store.save('12345','网页验收',{'scope':'当前项目','criteria':'返回数据',
            'steps':[{'title':'整理','description':'返回 count 字段','category':'data','spec':{'expected_keys':['count']}}]})
        with patch.object(TaskManager,'get_instance',return_value=manager),patch.object(manager,'start_worker'):
            self.assertEqual(len(self.call('/api/friend/12345/workflows')[1]['workflows']),1)
            self.assertEqual(self.call('/api/friend/67890/workflows',token=self.other_secret)[1]['workflows'],[])
            self.assertEqual(self.call('/api/friend/67890/workflow',{'action':'run','workflow_id':identifier},token=self.other_secret)[0],403)
            status,result=self.call('/api/friend/12345/workflow',{'action':'run','workflow_id':identifier})
            self.assertEqual(status,200)
            self.assertEqual(manager.get_goal(result['goal_id'])['owner_id'],'12345')
        manager.stop_worker()

    def test_page_is_open_but_data_needs_the_token(self):
        with urlopen(f'http://127.0.0.1:{self.server.server_port}/', timeout=10) as response:
            self.assertIn('好友小屋', response.read().decode('utf-8'))
        self.assertEqual(self.call('/api/overview', token='wrong')[0], 401)
        self.assertEqual(self.call('/api/overview')[0], 403)
        status, data = self.call('/api/session')
        self.assertEqual(status, 200)
        self.assertEqual(data, {'qq': '12345', 'name': 'testfriend'})
        self.assertEqual(self.call('/api/friend/99999')[0], 403)
        self.assertEqual(self.call('/api/session', token='secret-token')[0], 401)

    def test_each_token_is_persistent_and_cannot_read_or_change_another_friend(self):
        self.assertNotEqual(self.secret, self.other_secret)
        self.assertEqual(house_web.friend_token('12345'), self.secret)
        self.assertEqual(self.call('/api/session?qq=67890')[1]['qq'], '12345')
        self.assertEqual(self.call('/api/session', token=self.other_secret)[1]['qq'], '67890')
        before = (self.other_directory / 'friend.json').read_bytes()
        for suffix in ('', *(f'/{name}' for name in house_web.GET)):
            self.assertEqual(self.call('/api/friend/67890' + suffix)[0], 403, suffix)
        for name in house_web.POST:
            self.assertEqual(self.call('/api/friend/67890/' + name, {})[0], 403, name)
        self.assertEqual(self.call('/api/bridge', {'action': 'stop'})[0], 403)
        self.assertEqual((self.other_directory / 'friend.json').read_bytes(), before)

    def test_a_removed_friend_token_is_no_longer_accepted(self):
        registry = json.loads((self.root / 'friends.json').read_text('utf-8'))
        registry['friends'].pop('67890')
        main.dump_json(self.root / 'friends.json', registry)
        self.assertEqual(self.call('/api/session', token=self.other_secret)[0], 401)

    def test_admin_qq_pairing_token_still_cannot_cross_any_friend_route(self):
        import os
        calls = []
        def forbidden(*args):
            calls.append(args)
            return {'unexpected': True}
        with patch.dict(os.environ, {'QQ_ADMINS': '12345', 'QQ_BRIDGE_HOME': str(self.root)}), \
             patch.dict(house_web.GET, {key: forbidden for key in house_web.GET}), \
             patch.dict(house_web.POST, {key: forbidden for key in house_web.POST}):
            self.assertTrue(house_web.is_admin_user('12345'))
            for route in ('/api/overview', '/api/bridge', '/api/friend/67890',
                          *('/api/friend/67890/' + key for key in house_web.GET)):
                self.assertEqual(self.call(route)[0], 403, route)
            for key in house_web.POST:
                self.assertEqual(self.call('/api/friend/67890/' + key, {'admin': True})[0], 403, key)
            self.assertEqual(calls, [])

    def test_admin_qq_cannot_read_or_operate_other_owners_task_ids(self):
        import os
        from task_manager import TaskManager
        manager = TaskManager(self.root/'scoped-tasks.db', self.root)
        with patch.dict(os.environ, {'QQ_ADMINS': '12345', 'QQ_BRIDGE_HOME': str(self.root)}), \
             patch.object(TaskManager, 'get_instance', return_value=manager):
            mine = manager.create_task('自己的任务', '仅测试权限', owner_id='12345')
            other = manager.create_task('其他好友的私有任务', '不可泄漏', owner_id='67890')
            items = self.call('/api/friend/12345/agent_tasks?owner=67890&admin=true')[1]['tasks']
            self.assertEqual([row['task_id'] for row in items], [mine])
            self.assertEqual(self.call('/api/friend/12345/agent_task_detail?task_id=' + other)[0], 403)
            for action in ('pause', 'resume', 'cancel', 'approve', 'reject'):
                self.assertEqual(self.call('/api/friend/12345/agent_task_action', {'action': action, 'task_id': other})[0], 403)
            self.assertEqual(self.call('/api/friend/12345/agent_task_action', {
                'action': 'create', 'title': '非法跨好友依赖', 'description': '无模型调用', 'depends_on': [other]})[0], 403)
            self.assertEqual(self.call('/api/friend/12345/agent_task_detail?task_id=' + mine)[0], 200)
            self.assertEqual(manager.get_task(other).status, 'PENDING')
        manager.stop_worker()

    def test_admin_qq_approval_token_can_approve_only_its_own_request(self):
        import os
        from approval_manager import ApprovalManager, ApprovalCategory, ApprovalStatus
        manager = ApprovalManager(self.root/'scoped-approvals.db')
        own = manager.submit_request('task-own', ApprovalCategory.FILE_DELETE, '自己的审批', owner_id='12345')
        other = manager.submit_request('task-other', ApprovalCategory.FILE_DELETE, '其他好友的审批', owner_id='67890')
        with patch.dict(os.environ, {'QQ_ADMINS': '12345', 'QQ_BRIDGE_HOME': str(self.root)}), \
             patch.object(ApprovalManager, 'get_instance', return_value=manager), \
             patch.object(manager, '_after_decision'):
            self.assertEqual([row['req_id'] for row in self.call('/api/friend/12345/agent_approvals')[1]['approvals']], [own])
            self.assertEqual(self.call('/api/friend/12345/agent_approval_decision', {'req_id': other, 'decision': 'approve', 'admin': True})[0], 403)
            self.assertEqual(manager.get_request(other).status, ApprovalStatus.PENDING)
            self.assertEqual(self.call('/api/friend/12345/agent_approval_decision', {'req_id': own, 'decision': 'approve'})[0], 200)
            self.assertEqual(manager.get_request(own).status, ApprovalStatus.APPROVED)

    def test_admin_qq_notifications_remain_recipient_scoped(self):
        import os
        from notification_manager import NotificationManager
        manager = NotificationManager(self.root/'scoped-notifications.db')
        own = manager.publish('自己的通知', '消息', recipient_id='12345', channel='local')
        other = manager.publish('其他好友通知', '私有内容', recipient_id='67890', channel='local')
        with patch.dict(os.environ, {'QQ_ADMINS': '12345', 'QQ_BRIDGE_HOME': str(self.root)}), \
             patch.object(NotificationManager, 'get_instance', return_value=manager):
            self.assertEqual([row['id'] for row in self.call('/api/friend/12345/agent_notifications')[1]['notifications']], [own])
            for action in ('delete', 'mark_read'):
                status, answer = self.call('/api/friend/12345/agent_notification_action', {'action': action, 'id': other})
                self.assertEqual(status, 200)
                self.assertEqual(answer['status'], 'forbidden')
            self.assertEqual(len(manager.list_recent(recipient_id='67890', unread_only=True)), 1)

    def test_errors_do_not_expose_unrelated_paths_and_non_object_writes_are_refused(self):
        def unavailable(*args):
            raise OSError('private-other-friend-directory/secret.db')
        with patch.dict(house_web.GET, {'usage': unavailable}):
            status, result = self.call('/api/friend/12345/usage')
            self.assertEqual(status, 500)
            self.assertNotIn('private-other-friend-directory', json.dumps(result))
        self.assertEqual(self.call('/api/friend/12345/toggle', [])[0], 400)

    def test_admin_qq_cannot_operate_other_owner_research_by_project_id(self):
        import os
        from types import SimpleNamespace
        from research_tracker import ResearchTracker
        from task_manager import TaskManager
        tracker = SimpleNamespace(get_project=lambda _: SimpleNamespace(task_id='other-task'))
        manager = SimpleNamespace(get_task=lambda _: SimpleNamespace(owner_id='67890'))
        with patch.dict(os.environ, {'QQ_ADMINS': '12345', 'QQ_BRIDGE_HOME': str(self.root)}), \
             patch.object(ResearchTracker, 'get_instance', return_value=tracker), \
             patch.object(TaskManager, 'get_instance', return_value=manager):
            for action in ('stop', 'verify_stage', 'next_stage'):
                self.assertEqual(self.call('/api/friend/12345/agent_research_action', {
                    'project_id':'other-project', 'action':action, 'admin':True})[0], 403)

    def test_switches_history_and_toggle_reach_the_friend_files(self):
        status, data = self.call('/api/friend/12345')
        self.assertEqual(status, 200)
        self.assertEqual({item['key'] for item in data['switches']},
                         {'auto_reply', 'proactive', 'care', 'research'})
        self.assertEqual(self.call('/api/friend/12345/switch', {'key': 'romance_enabled', 'on': True})[0], 400)
        self.assertEqual(self.call('/api/friend/12345/switch', {'key': 'bridge_enabled', 'on': False})[0], 400)
        self.call('/api/friend/12345/toggle', {})
        self.assertFalse(self.profile()['bridge_enabled'])
        status, data = self.call('/api/friend/12345/history')
        self.assertEqual([m['body'] for m in data['messages']], ['Hello'])

    def test_packaged_running_status_is_recognized_with_uppercase(self):
        self.bridge = 'Running, PID 1234'
        data = self.call('/api/friend/12345')[1]
        self.assertEqual(data['state'], 'running')
        self.assertIn('自动回复：开启', data['summary'])
        self.assertNotIn('桥接未启动', data['summary'])

    def test_memory_saves_only_on_the_revision_it_was_read_at(self):
        _, data = self.call('/api/friend/12345/memory')
        status, saved = self.call('/api/friend/12345/memory', {'text': '喜欢猫', 'revision': data['revision']})
        self.assertEqual(status, 200)
        store = FriendStore(self.directory)
        try:
            self.assertIn('喜欢猫', store.memory())
        finally:
            store.close()
        status, error = self.call('/api/friend/12345/memory', {'text': '喜欢狗', 'revision': data['revision']})
        self.assertEqual(status, 400)
        self.assertIn('重新读取', error['error'])
        self.assertEqual(self.call('/api/friend/12345/memory', {'text': ' ', 'revision': saved['revision']})[0], 400)

    def test_retry_needs_a_running_bridge_and_a_failed_reply(self):
        status, error = self.call('/api/friend/12345/retry', {'message_id': 'm1'})
        self.assertEqual(status, 400)
        self.assertIn('启动桥接', error['error'])
        self.bridge = 'running, PID 1'
        status, error = self.call('/api/friend/12345/retry', {'message_id': 'm1'})
        self.assertEqual(status, 400)  # no failed attempt for this message
        store = FriendStore(self.directory)
        try:
            store.set_reply_attempt('m1', 'failed', 'testfailed')
        finally:
            store.close()
        self.assertEqual(self.call('/api/friend/12345/retry', {'message_id': 'm1'})[0], 200)

    def test_settings_edits_basic_info_style_and_the_proactive_page(self):
        """2026-10-11 user: the phone edits Friend Info / Speaking Style / proactivedays /  with the desktop panel's rules."""
        import house_status
        import persona_template
        with patch.object(persona_template, 'list_templates', return_value=[]):
            status, data = self.call('/api/friend/12345/settings')
        self.assertEqual(status, 200)
        self.assertEqual(data['basic_info']['nickname'], '')
        self.assertEqual(data['templates'], [{'id': '', 'name': '不使用模板'}])
        self.assertEqual(data['proactive']['window_start'], '10:00')
        self.assertIn('morning=开|06:00|10:00|1|1|', data['proactive']['care_text'])
        self.assertEqual(data['research_hours'], {'normal': 24, 'deep': 168})

        self.assertEqual(self.call('/api/friend/12345/basic', {'nickname': '小雨', 'age': '20', 'birthday': '03-04',
                                                               'interests': 'cat, badminton', 'deep_interests': 'astronomy',
                                                               'notes': '期末很忙'})[0], 200)
        info = self.profile()['basic_info']
        self.assertEqual((info['nickname'], info['interests'], info['deep_interests'], info['notes']),
                         ('小雨', 'cat, badminton', 'astronomy', '期末很忙'))

        values = {key: data['proactive'][key] for key in
                  ('rounds_min', 'rounds_max', 'bubbles_min', 'bubbles_max', 'window_start', 'window_end',
                   'topic_types', 'reply_pause_minutes', 'unanswered_round_limit', 'unanswered_round_pause_minutes')}
        lines = data['proactive']['care_text'].splitlines()
        lines[0] = 'morning=关|06:30|09:30|2|3|换个人问'
        values.update(rounds_max=20, reply_pause_minutes='90', topic_types='日常,游戏', care_text='\n'.join(lines))
        status, answer = self.call('/api/friend/12345/page_settings', values)
        self.assertEqual(status, 200)
        self.assertTrue('Save' in answer['message'] or '已保存' in answer['message'])
        profile = self.profile()
        self.assertEqual((profile['proactive']['rounds_max'], profile['proactive']['topic_types'],
                          profile['reply_pause_minutes'], profile['unanswered_round_limit']),
                         (20, ['日常', '游戏'], 90, 2))
        self.assertEqual(profile['proactive']['time_aware'], False)
        # 2026-10-13 user: follow-up count and the ignored mood; a form without them keeps the saved values
        self.assertEqual((profile['continuation_max'], profile['ignored_mood']), (1, {'enabled': False, 'after': 2}))
        self.assertEqual(self.call('/api/friend/12345/page_settings', {**values, 'continuation_max': '2'})[0], 200)
        self.assertEqual(self.call('/api/friend/12345/mood', {'ignored_mood_enabled': '1', 'ignored_mood_after': '3'})[0], 200)
        self.assertEqual(self.call('/api/friend/12345/emotion')[1]['mood']['ignored_mood_after'], 3)
        self.assertEqual((self.profile()['continuation_max'], self.profile()['ignored_mood']),
                         (2, {'enabled': True, 'after': 3}))
        self.assertEqual(self.call('/api/friend/12345/page_settings', {**values, 'continuation_max': '11'})[0], 400)
        morning = self.profile()['scheduled_care']['slots']['morning']
        self.assertEqual((morning['enabled'], morning['start'], morning['end'], morning['count_min'], morning['count'],
                          morning['instruction']), (False, '06:30', '09:30', 2, 3, '换个人问'))

        # bad values are refused and nothing is written
        self.assertEqual(self.call('/api/friend/12345/page_settings', {**values, 'rounds_min': '0'})[0], 400)
        self.assertEqual(self.call('/api/friend/12345/page_settings', {**values, 'care_text': 'morning=开|06:00'})[0], 400)
        self.assertEqual(self.call('/api/friend/12345/page_settings',
                                   {**values, 'care_text': 'snooze=开|06:00|07:00|1|1|'})[0], 400)
        self.assertEqual(self.profile()['proactive']['rounds_min'], data['proactive']['rounds_min'])

    def test_settings_applies_a_persona_template_and_asks_for_topic_research(self):
        import persona_template
        import topic_research
        profile = self.profile()
        profile['persona_template'] = 't1'
        main.dump_json(self.directory / 'friend.json', profile)
        with patch.object(persona_template, 'list_templates',
                          return_value=[{'id': 't1', 'name': '小雨', 'status': 'active',
                                         'source': {'speaker': '小雨'}},
                                        {'id': 't2', 'name': '另一位的私人人格', 'status': 'active',
                                         'source': {'speaker': '另一位'}}]), \
                patch.object(topic_research, 'request_refresh') as requested:
            status, data = self.call('/api/friend/12345/settings')
            self.assertEqual([item['name'] for item in data['templates']], ['不使用模板', '小雨 · 小雨'])
            self.assertEqual(self.call('/api/friend/12345/style', {'style': '说话短一点', 'persona_template': 't1',
                                                                   'proactive_style': '像老朋友'})[0], 200)
            profile = self.profile()
            self.assertEqual((profile['style'], profile['persona_template'], profile['proactive']['style']),
                             ('说话短一点', 't1', '像老朋友'))
            self.assertEqual(self.call('/api/friend/12345/style',
                                       {'persona_template': 't2'})[0], 400)
            self.assertEqual(self.call('/api/friend/12345/style',
                                       {'style': '', 'persona_template': '', 'proactive_style': ''})[0], 200)
            self.assertNotIn('persona_template', self.profile())
            self.assertEqual(self.call('/api/friend/12345/research', {'kind': 'deep', 'hours': '0'})[0], 200)
        requested.assert_called_once()
        self.assertEqual(requested.call_args[0][1], 'deep')
        self.assertEqual(self.profile()['research_refresh_hours'], {'deep': 0})

    def test_mobile_persona_policies_preserve_old_clients_and_validate_before_saving(self):
        profile = self.profile()
        profile.update(emotion_policy='按人设判断情绪', timing_policy='不打扰休息', memory_policy='按需提起往事')
        main.dump_json(self.directory / 'friend.json', profile)
        self.assertEqual(self.call('/api/friend/12345/settings')[1]['emotion_policy'], '按人设判断情绪')
        self.assertEqual(self.call('/api/friend/12345/style', {'style': '简短自然'})[0], 200)
        self.assertEqual(self.profile()['timing_policy'], '不打扰休息')
        self.assertEqual(self.call('/api/friend/12345/style',
            {'style': '简短自然', 'emotion_policy': '', 'memory_policy': '只提相关记忆'})[0], 200)
        self.assertNotIn('emotion_policy', self.profile())
        self.assertEqual(self.profile()['memory_policy'], '只提相关记忆')
        before = (self.directory / 'friend.json').read_bytes()
        self.assertEqual(self.call('/api/friend/12345/style', {'timing_policy': '长' * 8001})[0], 400)
        self.assertEqual((self.directory / 'friend.json').read_bytes(), before)

    def test_mobile_voice_uses_friend_settings_without_exposing_or_replacing_local_paths(self):
        profile = self.profile()
        profile['voice_reply'] = {'mode':'off', 'tier':'base', 'speaker':60,
            'reference_wav':'media/voice_reference.wav', 'reference_text':'原参考文字'}
        main.dump_json(self.directory / 'friend.json', profile)
        status, data = self.call('/api/friend/12345/voice')
        self.assertEqual(status, 200)
        self.assertTrue(data['reference_set'])
        self.assertNotIn('reference_wav', data['settings'])
        self.assertEqual(data['settings']['speaker'], 60)
        self.assertEqual({item['id'] for item in data['models']}, {'small', 'base', 'large'})
        self.assertEqual(self.call('/api/friend/12345/voice', {'mode':'sometimes', 'tier':'base',
            'speaker':7, 'ratio':0.4, 'speed':1.2, 'max_chars':70, 'design':'平静自然',
            'reference_wav':'C:/other/private.wav', 'python':'untrusted.exe'})[0], 200)
        value = self.profile()['voice_reply']
        self.assertEqual((value['mode'],value['speaker'],value['ratio'],value['speed']), ('sometimes',7,0.4,1.2))
        self.assertEqual(value['reference_wav'], 'media/voice_reference.wav')
        self.assertEqual(value['reference_text'], '原参考文字')
        self.assertNotIn('python', value)
        before = (self.directory / 'friend.json').read_bytes()
        for body in ({'mode':'invalid'}, {'tier':'invalid'}, {'speed':float('nan')}, {'ratio':2},
                     {'max_chars':5.5}, {'speaker':999}, {'reference_text':'长'*501}):
            self.assertEqual(self.call('/api/friend/12345/voice', body)[0], 400, body)
            self.assertEqual((self.directory / 'friend.json').read_bytes(), before)
        self.assertEqual(self.call('/api/friend/67890/voice')[0], 403)
        self.assertEqual(self.call('/api/friend/67890/voice', {'mode':'always'})[0], 403)

    def test_mobile_care_form_saves_structured_slots_and_keeps_global_switch(self):
        data = self.call('/api/friend/12345/settings')[1]['proactive']
        data['care']['morning'].update(start='06:20', end='09:20', enabled=True,
                                      count_min='1', count='2', instruction='关心今天的安排')
        self.assertEqual(self.call('/api/friend/12345/page_settings', data)[0], 200)
        # The legacy care_text must be omitted by the new form, or it takes precedence.
        data.pop('care_text')
        self.assertEqual(self.call('/api/friend/12345/page_settings', data)[0], 200)
        saved = self.profile()['scheduled_care']['slots']['morning']
        self.assertEqual((saved['start'],saved['end'],saved['count'],saved['instruction']),
                         ('06:20','09:20',2,'关心今天的安排'))

    def test_every_desktop_page_has_its_phone_route(self):
        """2026-10-12 user: the phone had fewer pages than the desktop panel."""
        import generation_log
        store = FriendStore(self.directory)
        store.set('sticker:abc123', json.dumps({'qq_saved': True}))
        store.record_emotions('happy', '安心', '回复', 60, '对方分享了好事')
        generation_log.start(store, 'normal reply', '【SYSTEM_RULES】 SOURCE=program TYPE=RULE\n规则正文')
        generation_log.finish(store, response='{"messages":["okay!"]}')
        record_id = generation_log.recent(store, 1)[0]['id']
        store.close()
        (self.directory / 'media').mkdir()
        (self.directory / 'media' / 'abc123.preview.png').write_bytes(b'\x89PNG fake')

        status, data = self.call('/api/friend/12345/stickers')
        self.assertEqual((status, [item['key'] for item in data['items']]), (200, ['abc123']))
        self.assertEqual(self.call('/api/friend/12345/sticker', {'key': 'abc123', 'meaning': 'happy'})[0], 200)
        self.assertEqual(self.call('/api/friend/12345/stickers')[1]['items'][0]['card']['by'], 'user')
        self.assertEqual(self.call('/api/friend/12345/sticker', {'key': '../friend', 'meaning': 'x'})[0], 400)
        request = Request(f'http://127.0.0.1:{self.server.server_port}/api/friend/12345/sticker_image?key=abc123',
                          headers={'Authorization': 'Bearer ' + self.secret})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.read(), b'\x89PNG fake')
        for key in ('../friend', 'missing'):
            self.assertEqual(self.call(f'/api/friend/12345/sticker_image?key={key}')[0], 404)
        self.assertEqual(self.call('/api/friend/12345/sticker_image?key=abc123', token=self.other_secret)[0], 403)

        status, data = self.call('/api/friend/12345/emotion')
        self.assertEqual((status, data['rows'][0]['self_label'], data['state']['self']), (200, '安心', '安心'))
        status, data = self.call(f'/api/friend/12345/record?id={record_id}')
        self.assertEqual((status, data['output'], data['blocks'][0]['key']), (200, '{"messages":["okay!"]}', 'SYSTEM_RULES'))
        self.assertEqual(self.call('/api/friend/12345/context')[1]['records'][0]['id'], record_id)
        self.assertEqual(self.call('/api/friend/12345/topics'), (200, {'items': []}))
        self.assertEqual(self.call('/api/friend/12345/template'), (200, {'template': None}))

    def test_model_settings_from_the_phone_keep_the_key_on_the_computer(self):
        status, data = self.call('/api/friend/12345/model')
        self.assertEqual((status, data['provider'], data['key_saved']), (200, 'codex', False))
        body = {'provider': 'claude', 'codex_model': 'gpt-6-luna', 'codex_effort': 'low', 'codex_fast': True,
                'claude_model': 'sonnet', 'claude_effort': 'low', 'api_protocol': 'openai-completions',
                'api_base_url': '', 'model': '', 'api_effort': 'low', 'compact_tokens': '300000',
                'research_effort': 'low', 'research_fast': False}
        self.assertEqual(self.call('/api/friend/12345/model', body)[0], 200)
        profile = self.profile()
        self.assertEqual((profile['model_api']['provider'], profile['model_api']['claude_model'],
                          profile['model_api']['compact_tokens'], profile['research_fast']), ('claude', 'sonnet', 300000, False))
        status, data = self.call('/api/friend/12345/model', {**body, 'provider': 'custom'})
        self.assertEqual(status, 400)
        self.assertIn('电脑', data['error'])  # no key yet; the key is entered only on the computer
        self.assertEqual(self.call('/api/friend/12345/model', {**body, 'compact_tokens': '10'})[0], 400)
        self.assertEqual(self.profile()['model_api']['provider'], 'claude')

    def test_blue_phone_background_settings_are_real_scoped_edits(self):
        import codex_settings
        with patch.object(codex_settings, 'model_options', return_value={'gpt-6-luna': ['low', 'high']}):
            status, data = self.call('/api/friend/12345/background_settings')
            self.assertEqual(status, 200)
            original = self.profile()
            body = {'revision': data['revision'], 'background_model': {'provider': 'codex',
                    'model': 'gpt-6-luna', 'effort': 'low'}, 'quota_reply': '额度暂时用完啦，恢复后接着聊。'}
            self.assertEqual(self.call('/api/friend/12345/background_settings', body)[0], 200)
            fresh = self.profile()
            self.assertEqual(fresh['background_model'], body['background_model'])
            self.assertEqual(fresh['model_api']['quota_reply'], body['quota_reply'])
            self.assertEqual(fresh['model_api'].get('provider', 'codex'), (original.get('model_api') or {}).get('provider', 'codex'))
            self.assertEqual(self.call('/api/friend/12345/background_settings', token=self.other_secret)[0], 403)
            self.assertEqual(self.call('/api/friend/67890/background_settings', body)[0], 403)
            self.assertEqual(self.call('/api/friend/12345/background_settings', {**body, 'owner':'67890'})[0], 403)
            # A stale screen cannot overwrite a newer setting, even for the same owner.
            self.assertEqual(self.call('/api/friend/12345/background_settings', {**body, 'quota_reply':'旧草稿'})[0], 400)
            reread = self.call('/api/friend/12345/background_settings')[1]
            self.assertEqual(reread['quota_reply'], body['quota_reply'])
            self.assertEqual(self.call('/api/friend/12345/background_settings', {**body,
                'revision':reread['revision'], 'quota_reply':'长'*1201})[0], 400)

    def test_blue_phone_manages_the_same_friend_owned_mother_rooms(self):
        import mobile_collaboration
        with patch.object(mobile_collaboration, 'available', return_value=['codex']):
            status, created = self.call('/api/friend/12345/collaboration_rooms', {'action':'create', 'title':'蓝色版真实房间管理'})
            self.assertEqual(status, 200)
            identifier = created['room']['id']
            _, added = self.call('/api/friend/12345/collaboration_rooms', {'action':'add', 'room_id':identifier,
                'provider':'codex', 'name':'主智能体', 'role':'规划并核查结果'})
            host = added['room']['members'][0]['id']
            self.assertEqual(self.call('/api/friend/12345/collaboration_rooms',
                {'action':'set_host', 'room_id':identifier, 'member_id':host})[0], 200)
            _, detail = self.call('/api/friend/12345/collaboration_rooms?room_id=' + identifier)
            self.assertEqual(detail['room']['host_id'], host)
            self.assertEqual(self.call('/api/friend/67890/collaboration_rooms?room_id=' + identifier)[0], 403)
            self.assertEqual(self.call('/api/friend/12345/collaboration_rooms?room_id=' + identifier, token=self.other_secret)[0], 403)

    def test_the_paired_phone_registers_only_for_the_authenticated_friend(self):
        """2026-10-11 user: the app POSTs its MCP address and token; the model then asks the phone on demand."""
        self.assertFalse(self.call('/api/friend/12345')[1]['phone']['connected'])
        other_before = (self.other_directory / 'friend.json').read_bytes()
        self.assertEqual(self.call('/api/phone/register', {'qq': '67890'})[0], 403)
        status, error = self.call('/api/phone/register',
                                  {'mcp_url': 'http://192.168.1.9:8080/mcp', 'token': 'x' * 20})
        self.assertEqual(status, 400)
        self.assertIn('Tailscale', error['error'])
        self.assertEqual(self.call('/api/phone/register',
                                   {'mcp_url': 'http://100.64.0.2:8080/mcp', 'token': 'short'})[0], 400)
        status, answer = self.call('/api/phone/register',
                                   {'mcp_url': 'http://100.64.0.2:8080/mcp', 'token': 'phone-token-0123456789'})
        self.assertEqual(status, 200)
        self.assertEqual(answer['friends'], ['12345'])
        self.assertEqual((self.other_directory / 'friend.json').read_bytes(), other_before)
        profile = self.profile()
        self.assertEqual((profile['phone_mcp_url'], profile['phone_mcp_token']),
                         ('http://100.64.0.2:8080/mcp', 'phone-token-0123456789'))
        phone = self.call('/api/friend/12345')[1]['phone']
        self.assertTrue(phone['connected'])
        self.assertEqual(phone['mcp_url'], 'http://100.64.0.2:8080/mcp')
        # the token itself is never echoed back to a page that only needs the status
        self.assertNotIn('phone-token-0123456789', json.dumps(phone))


if __name__ == '__main__':
    unittest.main()
