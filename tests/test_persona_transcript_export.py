from pathlib import Path
import json
import persona_template as pt


def test_load_our_transcript_export(tmp_path):
    document = {
        'schema': 'openqqdot.transcript/1',
        'source': {'platform': 'qq', 'kind': 'friend', 'id': '10001', 'title': '杰森', 'self_id': '10000'},
        'messages': [
            {'id': '1', 'timestamp': 100, 'sender_id': '10001', 'sender_name': '杰森',
             'role': 'user', 'type': 'text', 'text': '在吗', 'media': [], 'reply_to': ''},
            {'id': '2', 'timestamp': 110, 'sender_id': '10000', 'sender_name': '我',
             'role': 'assistant', 'type': 'text', 'text': '在的', 'media': [], 'reply_to': ''},
            {'id': '3', 'timestamp': 120, 'sender_id': '10001', 'sender_name': '杰森',
             'role': 'user', 'type': 'image', 'text': '', 'media': [{'kind': 'image'}], 'reply_to': ''},
        ],
    }
    path = tmp_path / 'chat.json'
    path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    info, messages = pt.load_export(path)
    assert info['self_uin'] == '10000' and info['peer_uin'] == '10001'
    assert [m['bubble'] for m in messages] == ['在吗', '在的', '[图片]']
    assert [s['uin'] for s in pt.speakers(messages)] == ['10001', '10000']
