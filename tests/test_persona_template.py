from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_template as pt


def message(uin, ms, text=None, kind='text', elements=None, **extra):
    return {'id': str(ms), 'seq': str(ms), 'timestamp': ms, 'time': '', 'type': kind,
            'sender': {'uin': uin, 'name': 'N' + uin}, 'recalled': False, 'system': False,
            'content': {'text': text or '', 'elements': elements if elements is not None else
                        [{'type': 'text', 'data': {'text': text}}]}, **extra}


def export(messages):
    return {'metadata': {'name': 'QQChatExporter'}, 'chatInfo': {'type': 'private', 'selfUin': '1', 'peerUin': '2'},
            'messages': messages}


class PersonaTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(pt, 'TEMPLATES', self.root / 'templates')
        patcher.start()
        self.addCleanup(patcher.stop)
        pt._cache.clear()

    def write(self, messages):
        path = self.root / 'export.json'
        path.write_text(json.dumps(export(messages), ensure_ascii=False), 'utf-8')
        return path

    def test_export_cleaning_reply_media_system_and_recalled(self):
        path = self.write([
            message('2', 1000, 'Hello'),
            message('1', 2000, None, 'reply', [{'type': 'reply', 'data': {'content': 'QUOTED'}},
                                              {'type': 'text', 'data': {'text': '在的'}},
                                              {'type': 'face', 'data': {'name': '/微笑'}}]),
            message('1', 3000, None, 'text', [{'type': 'image', 'data': {}}]),
            message('2', 4000, '撤回了', recalled=True),
            message('0', 5000, '系统', 'system', [{'type': 'system', 'data': {}}], system=True)])
        info, messages = pt.load_export(path)
        self.assertEqual(info['peer_uin'], '2')
        self.assertEqual([m['bubble'] for m in messages], ['Hello', '在的[表情:/微笑]', '[图片]'])
        self.assertEqual([(s['uin'], s['count']) for s in pt.speakers(messages)], [('1', 2), ('2', 1)])
        with self.assertRaises(ValueError):
            pt.load_export(self.root.joinpath('bad.json').write_text('{}') and self.root / 'bad.json')

    def test_pairs_merge_turns_and_split_sessions(self):
        rows = [message('2', 0, 'a'), message('2', 10_000, 'b'), message('1', 20_000, '回1'), message('1', 30_000, '回2'),
                message('2', 3_600_000, '新话题'), message('1', 3_700_000, 'yeahyeah')]
        _, messages = pt.load_export(self.write(rows))
        pairs = pt.build_pairs(messages, '1')
        self.assertEqual([(p['user'], p['target']) for p in pairs], [(['a', 'b'], ['回1', '回2']), (['新话题'], ['yeahyeah'])])
        self.assertEqual(pairs[1]['context'], [])  # an hour of silence starts a new conversation
        self.assertEqual(pairs[0]['delay'], 10)
        style = pt.style_stats(pairs)
        self.assertEqual((style['pairs'], style['bubbles_per_reply']), (2, 1.5))

    def test_program_template_does_not_call_any_model(self):
        rows = []
        for i in range(25):
            rows += [message('2', i * 4000000, 'Hello'), message('1', i * 4000000 + 1000, 'okay!')]
        with patch('house_settings.local_models_enabled', return_value=False), patch.object(pt, 'call_model') as model:
            template = pt.build_template(self.write(rows), '1', True, self.root)
        model.assert_not_called()
        self.assertEqual(template['model'], 'program')
        self.assertEqual(template['traits'], [])
        self.assertEqual(template['style']['pairs'], 25)
        self.assertTrue(template['examples'])

    def test_traits_keep_only_literal_evidence_and_template_lifecycle(self):
        rows = []
        for i in range(30):
            rows += [message('2', i * 4_000_000, f'问题{i}'), message('1', i * 4_000_000 + 1000, f'好呀好呀{i}～')]
        path = self.write(rows)
        traits = {'summary': '温和', 'traits': [{'trait': '爱用波浪号', 'evidence': ['好呀好呀3～']},
                                                {'trait': '编造', 'evidence': ['从没说过的话']}]}
        with patch('house_settings.local_models_enabled', return_value=True), \
                patch.object(pt, 'call_model', return_value=json.dumps(traits, ensure_ascii=False)), \
                patch.object(pt, 'tag_scenes', side_effect=lambda texts, _: ['greeting'] * len(texts)):
            template = pt.build_template(path, '1', True, self.root)
        self.assertEqual(template['traits'], [{'trait': '爱用波浪号', 'evidence': ['好呀好呀3～']}])
        self.assertEqual((template['status'], len(template['examples'])), ('draft', 30))
        self.assertEqual(template['source']['speaker'], '1')
        profile = {'persona_template': template['id']}
        self.assertEqual(pt.prompt_block(profile, self.root, '问题3', 'greeting'), '')  # drafts are never used
        pt.confirm(template['id'])
        block = pt.prompt_block(profile, self.root, '问题3', 'greeting')
        self.assertIn('SPEAK_AS_TA', block)  # the template decides how to talk, the persona who
        self.assertIn('PERSONA_KEEPS: IDENTITY RELATIONSHIP TOPIC_LIMITS SAFETY', block)
        self.assertIn('OUR_EARLIER_MESSAGES_NOT_STYLE_REFERENCE', block)
        # 2026-10-12 user: TA's own chats, no habit list (catchphrases, endings, traits, label percentages)
        for rule in ('CATCHPHRASES', 'SENTENCE_ENDINGS', 'RARELY_USED', 'LABELS', 'NO_COPY', '爱用波浪号'):
            self.assertNotIn(rule, block)
        examples = (block.split('【STYLE_EXAMPLE】')[1:] if '【STYLE_EXAMPLE】' in block else block.split(' [STYLE_EXAMPLE] ')[1:])
        self.assertEqual(len(examples), pt.CORE_SAMPLES + pt.RELEVANT_SAMPLES)  # no vector library -> word overlap
        self.assertTrue(any('对方：问题3\nTA：好呀好呀3～' in example for example in examples))  # the most similar one
        self.assertEqual(block, pt.prompt_block(profile, self.root, '问题3', 'greeting'))  # same core set every turn
        self.assertEqual([t['id'] for t in pt.list_templates()], [template['id']])
        pt.delete(template['id'])
        self.assertEqual(pt.prompt_block(profile, self.root, '问题3', 'greeting'), '')

    def test_signature_names_catchphrases_faces_and_emoji(self):
        replies = [['笑鼠了', '那很酣畅淋漓了（）'], ['wk', '真的假的（）[表情:/羊驼]'], ['笑鼠了', 'wk哈哈哈哈'],
                   ['笑鼠了 ', '坏了😌'], ['wk（）', '不懂😌[表情:/羊驼]']] * 2
        template = {'examples': [{'target': target} for target in replies]}
        phrases, faces, emoji = pt.signature(template)
        self.assertEqual(set(phrases), {'wk', '（）', '笑鼠了'})
        self.assertNotIn('那很', ' '.join(phrases))  # ordinary words are not catchphrases
        self.assertEqual((faces, emoji), (['羊驼'], ['😌']))
        import main
        with patch.object(pt, 'active', return_value=template):
            self.assertTrue(main.emoji_line({}).startswith('EMOJI: UNICODE | [[QQ表情:NAME]] NAME IN [羊驼、微笑'))
            # 2026-10-13 user (😌 ): each frequent mark comes with its meaning and TA's real lines
            self.assertIn('TA_MARKS: 😌=得意|欠揍|装淡定 e.g.「不懂😌[羊驼]」', main.emoji_line({}))
        self.assertEqual(main.SENDABLE_FACES['羊驼'], 185)
        # a matched sample answering with a bare mark or repeating one emoji is not picked (2026-10-13)
        self.assertTrue(pt._mark_heavy({'target': ['👂', '没事我听👂']}))
        self.assertTrue(pt._mark_heavy({'target': ['好', '[表情:/尊嘟假嘟][表情:/尊嘟假嘟]']}))
        self.assertFalse(pt._mark_heavy({'target': ['看一下😌', '那也没有']}))

    def test_package_round_trip_carries_the_vector_library(self):
        template = {'id': 'a1b2c3d4e5f6', 'name': 'test', 'status': 'draft', 'summary': 's', 'traits': [],
                    'style': {}, 'examples': [], 'source': {'speaker': '1'}}
        pt.TEMPLATES.mkdir(parents=True)
        pt.save(template)
        (pt.TEMPLATES / template['id']).mkdir()
        for name in pt.LIBRARY_FILES:
            (pt.TEMPLATES / template['id'] / name).write_text(name, 'utf-8')
        package = self.root / 'p.zip'
        pt.export_package(template['id'], package)
        pt.delete(template['id'])
        self.assertEqual((pt.list_templates(), pt.library_size(template['id'])), ([], 0))
        self.assertEqual(pt.import_package(package)['name'], 'test')
        self.assertEqual((pt.TEMPLATES / template['id'] / 'vectors.sqlite3').read_text('utf-8'), 'vectors.sqlite3')
        bad = self.root / 'bad.zip'
        import zipfile
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('../evil.json', '{}')
        with self.assertRaisesRegex(ValueError, 'Persona Template'):
            pt.import_package(bad)

    def test_public_corpus_is_paused(self):
        friend = self.root / 'paused'
        friend.mkdir()
        (friend / 'public_examples.json').write_text('{"examples":[{"user":["a"],"target":["b"]}]}', 'utf-8')
        self.assertEqual(pt.prompt_block({}, friend, 'a', 'unknown'), '')
        with self.assertRaisesRegex(ValueError, '暂不使用'):
            pt.match_public(friend)

    @patch.object(pt, 'USE_PUBLIC', True)
    def test_public_examples_follow_persona_tags_and_work_without_template(self):
        friend = self.root / 'friend'
        friend.mkdir()
        (friend / 'friend.json').write_text(json.dumps({'style': '温柔体贴的姐姐'}, ensure_ascii=False), 'utf-8')
        annotations, labels = self.root / 'a.sqlite3', self.root / 'l.sqlite3'
        with closing(sqlite3.connect(annotations)) as db:
            db.execute('CREATE TABLE annotations(data_source, conversation_id, speaker_id, message_seq, text, context_json)')
            for conversation, speaker, reply in (('c1', 'A', '慢慢来不着急'), ('c2', 'B', '烦死了'), ('c3', 'C', '别太累啦')):
                db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?)', ('CPED', conversation, speaker, 1, reply,
                           json.dumps([{'speaker': 'X', 'text': '我好累'}], ensure_ascii=False)))
            db.commit()
        with closing(sqlite3.connect(labels)) as db:
            db.execute('CREATE TABLE unit_labels(data_source, conversation_id, speaker_id, lines, tags_json, usable, version, updated_at)')
            db.execute("INSERT INTO unit_labels VALUES('CPED','c1','A',3,'[\"温柔\",\"关心体贴\"]',1,'v','t')")
            db.execute("INSERT INTO unit_labels VALUES('CPED','c2','B',3,'[\"暴躁\"]',1,'v','t')")
            db.execute("INSERT INTO unit_labels VALUES('CPED','c3','C',3,'[\"温柔\",\"关心体贴\"]',1,'v','t')")
            db.execute('CREATE TABLE unit_checks(data_source, conversation_id, speaker_id, knn_tags_json, agree, version, updated_at)')
            db.execute("INSERT INTO unit_checks VALUES('CPED','c1','A','[\"强势\"]',0,'v','t')")  # disputed
            db.execute("INSERT INTO unit_checks VALUES('CPED','c3','C','[\"温柔\"]',1,'v','t')")
            db.commit()
        with patch.object(pt, 'ANNOTATIONS', annotations), patch('public_persona_labels.TARGET', labels), \
                patch.object(pt, 'persona_tags', return_value=['温柔', '关心体贴']):
            value = pt.match_public(friend)
        self.assertEqual((value['units'], value['disputed_units']), (2, 1))
        self.assertEqual(value['examples'], [{'user': ['我好累'], 'target': ['别太累啦']},
                                             {'user': ['我好累'], 'target': ['慢慢来不着急'], 'review': True}])
        block = pt.prompt_block({}, friend, '我好累啊', 'emotion_low')
        self.assertLess(block.index('别太累啦'), block.index('慢慢来不着急'))
        self.assertNotIn('概括：', block)
        (friend / 'friend.json').write_text('{"style": ""}', 'utf-8')
        with self.assertRaises(ValueError):
            pt.match_public(friend)


if __name__ == '__main__':
    unittest.main()
