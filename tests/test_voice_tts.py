import base64
import json
import os
import io
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import model_downloader
import voice_tts
from store import FriendStore

ROOT = Path(__file__).resolve().parents[1]


class FakeNapCat:
    def __init__(self):
        self.calls = []

    def call(self, action, params=None, timeout=10):
        self.calls.append((action, params))
        if action == 'get_login_info':
            return {'user_id': 77777}
        if action == 'send_private_msg':
            return {'message_id': 9001}
        raise AssertionError(action)

    def next_event(self, timeout=1):
        return None


def tone(seconds=0.5, rate=44100):
    return (0.5 * np.sin(np.linspace(0, 440 * 2 * np.pi * seconds, int(rate * seconds)))).astype(np.float32), rate


class VoiceSettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_values_are_clamped(self):
        self.assertEqual(voice_tts.settings({})['mode'], 'off')
        value = voice_tts.settings({'voice_reply': {'mode': 'shout', 'tier': 'huge', 'speed': 9,
                                                    'max_chars': 0, 'ratio': 4, 'speaker': 500}})
        self.assertEqual((value['mode'], value['tier'], value['speed'], value['max_chars'], value['ratio']),
                         ('off', 'small', 2.0, 5, 1.0))
        self.assertEqual(value['speaker'], model_downloader.MODEL_CATALOG['tts']['small']['default_speaker'])
        kokoro = voice_tts.settings({'voice_reply': {'tier': 'base', 'speaker': 60}})
        self.assertEqual(kokoro['speaker'], 60)

    def test_speakable_drops_markers_and_emoji(self):
        self.assertEqual(voice_tts.speakable('好呀😄 [[QQ表情:微笑]] 明天见'), '好呀 明天见')
        self.assertEqual(voice_tts.speakable('😄😄 ✨'), '')

    def test_wants_voice_modes(self):
        always = {'voice_reply': {'mode': 'always', 'max_chars': 10}}
        self.assertFalse(voice_tts.wants_voice({}, '在呢'))
        self.assertTrue(voice_tts.wants_voice(always, '在呢'))
        self.assertFalse(voice_tts.wants_voice(always, '这一句话明显超过了十个字的长度限制'))
        self.assertFalse(voice_tts.wants_voice(always, '看 https://example.com'))
        self.assertFalse(voice_tts.wants_voice(always, '😄'))
        sometimes = {'voice_reply': {'mode': 'sometimes', 'ratio': 0.3}}
        self.assertTrue(voice_tts.wants_voice(sometimes, '在呢', rng=lambda: 0.1))
        self.assertFalse(voice_tts.wants_voice(sometimes, '在呢', rng=lambda: 0.5))
        self.assertTrue(voice_tts.wants_voice(sometimes, '在呢', rng=lambda: 0.9, force=True))
        self.assertFalse(voice_tts.wants_voice(always, '在呢', forbid=True))
        self.assertTrue(voice_tts.requested_voice('我就要听你发语音'))
        self.assertTrue(voice_tts.requested_voice('发一条语音给我呗'))
        self.assertFalse(voice_tts.requested_voice('今天好热'))

    def test_wav_is_mono_pcm16_at_qq_rate(self):
        samples, rate = tone(rate=44100)
        data = voice_tts.wav_bytes(samples * 3, rate)
        with wave.open(io.BytesIO(data)) as reader:
            self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()),
                             (1, 2, voice_tts.QQ_SAMPLE_RATE))
            self.assertAlmostEqual(reader.getnframes(), voice_tts.QQ_SAMPLE_RATE * 0.5, delta=2)
            pcm = np.frombuffer(reader.readframes(reader.getnframes()), '<i2')
        self.assertLessEqual(int(np.max(np.abs(pcm))), 32767)
        self.assertGreater(int(np.max(np.abs(pcm))), 30000)

    def test_record_segment_is_base64_wav(self):
        with patch.object(voice_tts, 'synthesize', return_value=tone()) as synth:
            part = voice_tts.record_segment('在呢', {'voice_reply': {'mode': 'always', 'tier': 'base', 'speaker': 7,
                                                                   'speed': 1.2}})
        synth.assert_called_once_with('在呢', 'base', 7, 1.2, None, None)
        self.assertEqual(part['type'], 'record')
        self.assertTrue(part['data']['file'].startswith('base64://'))
        self.assertEqual(base64.b64decode(part['data']['file'][9:])[:4], b'RIFF')

    def test_record_segment_caches_wav_for_phone(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(voice_tts, 'synthesize', return_value=tone()):
            voice_tts.record_segment('在呢', {'voice_reply': {'mode': 'always'}}, directory=Path(folder))
            files = list((Path(folder) / 'media' / 'voice').glob('*.wav'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes()[:4], b'RIFF')

    def test_missing_model_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, '尚未下载'):
                voice_tts.synthesize('你好', 'small', root=Path(folder))


class BridgeVoiceTests(unittest.TestCase):
    def setUp(self):
        classification = patch('message_classification.enqueue_for_database')
        classification.start()
        self.addCleanup(classification.stop)

    def send(self, profile, text, quote_to=None, synth=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'friends' / '12345'
            main.dump_json(path / 'friend.json', {'qq_id': '12345', 'bridge_enabled': True, **profile})
            store = FriendStore(path)
            fake = FakeNapCat()
            bridge = main.Bridge(fake)
            bridge.self_id = '77777'
            record = {'qq_id': '12345', 'directory': str(path), 'self_id': '77777'}
            main.dump_json(root / 'friends.json', {'friends': {'12345': record}})
            try:
                with patch.object(main, 'REGISTRY', root / 'friends.json'), \
                        patch.object(main, 'FRIENDS_DIR', root / 'friends'), \
                        patch.object(voice_tts, 'synthesize', side_effect=synth or (lambda *a: tone())):
                    bridge.send_text(record, store, text, quote_to=quote_to)
                history = [row['body'] for row in store.db.execute("SELECT body FROM messages WHERE direction='me'")]
            finally:
                store.close()
        sends = [params['message'] for action, params in fake.calls if action == 'send_private_msg']
        return sends, history

    def test_opted_in_text_goes_out_as_record_without_quote(self):
        sends, history = self.send({'voice_reply': {'mode': 'always'}}, '在呢，刚吃完饭', quote_to='42')
        self.assertEqual(len(sends), 1)
        self.assertEqual([part['type'] for part in sends[0]], ['record'])
        self.assertEqual(history, ['在呢，刚吃完饭'])

    def test_default_profile_stays_text(self):
        sends, _ = self.send({}, '在呢')
        self.assertEqual([part['type'] for part in sends[0]], ['text'])

    def test_faces_stay_text(self):
        sends, _ = self.send({'voice_reply': {'mode': 'always'}}, '好呀[[QQ表情:微笑]]')
        self.assertIn('face', [part['type'] for part in sends[0]])
        self.assertNotIn('record', [part['type'] for part in sends[0]])

    def test_clone_tier_uses_the_friends_own_reference(self):
        seen = {}

        def clone(base, text, reference, transcript, root=None):
            seen.update(text=text, reference=reference, transcript=transcript)
            return tone()

        profile = {'voice_reply': {'mode': 'always', 'tier': 'large', 'reference_wav': voice_tts.REFERENCE_WAV,
                                   'reference_text': '原话'}}
        original = main.dump_json

        def dump_with_reference(path, value):
            original(path, value)
            if Path(path).name == 'friend.json':
                (Path(path).parent / 'media').mkdir(exist_ok=True)
                (Path(path).parent / voice_tts.REFERENCE_WAV).write_bytes(b'RIFF')

        with patch.object(main, 'dump_json', side_effect=dump_with_reference), \
                patch.object(voice_tts, 'model_dir', return_value=Path('.')), \
                patch.object(voice_tts.CLONE, 'generate', side_effect=clone):
            sends, history = self.send(profile, '在呢', synth=voice_tts.synthesize)
        self.assertEqual([part['type'] for part in sends[0]], ['record'])
        self.assertTrue(seen['reference'].endswith(str(Path('12345') / voice_tts.REFERENCE_WAV)))
        self.assertEqual((seen['text'], seen['transcript']), ('在呢', '原话'))
        self.assertEqual(history, ['在呢'])

    def test_synthesis_failure_falls_back_to_text(self):
        def broken(*args):
            raise RuntimeError('语音模型尚未下载')
        sends, history = self.send({'voice_reply': {'mode': 'always'}}, '在呢', synth=broken)
        self.assertEqual(sends[0], [{'type': 'text', 'data': {'text': '在呢'}}])
        self.assertEqual(history, ['在呢'])


FAKE_WORKER = r'''
import json, math, os, sys, tempfile, wave
out = sys.argv[sys.argv.index('--out') + 1]
if os.environ.get('FAKE_CLONE_FAIL'):
    print(json.dumps({'ready': False, 'error': 'no voxcpm'}), flush=True)
    sys.exit(1)
print(json.dumps({'ready': True, 'sample_rate': 48000, 'device': 'cpu'}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    handle, path = tempfile.mkstemp(suffix='.wav', dir=out)
    os.close(handle)
    with wave.open(path, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(48000)
        writer.writeframes(b''.join(int(9000 * math.sin(i / 20)).to_bytes(2, 'little', signed=True)
                                    for i in range(48000)))
    with open(os.path.join(out, 'requests.jsonl'), 'a', encoding='utf-8') as log:
        log.write(json.dumps(request, ensure_ascii=False) + '\n')
    print(json.dumps({'id': request['id'], 'ok': True, 'wav': path, 'sample_rate': 48000}), flush=True)
'''


class CloneTierTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.friend = self.root / 'friends' / '12345'
        (self.friend / 'media').mkdir(parents=True)
        (self.friend / voice_tts.REFERENCE_WAV).write_bytes(b'RIFF')
        worker = self.root / 'fake_worker.py'
        worker.write_text(FAKE_WORKER, encoding='utf-8')
        patcher = patch.object(voice_tts, 'WORKER', worker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = voice_tts.CloneRuntime()
        self.addCleanup(self.runtime.stop)
        voice_tts.save_clone_runtime(sys.executable, self.root)
        self.base = self.root / 'model'
        self.base.mkdir()

    def test_reference_must_stay_inside_the_friend_folder(self):
        value = voice_tts.settings({'voice_reply': {'tier': 'large', 'reference_wav': '../../secret.wav'}})
        with self.assertRaisesRegex(RuntimeError, '参考语音'):
            voice_tts.clone_inputs(value, self.friend)
        value = voice_tts.settings({'voice_reply': {'tier': 'large', 'reference_wav': voice_tts.REFERENCE_WAV,
                                                    'reference_text': '原话'}})
        clone = voice_tts.clone_inputs(value, self.friend)
        self.assertEqual(Path(clone['reference_wav']), (self.friend / voice_tts.REFERENCE_WAV).resolve())
        self.assertEqual(clone['reference_text'], '原话')
        design = voice_tts.clone_inputs(voice_tts.settings({'voice_reply': {'tier': 'large', 'design': '低沉男声'}}),
                                        self.friend)
        self.assertEqual((design['reference_wav'], design['design']), (None, '低沉男声'))

    def test_design_text_is_prefixed_for_voxcpm(self):
        with patch.object(voice_tts, 'model_dir', return_value=self.base), \
                patch.object(voice_tts.CLONE, 'generate', return_value=tone()) as generate:
            voice_tts.synthesize('你好', 'large', clone={'reference_wav': None, 'reference_text': '', 'design': '少女'})
        self.assertEqual(generate.call_args.args[1], '(少女)你好')

    def test_first_request_loads_in_background_then_speaks(self):
        with self.assertRaisesRegex(RuntimeError, '正在加载'):
            self.runtime.generate(self.base, '在呢', None, None, self.root)
        self.runtime.wait_ready(self.base, self.root)
        samples, rate = self.runtime.generate(self.base, '在呢', 'C:/ref.wav', '原话', self.root)
        self.assertEqual(rate, 48000)
        self.assertAlmostEqual(len(samples) / rate, 1.0, places=2)
        request = json.loads((self.root / 'build/voice-clone-runtime/out/requests.jsonl').read_text('utf-8'))
        self.assertEqual((request['text'], request['reference_wav'], request['prompt_text']), ('在呢', 'C:/ref.wav', '原话'))
        self.assertEqual(list((self.root / 'build/voice-clone-runtime/out').glob('*.wav')), [])

    def test_worker_failure_is_reported_and_retried_later(self):
        with patch.dict(os.environ, {'FAKE_CLONE_FAIL': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'no voxcpm'):
                self.runtime.wait_ready(self.base, self.root)
        with self.assertRaisesRegex(RuntimeError, '启动失败'):
            self.runtime.generate(self.base, '在呢', None, None, self.root)
        with self.assertRaisesRegex(RuntimeError, '正在加载'):  # tried again on the next message
            self.runtime.generate(self.base, '在呢', None, None, self.root)
        self.runtime.wait_ready(self.base, self.root)  # and the retry recovers once the worker can start
        self.assertEqual(self.runtime.status(), ('ready', ''))

    def test_missing_runtime_python_is_explained(self):
        voice_tts.save_clone_runtime('', self.root)
        with self.assertRaisesRegex(RuntimeError, '未配置声音克隆运行环境'):
            self.runtime.wait_ready(self.base, self.root)


@unittest.skipUnless(any(model_downloader.is_installed('tts', tier, ROOT) for tier in ('small', 'base')),
                     'no CPU voice model downloaded')
class RealVoiceModelTests(unittest.TestCase):
    def test_installed_tiers_speak_chinese_and_english(self):
        for tier in ('small', 'base'):
            if not model_downloader.is_installed('tts', tier, ROOT):
                continue
            with self.subTest(tier=tier):
                samples, rate = voice_tts.synthesize('你好，今天下午三点开会，记得带 iPhone。', tier, root=ROOT)
                seconds = len(samples) / rate
                self.assertGreater(seconds, 1.0)
                self.assertLess(seconds, 15.0)
                self.assertGreater(float(np.sqrt(np.mean(samples ** 2))), 0.01)
        voice_tts.unload()


if __name__ == '__main__':
    unittest.main()
