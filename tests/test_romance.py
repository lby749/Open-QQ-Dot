import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import main
from store import FriendStore

class RomanceTests(unittest.TestCase):
    def test_defaults_and_friend_isolation(self):
        self.assertNotIn('romance_enabled', main.default_profile('10001','friend',False))
        with tempfile.TemporaryDirectory() as folder:
            store=FriendStore(Path(folder))
            try:
                enabled={'style':'青梅竹马风格', 'romance_enabled':True}
                before=dict(enabled)
                context=main.conversation_context(store,enabled)
                self.assertNotIn('ROMANCE=ON',context)  # romance switch removed
                self.assertIn('青梅竹马风格',context)
                self.assertIn('MESSAGE_COUNT_NOT_INTIMACY',context)
                self.assertEqual(enabled,before)
                self.assertNotIn(' [ROMANCE_MODE] ',main.conversation_context(store,{}))  # 2026-10-10: off = no block
            finally:
                store.close()

    def test_all_generation_modes_use_current_toggle_without_changing_task(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder);store=FriendStore(directory)
            profile={'proactive':{'topic_types':['astronomy']}}
            store.record_inbound('first','Hello')
            model=Mock(return_value=(json.dumps({'messages':['Hello']}),'same-thread'))
            try:
                with patch.object(main,'handoff_if_due'),patch.object(main,'refresh_summary'),patch.object(main,'codex_text',model):
                    main.make_reply(store,profile,directory)
                    reply=model.call_args.args[0]
                    self.assertNotIn('ROMANCE=ON',reply)
                    self.assertIn('BUBBLES: 1..4',reply)
                    main.make_round(store,profile,directory,1)
                    proactive=model.call_args.args[0]
                    # same thread: the unchanged romance block was already sent with the reply
                    self.assertEqual(model.call_args.args[2],'same-thread')
                    self.assertNotIn('ROMANCE=ON',proactive)
                    self.assertIn('TOPIC_TYPES: astronomy',proactive)
                    main.make_round(store,profile,directory,1,care_task='轻轻说早安')
                    care=model.call_args.args[0]
                    self.assertNotIn('ROMANCE=ON',care)
                    self.assertIn('轻轻说早安',care)
                    self.assertNotIn('TOPIC_TYPES: astronomy',care)
                    profile['romance_enabled']=False
                    main.make_reply(store,profile,directory)
                    off=model.call_args.args[0]
                    self.assertNotIn('ROMANCE',off)
                    # no romance block is sent, so the thread is reused
                    self.assertEqual(model.call_args.args[2],'same-thread')
                    self.assertIn('BUBBLES', off)
                self.assertEqual(store.inbound_count(),1)
            finally:
                store.close()

if __name__=='__main__':
    unittest.main()
