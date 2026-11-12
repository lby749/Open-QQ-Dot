"""Detect delivery claims and prevent saying "sent" when nothing was sent.

The guard is intentionally narrow: it only acts on media nouns (voice, image,
sticker, file, video) and explicit "sent it to you" style claims. A claim is
allowed only when the same outbound message contains that media, or a recent
tool delivery receipt exists for that kind.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time

KINDS = ('voice', 'image', 'sticker', 'file', 'video')

# Chinese and a few English forms.  Do not match ordinary "发消息"/"发文字".
_PATTERNS = {
    'voice': re.compile(r'(?:语音|录音|声音)(?:已经|刚刚|刚才)?(?:发|传|送)|(?:发|传|送)(?:了|过去|出去)?(?:语音|录音)'),
    'image': re.compile(r'(?:图片|照片|截图|图)(?:已经|刚刚|刚才)?(?:发|传|送)|(?:发|传|送)(?:了|过去|出去)?(?:图片|照片|截图)'),
    'sticker': re.compile(r'(?:表情包|表情)(?:已经|刚刚|刚才)?(?:发|传|送)|(?:发|传|送)(?:了|过去|出去)?(?:表情包|表情)'),
    'file': re.compile(r'(?:文件|附件)(?:已经|刚刚|刚才)?(?:发|传|送)|(?:发|传|送)(?:了|过去|出去)?(?:文件|附件)'),
    'video': re.compile(r'(?:视频|录像)(?:已经|刚刚|刚才)?(?:发|传|送)|(?:发|传|送)(?:了|过去|出去)?(?:视频|录像)'),
}
_GENERIC = re.compile(r'(?:已经|刚刚|刚才)?(?:发|传|送)(?:给你|过去|出去|好啦|了)(?:了)?')
_SAFE = {
    'voice': '语音还没发出去',
    'image': '图片还没发出去',
    'sticker': '表情还没发出去',
    'file': '文件还没发出去',
    'video': '视频还没发出去',
    'generic': '还没发出去',
}


@dataclass(frozen=True)
class ClaimCheck:
    allowed: bool
    claims: tuple[str, ...]
    sanitized: str
    reason: str = ''


def recent_kinds(store, window: float = 300.0) -> set[str]:
    """Kinds with a successful delivery receipt within the window."""
    found: set[str] = set()
    try:
        rows = store.db.execute("SELECT value FROM metadata WHERE key LIKE 'delivery:%'")
        now = time.time()
        for row in rows:
            try:
                value = json.loads(row['value'] or '{}')
            except (TypeError, ValueError):
                continue
            if now - float(value.get('at') or 0) <= window and value.get('kind') in KINDS:
                found.add(str(value['kind']))
    except Exception:
        pass
    return found


def inspect(text: str, actual_kinds=(), recent=()) -> ClaimCheck:
    text = str(text or '')
    available = set(actual_kinds) | set(recent)
    found: list[str] = []
    missing: list[str] = []
    for kind, pattern in _PATTERNS.items():
        if pattern.search(text):
            found.append(kind)
            if kind == 'sticker' and 'image' in available:
                continue
            if kind not in available:
                missing.append(kind)
    if _GENERIC.search(text):
        found.append('generic')
        if not available:
            missing.append('generic')
    if not missing:
        return ClaimCheck(True, tuple(found), text)
    sanitized = text
    for kind, pattern in _PATTERNS.items():
        if kind in missing:
            sanitized = pattern.sub(_SAFE[kind], sanitized)
    if 'generic' in missing:
        sanitized = _GENERIC.sub(_SAFE['generic'], sanitized)
    return ClaimCheck(False, tuple(found), sanitized, 'delivery_claim_without_receipt')
