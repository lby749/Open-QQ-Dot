"""Read-only projection of a friend's collected sticker library."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from sticker_library import original_path, stickers


def snapshot(root: Path, qq: str) -> dict:
    qq = str(qq or '').strip()
    directory = root / 'friends' / qq
    if not qq.isdigit() or not (directory / 'friend.json').is_file():
        raise ValueError('好友不存在')
    from store import FriendStore
    store = FriendStore(directory)
    try:
        rows = []
        for key, state in stickers(store):
            card = state.get('card') or {}
            path = None
            try:
                path = original_path(directory, key)
            except Exception:
                pass
            data_url = ''
            if path and path.is_file() and path.stat().st_size <= 3_000_000:
                mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                        'gif': 'image/gif', 'webp': 'image/webp'}.get(path.suffix.lower().lstrip('.'), 'image/png')
                data_url = f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')
            rows.append({
                'key': key,
                'text': str(card.get('text') or ''),
                'meaning': str(card.get('meaning') or ''),
                'emotion': str(card.get('emotion') or ''),
                'usage': str(card.get('usage') or ''),
                'by': str(card.get('by') or ''),
                'is_meme': card.get('is_meme'),
                'file': str(path) if path else '',
                'data_url': data_url,
            })
        return {'qq': qq, 'items': rows, 'count': len(rows)}
    finally:
        store.close()
