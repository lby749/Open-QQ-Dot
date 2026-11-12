"""Server-owned administrator ACL, independent of mutable friend profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path


def is_admin_user(user_id: str) -> bool:
    user_id = str(user_id or '').strip()
    if not user_id:
        return False
    env = os.environ.get('QQ_ADMINS', '') or os.environ.get('ADMIN_QQ', '')
    if user_id in {value.strip() for value in env.split(',') if value.strip()}:
        return True
    root = Path(os.environ.get('QQ_BRIDGE_HOME') or Path(__file__).resolve().parent)
    try:
        data = json.loads((root / 'house_web.local.json').read_text(encoding='utf-8'))
        return user_id in set(map(str, data.get('admin_qqs', [])))
    except (OSError, ValueError, TypeError):
        return False
