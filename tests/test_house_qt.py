import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import main
from store import FriendStore

try:
    from PySide6.QtWidgets import QApplication
    import house_qt.common as common
    import house_qt.pages_friend as pages_friend
    import house_qt.pages_library as pages_library
    import house_qt.pages_more as pages_more
    import house_qt.task_center as pages_task_center
    import house_qt.capsules as pages_capsules
    import house_qt.qq_connection as pages_connection
    from PySide6.QtCore import Qt, QEvent
    import house_qt.window as window_module
    import house_status
except ImportError:  # PySide6 is required by the current desktop UI
    QApplication = None


@unittest.skipIf(QApplication is None, 'PySide6 not installed')
class HouseQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        common.init_ui_thread()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / 'friends' / '12345'
        self.directory.mkdir(parents=True)
        main.dump_json(self.directory / 'friend.json', main.default_profile('12345', 'testfriend', True))
        main.dump_json(self.root / 'friends.json', {'friends': {'12345': {
            'qq_id': '12345', 'display_name': 'testfriend', 'directory': str(self.directory), 'self_id': '77777'}}})
        FriendStore(self.directory).close()
        patches = [patch.object(module, 'ROOT', self.root) for module in
                   (house_status, window_module, pages_friend, pages_more, pages_capsules, pages_connection)]
        patches += [patch.dict(os.environ, {'QQ_BRIDGE_HOME': str(self.root)}),
                    patch('house_qt.first_run.should_show', return_value=False),
                    patch.object(house_status, 'REGISTRY', self.root / 'friends.json'),
                    patch.object(window_module, 'REGISTRY', self.root / 'friends.json'),
                    patch.object(window_module, 'invoke_bridge', return_value=(0, 'running PID 1')),
                    patch('message_classification.enqueue_for_database')]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = window_module.HouseWindow()
        self.window.status_timer.stop()
        self.addCleanup(self.close_window)
        self.settle()

    def close_window(self):
        self.window.exiting = True
        self.window.tray.hide()
        self.window.close()
        for pool in (common.POOL, common.LONG_POOL, self.window.bridge_pool):
            pool.submit(lambda: None).result()  # background reads finished: no database file still open
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.settle()
        import gc
        gc.collect()
        try:
            self.temp.cleanup()
        except OSError:
            pass

    def settle(self, until=lambda: True, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.02)
            if until():
                self.app.processEvents()
                return
        self.fail('timed out waiting for the panel')

    def profile(self):
        return json.loads((self.directory / 'friend.json').read_text(encoding='utf-8'))

    def test_a_greeting_in_the_console_is_chat_not_a_task(self):
        """2026-10-26 user: 你好 created task_20261026_141126_31727e. Automatic routing must say chat."""
        import task_spec
        self.assertEqual(task_spec.input_intent('你好', 'workbench-auto'), 'chat')
        self.assertEqual(task_spec.input_intent('今天任务好多啊', 'workbench-auto'), 'chat')
        self.assertEqual(task_spec.input_intent('帮我修复登录 bug', 'workbench-auto'), 'task')
        self.assertEqual(task_spec.input_intent('任务：整理资料', 'workbench-auto'), 'task')
        page = self.window.pages['workbenchPage']
        self.window.show_friend('12345', 'workbenchPage')
        self.settle()
        self.assertEqual(page.input_mode_combo.currentData(), 'auto')
        self.assertEqual(page.resolve_input_mode('你好'), 'chat')
        self.assertEqual(page.resolve_input_mode('帮我修复登录 bug'), 'task')
        page.input_mode_combo.setCurrentIndex(page.input_mode_combo.findData('task'))
        self.assertEqual(page.resolve_input_mode('你好'), 'task')  # an explicit choice is respected
        page.input_mode_combo.setCurrentIndex(page.input_mode_combo.findData('auto'))

    def test_task_center_creates_lists_and_undoes(self):
        from task_manager import TaskManager, TaskStatus
        (self.root / 'data').mkdir(exist_ok=True)
        manager = TaskManager(db_path=self.root / 'data' / 'tasks.sqlite3', workspace=self.root)
        self.addCleanup(manager.stop_worker)
        for name, value in (('get_instance', manager), ('start_worker', None)):
            patcher = patch.object(TaskManager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        page = self.window.pages['taskCenterPage']
        self.window.switchTo(self.window.workspaces['tasks'])
        page.load()
        self.settle(lambda: page.items and page.records_owner == '12345')
        self.assertFalse(page.items['dev_project']['available'])  # this friend has no computer tools
        self.assertTrue(page.items['general']['available'])
        general = next(child for group in range(page.tree.topLevelItemCount())
                       for child in [page.tree.topLevelItem(group).child(i)
                                     for i in range(page.tree.topLevelItem(group).childCount())]
                       if child.data(0, Qt.UserRole) == 'general')
        page.tree.setCurrentItem(general)
        self.assertEqual(set(page.inputs), {'description', 'workspace'})
        page.inputs['description'].setPlainText('整理本周会议纪要')
        page.notify.setChecked(True)
        toasts = []
        with patch.object(pages_task_center, 'toast', side_effect=lambda *args, **kw: toasts.append(args[1:])):
            page.create()
            self.settle(lambda: toasts, seconds=90)
        self.assertEqual(toasts[0][0], 'success', toasts)
        self.settle(lambda: any(record.get('title', '').startswith('整理本周') for _, record in page.records))
        kind, record = page.selected()
        self.assertEqual((kind, record['checkpoint']['template'], record['checkpoint']['notify_qq']),
                         ('task', 'general', True))
        self.assertEqual(page.table.item(page.table.currentRow(), 0).text(), '通用任务')
        with patch.object(pages_task_center, 'confirm', return_value=True):
            page.control('undo')
            self.settle(lambda: manager.get_task(record['task_id']).status == TaskStatus.CANCELLED)
        page.filter.setCurrentIndex(3)  # 已结束
        self.settle(lambda: page.table.rowCount() == 1)

    def test_shared_learning_controls_import_and_search_without_web_workbench(self):
        project = self.root / 'study-project'
        project.mkdir()
        (project / 'lesson.md').write_text('光合作用主要发生在叶绿体。', 'utf-8')
        profile = self.profile()
        profile['computer_tools'] = {'enabled': True, 'commands': False, 'project_path': str(project)}
        main.dump_json(self.directory / 'friend.json', profile)
        page = self.window.pages['taskCenterPage']
        page.study_path.setText('lesson.md')
        page.study_action('import')
        self.settle(lambda: '已导入' in page.study_result.toPlainText())
        page.study_query.setText('光合作用')
        page.study_action('search')
        self.settle(lambda: '叶绿体' in page.study_result.toPlainText())
        self.assertIn('引用：mat_', page.study_result.toPlainText())

    def test_workflow_editor_saves_corrections_and_keeps_chosen_schedule(self):
        from task_manager import TaskManager
        from task_workflows import WorkflowStore
        import session_manager
        import house_qt.workflows as workflows
        manager=TaskManager(self.root/'data/tasks.sqlite3',self.root)
        with patch.object(TaskManager,'get_instance',return_value=manager), \
                patch.object(manager,'start_worker',return_value=True), \
                patch.object(session_manager,'ROOT',self.root),patch.object(workflows,'toast'):
            page=self.window.pages['workflowPage']
            page.load()
            page.title.setText('周期日报')
            page.stage_title.setText('整理数据')
            page.description.setPlainText('返回包含 count 的 JSON')
            page.category.setCurrentIndex(page.category.findData('data'))
            page.backend.setCurrentIndex(page.backend.findData('codex'))
            page.keys.setText('count')
            page.interval.setValue(17); page.rounds.setValue(3)
            page.first.setDateTime(page.first.dateTime().addSecs(3600))
            page.enable()
            record=WorkflowStore(manager).get(page.identifier,'12345')
            self.assertEqual((record['interval_seconds'],record['remaining_runs'],record['enabled']),(1020,3,1))
            self.assertEqual(record['definition']['steps'][0]['backend'],'codex')
            page.correction.setPlainText('数值必须来自真实记录')
            self.assertTrue(page.save())
            record=WorkflowStore(manager).get(page.identifier,'12345')
            self.assertEqual((record['version'],record['enabled']),(2,0))
            self.assertEqual(record['versions'][0]['correction'],'数值必须来自真实记录')
            page.auto_finish.setChecked(True)
            self.assertFalse(page.criteria.isEnabled())
            page.run_once()
            self.assertEqual(len(manager.list_goals('12345')),1)
            self.assertEqual(WorkflowStore(manager).get(page.identifier,'12345')['definition']['review_policy'],'verified_stages')
        manager.stop_worker()

    def test_every_page_opens_for_the_friend(self):
        for page in [self.window.overview, *self.window.pages.values()]:
            self.window.switchTo(page)
            self.settle()
            if page is self.window.pages['dotlinkPage']:
                self.assertEqual(self.window.pages['qqConnectionPage'].mode.currentRouteKey(), 'legacy')
                self.assertFalse(self.window.pages['qqConnectionPage'].legacy_content.isHidden())
                self.assertIs(self.window.workspaces['qq'].tabs.currentWidget(), self.window.pages['qqConnectionPage'])
                continue
            current = self.window.stackedWidget.currentWidget()
            while current is not page:
                if hasattr(current, 'tabs'):
                    current = current.tabs.currentWidget()
                elif hasattr(current, 'stacked'):
                    current = current.stacked.currentWidget()
                else:
                    break
            self.assertIs(current, page)
        self.assertEqual(self.window.friend_title.text(), 'testfriend')

    def test_console_is_default_home_and_overview_remains_accessible(self):
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['home'])
        self.assertIs(self.window.workspaces['home'].tabs.currentWidget(), self.window.pages['workbenchPage'])
        self.assertEqual(self.window.pages['workbenchPage'].loaded_for, '12345')
        self.window.switchTo(self.window.overview)
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['friends'])
        self.assertEqual(self.window.friend_title.text(), '好友总览')
        # 2026-10-26 user: switching friend refreshes the page being read instead of jumping to the console,
        # so the assertion below now expects the overview to stay on screen and redraw for the same friend.
        self.window.show_friend('12345')
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['friends'])
        self.assertIs(self.window.workspaces['friends'].tabs.currentWidget(), self.window.overview)

    def test_reload_binds_first_friend_and_console_navigation_works(self):
        self.window.current_qq = None
        self.window.reload_friends()
        self.assertEqual(self.window.current_qq, '12345')
        self.window.show_friend('12345', 'workbenchPage')
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['home'])
        self.assertIs(self.window.workspaces['home'].tabs.currentWidget(), self.window.pages['workbenchPage'])

    def test_empty_friend_console_preserves_draft_without_network(self):
        page = self.window.pages['workbenchPage']
        self.window.current_qq = None
        page.input_edit.setPlainText('保留草稿')
        with patch.object(page, 'send_qq_message') as send:
            page.send_message()
        send.assert_not_called()
        self.assertEqual(page.input_edit.toPlainText(), '保留草稿')

    def test_native_console_turn_reads_actual_topic_library(self):
        import session_manager
        with patch.object(session_manager, 'ROOT', self.root), \
                patch('topic_research.items', return_value=[{'title': '自动化', 'summary': '有效的话题资料接口'}]), \
                patch('main.codex_text', return_value=('原生控制台正常', None)) as model:
            self.window.show_friend('12345', 'workbenchPage')
            page = self.window.pages['workbenchPage']
            session = session_manager.create_session('12345', title='原生控制台回归')
            page.active_ws_id = 'default'
            page.load_session_ui(session['id'])
            page.input_mode_combo.setCurrentIndex(page.input_mode_combo.findData('chat'))  # 2026-10-26: 自动判断 is the first item
            page.input_edit.setPlainText('请验证原生控制台')
            page.send_message()
            self.settle(until=lambda: model.called and not page.is_generating)
            self.assertIn('有效的话题资料接口', model.call_args.args[0])
            stored = session_manager.load_session('12345', session['id'])
            self.assertEqual(stored['messages'][-1]['content'], '原生控制台正常')

    def test_online_dotlink_connect_also_starts_bridge(self):
        with patch('socket.socket') as socket, patch.object(self.window, 'request') as request:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            self.window.active_connect_personal_qq()
        request.assert_called_once_with('start')

    def test_capsule_has_own_sidebar_route_and_discards_previous_friends_input(self):
        page = self.window.pages['capsulePage']
        self.window.switchTo(page)
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['capsules'])
        self.assertEqual(self.window.workspaces['tasks'].tabs.indexOf(page), -1)
        page.input.setPlainText('仅属于第一位好友的材料')
        second = self.root / 'friends/67890'
        second.mkdir()
        main.dump_json(second / 'friend.json', main.default_profile('67890', '好友二', False))
        self.window.records['67890'] = dict(qq_id='67890', display_name='好友二', directory=str(second))
        self.window.show_friend('67890')
        self.settle()
        self.assertEqual(page.owner, '67890')
        self.assertEqual(page.input.toPlainText(), '')
        self.assertEqual(page.runs, [])
        self.assertFalse(page.resume_button.isEnabled())

    def test_connection_defaults_to_official_with_complete_legacy_ui_collapsed(self):
        page = self.window.pages['qqConnectionPage']
        self.assertEqual(page.mode.currentRouteKey(), 'official')
        self.assertTrue(page.legacy_content.isHidden())
        self.assertFalse(page.official_card.isHidden())
        self.assertIs(page.legacy, self.window.pages['dotlinkPage'])
        with patch.object(page, 'connect_bot') as official, patch.object(self.window, 'active_connect_personal_qq') as legacy:
            self.window.active_connect_qq()
        official.assert_called_once()
        legacy.assert_not_called()
        with patch.object(page.legacy, 'shown'):
            self.window.switchTo(page.legacy)
        self.assertFalse(page.legacy_content.isHidden())
        self.assertTrue(page.official_card.isHidden())
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['qq'])

    def test_closing_window_keeps_background_running(self):
        from PySide6.QtGui import QCloseEvent
        event = QCloseEvent()
        with patch.object(self.window, 'exit') as exit_app, patch.object(self.window, 'hide') as hide, \
                patch.object(self.window.tray, 'showMessage'):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        hide.assert_called_once()
        exit_app.assert_not_called()

    def test_autonomy_form_submits_selected_owner_and_explicit_authorization(self):
        page = self.window.pages['autonomyPage']
        # 2026-10-26 user asked for one task per entry, so the form takes a list instead of a text box.
        page.entry.setText('生成结构化清单')
        page.add_entry()
        page.entry.setText('生成第二份清单')
        page.add_entry()
        self.assertEqual(page.entries, ['生成结构化清单', '生成第二份清单'])
        self.assertEqual(page.submit.text(), '规划 2 个目标')
        page.scope.setText('仅返回文字')
        page.criteria.setText('JSON 字段 checklist 存在')
        with patch('house_web.post_agent_goal_action', return_value={'task_id': 'plan', 'message': '已提交'}) as submit:
            page.create()
            self.settle(until=lambda: submit.call_count == 2 and page.submit.isEnabled())
            self.assertEqual([call.args[0] for call in submit.call_args_list], ['12345', '12345'])
            self.assertTrue(submit.call_args.args[1]['auto_start'])
            self.assertEqual(submit.call_args.args[1]['scope'], '仅返回文字')
            self.assertEqual([call.args[1]['description'] for call in submit.call_args_list],
                             ['生成结构化清单', '生成第二份清单'])
        self.assertEqual(page.entries, [])  # created entries leave the list

    def test_web_button_starts_server_before_opening_browser(self):
        with patch('launcher_codex.start_server_if_needed') as start, patch('webbrowser.open') as opened:
            self.assertIs(self.window.workspaces['friends'].tabs.widget(0), self.window.overview)
            self.window.workspaces['friends'].tabs.widget(1).open_web()
            self.settle(until=lambda: opened.called)
            start.assert_called_once_with()
            opened.assert_called_once_with('http://127.0.0.1:8791/#home')

    def test_template_drafts_preview_after_building(self):
        """2026-10-12: show_preview imported a removed persona_template.style_text, so new drafts never showed."""
        style = {'bubbles_per_reply': 3.17, 'median_chars_per_bubble': 9, 'face_rate': 0.082, 'image_rate': 0.095,
                 'question_rate': 0.057, 'exclaim_rate': 0.015, 'tilde_rate': 0.001, 'period_rate': 0.012,
                 'laugh_rate': 0.059, 'top_endings': ['啊', '呢']}
        page = self.window.pages['templatePage']
        page.show_preview([{'name': '小雨', 'model': 'cloud', 'summary': '爱吐槽', 'style': style, 'examples': [1, 2],
                            'traits': [{'trait': '爱笑', 'evidence': ['haha']}],
                            'source': {'speaker': '小雨', 'messages': 100, 'pairs': 40}}])
        text = page.preview.toPlainText()
        self.assertIn('平均每次回复 3.17 条消息', text)
        self.assertIn('常用句尾字：啊、呢', text)

    def test_saves_reach_the_friend_files(self):
        basic = self.window.pages['basicPage']
        self.window.switchTo(basic)
        self.settle()
        basic.fields['nickname'].setText('小雨')
        basic.interest_fields['interests'].setText('cat, badminton')
        basic.interest_fields['deep_interests'].setText('astronomy')
        basic.notes.setPlainText('期末很忙')
        basic.save_basic()
        info = self.profile()['basic_info']
        self.assertEqual((info['nickname'], info['interests'], info['deep_interests'], info['notes']),
                         ('小雨', 'cat, badminton', 'astronomy', '期末很忙'))
        # 2026-10-11 Settings: the topic library has its own page; its switch is on the status page
        topic = self.window.pages['topicPage']
        self.window.switchTo(topic)
        self.settle()
        topic.hours['deep'].setText('0')
        topic.save_frequency('deep')
        self.assertEqual(self.profile()['research_refresh_hours'], {'deep': 0})

        status = self.window.pages['statusPage']
        self.window.switchTo(status)
        self.settle()
        status.switches['research'].setChecked(False)  # switches save at once
        self.settle()
        self.assertFalse(self.profile()['daily_research_enabled'])
        status.switches['auto_reply'].setChecked(False)
        self.settle()
        self.assertFalse(self.profile()['auto_reply'])
        status.switches['proactive'].setChecked(True)
        self.settle()
        self.assertTrue(self.profile()['proactive']['enabled'])
        status.switches['care'].setChecked(True)
        self.settle()
        self.assertTrue(self.profile()['scheduled_care']['enabled'])

        style = self.window.pages['stylePage']
        self.window.switchTo(style)
        self.settle()
        style.style.setPlainText('说话短一点')
        style.save()
        self.assertEqual(self.profile()['style'], '说话短一点')
        style.proactive_style.setPlainText('像老朋友一样开场')
        style.save_proactive_style()
        self.assertEqual(self.profile()['proactive']['style'], '像老朋友一样开场')

        # reply policies: filled text is saved, cleared text removes the key (empty = feature off)
        style.policies['emotion_policy'].setPlainText('被冷落会有点失落')
        style.policies['memory_policy'].setPlainText('自然提起近况')
        style.save_policies()
        self.assertEqual((self.profile()['emotion_policy'], self.profile()['memory_policy']),
                         ('被冷落会有点失落', '自然提起近况'))
        self.assertNotIn('timing_policy', self.profile())
        style.policies['memory_policy'].setPlainText('  ')
        style.save_policies()
        self.assertNotIn('memory_policy', self.profile())
        self.assertEqual(self.profile()['style'], '说话短一点')

        # persona switches: preview until applied; unknown and unwired flags are kept as they were
        profile = self.profile()
        profile['feature_flags'] = {'future_flag': True, 'sticker_metadata_enabled': True}
        main.dump_json(self.directory / 'friend.json', profile)
        style.load()
        self.assertTrue(style.flags['sticker_metadata_enabled'].isChecked())
        self.assertFalse(style.flags['sticker_metadata_enabled'].isEnabled())
        style.flags['persona_enabled'].setChecked(True)
        self.assertNotIn('persona_enabled', self.profile()['feature_flags'])
        style.apply_feature_flags()
        self.assertEqual(self.profile()['feature_flags'], {'future_flag': True, 'sticker_metadata_enabled': True,
                                                           'persona_enabled': True, 'hybrid_enabled': False})
        self.assertIn('人格检索', style.flag_state.text())
        with patch('persona_workbench_host.launch', side_effect=[True, False]) as launch:
            style.open_workbench('friend')
            style.open_workbench('friend')
        launch.assert_called_with('friend', '12345', self.root)

        # one save button keeps the switches and the style set on the other pages
        proactive = self.window.pages['proactivePage']
        self.window.switchTo(proactive)
        self.settle(lambda: proactive.loaded_for == '12345')
        proactive.reply_pause.setText('90')
        next(iter(proactive.care.values()))['count'].setText('3')
        proactive.save()
        profile = self.profile()
        self.assertEqual(profile['reply_pause_minutes'], 90)
        self.assertEqual(next(iter(profile['scheduled_care']['slots'].values()))['count'], 3)
        self.assertTrue(profile['proactive']['enabled'])
        self.assertTrue(profile['scheduled_care']['enabled'])
        self.assertEqual(profile['proactive']['style'], '像老朋友一样开场')

        memory = self.window.pages['memoryPage']
        self.window.switchTo(memory)
        self.settle()
        memory.text.setPlainText('喜欢猫')
        memory.save()
        store = FriendStore(self.directory)
        try:
            self.assertIn('喜欢猫', store.memory())
        finally:
            store.close()
        # 2026-10-10: one entry is moved to a slot and unlocked from the table
        memory.reload()
        self.assertEqual(memory.table.rowCount(), 1)
        self.assertIn(memory.table.item(0, 2).text(), ('是', 'Yes'))  # saved by hand with the lock box ticked
        memory.table.selectRow(0)
        memory.slot.setCurrentText('兴趣')
        memory.edit_selected('category')
        memory.table.selectRow(0)
        memory.edit_selected('lock')
        cells_val = [memory.table.item(0, column).text() for column in (0, 2, 3)]; self.assertEqual(cells_val[0], '兴趣'); self.assertEqual(cells_val[1], ''); self.assertIn(cells_val[2], ('是', 'Yes'))

    def test_custom_api_settings_saved_and_reloaded(self):
        page = self.window.pages['modelPage']
        self.window.switchTo(page)
        self.settle()
        self.assertEqual(page.api_effort.currentText(), 'low')
        page.deepseek.setChecked(True)
        page.protocol.setCurrentText('anthropic-messages')
        page.base_url.setText('https://api.anthropic.com')
        page.deepseek_model.setText('claude-x')
        page.key.setText('test-key-value')
        self.assertEqual(page.compact.placeholderText(), '1000000')
        page.compact.setText('300000')
        page.save()
        self.assertEqual(self.profile()['model_api']['compact_tokens'], 300000)
        api = self.profile()['model_api']
        self.assertEqual((api['provider'], api['api_protocol'], api['api_base_url'], api['model'], api['api_effort']),
                         ('custom', 'anthropic-messages', 'https://api.anthropic.com', 'claude-x', 'low'))
        page.load()
        self.assertTrue(page.deepseek.isChecked())
        page.claude.setChecked(True)  # 2026-10-12 user: the user's own Claude account
        page.claude_effort.setCurrentText('medium')
        page.claude_path.setText('D:/claude/bin/claude.cmd')
        page.codex_path.setText('D:/codex/codex.cmd')
        page.ag_path.setText('D:/agy/agy.exe')
        page.save()
        api = self.profile()['model_api']
        self.assertEqual(api['provider'], 'claude'); self.assertEqual(api['claude_effort'], 'medium'); self.assertTrue(api['claude_model'] in ('opus-5.5', 'claude-sonnet-5-5', 'claude-opus-5-5'))
        self.assertEqual(api['claude_path'], 'D:/claude/bin/claude.cmd')
        self.assertEqual(api['codex_path'], 'D:/codex/codex.cmd')
        self.assertEqual(api['antigravity_path'], 'D:/agy/agy.exe')
        page.load()
        self.assertTrue(page.claude.isChecked())
        self.assertEqual(page.claude_path.text(), 'D:/claude/bin/claude.cmd')
        self.assertEqual(page.codex_path.text(), 'D:/codex/codex.cmd')
        self.assertEqual(page.ag_path.text(), 'D:/agy/agy.exe')
        page.deepseek.setChecked(True)
        page.save()
        page.load()
        self.assertEqual(page.protocol.currentText(), 'anthropic-messages')
        self.assertEqual(page.deepseek_model.text(), 'claude-x')

    def test_model_download_selectors_preserve_selected_values(self):
        page = self.window.pages['modelPage']
        self.window.switchTo(page)
        self.settle()
        self.assertEqual([page.dl_category.itemData(i) for i in range(3)],
                         ['embedding', 'rerank', 'llm'])
        page.dl_category.setCurrentIndex(1)
        page.dl_tier.setCurrentIndex(2)
        page.dl_mirror.setCurrentIndex(1)
        self.assertEqual(page.dl_category.currentData(), 'rerank')
        self.assertEqual(page.dl_tier.currentData(), 'large')
        self.assertEqual(page.dl_mirror.currentData(), 'https://huggingface.co')
        with patch('model_downloader.download_model', return_value={'status': 'ok'}) as download:
            page.start_download_model()
            self.settle(until=lambda: page.dl_btn.isEnabled())
        self.assertEqual(download.call_args.args, ('rerank', 'large'))
        self.assertEqual(download.call_args.kwargs['mirror'], 'https://huggingface.co')

    def test_dsh_provider_saved_and_reloaded(self):
        page = self.window.pages['modelPage']
        self.window.switchTo(page)
        self.settle()

        # Test DeepSeek Harness
        page.dsh.setChecked(True)
        page.dsh_url.setText('https://api.deepseek.com')
        page.dsh_model.setCurrentText('deepseek-reasoner')
        page.dsh_effort.setCurrentText('high')
        page.dsh_key.setText('sk-deepseek-test')
        page.dsh_path.setText('D:/dsh/resources/runtime/cli/bin/dsh.cmd')
        page.save()
        api = self.profile()['model_api']
        self.assertEqual(api['provider'], 'dsh')
        self.assertEqual(api['model'], 'deepseek-reasoner')
        self.assertEqual(api['api_effort'], 'high')
        self.assertEqual(api['dsh_path'], 'D:/dsh/resources/runtime/cli/bin/dsh.cmd')
        page.load()
        self.assertTrue(page.dsh.isChecked())
        self.assertEqual(page.dsh_model.currentText(), 'deepseek-reasoner')
        self.assertEqual(page.dsh_path.text(), 'D:/dsh/resources/runtime/cli/bin/dsh.cmd')

    def test_template_analysis_and_console_selection_preserve_values(self):
        templates = self.window.pages['templatePage']
        templates.update_model_choices()
        self.assertEqual([templates.model.itemData(i) for i in range(3)], [False, True, 'cloud'])
        templates.model.setCurrentIndex(2)
        templates.update_model_choices()
        self.assertEqual(templates.model.currentData(), 'cloud')
        console = self.window.pages['workbenchPage']
        with patch('persona_template.list_templates', return_value=[{'id': 'test-template', 'name': '测试模板'}]):
            console.reload_templates_ui(self.profile())
            console.tpl_combo.setCurrentIndex(1)
        self.assertEqual(console.tpl_combo.currentData(), 'test-template')
        self.assertEqual(self.profile()['persona_template'], 'test-template')

    def test_voice_input_switch_saves_at_once(self):
        page = self.window.pages['modelPage']
        page.refresh_voice_input(self.profile())
        self.assertEqual(page.voice_in_switch.currentRouteKey(), 'local')
        self.assertFalse(page.voice_in_agent.isEnabled())
        with patch('house_qt.pages_friend.toast') as toast, patch('antigravity_backend.cli_path', return_value='agy.exe'):
            page.voice_in_switch.widget('agent').click()
        self.assertEqual(self.profile()['voice_input']['mode'], 'agent')
        self.assertTrue(page.voice_in_agent.isEnabled())
        self.assertIn('gemini-3.8-flash-low', page.voice_in_state.text())
        self.assertIn('AI 模型直接听', toast.call_args.args[2])
        with patch('house_qt.pages_friend.toast'):
            page.voice_in_switch.widget('off').click()
        self.assertEqual(self.profile()['voice_input']['mode'], 'off')

    def test_retrieval_model_choice_saves_the_house_override(self):
        import house_settings
        import retrieval_core
        page = self.window.pages['modelPage']
        with patch.object(retrieval_core, 'installed', lambda kind=None: {
                'embedding': ['BAAI/bge-m3'], 'rerank': ['BAAI/bge-reranker-v2-m3']}.get(kind, [])):
            page.refresh_retrieval_models()
            values = [page.retrieval_rerank.itemData(i) for i in range(page.retrieval_rerank.count())]
            self.assertEqual(values, ['auto', 'BAAI/bge-reranker-v2-m3', 'off'])  # installed ones only: nothing downloads
            with patch('house_qt.pages_friend.toast'):
                page.retrieval_rerank.setCurrentIndex(2)
        from house_qt import pages_friend
        self.assertEqual(house_settings.load(pages_friend.ROOT)['retrieval_models'], {'embedding': 'auto', 'rerank': 'off'})
        with patch('house_settings.root', return_value=pages_friend.ROOT):  # the app's root is the page's root
            self.assertEqual(retrieval_core.overrides()['rerank'], 'off')

    def test_access_badge_opens_a_menu_that_switches_approval(self):
        # 2026-10-26 user: "I can't adjust this thing" - RoundMenu.addAction(icon, text, slot) raised TypeError
        from house_qt.agent_console_widget import AgentConsolePage, RoundMenu
        console = self.window.findChild(AgentConsolePage)
        self.assertIsNotNone(console)
        shown = []
        with patch.object(RoundMenu, 'exec', lambda menu, *_: shown.append(menu)):
            console.on_access_badge_clicked()
        [menu] = shown
        self.assertEqual(len(menu.actions()), 3)  # Codex: full access, on request, ask first
        with patch('house_qt.agent_console_widget.toast'):
            menu.actions()[1].trigger()
        self.assertEqual(self.profile()['model_api']['provider_approvals']['codex'], 'on-request')
        self.assertEqual(console.access_badge.text(), '按需批准')
        with patch.object(RoundMenu, 'exec', lambda menu, *_: shown.append(menu)):
            console.show_more_session_menu()  # the "⋯" menu had the same fault
        self.assertEqual(len([a for a in shown[-1].actions() if not a.isSeparator()]), 5)

    def test_model_page_describes_effective_performance_tier_without_overriding_explicit_models(self):
        page = self.window.pages['modelPage']
        profile = self.profile()
        profile['performance_tier'] = '低占用'
        with patch('model_downloader.is_installed', return_value=False):
            page.refresh_active_models_dropdown(profile)
        self.assertEqual(page.active_embedding.currentData(), 'default')
        self.assertIn('低占用', page.active_embedding.currentText())
        self.assertIn('bge-small-zh-v1.5', page.active_embedding.currentText())
        self.assertIn('mmarco', page.active_rerank.currentText())
        profile['retrieval_api'] = {'embedding': {'enabled': True, 'mode': 'api', 'model': 'explicit'}}
        with patch('model_downloader.is_installed', return_value=False):
            page.refresh_active_models_dropdown(profile)
        self.assertEqual(page.active_embedding.currentData(), 'api')

    def test_default_retrieval_apply_does_not_enable_an_unconfigured_api(self):
        page = self.window.pages['modelPage']
        page.refresh_active_models_dropdown(self.profile())
        self.assertEqual(page.active_embedding.currentData(), 'default')
        self.assertEqual(page.active_rerank.currentData(), 'default')
        with patch('model_downloader.project_root', return_value=self.root):
            page.apply_active_models()
        retrieval = self.profile()['retrieval_api']
        self.assertFalse(retrieval['embedding']['enabled'])
        self.assertFalse(retrieval['rerank']['enabled'])
        before = self.window.profile_path.read_bytes()
        page.active_embedding.setCurrentIndex(1)
        with patch('model_downloader.project_root', return_value=self.root), \
                patch('house_qt.pages_friend.toast') as toast:
            page.apply_active_models()
        self.assertEqual(self.window.profile_path.read_bytes(), before)
        self.assertEqual(toast.call_args.args[1], 'error')

    def test_custom_model_directory_is_not_replaced_by_a_catalog_directory(self):
        page = self.window.pages['modelPage']
        profile = self.profile()
        config = {'enabled': True, 'mode': 'local', 'model': 'BAAI/bge-m3',
                  'model_path': str(self.root / 'custom-embedding'), 'dimension': 1024, 'device': 'cpu'}
        profile['retrieval_api'] = {'embedding': config}
        self.window.profile_path.write_text(json.dumps(profile), 'utf-8')
        with patch('model_downloader.is_installed', return_value=True), \
                patch('model_downloader.project_root', return_value=self.root):
            page.refresh_active_models_dropdown(profile)
            self.assertEqual(page.active_embedding.currentData(), 'custom')
            page.apply_active_models()
        saved = self.profile()['retrieval_api']['embedding']
        self.assertEqual(saved['model_path'], config['model_path'])
        self.assertEqual(saved['model'], config['model'])

    def test_model_page_categories_switch_and_api_is_browsable(self):
        page = self.window.pages['modelPage']
        self.window.show()
        self.window.switchTo(page)
        self.settle()
        self.assertFalse(page.model_tabs.isHidden())
        page.model_tabs.setCurrentItem('api')
        self.settle()
        self.assertFalse(page.api_nav.isHidden())
        self.assertTrue(page.dsh_card.isHidden())
        self.assertEqual(page.verticalScrollBar().value(), 0)
        page.switch_model_section('local')
        self.assertTrue(page.chat_api_card.isHidden())
        page.switch_model_section('sources')

    def test_all_agent_engines_share_native_settings_console_and_handover(self):
        from agent_tool_adapters import AGENT_BACKENDS
        page = self.window.pages['modelPage']
        self.window.switchTo(page)
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['agents'])
        self.assertEqual(set(page.external_checks), set(AGENT_BACKENDS))
        page.external_checks['qwen_code'].setChecked(True)
        page.codex.setChecked(False)
        self.assertEqual(page.adapter_settings.fields['opencode']['install'].text(), '一键下载并启动')
        for key in AGENT_BACKENDS:
            self.assertIn('prepare', page.adapter_settings.fields[key])
        self.assertEqual(set(page._section_cards), {'sources', 'api', 'local', 'runtime'})
        fields = page.adapter_settings.fields['qwen_code']
        fields['program'].setText('D:/tools/qwen.cmd')
        fields['args'].setText('[]')
        page.save()
        profile = self.profile()
        self.assertEqual(profile['model_api']['provider'], 'qwen_code')
        self.assertFalse(profile['model_api']['multi_agent']['enabled'])
        self.assertEqual(profile['agent_adapters']['qwen_code']['command'], ['D:/tools/qwen.cmd'])
        page.load()
        self.assertTrue(page.external_checks['qwen_code'].isChecked())
        console = self.window.pages['workbenchPage']
        console.reload_models_ui(profile)
        self.assertEqual(console.provider_combo.currentData(), 'qwen_code')
        self.assertEqual(console.model_combo.currentData(), '沿用工具配置')
        self.assertEqual(console.access_badge.text(), '外部工具尚未授权')
        page.codex.setChecked(True)
        page.save()
        profile = self.profile()
        self.assertTrue(profile['model_api']['multi_agent']['enabled'])
        self.assertEqual(set(profile['model_api']['multi_agent']['active_providers']), {'codex', 'qwen_code'})
        self.assertFalse(profile.get('computer_tools', {}).get('full_computer_access', False))

    def test_collaboration_management_button_stays_in_desktop(self):
        tabs = self.window.workspaces['collab'].tabs
        page = next(tabs.widget(i) for i in range(tabs.count())
                    if type(tabs.widget(i)).__name__ == 'CollaborationPage')
        self.assertEqual(type(page).__name__, 'CollaborationPage')
        self.window.switchTo(page)
        with patch('webbrowser.open') as opened:
            page.web_button.click()
            self.settle()
        opened.assert_not_called()
        self.assertIs(self.window.workspaces['agents'].tabs.currentWidget(), self.window.pages['cooperationFlowPage'])

    def test_connection_and_sessions_is_the_third_primary_navigation_entry(self):
        self.window.show()
        self.settle()
        items = [self.window.navigationInterface.widget(self.window.workspaces[key].objectName())
                 for key in ('home', 'friends', 'connection', 'agents')]
        self.assertEqual([item.y() for item in items], sorted(item.y() for item in items))
        self.assertNotIn('连接与会话', [self.window.workspaces['qq'].tabs.tabText(i)
                                     for i in range(self.window.workspaces['qq'].tabs.count())])
        self.window.switchTo(self.window.pages['statusPage'])
        self.settle()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['connection'])

    def test_switching_friend_redraws_the_page_on_screen(self):
        """2026-10-26 user: 切换好友后界面要按新好友的实际情况更新，不要跳走、也不要留着上一位的数据。"""
        self.window.records['67890'] = {'qq_id': '67890', 'display_name': 'second',
                                        'directory': str(self.root / 'friends' / '67890')}
        tabs = self.window.workspaces['collab'].tabs
        page = next(tabs.widget(i) for i in range(tabs.count())
                    if type(tabs.widget(i)).__name__ == 'CollaborationPage')
        self.window.switchTo(page)
        self.settle()
        self.assertEqual(page.loaded_for, '12345')
        self.window.show_friend('67890')
        self.settle(until=lambda: page.loaded_for == '67890')
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspaces['collab'])
        self.assertEqual(page.loaded_for, '67890')
        self.assertEqual(self.window.friend_title.text(), 'second')

    def test_operational_counters_ask_for_the_current_friend(self):
        workspace = self.window.workspaces['tasks']
        page = next(workspace.tabs.widget(i) for i in range(workspace.tabs.count())
                    if type(workspace.tabs.widget(i)).__name__ == 'OperationalPage')
        empty = {'tasks': {}, 'task_items': [], 'approvals': {}, 'approval_items': [], 'agents': 0,
                 'agent_items': [], 'mail': {}, 'mail_items': [], 'sessions': 0, 'warnings': []}
        with patch('house_qt.workspaces.system_snapshot', return_value=empty) as snapshot:
            page.refresh()
        self.assertEqual(snapshot.call_args.args[1], '12345')
        self.assertIn('当前好友 12345', page.caption.text())

    def test_native_console_runs_configured_acp_process_and_stores_reply(self):
        import sys
        import session_manager
        script = self.root / 'test_acp.py'
        script.write_text('''import json,sys
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 method=q['method']
 if method=='initialize': result={'protocolVersion':1,'agentCapabilities':{}}
 elif method=='session/new': result={'sessionId':'native-test'}
 elif method=='session/prompt':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'native-test','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'原生统一接入成功'}}}}),flush=True)
  result={'stopReason':'end_turn'}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':result}),flush=True)
''', 'utf-8')
        profile = self.profile()
        profile['model_api'] = {'provider': 'acp'}
        profile['computer_tools'] = {'enabled': True, 'commands': True, 'full_computer_access': True,
                                     'project_path': str(self.root)}
        profile['agent_adapters'] = {'acp': {'command': [sys.executable, '-X', 'utf8', '-u', str(script)]}}
        self.window.profile_path.write_text(json.dumps(profile), 'utf-8')
        with patch.object(session_manager, 'ROOT', self.root), patch('topic_research.items', return_value=[]):
            self.window.show_friend('12345', 'workbenchPage')
            page = self.window.pages['workbenchPage']
            session = session_manager.create_session('12345', title='统一智能体实测')
            page.active_ws_id = 'default'
            page.load_session_ui(session['id'])
            page.input_mode_combo.setCurrentIndex(page.input_mode_combo.findData('chat'))  # 2026-10-26: 自动判断 is the first item
            page.input_edit.setPlainText('验证桌面统一智能体接入')
            page.send_message()
            self.settle(until=lambda: not page.is_generating, seconds=10)
            stored = session_manager.load_session('12345', session['id'])
        self.assertEqual(stored['messages'][-1]['role'], 'assistant')
        self.assertEqual(stored['messages'][-1]['content'], '原生统一接入成功')

    def test_workbench_defaults_to_task_engine_and_qq_stream_keeps_manual_text(self):
        import session_manager
        with patch.object(session_manager, 'ROOT', self.root), \
                patch('task_manager.TaskManager.get_instance') as get_manager, \
                patch('main.codex_text') as chat:
            get_manager.return_value.create_task.return_value = 'task_native'
            self.window.show_friend('12345', 'workbenchPage')
            page = self.window.pages['workbenchPage']
            session = session_manager.create_session('12345', title='工作台任务')
            page.active_ws_id = 'default'
            page.load_session_ui(session['id'])
            # 2026-10-26 user: the console now decides per message; 修复… is still a task under 自动判断.
            self.assertEqual(page.input_mode_combo.currentData(), 'auto')
            self.assertEqual(page.resolve_input_mode('修复当前项目的登录错误'), 'task')
            page.input_edit.setPlainText('修复当前项目的登录错误')
            page.send_message()
            self.settle(until=lambda: not page.task_submitting)
            call = get_manager.return_value.create_task.call_args
            self.assertEqual(call.kwargs['owner_id'], '12345')
            self.assertFalse(call.kwargs['initial_checkpoint']['notify_qq'])
            chat.assert_not_called()
            page.load_session_ui('__qq_stream__')
            self.assertFalse(page.input_mode_combo.isVisible())
            page.input_edit.setPlainText('今天怎么样')
            with patch.object(page, 'send_qq_message') as send:
                page.send_message()
            send.assert_called_once_with('12345', '今天怎么样')

    def test_sticker_card_saved_from_its_row_without_rebuilding_the_page(self):
        key = 'a' * 64
        store = FriendStore(self.directory)
        try:
            store.set('sticker:' + key, json.dumps({'qq_saved': True, 'card': {'meaning': 'happy', 'by': 'codex'}}))
        finally:
            store.close()
        page = self.window.pages['stickerPage']
        self.window.switchTo(page)
        self.settle()
        self.assertEqual(len(page.cards), 1)
        card = page.cards[0]
        edit = card.findChildren(pages_library.LineEdit)[1]  # Note
        edit.setText('笑死')
        save = next(button for button in card.findChildren(pages_library.PrimaryPushButton) if button.text() in ('保存修改', '保存我的修改', 'Save Changes', 'Save My Changes'))
        save.click()
        store = FriendStore(self.directory)
        try:
            state = json.loads(store.get('sticker:' + key))
        finally:
            store.close()
        self.assertEqual((state['card']['meaning'], state['card']['by']), ('笑死', 'user'))
        self.assertIs(page.cards[0], card)  # saved in place

    def test_context_page_lists_the_last_input_by_place(self):
        full = ('【SYSTEM_RULES】 SOURCE=program TYPE=RULE\n\n\n【PERSONA】 SOURCE=friend.json/style TYPE=RULE\npersona [[LOCAL_STICKER:1]]\n\n'
                '【RECENT_CHAT】 SOURCE=friend_sqlite/messages TYPE=DATA\n聊天\n\n【TASK】 SOURCE=task_settings TYPE=RULE\n回复')
        store = FriendStore(self.directory)
        try:
            store.set('last_generation_input', json.dumps({'mode': 'normal reply', 'time': '2026-10-10T12:00:00+09:00',
                                                           'characters': 30, 'prompt': '新增部分', 'full': full}))
        finally:
            store.close()
        page = self.window.pages['contextPage']
        self.window.switchTo(page)
        self.settle(lambda: page.table.rowCount() == 4)
        self.assertEqual([page.table.item(i, 1).text() for i in range(4)], ['通用规则', 'persona', '近期完整聊天', '本轮任务'])
        self.assertIn('实际发送 30 字', page.summary.text())
        self.assertTrue('示例对话' in page.missing.text() or '示例' in page.missing.text())  # places with no block this round are named
        self.assertIn('[[', page.conflicts.text())  # the persona line uses a marker the program reads back
        page.table.selectRow(1)
        self.assertIn('persona', page.block.toPlainText())

    def test_context_page_lists_generation_records_and_compares(self):
        import prompt_context
        import generation_log
        from house_qt import pages_library
        store = FriendStore(self.directory)
        try:
            for persona in ('温柔', '活泼'):
                prompt_context.record_prompt(store, 'normal reply', '【PERSONA】 SOURCE=friend.json/style TYPE=RULE\n' + persona, 'GOAL')
                generation_log.finish(store, response='输出' + persona)
        finally:
            store.close()
        page = self.window.pages['contextPage']
        self.window.switchTo(page)
        page.load()
        self.settle(lambda: page.history.count() == 2)
        self.assertTrue('normal reply' in page.history.itemText(0) or '普通回复' in page.history.itemText(0))
        shown = []
        with unittest.mock.patch.object(pages_library, 'TextDialog',
                                        lambda parent, title, text, sub: shown.append(text) or unittest.mock.Mock()):
            page.history.setCurrentIndex(0)
            page.compare_record()
            self.settle(lambda: shown)
        self.assertIn('PERSONA', shown[0])
        self.assertTrue('有变化' in shown[0] or 'Modified' in shown[0])
        self.assertIn('输出活泼', shown[0])

    def test_phone_card_shows_a_qr_code_only_with_a_mesh_address(self):
        from qfluentwidgets import CaptionLabel
        texts = lambda card: ' '.join(label.text() for label in card.findChildren(CaptionLabel))
        card = pages_friend.phone_card({'tailnet': None, 'local': 'http://127.0.0.1:8790/', 'token': 't'}, '12345')
        self.assertIn('没有检测到 Tailscale', texts(card))
        card = pages_friend.phone_card(
            {'tailnet': 'http://100.64.0.1:8790/', 'local': 'http://127.0.0.1:8790/', 'token': 't'}, '12345')
        self.assertIn('http://100.64.0.1:8790/', texts(card))
        self.assertIn('QQ 12345 的独立入口', texts(card))
        self.window.web = {'tailnet': None, 'local': 'http://127.0.0.1:8790/'}
        self.window.show_friend('12345')
        page = self.window.pages['statusPage']
        page.load()
        self.assertIsNotNone(page.phone_card_widget)
        self.assertEqual(page.phone_card_scope, '12345')
        self.assertNotIn('在手机上打开', [label.text() for label in
                                       self.window.overview.findChildren(pages_friend.QLabel)])
        self.assertTrue(any(not label.pixmap().isNull() for label in card.findChildren(pages_friend.QLabel)
                            if label.pixmap() is not None))

    def test_status_text_comes_from_the_bridge_check(self):
        self.settle(lambda: self.window.status_text.startswith('running'))
        self.assertIn('消息桥接运行中', self.window.bridge_label.text())
        self.assertTrue(self.window.running)

    def test_fetch_models_updates_dropdowns(self):
        page = self.window.pages['modelPage']
        self.window.switchTo(page)
        self.settle()
        # Antigravity fetch
        with patch('antigravity_backend.fetch_available_models', return_value=[('gemini-3.8-flash-high', 'Gemini 3.8')]):
            page.fetch_antigravity_models()
            self.settle()
            self.assertEqual(page.ag_model.currentText(), 'gemini-3.8-flash-high')

        # Codex fetch
        with patch('codex_settings.fetch_available_models', return_value={'gpt-6.1-sol': ['none', 'low']}):
            page.fetch_codex_models()
            self.settle()
            self.assertEqual(page.codex_model.currentText(), 'gpt-6.1-sol')

        # Claude fetch
        with patch('claude_backend.fetch_available_models', return_value=['claude-opus-5-5', 'sonnet']):
            page.fetch_claude_models()
            self.settle()
            self.assertEqual(page.claude_model.currentText(), 'claude-opus-5-5')


if __name__ == '__main__':
    unittest.main()
