"""Application-wide release recommendations; never downloads or executes an update."""
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import house_settings
from version import VERSION

SOURCE = Path(__file__).resolve().with_name('release_source.json')
CHECK_INTERVAL = 24 * 60 * 60


def repository():
    try:
        value = json.loads(SOURCE.read_text('utf-8')).get('repository', '')
    except (OSError, ValueError, AttributeError):
        return ''
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}', value):
        return ''
    return value if value.split('/')[1] not in ('.', '..') else ''


def version_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc)[.-]?(\d+))?(?:\+[A-Za-z0-9.-]+)?', str(value))
    if not match:
        raise ValueError('无法识别发行版本号')
    major, minor, patch, stage, serial = match.groups()
    return (int(major), int(minor), int(patch), {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(serial or 0))


def choose_release(releases, repo, current=VERSION):
    current_key = version_key(current)
    choices = []
    for item in releases:
        if not isinstance(item, dict) or item.get('draft'):
            continue
        tag = item.get('tag_name', '')
        try:
            key = version_key(tag)
        except ValueError:
            continue
        if key <= current_key or (current_key[3] == 3 and (item.get('prerelease') or key[3] < 3)):
            continue
        choices.append((key, {'version': str(tag).removeprefix('v'), 'tag': str(tag),
                              'notes': str(item.get('body') or '此版本未提供更新说明。')[:6000],
                              'url': f'https://github.com/{repo}/releases/tag/{quote(str(tag), safe="")}' }))
    return max(choices, key=lambda pair: pair[0])[1] if choices else None


def state(home):
    try:
        value = json.loads((Path(home) / 'data/app_update_state.json').read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(home, value):
    path = Path(home) / 'data/app_update_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
        house_settings.replace_file(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(home, force=False, current=VERSION):
    if not force and not house_settings.load(home).get('app_auto_check_updates', True):
        return {'status': 'disabled'}
    repo = repository()
    if not repo:
        return {'status': 'unconfigured'}
    previous = state(home)
    now = time.time()
    try:
        age = now - float(previous.get('checked_at', 0))
    except (TypeError, ValueError):
        age = CHECK_INTERVAL
    if (not force and previous.get('repository') == repo and previous.get('current') == current
            and 0 <= age < CHECK_INTERVAL):
        if previous.get('status') == 'available':
            cached = previous.get('release')
            release = choose_release([{'tag_name': cached.get('tag', ''), 'body': cached.get('notes', '')}]
                                     if isinstance(cached, dict) else [], repo, current)
            if release:
                return dict(previous, release=release, cached=True)
        elif previous.get('status') in ('latest', 'offline'):
            return dict(previous, cached=True)
    result = {'repository': repo, 'current': current, 'checked_at': now,
              'notified_version': previous.get('notified_version', '')}
    request = Request(f'https://api.github.com/repos/{repo}/releases?per_page=30',
                      headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'OpenQQDot/{current}'})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError('发行信息过大')
        releases = json.loads(raw)
        if not isinstance(releases, list):
            raise ValueError('发行信息格式错误')
        release = choose_release(releases, repo, current)
        result.update(status='available' if release else 'latest', release=release)
    except (OSError, ValueError, TypeError):
        result.update(status='offline', release=None)
    _save(home, result)
    return result


def mark_notified(home, version):
    value = state(home)
    if (value.get('release') or {}).get('version') == version:
        value['notified_version'] = version
        _save(home, value)
