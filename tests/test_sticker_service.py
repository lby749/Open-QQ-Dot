import json
from pathlib import Path

from PIL import Image
from store import FriendStore
import sticker_service


def test_snapshot_returns_card_and_data_url(tmp_path):
    friend = tmp_path / 'friends' / '12345'
    friend.mkdir(parents=True)
    (friend / 'friend.json').write_text('{}', 'utf-8')
    key = 'a' * 64
    store = FriendStore(friend)
    store.set('sticker:' + key, json.dumps({'card': {'text': '笑', 'meaning': '笑死', 'emotion': '开心', 'usage': '接梗', 'by': 'user'}}))
    store.close()
    media = friend / 'media'
    media.mkdir()
    Image.new('RGB', (8, 8), 'white').save(media / (key + '.png'))
    result = sticker_service.snapshot(tmp_path, '12345')
    assert result['count'] == 1
    assert result['items'][0]['meaning'] == '笑死'
    assert result['items'][0]['data_url'].startswith('data:image/png;base64,')
