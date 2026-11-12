import base64
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import voice_input
from store import FriendStore


def wav_bytes(seconds=0.2, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b'\x00\x10' * int(seconds * rate))
    return buffer.getvalue()


class FakeNapCat:
    def __init__(self):
        self.calls = []

    def call(self, action, params=None, timeout=10):
        self.calls.append((action, params))
        if action == 'get_record':
            return {'file': 'C:/napcat/temp/x.wav', 'base64': base64.b64encode(wav_bytes()).decode('ascii')}
        if action == 'get_login_info':
            return {'user_id': 77777}
        if action == 'send_private_msg':
            return {'message_id': 9001}
        raise AssertionError(action)

    def next_event(self, timeout=1):
        return None


VOICE_EVENT = {'post_type': 'message', 'message_type': 'private', 'self_id': 77777, 'user_id': 12345,
               'message_id': 42, 'message': [{'type': 'record', 'data': {'file': 'abc.amr', 'file_size': '3000'}}]}


class SettingsTests(unittest.TestCase):
    def test_defaults_and_invalid_values(self):
        self.assertEqual(voice_input.settings({}), voice_input.DEFAULTS)
        value = voice_input.settings({'voice_input': {'mode': 'shout', 'tier': 'huge', 'agent': 'codex',
                                                      'agent_model': '--evil flag'}})
        self.assertEqual(value, voice_input.DEFAULTS)

    def test_voice_segment_becomes_a_marker(self):
        self.assertEqual(main.message_parts(VOICE_EVENT), (voice_input.MARKER, None))
        self.assertEqual(voice_input.segment(VOICE_EVENT), {'file': 'abc.amr', 'file_size': '3000'})
        self.assertIsNone(voice_input.segment({'message': [{'type': 'text', 'data': {'text': 'hi'}}]}))

    def test_agent_choice_never_picks_a_backend_that_cannot_hear(self):
        value = voice_input.settings({})
        with patch('antigravity_backend.cli_path', return_value='agy.exe'):
            for provider in ('codex', 'claude', 'dsh'):
                profile = {'model_api': {'provider': provider}}
                self.assertEqual(voice_input.agent_choice(profile, value), ('antigravity', voice_input.DEFAULT_GEMINI))
            profile = {'model_api': {'provider': 'antigravity', 'antigravity_model': 'gemini-3.8-flash-high'}}
            self.assertEqual(voice_input.agent_choice(profile, value), ('antigravity', 'gemini-3.8-flash-high'))
            profile = {'model_api': {'provider': 'antigravity', 'antigravity_model': 'gpt-oss-120b-medium'}}
            self.assertEqual(voice_input.agent_choice(profile, value)[1], voice_input.DEFAULT_GEMINI)
        profile = {'model_api': {'provider': 'api', 'api_protocol': 'anthropic-messages', 'model': 'x'}}
        self.assertIsNone(voice_input.agent_choice(profile, value)[0])
        profile = {'model_api': {'provider': 'api', 'model': 'gpt-audio'}}
        self.assertEqual(voice_input.agent_choice(profile, value), ('api', 'gpt-audio'))
        with patch('antigravity_backend.cli_path', return_value=None):
            self.assertIsNone(voice_input.agent_choice({'model_api': {'provider': 'codex'}}, value)[0])

    def test_parse_model_answer(self):
        self.assertEqual(voice_input.parse('{}\n明天见。\n语气：开心'), ('明天见。', '开心'))
        self.assertEqual(voice_input.body_text('明天见。', '开心'), '[语音] 明天见。（语气：开心）')
        self.assertEqual(voice_input.body_text('（听不清）'), '[语音]（听不清内容）')


class UnderstandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)

    def test_agent_mode_hears_the_voice_file(self):
        profile = {'voice_input': {'mode': 'agent'}, 'model_api': {'provider': 'codex'}}
        seen = {}

        def generate(prompt, directory, api, image_path=None, **kwargs):
            seen.update(prompt=prompt, api=api, path=image_path, size=image_path.stat().st_size)
            return '{}\n明天下午三点图书馆见。\n语气：开心', [], []

        with patch('antigravity_backend.cli_path', return_value='agy.exe'), \
                patch('antigravity_backend.generate', side_effect=generate):
            text, note = voice_input.understand(FakeNapCat(), {'file': 'abc.amr'}, self.directory, profile, '42')
        self.assertEqual(text, '[语音] 明天下午三点图书馆见。（语气：开心）')
        self.assertIn(voice_input.DEFAULT_GEMINI, note)
        self.assertEqual(seen['api']['antigravity_model'], voice_input.DEFAULT_GEMINI)
        self.assertEqual(seen['path'], self.directory / 'media' / 'voice_in' / '42.wav')
        self.assertGreater(seen['size'], 44)
        self.assertIn('不要回答', seen['prompt'])

    def test_failed_agent_falls_back_to_the_free_local_model(self):
        profile = {'voice_input': {'mode': 'agent'}, 'model_api': {'provider': 'antigravity'}}
        with patch('antigravity_backend.cli_path', return_value='agy.exe'), \
                patch('antigravity_backend.generate', side_effect=RuntimeError('quota')), \
                patch.object(voice_input, 'local_ready', return_value=True), \
                patch.object(voice_input, 'transcribe_local', return_value=('好的', '')) as local:
            text, note = voice_input.understand(FakeNapCat(), {'file': 'a'}, self.directory, profile, '7')
        self.assertEqual(text, '[语音] 好的')
        self.assertIn('改用本地模型', note)
        local.assert_called_once()

    def test_missing_local_model_never_spends_model_quota(self):
        profile = {'voice_input': {'mode': 'local'}, 'model_api': {'provider': 'antigravity'}}
        with patch('antigravity_backend.generate') as agent, patch.object(voice_input, 'local_ready', return_value=False):
            text, note = voice_input.understand(FakeNapCat(), {'file': 'a'}, self.directory, profile, '8')
        agent.assert_not_called()
        self.assertTrue(text.startswith('[语音]（未能识别'))
        self.assertIn('尚未下载', note)

    def test_off_does_not_fetch_the_file(self):
        client = FakeNapCat()
        text, _ = voice_input.understand(client, {'file': 'a'}, self.directory, {'voice_input': {'mode': 'off'}}, '9')
        self.assertEqual(text, '[语音]（未开启语音识别）')
        self.assertEqual(client.calls, [])

    def test_api_model_gets_input_audio(self):
        profile = {'voice_input': {'mode': 'agent', 'agent': 'api'},
                   'model_api': {'provider': 'api', 'api_base_url': 'https://api.example.com/v1', 'model': 'gpt-audio'}}
        sent = {}

        def post(url, headers, payload, retry_key):
            sent.update(url=url, payload=payload)
            return {'choices': [{'message': {'content': '收到'}}]}

        with patch('api_backend.load_key', return_value='k'), patch('api_backend._post', side_effect=post):
            text, _ = voice_input.understand(FakeNapCat(), {'file': 'a'}, self.directory, profile, '10')
        self.assertEqual(text, '[语音] 收到')
        self.assertEqual(sent['url'], 'https://api.example.com/v1/chat/completions')
        audio = sent['payload']['messages'][0]['content'][1]
        self.assertEqual(audio['type'], 'input_audio')
        self.assertEqual(audio['input_audio']['format'], 'wav')

    def test_resampling_to_the_model_rate_keeps_pitch(self):
        import numpy as np
        for rate in (8000, 24000, 44100, 48000):
            t = np.arange(rate) / rate
            out = voice_input.to_model_rate(np.sin(2 * np.pi * 440 * t).astype(np.float32), rate)
            self.assertEqual(len(out), voice_input.MODEL_RATE)
            peak = np.argmax(np.abs(np.fft.rfft(out)))  # 1 s at 16 kHz: bin index = Hz
            self.assertEqual(peak, 440)
            self.assertAlmostEqual(float(np.max(np.abs(out[100:-100]))), 1.0, delta=0.05)
        same = np.ones(10, dtype=np.float32)
        self.assertIs(type(voice_input.to_model_rate(same, 16000)), np.ndarray)

    def test_read_wav_stereo_and_rate(self):
        path = self.directory / 's.wav'
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(24000)
            out.writeframes(b'\x00\x40\x00\x00' * 240)
        path.write_bytes(buffer.getvalue())
        samples, rate = voice_input.read_wav(path)
        self.assertEqual((len(samples), rate), (240, 24000))
        self.assertAlmostEqual(float(samples[0]), 0.25)


class PhonePageTests(unittest.TestCase):
    def test_switch_and_details_are_checked_and_saved(self):
        import house_web
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            main.dump_json(directory / 'friend.json', main.default_profile('12345', 'friend', False))
            with patch.object(house_web, '_directory', return_value=directory), \
                    patch('antigravity_backend.cli_path', return_value='agy.exe'):
                state = house_web.voice_settings('12345', {})['input']
                self.assertEqual(state['settings']['mode'], 'local')
                self.assertEqual([model['id'] for model in state['models']], ['small', 'standard', 'base'])
                result = house_web.post_voice_input('12345', {'mode': 'agent'})
                self.assertEqual(result['input']['agent'], 'antigravity')
                self.assertIn('AI 模型直接听', result['message'])
                house_web.post_voice_input('12345', {'agent_model': 'gemini-3.8-flash-high', 'tier': 'base'})
                for bad in ({'mode': 'loud'}, {'agent': 'codex'}, {'tier': 'x'}):
                    with self.assertRaises(ValueError):
                        house_web.post_voice_input('12345', bad)
            saved = json.loads((directory / 'friend.json').read_text('utf-8'))['voice_input']
            self.assertEqual(saved, {'mode': 'agent', 'tier': 'base', 'agent': 'auto',
                                     'agent_model': 'gemini-3.8-flash-high'})


class BridgeVoiceTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def test_voice_message_is_heard_once_and_replied_to(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            profile = main.default_profile('12345', 'friend', False)
            profile['bridge_enabled'] = True
            profile['voice_input'] = {'mode': 'local'}
            (friend / 'friend.json').write_text(json.dumps(profile, ensure_ascii=False), encoding='utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend), 'display_name': 'friend', 'self_id': '77777'}}}), 'utf-8')
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = '77777'
            with patch.object(main, 'REGISTRY', root / 'friends.json'), patch.object(main, 'FRIENDS_DIR', root / 'friends'), \
                    patch.object(main, 'make_reply', return_value='好，三点见') as reply, \
                    patch.object(main, 'refresh_memory_if_due'), \
                    patch.object(voice_input, 'local_ready', return_value=True), \
                    patch.object(voice_input, 'transcribe_local', return_value=('三点图书馆见', '开心')) as heard:
                bridge.receive(VOICE_EVENT)
                bridge.receive(VOICE_EVENT)  # NapCat redelivery: not heard twice
            heard.assert_called_once()
            reply.assert_called_once()
            self.assertEqual(len([call for call in fake.calls if call[0] == 'get_record']), 1)
            store = FriendStore(friend)
            try:
                row = store.db.execute("SELECT body FROM messages WHERE qq_id='42'").fetchone()
                self.assertEqual(row['body'], '[语音] 三点图书馆见（语气：开心）')
                self.assertTrue(store.is_handled('42'))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
