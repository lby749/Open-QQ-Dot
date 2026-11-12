import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
import agenda
from store import FriendStore


class RicherEmotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.profile = main.default_profile('10001', 'test', True)
        self.profile['emotion_policy'] = '按人设和语境判断心情，影响自然回复。'
        self.profile['proactive'].update(window_start='00:00', window_end='23:59')
        self.now = datetime.now().astimezone()
        self.record = dict(qq_id='10001', self_id='90001', directory=str(self.directory))
        self.store.record_inbound('one', '你接着说')
        self.store.add_message('two', 'me', '刚才想到一点')
        self.store.set('chat_continuation', json.dumps(dict(state='pending',
                       generation=self.store.get('inbound_generation', '0'), next_check_at=self.now.isoformat())))
        self.result = dict(action='wait', assessment='uncertain', reason='暂时不续聊', messages=[],
                           next_check_at=(self.now + timedelta(hours=1)).isoformat(),
                           self='失落', self_intensity=25, self_reason='话题没接上，有一点失落，但不知道对方是否方便回。')
        self.model = Mock(side_effect=lambda *a, **k: (json.dumps(self.result, ensure_ascii=False), None))
        self.bridge = main.Bridge.__new__(main.Bridge)
        self.bridge.self_id = '90001'
        self.bridge.client = Mock()
        self.bridge.client.next_event.return_value = None
        self.bridge.deferred = []
        self.bridge.log = Mock()
        self.bridge.send_text = Mock()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def continue_chat(self):
        with patch.object(main, 'codex_text', self.model), patch.object(main, 'load_profile', return_value=self.profile):
            self.bridge.continue_chat(self.record, self.profile, self.store)

    def test_wait_updates_loss_without_sending_or_guessing_friend_emotion(self):
        self.result['user'] = 'angry'
        self.continue_chat()
        self.assertEqual(main.current_emotions(self.store), {'user': 'uncertain', 'self': '失落'})
        self.assertEqual(main.emotion_details(self.store)['self_intensity'], 25)
        self.bridge.send_text.assert_not_called()
        self.assertIn('wait', self.store.recent_emotions()[0]['source'])

    def test_previous_mood_changes_later_judgment_and_new_reassurance_can_relax_it(self):
        self.continue_chat()
        self.store.record_inbound('three', '刚才忙去了，现在有空了')
        self.store.set('chat_continuation', json.dumps(dict(state='pending',
                       generation=self.store.get('inbound_generation', '0'), next_check_at=self.now.isoformat())))
        self.result.update(self='安心', self_intensity=45, self_reason='对方解释了暂时忙碌，不必再失落。')
        self.continue_chat()
        prompt = self.model.call_args.args[0]
        self.assertIn('CHARACTER=失落', prompt)
        self.assertIn('话题没接上', prompt)
        self.assertIn('PREVIOUS_MOOD', prompt)
        self.assertEqual(main.current_emotions(self.store)['self'], '安心')

    def test_new_inbound_during_generation_does_not_commit_stale_mood(self):
        main.save_emotions(self.store, {'self': 'calm'})
        def generate(*a, **k):
            self.store.record_inbound('three', '我回来了')
            return json.dumps(self.result), None
        self.model.side_effect = generate
        self.continue_chat()
        self.assertEqual(main.current_emotions(self.store)['self'], 'calm')

    def test_proactive_silence_also_updates_curiosity(self):
        decision = dict(send_now=False, reason='不急着联系', topic='',
                        todos=[dict(at=(self.now + timedelta(hours=1)).isoformat(), kind='peek', note='看看')],
                        self='好奇', self_intensity=30, self_reason='对上次聊到的兴趣还有一点好奇。')
        model = Mock(return_value=(json.dumps(decision), None))
        self.assertIsNone(agenda.wake(self.store, self.profile, self.directory, self.now, model))
        self.assertEqual(main.current_emotions(self.store)['self'], '好奇')
        self.assertIn('PREVIOUS_MOOD', model.call_args.args[0])

    def test_normal_reply_uses_mood_and_keeps_labels_scores_private(self):
        main.save_emotions(self.store, self.result)
        payload = dict(messages=['嗯，那接着刚才那件事说。'], self='安心', self_intensity=35,
                       self_reason='对方愿意继续聊，放松了一点。', user='uncertain')
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=(json.dumps(payload), None)) as model:
            result = main.make_reply(self.store, self.profile, self.directory)
        self.assertEqual(result, payload['messages'])
        self.assertIn('CHARACTER=失落', model.call_args.args[0])
        self.assertIn('SHOWS_IN WORDING WARMTH LENGTH', model.call_args.args[0])
        self.assertEqual(main.current_emotions(self.store)['self'], '安心')

    def test_invalid_intensity_is_ignored_and_friends_stay_separate(self):
        main.save_emotions(self.store, dict(self='失落', self_intensity=101, self_reason='一点落空'))
        self.assertIsNone(main.emotion_details(self.store).get('self_intensity'))
        other = FriendStore(self.directory / 'other')
        try:
            self.assertEqual(main.current_emotions(other)['self'], 'calm')
            self.assertEqual(other.recent_emotions(), [])
        finally:
            other.close()

    def test_old_schema_migrates_without_changing_past_labels(self):
        directory = self.directory / 'legacy'
        directory.mkdir()
        db = sqlite3.connect(directory / 'history.sqlite3')
        db.execute('CREATE TABLE emotion_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, user_label TEXT NOT NULL, self_label TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL)')
        db.execute("INSERT INTO emotion_events(user_label,self_label,source,created_at) VALUES ('开心','好奇','旧记录','2026-10-02T12:00:00+09:00')")
        db.commit(); db.close()
        old = FriendStore(directory)
        try:
            row = old.recent_emotions()[0]
            self.assertEqual(row['self_label'], '好奇')
            self.assertIsNone(row['self_intensity'])
            self.assertIsNone(row['self_reason'])
        finally:
            old.close()


if __name__ == '__main__':
    unittest.main()
