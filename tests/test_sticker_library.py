import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import main
import sticker_library
from sticker_library import prepare, collect, save_card, send_reference
from tests.test_house_recovery import RecoveryTests


class StickerTests(RecoveryTests):
    def test_different_sticker_and_short_text_can_follow_a_sticker_reply(self):
        source = self.root / 'incoming-mixed.png'
        other = self.root / 'other-mixed.png'
        Image.new('RGB', (12, 12), 'red').save(source)
        Image.new('RGB', (12, 12), 'green').save(other)
        current_key = hashlib.sha256(source.read_bytes()).hexdigest()
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(other if params['file'] == 'other' else source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out-' + params['message'][0]['type']}
            raise AssertionError(action)
        with patch.object(self.fake, 'call', side_effect=call) as api:
            key, original, _, _ = prepare(self.fake, self.directory, 'other')
            collect(self.fake, self.store, self.directory, key)
            card = {'text': '你赢了', 'meaning': '认输', 'emotion': '无奈', 'usage': '被怼到说不过时'}
            save_card(self.store, key, card, 'user')
            self.store.add_message('previous-out', 'me', f'[[LOCAL_STICKER:{current_key}]]')
            payload = {'expression': {'intent': '轻松接梗', 'emotion': 'happy', 'reply_focus': '顺着玩笑'},
                       'messages': ['[[LOCAL_STICKER:1]]', '哈哈你赢了']}  # the model answers with the offered number
            event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'incoming', 'sub_type': 1}}])
            with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                    patch.object(main, 'refresh_memory_if_due'), patch.object(main.random, 'uniform', return_value=0), \
                    patch.object(sticker_library, 'candidates', return_value=[(key, dict(card, by='user'))]), \
                    patch.object(main, 'codex_text', return_value=(json.dumps(payload, ensure_ascii=False), '')) as model:
                self.bridge.receive(event)
        model.assert_called_once()
        prompt = model.call_args.args[0]
        self.assertIn('STICKER_1: 图上文字：你赢了；意思：认输；情绪：无奈；适合什么时候用：被怼到说不过时', prompt)
        self.assertIn('STICKERS: [[LOCAL_STICKER:N]]', prompt)
        # the same sticker just sent is blocked by the program (tidy_bubbles), not described in the prompt
        self.assertNotIn(f'SAME_STICKER_OK_IF_MEME: [[LOCAL_STICKER:{current_key}]]', prompt)
        self.assertNotIn('这轮优先文字', prompt)
        sends = [item for item in api.call_args_list if item.args[0] == 'send_private_msg']
        self.assertEqual([item.args[1]['message'][0]['type'] for item in sends], ['image', 'text'])
        self.assertEqual(sends[0].args[1]['message'][0]['data']['file'], original.resolve().as_uri())
        self.assertEqual(sends[1].args[1]['message'][0]['data']['text'], '哈哈你赢了')
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')

    def test_no_different_favorite_allows_one_echo_but_not_repeated_echo(self):
        preview = self.root / 'echo-preview.png'
        Image.new('RGB', (12, 12), 'red').save(preview)
        key = 'a' * 64
        for limited in (False, True):
            with self.subTest(limited=limited), patch.object(main, 'handoff_if_due'), \
                    patch.object(main, 'refresh_summary'), \
                    patch.object(main, 'codex_text', return_value=('{"messages":["okay!"]}', '')) as model:
                main.make_reply(self.store, self.profile, self.directory, image_path=preview,
                                image_key=key, sticker_limit=limited, sticker_choices=[])
            prompt = model.call_args.args[0]
            self.assertNotIn('没有可预览的不同收藏表情，优先文字回应', prompt)
            if limited:
                self.assertNotIn('SAME_STICKER_OK_IF_MEME', prompt)
            else:
                self.assertIn(f'SAME_STICKER_OK_IF_MEME: [[LOCAL_STICKER:{key}]]', prompt)
            self.assertIn('PHOTO|SCREENSHOT|HELP_REQUEST -> TEXT_REPLY', prompt)
        # an echo the model sends anyway while limited is dropped by the program
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=(json.dumps({'messages': [f'[[LOCAL_STICKER:{key}]]', 'okay!']}), '')):
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory, image_path=preview,
                                             image_key=key, sticker_limit=True, sticker_choices=[]), ['okay!'])

    def test_sticker_text_reply_sees_image_and_context_in_one_model_call(self):
        source = self.root / 'incoming-context.png'
        Image.new('RGB', (12, 12), 'red').save(source)
        self.store.add_message('previous-in', 'friend', '刚才说要早睡，结果又熬夜了')
        self.store.add_message('previous-out', 'me', '你这早睡计划又泡汤啦')
        payload = {'expression': {'intent': '接着早睡玩笑装委屈',
                                 'emotion': 'banter, ', 'reply_focus': '早睡计划的玩笑'},
                   'messages': ['好啦不逗你了，现在睡还来得及']}
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out'}
            raise AssertionError(action)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'incoming', 'sub_type': 1}}])
        with patch.object(self.fake, 'call', side_effect=call) as api, \
                patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'refresh_memory_if_due'), \
                patch.object(main, 'codex_text', return_value=(json.dumps(payload, ensure_ascii=False), '')) as model:
            self.bridge.receive(event)
        model.assert_called_once()
        prompt = model.call_args.args[0]
        for text in ('PICTURE_TEXT=CLUE_NOT_EMOTION',
        'UNCERTAIN_MEANING',
        'PHOTO|SCREENSHOT|HELP_REQUEST',
        'image_card',
        '好友：刚才说要早睡，结果又熬夜了',
        '我：你这早睡计划又泡汤啦'):
            self.assertIn(text, prompt)
        self.assertTrue(model.call_args.kwargs['image_path'].is_file())
        sends = [item for item in api.call_args_list if item.args[0] == 'send_private_msg']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].args[1]['message'],
                         [{'type': 'text', 'data': {'text': payload['messages'][0]}}])
        self.assertEqual(self.store.reply_attempt('42')['state'], 'sent')

    def test_model_sees_friend_saved_candidates_and_sends_different_original(self):
        source = self.root / 'incoming.png'
        other = self.root / 'other.png'
        Image.new('RGB', (12, 12), 'red').save(source)
        Image.new('RGB', (12, 12), 'green').save(other)
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(other if params['file'] == 'other' else source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out'}
            raise AssertionError(action)
        with patch.object(self.fake, 'call', side_effect=call) as api:
            key, original, _, _ = prepare(self.fake, self.directory, 'other')
            collect(self.fake, self.store, self.directory, key)
            offered = [(key, {'meaning': 'happy'})]
            event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'incoming', 'sub_type': 1}}])
            with patch.object(main, 'make_reply', return_value=[f'[[LOCAL_STICKER:{key}]]']) as model, \
                    patch.object(sticker_library, 'candidates', return_value=offered) as search, \
                    patch.object(main, 'refresh_memory_if_due'):
                self.bridge.receive(event)
            self.assertEqual(model.call_args.kwargs['sticker_choices'], offered)
            self.assertEqual(search.call_args.kwargs['exclude'], [hashlib.sha256(source.read_bytes()).hexdigest()])
            self.assertTrue(model.call_args.kwargs['image_path'].name.endswith('.preview.png'))  # incoming image itself
            sends = [item for item in api.call_args_list if item.args[0] == 'send_private_msg']
            self.assertEqual(sends[0].args[1]['message'][0]['data']['file'], original.resolve().as_uri())
            self.assertEqual(original.read_bytes(), other.read_bytes())
            with self.assertRaisesRegex(RuntimeError, '上限为1次'):
                self.bridge.send_text(self.record, self.store, f'[[LOCAL_STICKER:{key}]]')

    def test_limit_prompt_requests_image_understanding_without_same_image_instruction(self):
        preview = self.root / 'preview.png'
        Image.new('RGB', (12, 12), 'red').save(preview)
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=('正在偷笑是吧', '')) as model:
            reply = main.make_reply(self.store, self.profile, self.directory,
                image_path=preview, image_key='a' * 64, sticker_limit=True)
        self.assertEqual(reply, ['正在偷笑是吧'])
        prompt = model.call_args.args[0]
        self.assertIn('ANSWER_WHAT_FRIEND_MEANT', prompt)
        self.assertNotIn('SAME_STICKER_OK_IF_MEME', prompt)
        self.assertNotIn('本轮只回同一张', prompt)
        self.assertEqual(model.call_args.kwargs['image_path'], preview)

    def test_every_image_uses_model_and_one_echo_then_text_resets_limit(self):
        source = self.root / 'sticker.png'
        Image.new('RGB', (12, 12), 'red').save(source)
        marker = '[[LOCAL_STICKER:' + hashlib.sha256(source.read_bytes()).hexdigest() + ']]'
        sends = []
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg':
                sends.append(params['message'])
                return {'message_id': 'out' + str(len(sends))}
            raise AssertionError(action)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'image-id', 'sub_type': 1}}])
        with patch.object(self.fake, 'call', side_effect=call), \
                patch.object(main, 'make_reply', side_effect=[[marker], ['你这表情笑死我了'], [marker]]) as model, \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(event)
            model.assert_called_once()
            self.bridge.receive(dict(event, message_id=47))
            self.assertEqual(model.call_count, 2)
            self.assertTrue(model.call_args.kwargs['sticker_limit'])
            self.assertTrue(model.call_args.kwargs['image_path'].exists())
            self.assertEqual(sends[-1][0]['type'], 'text')
            self.bridge.receive(dict(event, message_id=48))
            self.assertEqual(model.call_count, 3)
            self.assertEqual(sends[-1][0]['type'], 'image')

    def test_limit_survives_reopen_is_friend_local_and_counts_confirmed_sends(self):
        from store import FriendStore
        marker = '[[LOCAL_STICKER:' + 'a' * 64 + ']]'
        for number in range(5):
            self.store.add_message('out' + str(number), 'me', marker)
            self.store.add_message('in' + str(number), 'friend', '[image]')
        # 2026-10-10: the program (tidy_bubbles) drops the sticker we sent last; friend-local and from SQLite
        def echo(store):
            return main.tidy_bubbles(store, [marker], {}, allowed=['a' * 64])
        other = FriendStore(self.root / 'other-friend')
        try:
            self.assertEqual(echo(other), [marker])
        finally:
            other.close()
        self.store.close()
        self.store = FriendStore(self.directory)
        self.assertEqual(echo(self.store), [])
        with patch.object(self.fake, 'call', side_effect=RuntimeError('Sendfailed')):
            with self.assertRaises(RuntimeError):
                self.bridge.send_text(self.record, self.store, 'chars')
        self.assertEqual(echo(self.store), [])
        self.store.add_message('native', 'me', '[[QQ表情:微笑]]')
        self.assertEqual(echo(self.store), [marker])

    def test_repeated_bracket_tail_is_reported_not_cut(self):
        # 2026-10-12 user:  (? )  ended most of our messages; the model is told and decides, nothing is removed
        self.store.add_message('a', 'me', '你咋还没睡（？）')
        self.store.add_message('b', 'me', '（刚下课刷到的）')
        self.assertEqual(main.tail_line(self.store), '')  # once is not a habit; a written aside never counts
        self.store.add_message('c', 'me', '今天不会困死吧（？）')
        self.assertEqual(main.tail_line(self.store), f'OUR_RECENT_ENDINGS: （？） ENDED 2_OF_OUR_LAST_{main.TAIL_MEMORY}; '
                                                     'USE_ONLY_WHEN_IT_ADDS_TONE_HERE; NOT_A_DEFAULT_ENDING')
        self.assertEqual(main.tidy_bubbles(self.store, ['困死吧（？）'], {}), ['困死吧（？）'])
        # 2026-10-13: the template samples' [:/NAME] is sent as a real face, an unknown one is dropped
        self.assertEqual(main.tidy_bubbles(self.store, ['你好你好[表情:/牛气冲天]', '嗯[表情:/不存在]'], {}),
                         ['你好你好[[QQ表情:牛气冲天]]', '嗯'])
        for number in range(main.TAIL_MEMORY):
            self.store.add_message('d' + str(number), 'me', 'yeah')
        self.assertEqual(main.tail_line(self.store), '')
        self.store.add_message('e', 'me', '还没猜到啊😌')
        self.store.add_message('f', 'me', '看不到你在干嘛呀😌')  # 2026-10-13: an emoji tail counts too
        self.assertIn('😌 ENDED 2_OF', main.tail_line(self.store))

    def test_limit_rejects_same_image_even_if_model_ignores_instruction(self):
        source = self.root / 'sticker.png'
        Image.new('RGB', (12, 12), 'blue').save(source)
        key = hashlib.sha256(source.read_bytes()).hexdigest()
        marker = f'[[LOCAL_STICKER:{key}]]'
        for number in range(5):
            self.store.add_message('out' + str(number), 'me', marker)
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            raise AssertionError('不应继续发送：' + action)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'image-id', 'sub_type': 1}}])
        with patch.object(self.fake, 'call', side_effect=call), patch.object(main, 'make_reply', return_value=[marker]):
            self.bridge.receive(event)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'failed')
        self.assertIn('连续回发1次', self.store.reply_attempt('42')['error'])

    def test_animated_sticker_is_collected_once_and_sent_from_qq_favorites(self):
        source = self.root / 'animation.gif'
        Image.new('RGB', (12, 12), 'red').save(source, save_all=True,
            append_images=[Image.new('RGB', (12, 12), 'blue')], duration=100, loop=0)
        digest = hashlib.md5(source.read_bytes()).hexdigest()
        favorites = []
        calls = []
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            calls.append((action, params))
            if action == 'get_image':
                return {'file': str(source)}
            if action == 'fetch_custom_face_detail':
                return list(favorites)
            if action == 'add_custom_face':
                original = self.directory / 'media' / (hashlib.sha256(source.read_bytes()).hexdigest() + '.gif')
                favorites.append({'md5': digest, 'resId': 'qq-favorite', 'url': original.resolve().as_uri()})
                return {'result': 0}
            if action == 'send_private_msg':
                return {'message_id': 'out' + str(len(calls))}
            raise AssertionError(action)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'gif-id', 'sub_type': 1}}])
        marker = '[[LOCAL_STICKER:' + hashlib.sha256(source.read_bytes()).hexdigest() + ']]'
        with patch.object(self.fake, 'call', side_effect=call), patch.object(self.bridge, 'log') as log, \
                patch.object(main, 'make_reply', side_effect=[[marker], ['charsrespond']]) as model, \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(event)
            self.bridge.receive(dict(event, message_id=43))
        self.assertEqual(model.call_count, 2)
        self.assertEqual(sum(action == 'add_custom_face' for action, _ in calls), 1)
        self.assertEqual(sum('已收藏到小屋本地' in item.args[0] for item in log.call_args_list), 1)
        sends = [params for action, params in calls if action == 'send_private_msg']
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0]['message'][0]['data']['file'], favorites[0]['url'])
        self.assertEqual(sends[1]['message'][0]['type'], 'text')
        originals = list((self.directory / 'media').glob('*.gif'))
        self.assertEqual(originals[0].read_bytes(), source.read_bytes())
        with Image.open(next((self.directory / 'media').glob('*.preview.png'))) as preview:
            self.assertEqual(preview.format, 'PNG')
            self.assertEqual(preview.width, 24)

    def test_confirmed_collection_is_reused_after_reopen_without_qq_calls(self):
        from store import FriendStore
        source = self.root / 'already-saved.png'
        Image.new('RGB', (10, 10), 'yellow').save(source)
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, _, _, _ = prepare(self.fake, self.directory, 'image-id')
        with patch.object(self.fake, 'call', return_value=[{'md5': hashlib.md5(source.read_bytes()).hexdigest(), 'resId': 'saved'}]):
            self.assertTrue(collect(self.fake, self.store, self.directory, key)['qq_saved'])
        self.store.close()
        self.store = FriendStore(self.directory)
        with patch.object(self.fake, 'call') as api:
            result = collect(self.fake, self.store, self.directory, key)
        api.assert_not_called()
        self.assertTrue(result['qq_saved'])
        self.assertTrue(result['unchanged'])

    def test_delayed_qq_favorite_confirmation_does_not_add_again(self):
        from store import FriendStore
        source = self.root / 'pending-saved.png'
        Image.new('RGB', (10, 10), 'yellow').save(source)
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, _, _, _ = prepare(self.fake, self.directory, 'image-id')
        with patch.object(self.fake, 'call', side_effect=[[], {'result': 0}, []]) as api:
            result = collect(self.fake, self.store, self.directory, key)
        self.assertFalse(result['qq_saved'])
        self.assertEqual(sum(item.args[0] == 'add_custom_face' for item in api.call_args_list), 1)
        self.store.close()
        self.store = FriendStore(self.directory)
        with patch.object(self.fake, 'call', return_value=[]) as api:
            self.assertFalse(collect(self.fake, self.store, self.directory, key)['qq_saved'])
        self.assertEqual([item.args[0] for item in api.call_args_list], ['fetch_custom_face_detail'])
        with patch.object(self.fake, 'call', return_value=[{'md5': hashlib.md5(source.read_bytes()).hexdigest(), 'resId': 'saved'}]) as api:
            self.assertTrue(collect(self.fake, self.store, self.directory, key)['qq_saved'])
        self.assertEqual([item.args[0] for item in api.call_args_list], ['fetch_custom_face_detail'])

    def test_explicit_qq_add_failure_can_retry(self):
        source = self.root / 'failed-saved.png'
        Image.new('RGB', (10, 10), 'yellow').save(source)
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, _, _, _ = prepare(self.fake, self.directory, 'image-id')
        for failure in ({'result': 1}, RuntimeError('QQ 直连 add_custom_face failed: refuse')):
            with self.subTest(failure=failure), patch.object(self.fake, 'call', side_effect=[[], failure]) as api:
                result = collect(self.fake, self.store, self.directory, key)
            self.assertFalse(result['qq_saved'])
            self.assertFalse(result['qq_add_requested'])
            self.assertEqual([item.args[0] for item in api.call_args_list], ['fetch_custom_face_detail', 'add_custom_face'])

    def test_uncertain_add_timeout_is_not_submitted_again(self):
        source = self.root / 'timeout-saved.png'
        Image.new('RGB', (10, 10), 'yellow').save(source)
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, _, _, _ = prepare(self.fake, self.directory, 'image-id')
        with patch.object(self.fake, 'call', side_effect=[[], TimeoutError('结果未知')]):
            self.assertFalse(collect(self.fake, self.store, self.directory, key)['qq_saved'])
        with patch.object(self.fake, 'call', return_value=[]) as api:
            result = collect(self.fake, self.store, self.directory, key)
        self.assertFalse(result['qq_saved'])
        self.assertEqual([item.args[0] for item in api.call_args_list], ['fetch_custom_face_detail'])

    def test_photo_is_given_to_model_and_not_added_to_qq_favorites(self):
        source = self.root / 'photo.jpg'
        Image.new('RGB', (10, 10), 'green').save(source)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'photo'}}])
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'send_private_msg': return {'message_id': 'out'}
            raise AssertionError(action)
        with patch.object(self.fake, 'call', side_effect=call), patch.object(main, 'make_reply', return_value=['chars']) as model, \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(event)
        self.assertEqual(model.call_args.kwargs['image_path'].suffix, '.png')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'sticker:%'").fetchone()[0], 0)

    def test_all_images_of_one_message_reach_the_model(self):
        files = {}
        for name, color in (('first', 'red'), ('second', 'green'), ('third', 'blue')):
            files[name] = self.root / f'{name}.png'
            Image.new('RGB', (20, 10), color).save(files[name])
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(files[params['file']])}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out'}
            raise AssertionError(action)
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'first'}},
                                          {'type': 'text', 'data': {'text': '你看这几张'}},
                                          {'type': 'image', 'data': {'file': 'second', 'sub_type': 1}},
                                          {'type': 'image', 'data': {'file': 'third'}}])
        with patch.object(self.fake, 'call', side_effect=call), \
                patch.object(main, 'make_reply', return_value=['都好看']) as model, \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(event)
        kwargs = model.call_args.kwargs
        self.assertEqual(kwargs['image_count'], 3)
        self.assertTrue(kwargs['image_path'].name.endswith('.multi.png'))
        with Image.open(kwargs['image_path']) as sheet:
            self.assertEqual(sheet.size, (1024, 2 * (512 + 34)))
        self.assertNotIn('image_key', kwargs)  # no same-image echo for a multi-image message
        second = hashlib.sha256(files['second'].read_bytes()).hexdigest()
        self.assertIsNotNone(self.store.get('sticker:' + second))  # the QQ-tagged sticker is still collected
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'sticker:%'").fetchone()[0], 1)

    def test_untagged_meme_is_collected_with_its_card_and_user_cards_are_kept(self):
        source = self.root / 'meme.png'
        Image.new('RGB', (10, 10), 'purple').save(source)
        key = hashlib.sha256(source.read_bytes()).hexdigest()
        card = {'is_meme': True, 'text': '6', 'meaning': '无语', 'emotion': '无奈', 'usage': '对方说了离谱的话'}
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out'}
            raise AssertionError(action)
        def reply(*args, card_sink=None, **kwargs):
            card_sink['card'] = card
            return ['haha']
        event = dict(self.event, message=[{'type': 'image', 'data': {'file': 'meme'}}])  # not tagged by QQ
        with patch.object(self.fake, 'call', side_effect=call), patch.object(main, 'make_reply', side_effect=reply), \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(event)
        state = json.loads(self.store.get('sticker:' + key))
        self.assertEqual((state['card']['meaning'], state['card']['by']), ('无语', 'codex'))
        save_card(self.store, key, {'meaning': '我写的'}, 'user')
        self.assertFalse(save_card(self.store, key, card, 'codex'))  # automatic drafts never replace the user's
        self.assertEqual(json.loads(self.store.get('sticker:' + key))['card']['meaning'], '我写的')

    def test_sticker_not_offered_this_round_is_refused(self):
        with patch.object(main, 'make_reply', return_value=['[[LOCAL_STICKER:' + 'b' * 64 + ']]']), \
                patch.object(main, 'refresh_memory_if_due'):
            self.bridge.receive(self.event)
        self.assertEqual(self.store.reply_attempt('42')['state'], 'failed')
        self.assertIn('只能发本轮给出的收藏表情', self.store.reply_attempt('42')['error'])

    def test_proactive_round_can_use_one_offered_sticker(self):
        key = 'c' * 64
        card = {'text': '在吗', 'meaning': '找你聊天', 'emotion': '期待', 'usage': 'proactive', 'by': 'user'}
        def round_with(messages):
            with patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                    patch.object(sticker_library, 'candidates', return_value=[(key, card)]), \
                    patch.object(main, 'codex_text', return_value=(json.dumps({'messages': messages}, ensure_ascii=False), '')) as model:
                return main.make_round(self.store, self.profile, self.directory, len(messages)), model.call_args.args[0]
        bubbles, prompt = round_with(['[[LOCAL_STICKER:1]]', '刚看到一个好玩的'])
        self.assertEqual(bubbles, [f'[[LOCAL_STICKER:{key}]]', '刚看到一个好玩的'])
        self.assertIn('STICKER_1: 图上文字：在吗；意思：找你聊天', prompt)
        # the program keeps one offered sticker and drops stickers that were not offered
        self.assertEqual(round_with(['[[LOCAL_STICKER:1]]', '[[LOCAL_STICKER:1]] '])[0], [f'[[LOCAL_STICKER:{key}]]'])
        with self.assertRaises(RuntimeError):
            round_with(['[[LOCAL_STICKER:' + 'd' * 64 + ']]'])

    def test_proactive_sticker_is_sent_from_the_collection(self):
        source = self.root / 'hello.png'
        Image.new('RGB', (12, 12), 'blue').save(source)
        def call(action, params=None, **kwargs):
            if action == 'get_login_info': return {'user_id': 77777}
            if action == 'get_image': return {'file': str(source)}
            if action == 'fetch_custom_face_detail': return []
            if action == 'add_custom_face': return {'result': 0}
            if action == 'send_private_msg': return {'message_id': 'out-' + params['message'][0]['type']}
            raise AssertionError(action)
        self.profile['proactive'].update(enabled=True, bubbles_min=2, bubbles_max=2, bubble_interval_seconds=[0, 0])
        main.dump_json(self.directory / 'friend.json', self.profile)
        with patch.object(self.fake, 'call', side_effect=call) as api:
            key, original, _, _ = prepare(self.fake, self.directory, 'hello')
            collect(self.fake, self.store, self.directory, key)
            with patch.object(main, 'make_round', return_value=[f'[[LOCAL_STICKER:{key}]]', '在干嘛']):
                self.bridge.send_round(self.record, main.load_profile(self.record), self.store, 'sticker-test', test=True)
        sends = [item.args[1]['message'][0] for item in api.call_args_list if item.args[0] == 'send_private_msg']
        self.assertEqual([part['type'] for part in sends], ['image', 'text'])
        self.assertEqual(sends[0]['data']['file'], original.resolve().as_uri())

    def test_remove_takes_sticker_out_of_collection_and_qq_favorites(self):
        source = self.root / 'unwanted.png'
        Image.new('RGB', (10, 10), 'gray').save(source)
        md5 = hashlib.md5(source.read_bytes()).hexdigest()
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, original, _, _ = prepare(self.fake, self.directory, 'unwanted')
        with patch.object(self.fake, 'call', return_value=[{'md5': md5, 'resId': 'res-1'}]):
            collect(self.fake, self.store, self.directory, key)
        def call(action, params=None, **kwargs):
            if action == 'fetch_custom_face_detail': return [{'md5': md5, 'resId': 'res-1'}]
            if action == 'delete_custom_face': return None
            raise AssertionError(action)
        with patch.object(self.fake, 'call', side_effect=call) as api:
            sticker_library.remove(self.store, self.directory, key, self.fake)
        api.assert_any_call('delete_custom_face', {'res_id': 'res-1'})
        self.assertIsNone(self.store.get('sticker:' + key))
        self.assertTrue(original.exists())  # the local file stays as the reply cache
        with patch.object(self.fake, 'call') as api:
            sticker_library.remove(self.store, self.directory, key)  # local only: no QQ call
        api.assert_not_called()

    def test_picture_judged_not_a_meme_is_not_offered_until_the_user_saves_its_card(self):
        for key in ('a' * 64, 'b' * 64):
            self.store.set('sticker:' + key, '{}')
        save_card(self.store, 'a' * 64, {'is_meme': False, 'meaning': '一张截图'}, 'codex')
        save_card(self.store, 'b' * 64, {'is_meme': True, 'meaning': 'happy'}, 'codex')
        with patch('embedding_provider.embed_unscoped', side_effect=lambda texts, **k: [[1.0, 0.0]] * len(texts)):
            self.assertEqual(list(sticker_library.card_vectors(self.store, self.directory)), ['b' * 64])
            save_card(self.store, 'a' * 64, {'meaning': '其实能当表情用'}, 'user')
            self.assertEqual(sorted(sticker_library.card_vectors(self.store, self.directory)), ['a' * 64, 'b' * 64])

    def test_proactive_threshold_leaves_out_stickers_that_do_not_fit(self):
        for key, meaning in (('a' * 64, '笑到停不下来'), ('b' * 64, '困惑')):
            self.store.set('sticker:' + key, '{}')
            save_card(self.store, key, {'meaning': meaning}, 'user')
        vectors = {'笑到停不下来': [1.0, 0.0], '困惑': [0.0, 1.0], 'haha': [0.8, 0.6]}
        def embed(texts, **kwargs):
            return [next(v for word, v in vectors.items() if word in text) for text in texts]
        rerank = {'意思：笑到停不下来': 0.8, '意思：困惑': 0.1}  # bge-reranker stand-in
        with patch('embedding_provider.embed_unscoped', side_effect=embed), \
                patch('reranker.score', lambda query, passages: [rerank[p] for p in passages]):
            self.assertEqual([key for key, _ in sticker_library.candidates(self.store, self.directory, 'haha')],
                             ['a' * 64, 'b' * 64])  # replies: no threshold
            self.assertEqual([key for key, _ in sticker_library.candidates(self.store, self.directory, 'haha',
                                                                           min_score=0.7)], ['a' * 64])
            self.assertEqual(sticker_library.candidates(self.store, self.directory, 'haha', min_score=0.9), [])

    def test_picture_only_message_searches_examples_with_its_card_and_recent_lines(self):
        key = 'e' * 64
        self.store.set('sticker:' + key, '{}')
        save_card(self.store, key, {'meaning': '笑到停不下来'}, 'user')
        self.store.add_message('m1', 'me', '你今天考得怎么样')
        self.store.record_inbound('f1', '数学炸了')
        self.store.record_inbound('f2', '[image]')
        self.assertEqual(main.retrieval_query(self.store, '数学炸了'), '数学炸了')  # words: used as they are
        query = main.retrieval_query(self.store, '[image]', key)
        self.assertIn('意思：笑到停不下来', query)
        self.assertIn('你今天考得怎么样', query)
        self.assertIn('数学炸了', query)
        seen = []
        with patch('persona_template.prompt_block', side_effect=lambda *a, **k: seen.append(a[2]) or ''), \
                patch('local_persona_analyzer.reply_scene', return_value='unknown'), \
                patch.object(main, 'handoff_if_due'), patch.object(main, 'refresh_summary'), \
                patch.object(main, 'codex_text', return_value=('{"messages":["haha"]}', '')):
            self.profile['persona_template'] = 'abc'
            main.make_reply(self.store, self.profile, self.directory, image_key=key)
        self.assertIn('意思：笑到停不下来', seen[0])

    def test_qq_failure_keeps_original_and_does_not_claim_favorite_success(self):
        source = self.root / 'sticker.png'
        Image.new('RGB', (10, 10), 'yellow').save(source)
        with patch.object(self.fake, 'call', return_value={'file': str(source)}):
            key, original, _, _ = prepare(self.fake, self.directory, 'image-id')
        with patch.object(self.fake, 'call', side_effect=RuntimeError('failed')):
            state = collect(self.fake, self.store, self.directory, key)
            self.assertEqual(send_reference(self.fake, self.directory, key), original.resolve().as_uri())
        self.assertFalse(state['qq_saved'])
        self.assertTrue(original.exists())
        self.assertIn('QQ confirmsuccess', state['error'])
        with self.assertRaises(RuntimeError):
            self.bridge.send_text(self.record, self.store, '[[LOCAL_STICKER:' + '0' * 64 + ']]')


class StickerCardProviderTests(RecoveryTests):
    def test_card_writer_uses_the_configured_vision_provider(self):
        key = 'f' * 64
        preview = self.directory / 'media' / (key + '.preview.png')
        preview.parent.mkdir(exist_ok=True)
        Image.new('RGB', (12, 12), 'purple').save(preview)
        profile = {'model_api': {'provider': 'claude'}}
        card = {'is_meme': True, 'text': '', 'meaning': '无语', 'emotion': '无奈', 'usage': '对方说离谱话时'}
        with patch.object(main, 'codex_text', return_value=(json.dumps(card, ensure_ascii=False), '')) as model:
            value = sticker_library.write_card_with_model(self.store, self.directory, key, profile)
        self.assertEqual(value['meaning'], '无语')
        self.assertEqual(json.loads(self.store.get('sticker:' + key))['card']['by'], 'claude')
        self.assertEqual(model.call_args.kwargs['profile'], profile)
        self.assertEqual(model.call_args.kwargs['image_path'], preview)
        with self.assertRaisesRegex(RuntimeError, '不支持看图'):
            sticker_library.write_card_with_model(self.store, self.directory, key,
                                                  {'model_api': {'provider': 'dsh'}})
