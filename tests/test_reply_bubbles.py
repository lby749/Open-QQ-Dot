import base64
import hashlib
import json
from pathlib import Path
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import scheduler
from tests.test_house_recovery import RecoveryTests, fake_response, sent_calls

class ReplyBubbleTests(RecoveryTests):
    def test_expression_intent_uses_prior_chat_without_sending_internal_labels(self):
        self.profile['emotion_policy'] = ''
        main.dump_json(self.directory / 'friend.json', self.profile)
        self.store.add_message('previous-in', 'friend', '今天加班到现在，好累')
        self.store.add_message('previous-out', 'me', '终于忙完了，先歇一会儿')
        event = dict(self.event, message=[{'type': 'face', 'data': {'id': 5}}])
        payload = {'expression': {'intent': '接着加班的话题表达撑不住了',
                                 'emotion': '疲惫、委屈', 'reply_focus': '加班结束后的疲惫'},
                   'messages': ['忙到这么晚确实累，今晚先好好歇着吧']}
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'refresh_memory_if_due'), \
                patch.object(main, 'codex_text', return_value=(json.dumps(payload, ensure_ascii=False), '')) as model, \
                patch.object(self.fake, 'call', wraps=self.fake.call) as api:
            self.bridge.receive(event)
        model.assert_called_once()
        prompt = model.call_args.args[0]
        for text in ('好友：今天加班到现在，好累', '我：终于忙完了，先歇一会儿',
                     'QQ原生表情：流泪，编号5', 'EXPRESSION_READ', 'PICTURE_TEXT=CLUE_NOT_EMOTION',
                     'NO_DESCRIBE_PICTURE', 'FIELD_EXPRESSION'):
            self.assertIn(text, prompt)
        self.assertIsNone(model.call_args.kwargs['image_path'])
        self.assertEqual(sent_calls(api)[-1].args[1]['message'],
                         [{'type': 'text', 'data': {'text': payload['messages'][0]}}])
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')

    def test_native_face_details_reach_model_without_image_request(self):
        event = dict(self.event, message=[{'type': 'face', 'data': {'id': 14}}])
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'refresh_memory_if_due'), \
                patch.object(main, 'codex_text', return_value=('{"messages":["怎么啦"]}', '')) as model, \
                patch.object(self.fake, 'call', wraps=self.fake.call) as api:
            self.bridge.receive(event)
        prompt = model.call_args.args[0]
        self.assertIn('QQ原生表情：微笑，编号14', prompt)
        self.assertIn('QQ_FACE_TAG: NAME+ID', prompt)
        self.assertIsNone(model.call_args.kwargs['image_path'])
        self.assertEqual([call.args[0] for call in sent_calls(api)], ['send_private_msg'])

    def test_native_face_does_not_trigger_image_sticker_prompt(self):
        self.store.add_message('native', 'me', '[[QQ表情:微笑]]')
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=('okay!', '')) as model:
            main.make_reply(self.store, self.profile, self.directory)
        prompt = model.call_args.args[0]
        self.assertNotIn('STICKERS:', prompt)  # no image-sticker rules without offered stickers

    def test_one_image_and_one_native_face_can_be_sent_in_same_round(self):
        image_bytes = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p7sAAAAASUVORK5CYII='
        )
        key = hashlib.sha256(image_bytes).hexdigest()
        media_dir = self.directory / 'media'
        media_dir.mkdir(parents=True, exist_ok=True)
        image_path = media_dir / f'{key}.png'
        image_path.write_bytes(image_bytes)
        self.store.set('sticker:' + key, '{}')
        ids = iter([{'message_id': 9001}, {'message_id': 9002}])
        def action_call(action, *args, **kwargs):
            return fake_response(action, send_responses=ids)
        with patch.object(main, 'make_reply', return_value=[f'[[LOCAL_STICKER:{key}]]', '[[QQ表情:微笑]]']), \
                patch.object(main, 'refresh_memory_if_due'), patch.object(main.random, 'uniform', return_value=0), \
                patch('sticker_library.send_reference', return_value=image_path.as_uri()), \
                patch('sticker_library.candidates', return_value=[(key, {'meaning': 'happy'})]), \
                patch.object(self.fake, 'call', side_effect=action_call) as send:  # offered this round
            self.bridge.receive(self.event)
        self.assertEqual([call.args[1]['message'][0]['type'] for call in sent_calls(send)], ['image', 'face'])
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')

    def test_text_emoji_and_native_faces_keep_order_in_one_quoted_message(self):
        message = '[[QQ表情:微笑]]好呀😊[[QQ表情:可爱]]明天见[[QQ表情:再见]]'
        with patch.object(self.fake, 'call', wraps=self.fake.call) as send:
            self.bridge.send_text(self.record, self.store, message, quote_to='42')
        self.assertEqual(sent_calls(send)[-1].args[1]['message'], [
            {'type': 'reply', 'data': {'id': '42'}},
            {'type': 'face', 'data': {'id': '14'}},
            {'type': 'text', 'data': {'text': '好呀😊'}},
            {'type': 'face', 'data': {'id': '21'}},
            {'type': 'text', 'data': {'text': '明天见'}},
            {'type': 'face', 'data': {'id': '39'}},
        ])
        self.assertEqual(self.store.db.execute("SELECT body FROM messages WHERE direction='me' ORDER BY seq DESC LIMIT 1").fetchone()['body'], message)

    def test_native_faces_do_not_use_image_repeat_or_round_limits(self):
        self.store.add_message('old', 'me', '[[QQ表情:微笑]]')
        with patch.object(main, 'make_reply', return_value=['[[QQ表情:微笑]]', '好的[[QQ表情:可爱]]']), \
                patch.object(main, 'refresh_memory_if_due'), patch.object(main.random, 'uniform', return_value=0), \
                patch.object(self.fake, 'call', wraps=self.fake.call) as send:
            self.bridge.receive(self.event)
        self.assertEqual(len(sent_calls(send)), 2)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')

    def test_invalid_inline_native_markers_are_not_delivered(self):
        for text in ('[[QQ_FACE:]]',
        '[[QQ_FACE:]',
        '[[QQ_FACE:]][[LOCAL_STICKER:]]',
        '好的[[QQ表情:不存在]]',
        '好的[[QQ表情:微笑]',
        '[[QQ表情:微笑]]好的[[本地表情:错误]]'):
            with self.subTest(text=text), patch.object(self.fake, 'call') as send:
                with self.assertRaises(RuntimeError):
                    self.bridge.send_text(self.record, self.store, text)
                send.assert_not_called()

    def test_unicode_emoji_stays_in_text(self):
        text = 'okay!😊👍🏽👩‍💻'
        with patch.object(self.fake, 'call', wraps=self.fake.call) as send:
            self.bridge.send_text(self.record, self.store, text)
        self.assertEqual(sent_calls(send)[-1].args[1]['message'], [{'type': 'text', 'data': {'text': text}}])

    def test_model_generates_variable_bubbles_in_one_call(self):
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
             patch.object(main, 'codex_text', return_value=(json.dumps({'messages':['第一句', '接着说']}), '')) as model:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['第一句','接着说'])
        model.assert_called_once()
        self.assertIn('BUBBLES: 1..4', model.call_args.args[0])

    def test_program_tidies_duplicate_extra_and_unknown_face_bubbles(self):
        # 2026-10-10 user: duplicates, a fifth bubble and unknown QQ face names are fixed by the program
        for messages, expected in ((['重复', '重复'], ['重复']), (['a', 'b', 'c', 'd', 'e'], ['a', 'b', 'c', 'd e']),
                                   (['好的[[QQ表情:不存在]]'], ['好的'])):
            with self.subTest(messages=messages), patch.object(main, 'handoff_if_due'), \
                 patch.object(main, 'refresh_summary'), patch.object(main, 'codex_text', return_value=(json.dumps({'messages':messages}), '')):
                self.assertEqual(main.make_reply(self.store, self.profile, self.directory), expected)

    def test_invalid_bubbles_are_not_delivered(self):
        for messages in ([], [''], ['a', 2]):
            with self.subTest(messages=messages), patch.object(main, 'handoff_if_due'), \
                 patch.object(main, 'refresh_summary'), patch.object(main, 'codex_text', return_value=(json.dumps({'messages':messages}), '')):
                with self.assertRaises(RuntimeError):
                    main.make_reply(self.store, self.profile, self.directory)

    def test_multiple_bubbles_then_wait_until_new_inbound(self):
        ids = iter([{'message_id':9001},{'message_id':9002}])
        def action_call(action, *args, **kwargs):
            return fake_response(action, send_responses=ids)
        with patch.object(main,'make_reply',return_value=['一句','另句']), patch.object(main,'refresh_memory_if_due'), \
             patch.object(main.random,'uniform',return_value=0), patch.object(self.fake,'call',side_effect=action_call) as send:
            self.bridge.receive(self.event)
            self.assertEqual(len(sent_calls(send)),2)
        self.assertEqual(self.store.get('reply_waiting'),'1')
        self.assertIsNone(scheduler.due_slot(self.store,datetime.now().astimezone(),{'enabled':True}))
        with self.assertRaises(RuntimeError):
            self.bridge.send_round(self.record,self.profile,self.store,'test',test=True)
        self.store.record_inbound('next','新来信')
        self.assertEqual(self.store.get('reply_waiting'),'0')

    def test_new_message_interrupts_remaining_bubbles(self):
        incoming=dict(self.event,message_id=43)
        with patch.object(main,'make_reply',return_value=['一句','另句']), patch.object(main,'refresh_memory_if_due'), \
             patch.object(self.fake,'next_event',side_effect=[None,None,None,None,incoming]), patch.object(self.fake,'call',wraps=self.fake.call) as send:
            self.bridge.receive(self.event)
        self.assertEqual(len(sent_calls(send)),1)
        self.assertEqual(self.store.reply_attempt('42')['state'],'sent')
        self.assertEqual(self.store.inbound_count(), 2)
        self.assertEqual(self.store.reply_attempt('43')['state'], 'waiting')
        self.assertEqual(self.store.db.execute(
            "SELECT body FROM messages WHERE qq_id='43' AND direction='friend'"
        ).fetchone()['body'], '测试来信')

    def test_second_bubble_failure_cannot_retry_whole_reply(self):
        responses = iter([{'message_id':9001}, TimeoutError('超时')])
        def action_call(action, *args, **kwargs):
            if action == 'get_login_info':
                return {'user_id': 77777}
            if action == 'send_private_msg':
                result = next(responses)
                if isinstance(result, Exception):
                    raise result
                return result
        with patch.object(main,'make_reply',return_value=['一句','另句']), patch.object(main.random,'uniform',return_value=0), \
             patch.object(self.fake,'call',side_effect=action_call):
            self.bridge.receive(self.event)
        self.assertEqual(self.store.reply_attempt('42')['state'],'uncertain')
        self.assertEqual(self.store.get('reply_waiting'),'1')
        with self.assertRaises(ValueError):
            self.store.request_reply_retry('42')

if __name__ == '__main__':
    unittest.main()

