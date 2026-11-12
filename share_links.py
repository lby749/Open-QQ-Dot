"""Extract and preview Douyin / Bilibili shares from QQ message segments.

QQ often delivers a share as a `json` or `xml` segment, not as plain text.
This module keeps only links from the two supported platforms, resolves short
links, and reads OpenGraph metadata. It never follows a redirect to another
host and never sends cookies or credentials.
"""
from __future__ import annotations

from html import unescape
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

PLATFORMS = {
    'bilibili': ('bilibili.com', 'b23.tv'),
    'douyin': ('douyin.com', 'iesdouyin.com'),
}
URL_RE = re.compile(r'https?://[^\s<>"\'\\)\]]+', re.I)
MAX_BYTES = 2_000_000
TIMEOUT = 8


def platform(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''
    for name, hosts in PLATFORMS.items():
        if any(host == item or host.endswith('.' + item) for item in hosts):
            return name
    return ''


def _json_strings(value, found, depth=0):
    if depth > 8:
        return
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('{') or text.startswith('['):
            try:
                _json_strings(json.loads(text), found, depth + 1)
                return
            except (ValueError, TypeError):
                pass
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _json_strings(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_strings(item, found, depth + 1)


def _find_urls(texts):
    seen = set()
    urls = []
    for text in texts:
        for match in URL_RE.findall(text or ''):
            url = unescape(match).rstrip('.,;，。；')
            if platform(url) and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _metadata(texts):
    titles = []
    descs = []
    covers = []
    for text in texts:
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text) if text.strip().startswith(('{', '[')) else None
        except (ValueError, TypeError):
            value = None
        if isinstance(value, dict):
            meta = value.get('meta') or {}
            if isinstance(meta, dict):
                for item in meta.values():
                    if not isinstance(item, dict):
                        continue
                    for key, target in (('title', titles), ('desc', descs),
                                        ('preview', covers), ('image', covers)):
                        text_value = item.get(key)
                        if isinstance(text_value, str) and text_value.strip():
                            target.append(text_value.strip())
        for match in re.findall(r'<title[^>]*>(.*?)</title>', text or '', re.I | re.S):
            titles.append(unescape(re.sub(r'<[^>]+>', '', match)).strip())
        for attr in ('title', 'desc', 'preview', 'image'):
            for match in re.findall(attr + r'\s*=\s*["\']([^"\']+)["\']', text or '', re.I):
                (titles if attr == 'title' else descs if attr == 'desc' else covers).append(unescape(match))
    return (titles[0] if titles else '', descs[0] if descs else '', covers[0] if covers else '')


def extract_from_event(event):
    """List of share dicts from NapCat private event segments."""
    texts = []
    value = event.get('message') if isinstance(event, dict) else None
    if isinstance(value, list):
        for part in value:
            if not isinstance(part, dict):
                continue
            kind = str(part.get('type') or '')
            data = part.get('data') or {}
            if kind in ('json', 'xml'):
                _json_strings(data.get('data', data), texts)
            elif kind == 'text':
                texts.append(str(data.get('text') or ''))
    elif isinstance(value, str):
        texts.append(value)
    urls = _find_urls(texts)
    title, desc, cover = _metadata(texts)
    return [{'platform': platform(url), 'url': url, 'title': title, 'desc': desc,
             'cover': cover} for url in urls]


def text_for(shares):
    lines = []
    for share in shares:
        name = 'B站' if share['platform'] == 'bilibili' else '抖音'
        title = share.get('title') or ''
        lines.append(f'[{name}分享] {title}\n{share["url"]}'.strip())
    return '\n\n'.join(lines)


def _safe_fetch(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not platform(url):
        raise ValueError('不支持的分享链接')
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 Open-QQ-Dot'})
    opener = build_opener()
    with opener.open(request, timeout=TIMEOUT) as response:
        final = response.geturl()
        if not platform(final):
            raise ValueError('分享链接跳转到了非 B站/抖音 域名')
        raw = response.read(MAX_BYTES + 1)
    return raw[:MAX_BYTES].decode('utf-8', errors='replace'), final


def _search_fallback(url, platform_name):
    """Use the configured web search when the page itself blocks metadata."""
    try:
        from group_bot import load_config
        config = (load_config().get('search') or {})
        if (config.get('provider') or 'off') == 'off':
            return {}
        import web_search
        results = web_search.search(f'{platform_name} {url}', config)
        first = next((item for item in results if item.get('url')), {})
        return {'title': str(first.get('title') or ''), 'desc': str(first.get('description') or '')}
    except Exception:
        return {}


def fetch_preview(url, *, search_fallback=True):
    """Best-effort title/description/cover; deletes nothing and never bypasses login."""
    html, final = _safe_fetch(url)
    titles = re.findall(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    descs = re.findall(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', html, re.I)
    covers = re.findall(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not titles:
        titles = re.findall(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = unescape(re.sub(r'<[^>]+>', '', titles[0])).strip() if titles else ''
    desc = unescape(descs[0]).strip() if descs else ''
    cover = unescape(covers[0]).strip() if covers else ''
    if platform(final) == 'bilibili' and not title:
        # Bilibili's SPA has no useful OG tags; its public view API returns title/desc/cover.
        found = re.search(r'(BV[0-9A-Za-z]{10})', final)
        if found:
            try:
                request = Request('https://api.bilibili.com/x/web-interface/view?bvid=' + found.group(1),
                                  headers={'User-Agent': 'Open-QQ-Dot/1.0'})
                with build_opener().open(request, timeout=TIMEOUT) as response:
                    payload = json.loads(response.read(1_000_000).decode('utf-8'))
                data = payload.get('data') or {}
                title = str(data.get('title') or title).strip()
                desc = str(data.get('desc') or desc).strip()
                cover = str(data.get('pic') or cover).strip()
            except Exception:
                pass
    if search_fallback and not title:
        found = _search_fallback(final, platform(final))
        title = title or str(found.get('title') or '').strip()
        desc = desc or str(found.get('desc') or '').strip()
    return {'url': final, 'platform': platform(final),
            'title': title, 'desc': desc, 'cover': cover}
