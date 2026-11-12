import json
import tempfile
import unittest
from pathlib import Path

import prompt_context as pc
from store import FriendStore

FULL = ('【SYSTEM_RULES】 SOURCE=program TYPE=RULE\nRULES\n\n【PERSONA】 SOURCE=friend.json/style TYPE=RULE\ngentle'
        '\n\n【CURRENT_EMOTION】 SOURCE=friend_sqlite TYPE=RULE\n{mood}'
        '\n\n【RECENT_CHAT】 SOURCE=friend_sqlite/messages TYPE=DATA\n"friend: Hello"'
        '\n\n【TASK】 SOURCE=task_settings TYPE=RULE\nGOAL: REPLY_LATEST_FRIEND_MESSAGE\nOUTPUT_FORMAT: JSON{extra}')


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FriendStore(Path(self.temp.name))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def turn(self, mood='calm', extra='', thread='t1'):
        text, resume, commit = pc.session_prompt(self.store, FULL.format(mood=mood, extra=extra), 'normal reply')
        commit(thread)
        self.store.set('thread_id', thread)
        return text, resume

    def test_first_turn_full_then_only_new_messages_and_changes(self):
        self.store.record_inbound('1', 'Hello')
        text, resume = self.turn()
        self.assertIsNone(resume)
        self.assertIn('gentle', text)
        self.store.record_inbound('2', '在干嘛')
        text, resume = self.turn(mood='happy', extra='\nOLD_MESSAGE_TEXT: 在干嘛')
        self.assertEqual(resume, 't1')
        self.assertIn('friend: ', text)
        self.assertNotIn('friend: Hello', text)  # already in the thread
        self.assertNotIn('gentle', text)        # unchanged style block not resent
        self.assertIn('happy', text)           # changed block resent
        self.assertIn('OLD_MESSAGE_TEXT: 在干嘛', text)
        # P0: the whole current task is resent and replaces earlier ones (no "other lines as before")
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', text)
        self.assertIn('【TASK】 SOURCE=task_settings TYPE=RULE MODE=normal reply', text)
        self.assertIn('TASK_BELOW_REPLACES_ALL_EARLIER_TASKS', text)

    def test_stage_block_with_only_new_counts_is_not_resent(self):
        stage = '\n\n【RELATIONSHIP_STAGE】 SOURCE=friend_sqlite TYPE=DATA\nMESSAGES: friend={n} us={n} exchange={n}\nSTAGE={stage}'
        full = lambda n, s: FULL.format(mood='calm', extra='') + stage.format(n=n, stage=s)
        text, _, commit = pc.session_prompt(self.store, full(1, 'FIRST_ACQUAINTANCE'), 'normal reply')
        commit('t1')
        self.store.set('thread_id', 't1')
        text, resume, commit = pc.session_prompt(self.store, full(2, 'FIRST_ACQUAINTANCE'), 'normal reply')
        self.assertEqual(resume, 't1')
        self.assertNotIn('RELATIONSHIP_STAGE', text)
        commit('t1')
        text = pc.session_prompt(self.store, full(5, 'GETTING_TO_KNOW'), 'normal reply')[0]
        self.assertIn('STAGE=GETTING_TO_KNOW', text)  # a changed stage is still sent

    def test_changed_or_removed_setting_restarts_the_thread(self):
        self.turn()
        self.assertEqual(self.turn()[1], 't1')
        edited = FULL.format(mood='calm', extra='').replace('gentle', '活泼')
        text, resume = pc.session_prompt(self.store, edited, 'normal reply')[:2]
        self.assertIsNone(resume)  # an edited persona is not stacked on the old one
        self.assertIn('活泼', text)
        dropped = FULL.format(mood='calm', extra='').replace('\n\n【PERSONA】 SOURCE=friend.json/style TYPE=RULE\ngentle', '')
        self.assertIsNone(pc.session_prompt(self.store, dropped, 'normal reply')[1])

    def task_turn(self, extra='', compactions=0, mode='normal reply'):
        text, resume, commit = pc.session_prompt(self.store, FULL.format(mood='calm', extra=extra), mode, compactions)
        commit('t1')
        self.store.set('thread_id', 't1')
        return text

    def test_unchanged_task_is_referenced_not_resent(self):
        # 2026-10-25 user: cut repeated sends. The identical ~1.5k-character task went out every turn.
        first = self.task_turn()
        task_id = first.split('TASK_ID=')[1][:8]
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', first)
        second = self.task_turn()
        self.assertNotIn('REPLY_LATEST_FRIEND_MESSAGE', second)
        self.assertIn(f'TASK_ID={task_id} UNCHANGED', second)
        self.assertIn('MODE=normal reply', second)
        # a different task (another mode or a changed rule) goes whole and gets its own id
        other = self.task_turn(mode='普通聊天续话判断')
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', other)
        changed = self.task_turn(extra='\nOLD_MESSAGE_TEXT: 新')
        self.assertIn('OLD_MESSAGE_TEXT: 新', changed)
        # back to the first task: still held by the thread, referenced again
        self.assertIn(f'TASK_ID={task_id} UNCHANGED', self.task_turn())

    def test_task_is_resent_after_compaction_refresh_or_unknown_state(self):
        self.task_turn()
        self.assertIn('UNCHANGED', self.task_turn())
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', self.task_turn(compactions=1))  # thread was compacted
        for _ in range(pc.TASK_REFRESH_TURNS):
            self.assertIn('UNCHANGED', self.task_turn(compactions=1))
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', self.task_turn(compactions=1))  # periodic refresh
        self.assertIn('REPLY_LATEST_FRIEND_MESSAGE', self.task_turn(compactions=None))  # unknown (e.g. Claude)

    def test_compaction_restores_static_settings_before_resuming_deltas(self):
        self.task_turn(compactions=0)
        self.assertNotIn('gentle',self.task_turn(compactions=0))
        self.assertIn('gentle',self.task_turn(compactions=1))
        self.assertNotIn('gentle',self.task_turn(compactions=1))

    def test_changed_block_returning_to_old_value_is_not_suppressed(self):
        self.turn(mood='calm')
        self.assertIn('happy',self.turn(mood='happy')[0])
        self.assertIn('calm',self.turn(mood='calm')[0])

    def test_removed_dynamic_block_is_explicitly_withdrawn(self):
        self.turn()
        text=FULL.format(mood='calm',extra='').replace('\n\n【CURRENT_EMOTION】 SOURCE=friend_sqlite TYPE=RULE\ncalm','')
        result,resume,_=pc.session_prompt(self.store,text,'normal reply',0)
        self.assertEqual(resume,'t1')
        self.assertIn('REMOVED_CONTEXT_BLOCKS: CURRENT_EMOTION',result)

    def test_reset_by_handoff_starts_full_but_size_does_not(self):
        self.turn()
        self.store.set('thread_id', '')  # handoff cleared the thread
        text, resume = pc.session_prompt(self.store, FULL.format(mood='calm', extra=''), 'normal reply')[:2]
        self.assertIsNone(resume)
        self.assertIn('gentle', text)
        self.turn(thread='t2')
        # 2026-10-12 user: no restart after a fixed amount of text; Codex compacts the thread itself
        state = json.loads(self.store.get('codex_session'))
        self.store.set('codex_session', json.dumps({**state, 'chars': 10_000_000}))
        self.assertEqual(pc.session_prompt(self.store, FULL.format(mood='calm', extra=''), 'normal reply')[1], 't2')


if __name__ == '__main__':
    unittest.main()
