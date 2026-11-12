"""The friend house on the phone (2026-10-11 user): a small web page served by the desktop panel's process.

Models, data and the bridge stay on the computer. Default listener: loopback only.
Explicit allow_remote=True or QQ_ALLOW_REMOTE=1 enables all IPv4 interfaces
(0.0.0.0), including LAN/VPN. Every /api call requires the friend's bearer token;
forwarding does not grant administrator access. Remote mode must be chosen locally.

Tailscale Funnel is denied by default even when it reaches loopback. The desktop
pairing card explicitly enables it after verifying the matching forwarding rule;
QQ_ALLOW_FUNNEL can also override the local setting.

It runs in the panel's process, not the bridge's: the proactive preview closes the app-server it uses, which must
not be the bridge's own (house_status.preview_proactive)."""
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import house_status
import network
import phone_client
import phone_relay
from house_status import (SWITCHES, care_slot_text, care_slots_from_text, followup_values, friend_mode_values,
                          invoke_bridge, load_profile, page_settings, save_ignored_mood, save_page_settings, set_switch,
                          write_profile)
from store import FriendStore

PORT = 8790
PAGE = Path(__file__).resolve().parent / 'house_web.html'
MAX_BODY = 256 * 1024
FORWARDED = ('X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'Forwarded', 'Tailscale-Funnel-Request')
FUNNEL_HEADER = 'Tailscale-Funnel-Request'


def funnel_request(headers) -> bool:
    return any(str(name).lower() == FUNNEL_HEADER.lower() for name in headers)


def funnel_allowed() -> bool:
    import os
    if 'QQ_ALLOW_FUNNEL' in os.environ:
        return os.environ['QQ_ALLOW_FUNNEL'] in ('1', 'true', 'True')
    import house_settings
    return house_settings.load().get('tailscale_funnel_enabled') is True
_bridge = {'at': 0.0, 'text': ''}
_preview = threading.Lock()
_token_lock = threading.Lock()


def _token_document() -> dict:
    path = house_status.ROOT / 'house_web.local.json'
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _tokens():
    document = _token_document()
    protected = document.get('friend_tokens_protected')
    if isinstance(protected, str) and protected:
        try:
            import base64
            from api_backend import _protect
            raw = _protect(base64.b64decode(protected), decrypt=True)
            value = json.loads(raw.decode('utf-8'))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    value = document.get('friend_tokens', {})
    return value if isinstance(value, dict) else {}


def _save_tokens(tokens: dict):
    path = house_status.ROOT / 'house_web.local.json'
    document = _token_document()
    document.pop('friend_tokens', None)
    if os.name == 'nt':
        import base64
        from api_backend import _protect
        payload = json.dumps(tokens, ensure_ascii=False).encode('utf-8')
        document['friend_tokens_protected'] = base64.b64encode(_protect(payload)).decode('ascii')
    else:
        # Non-Windows test/development fallback; production Windows uses DPAPI.
        document['friend_tokens'] = tokens
    path.write_text(json.dumps(document, ensure_ascii=False), 'utf-8')
    try:
        path.chmod(0o600)
    except OSError:
        pass


def friend_token(qq):
    _directory(qq)
    with _token_lock:
        tokens = _tokens()
        if qq not in tokens:
            tokens[qq] = secrets.token_urlsafe(24)
            _save_tokens(tokens)
        return tokens[qq]


def rotate_friend_token(qq):
    """Invalidate the old QR/link and issue a new per-friend token."""
    _directory(qq)
    with _token_lock:
        tokens = _tokens()
        tokens[qq] = secrets.token_urlsafe(24)
        _save_tokens(tokens)
        return tokens[qq]


def friend_for_token(given):
    if not given:
        return None
    with _token_lock:
        tokens = _tokens()
    friends = _friends()
    return next((qq for qq, secret in tokens.items() if qq in friends
                 and hmac.compare_digest(given.encode(), secret.encode())), None)


def is_admin_user(qq):
    """Server-authenticated administrators only; friend.json is never an ACL."""
    from trusted_admin_policy import is_admin_user as _is_admin
    return _is_admin(qq)


def tailnet_address():
    """This machine's Tailscale address (the name is kept so the panel's phone card needs no change)."""
    return network.address()


def bridge_status(fresh=False):
    """invoke_bridge('status') starts PowerShell (~1 s); the phone polls, so the answer is kept 5 s."""
    if fresh or time.monotonic() - _bridge['at'] > 5:
        code, text = invoke_bridge('status')
        _bridge.update(at=time.monotonic(), text=text if not code else '状态读取失败：' + text)
    return _bridge['text']


def _friends():
    try:
        if house_status.REGISTRY.exists():
            return json.loads(house_status.REGISTRY.read_text('utf-8')).get('friends', {})
    except Exception:
        pass
    return {}


def _directory(qq):
    record = _friends().get(qq)
    if not record or 'directory' not in record:
        dir_path = house_status.ROOT / 'friends' / str(qq)
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path
    return Path(record['directory'])


def _switches(profile):
    return [{'key': key, 'title': title, 'note': note, 'on': bool(read(profile))}
            for key, title, note, read, _ in SWITCHES]


# ---------- API ----------

def friend(qq, _query):
    directory = _directory(qq)
    values = friend_mode_values(qq, directory, bridge_status().casefold().startswith('running'))
    log = directory / 'activity.log'
    recent = log.read_text(encoding='utf-8', errors='replace').splitlines()[-30:] if log.exists() else []
    profile = load_profile(directory / 'friend.json')
    return {**values, 'name': _friends()[qq].get('display_name', qq), 'switches': _switches(profile),
            'phone': status_phone(profile, directory), 'log': list(reversed(recent))}


def history(qq, query):
    import hashlib
    limit = min(int(query.get('limit', ['100'])[0]), 1000)
    directory = _directory(qq)
    store = FriendStore(directory)
    try:
        items = []
        for row in store.recent_messages(limit):
            voice_name = hashlib.sha256((row['body'] or '').encode('utf-8')).hexdigest() + '.wav'
            voice = (directory / 'media' / 'voice' / voice_name).is_file()
            items.append({'direction': row['direction'], 'identity': row['identity'],
                          'body': row['body'], 'created_at': row['created_at'],
                          'voice': voice, 'voice_name': voice_name if voice else ''})
        return {'messages': items, 'count': store.message_count()}
    finally:
        store.close()


def voice_audio(qq, query):
    """Serve a cached TTS WAV to the phone; the token still protects the API."""
    import re
    name = str((query.get('name') or [''])[0])
    if not re.fullmatch(r'[0-9a-f]{64}\.wav', name):
        raise ValueError('语音文件名无效')
    path = _directory(qq) / 'media' / 'voice' / name
    if not path.is_file():
        raise LookupError('语音文件不存在')
    return path.read_bytes()


def proactive(qq, _query):
    _directory(qq)
    return {'text': house_status.proactive_debug_text(qq, bridge_status().casefold().startswith('running'))}


def memory(qq, _query):
    store = FriendStore(_directory(qq))
    try:
        return {'text': store.memory(), 'revision': store.memory_revision()}
    finally:
        store.close()


def usage(qq, _query):
    from usage_accounting import display, summary
    return {'text': display(summary(_directory(qq)))}


def post_switch(qq, body):
    if body.get('key') not in {item[0] for item in SWITCHES} or type(body.get('on')) is not bool:
        raise ValueError('开关无效')
    title = set_switch(_directory(qq) / 'friend.json', body['key'], body['on'])
    return {'message': f"{title}已{'开启' if body['on'] else '关闭'}。"}


def post_toggle(qq, _body):
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    profile['bridge_enabled'] = not profile.get('bridge_enabled', True)
    write_profile(path, profile)
    return {'message': '这位好友已启用。' if profile['bridge_enabled'] else '这位好友已暂停。'}


def post_retry(qq, body):
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    if (not bridge_status(fresh=True).casefold().startswith('running') or not profile.get('bridge_enabled', True)
            or not profile.get('auto_reply', True)):
        raise ValueError('请先启动桥接，并开启当前好友和自动回复。')
    store = FriendStore(directory)
    try:
        item = next((row for row in store.reply_problems() if row['message_id'] == body.get('message_id')), None)
        if not item or item['state'] != 'failed':
            raise ValueError('只能重试明确失败的回复；发送中或结果未确认的消息不能重发。')
        store.request_reply_retry(item['message_id'])
    finally:
        store.close()
    return {'message': '已提交重试。'}


def post_memory(qq, body):
    content = str(body.get('text', '')).strip()
    if not content:
        raise ValueError('清空长期记忆请在电脑上使用重置按钮。')
    store = FriendStore(_directory(qq))
    try:
        if store.memory_revision() != body.get('revision'):
            raise ValueError('长期记忆已被后台更新，请重新读取后再修改。')
        store.set_memory(content, expected_revision=body['revision'])
        return {'message': '长期记忆已保存；下次生成消息时生效。', 'revision': store.memory_revision()}
    finally:
        store.close()


def post_preview(qq, _body):
    _directory(qq)
    if not _preview.acquire(blocking=False):
        raise ValueError('已有主动聊天预览正在生成。')
    try:
        return {'messages': house_status.preview_proactive(qq)}
    finally:
        _preview.release()


# ---------- Settings (2026-10-11 user): the phone edits Friend Info / Speaking Style / proactivedays /  / Topic Knowledge Library ----------

MAX_FIELD = 2000  # one line of profile text
MAX_TEXT = 8000  # the persona block


def _text(body, key, limit=MAX_FIELD):
    """One submitted text field, length-checked so the phone cannot make friend.json unreadable."""
    value = str(body.get(key) or '').strip()
    if len(value) > limit:
        raise ValueError(f'「{key}」最多 {limit} 个字，请缩短后再保存。')
    return value


def settings(qq, _query):
    """Everything the settings tab edits, plus what it needs to show ( names, Topic Knowledge Library runs)."""
    import topic_research
    from persona_template import list_templates
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    info = profile.get('basic_info', {})
    templates = [{'id': '', 'name': '不使用模板'}]
    for item in list_templates():
        if item['id'] != profile.get('persona_template'):
            continue
        templates.append({'id': item['id'], 'name': f"{item['name']} · {item['source']['speaker']}"
                          + ('' if item['status'] == 'active' else '（未启用）')})
    return {'qq': qq, 'name': _friends()[qq].get('display_name', qq),
            'basic_info': {key: info.get(key, '') for key in ('nickname', 'age', 'birthday', 'interests',
                                                              'deep_interests', 'notes')},
            'style': profile.get('style', ''),
            **{key: profile.get(key, '') for key in ('emotion_policy', 'timing_policy', 'memory_policy')},
            'proactive_style': profile.get('proactive', {}).get('style', ''),
            'persona_template': profile.get('persona_template', ''),
            'templates': templates, 'proactive': page_settings(profile),
            'research_hours': {kind: profile.get('research_refresh_hours', {}).get(kind, hours)
                               for kind, hours in topic_research.KINDS.items()},
            'research_state': topic_research.state(directory),
            'phone': status_phone(profile, directory)}


def status_phone(profile, directory):
    """What the phone page and the panel show about the paired phone app (2026-10-11 user); 2026-10-12 user: whether
    it is actually connected now, not only paired."""
    import phone_client
    connected = phone_client.configured(profile)
    online = phone_client.online(profile, directory)
    seen = phone_client.last_seen(directory)
    if not connected:
        note = '还没连上：用手机相机或手机小屋里扫电脑的配对二维码。'
    elif online:
        note = '手机在线；模型可按需查询。'
    elif profile.get('phone_transport') != 'relay':
        note = '手机已配对（旧连接方式，无法显示是否在线）；保持手机小屋后台运行。'
    elif seen is None:
        note = '已配对，但手机还没连上来过：检查手机小屋是否在后台运行、网络/代理是否可达。'
    else:
        minutes = int((time.time() - seen) // 60)
        note = (f'手机离线：最后连接 {time.strftime("%m-%d %H:%M", time.localtime(seen))}（{minutes} 分钟前）。'
                '检查手机小屋是否在后台运行、网络/代理是否可达；离线时模型不会去查手机。')
    return {'connected': connected, 'online': online, 'mcp_url': str(profile.get('phone_mcp_url') or ''),
            'note': note}


def post_basic(qq, body):
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    info = {key: _text(body, key) for key in ('nickname', 'age', 'birthday', 'interests', 'deep_interests', 'notes')}
    previous = profile.get('basic_info', {})
    profile['basic_info'] = info
    write_profile(path, profile)
    interests = [info['interests'], info['deep_interests']]
    return {'message': '好友资料已保存。' + ('兴趣改了：想马上按新兴趣联网整理，到本页点「立即整理」。'
                                            if [previous.get('interests', ''), previous.get('deep_interests', '')] != interests
                                            else '')}


def post_style(qq, body):
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    template = _text(body, 'persona_template')
    if template:
        if template != profile.get('persona_template'):
            raise ValueError('更换人格模板请在电脑上设置；手机只能保留当前模板或不使用。')
        profile['persona_template'] = template
    else:
        profile.pop('persona_template', None)
    profile['style'] = _text(body, 'style', MAX_TEXT)
    profile.setdefault('proactive', {})['style'] = _text(body, 'proactive_style')
    for key in ('emotion_policy', 'timing_policy', 'memory_policy'):
        if key in body:  # Older phone pages must not clear policies they do not edit.
            value = _text(body, key, MAX_TEXT)
            if value:
                profile[key] = value
            else:
                profile.pop(key, None)
    write_profile(path, profile)
    return {'message': '说话方式已保存；下次生成消息时生效。'}


def voice_settings(qq, _query):
    import voice_tts
    from model_downloader import MODEL_CATALOG, is_installed
    value = voice_tts.settings(load_profile(_directory(qq) / 'friend.json'))
    reference_set = bool(value.pop('reference_wav'))
    return {'settings': value, 'reference_set': reference_set,
            'models': [{'id': tier, 'name': cfg['name'], 'speakers': cfg['speakers'],
                        'installed': is_installed('tts', tier)}
                       for tier, cfg in MODEL_CATALOG['tts'].items()],
            'input': voice_input_state(load_profile(_directory(qq) / 'friend.json'))}


def collaboration(qq, _query):
    """This friend's agents and their collaborations (collaboration_view.py), read-only."""
    import collaboration_view
    from house_settings import data_dir
    return collaboration_view.snapshot(data_dir(), owner=str(qq))


def voice_input_state(profile):
    """How the friend's voice messages are heard (voice_input.py): the switch and what each way would use."""
    import voice_input
    from model_downloader import MODEL_CATALOG, is_installed
    value = voice_input.settings(profile)
    agent, detail = voice_input.agent_choice(profile, value)
    return {'settings': value, 'local_ready': voice_input.local_ready(value),
            'agent': agent, 'agent_detail': detail,
            'models': [{'id': tier, 'name': cfg['name'], 'installed': is_installed('asr', tier)}
                       for tier, cfg in MODEL_CATALOG['asr'].items()]}


def post_voice_input(qq, body):
    import voice_input
    from model_downloader import MODEL_CATALOG
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    value = voice_input.settings(profile)
    checks = {'mode': voice_input.MODES, 'tier': tuple(MODEL_CATALOG['asr']), 'agent': voice_input.AGENTS}
    for key, allowed in checks.items():
        if key in body:
            if body[key] not in allowed:
                raise ValueError('语音识别设置无效')
            value[key] = body[key]
    if 'agent_model' in body:
        value['agent_model'] = _text(body, 'agent_model', 120)
    profile['voice_input'] = voice_input.settings({'voice_input': value})
    write_profile(path, profile)
    names = {'local': '本地模型识别', 'agent': 'AI 模型直接听', 'off': '关闭'}
    return {'message': f'收到语音时：{names[profile["voice_input"]["mode"]]}（下一条语音起生效）',
            'input': voice_input_state(profile)}


def post_voice_settings(qq, body):
    import math
    import voice_tts
    from model_downloader import MODEL_CATALOG
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    value = voice_tts.settings(profile)
    if body.get('mode', value['mode']) not in voice_tts.MODES:
        raise ValueError('语音发送方式无效')
    if body.get('tier', value['tier']) not in MODEL_CATALOG['tts']:
        raise ValueError('语音模型无效')
    for key, low, high in (('speed', 0.5, 2), ('max_chars', 5, 300), ('ratio', 0, 1),
                           ('speaker', 0, MODEL_CATALOG['tts'][body.get('tier', value['tier'])]['speakers'] - 1)):
        if key in body:
            number = float(body[key])
            if not math.isfinite(number) or not low <= number <= high or (
                    key in ('max_chars', 'speaker') and not number.is_integer()):
                raise ValueError('语音参数超出允许范围')
            value[key] = number
    for key in ('mode', 'tier'):
        if key in body:
            value[key] = body[key]
    for key in ('reference_text', 'design'):
        if key in body:
            value[key] = _text(body, key, 500)
    if isinstance(body.get('atmosphere'), dict):
        import voice_acoustics
        atmosphere = dict(value.get('atmosphere') or {})
        atmosphere.update({k: v for k, v in body['atmosphere'].items()
                           if k in ('preset', 'noise_db', 'reverb_wet', 'reverb_rt60', 'breath', 'jitter')})
        if atmosphere.get('preset') not in voice_acoustics.PRESETS:
            raise ValueError('声学氛围预设无效')
        value['atmosphere'] = voice_acoustics.normalize(atmosphere)
    # Reference files and the shared interpreter remain managed locally on desktop.
    profile['voice_reply'] = voice_tts.settings({'voice_reply': value})
    write_profile(path, profile)
    return {'message': '语音回复设置已保存；下一条符合条件的回复生效。'}


def post_page_settings(qq, body):
    """The whole proactivedays page in one save, exactly like the desktop panel'sSaveSettings."""
    values = {key: body.get(key) for key in ('rounds_min', 'rounds_max', 'bubbles_min', 'bubbles_max',
                                             'window_start', 'window_end', 'topic_types', 'reply_pause_minutes',
                                             'unanswered_round_limit', 'unanswered_round_pause_minutes',
                                             'continuation_max', 'agenda_min_gap_minutes')}
    care = body.get('care')
    if body.get('care_text') is not None:  # the phone's text form: one line per slot
        care = care_slots_from_text(body['care_text'])['slots']
    saved = save_page_settings(_directory(qq) / 'friend.json', {**values, 'care': care})
    return {'message': '已保存；可点「按已保存设置重新判断时机」更新今天剩余轮次。' if saved['changed'] else
            '主动聊天和时段关心设置已保存；暂停时间按最近一次发出回复的时间重新计算。'}


def post_research(qq, body):
    """Immediate topic research (the bridge's background thread does the web search)."""
    import topic_research
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    if not profile.get('daily_research_enabled', True) or not profile.get('bridge_enabled', True):
        raise ValueError('当前好友或话题资料整理已暂停，请先开启。')
    kind = body.get('kind')
    if kind not in topic_research.KINDS:
        raise ValueError('整理类型无效')
    hours = body.get('hours')
    if hours is not None:
        value = str(hours).strip()
        if not value.isdigit():
            raise ValueError('请输入整数小时；0 表示只手动整理。')
        profile.setdefault('research_refresh_hours', {})[kind] = int(value)
        topic_research.refresh_hours(profile, kind)
        write_profile(directory / 'friend.json', profile)
    topic_research.request_refresh(directory, kind)
    return {'message': ('普通' if kind == 'normal' else '深度') + '话题已请求整理；保持后台桥接运行，结果会自动显示。'}


# ----------  (2026-10-12 user: the phone has every page the desktop panel has) ----------
# Same rules as the panel pages (house_qt). Kept on the computer: the API key, the local-9B fallback (it changes
# other friends), building / importing templates, and the Codex card writer (a long call).

def topics(qq, _query):
    """Topic Knowledge Library: the live items, newest first (the panel's table)."""
    import topic_research
    keep = ('id', 'topic', 'title', 'published', 'fetched_at', 'expires_at', 'summary', 'url')
    return {'items': [{key: row.get(key) for key in keep} for row in topic_research.items(_directory(qq))]}


def post_topic_remove(qq, body):
    import topic_research
    directory = _directory(qq)
    if body.get('id') not in {row['id'] for row in topic_research.items(directory)}:
        raise ValueError('没有这条资料')
    topic_research.remove(directory, body['id'])
    return {'message': '资料已删除。'}


def _sticker_keys(store):
    from sticker_library import stickers as collection
    return collection(store)


def stickers(qq, _query):
    from sticker_library import CARD_FIELDS
    store = FriendStore(_directory(qq))
    try:
        items = _sticker_keys(store)
    finally:
        store.close()
    return {'fields': CARD_FIELDS, 'items': [{'key': key, 'card': state.get('card') or {},
                                              'qq_saved': bool(state.get('qq_saved'))} for key, state in items]}


def sticker_image(qq, query):
    """One sticker's preview picture (bytes); only keys of this friend's collection."""
    directory = _directory(qq)
    key = query.get('key', [''])[0]
    store = FriendStore(directory)
    try:
        known = {item for item, _ in _sticker_keys(store)}
    finally:
        store.close()
    path = directory / 'media' / (key + '.preview.png')
    if key not in known or path.resolve().parent != (directory / 'media').resolve() or not path.is_file():
        raise LookupError('预览缺失')
    return path.read_bytes()


def post_sticker(qq, body):
    from sticker_library import CARD_FIELDS, save_card
    store = FriendStore(_directory(qq))
    try:
        if body.get('key') not in {item for item, _ in _sticker_keys(store)}:
            raise ValueError('没有这张表情')
        save_card(store, body['key'], {name: _text(body, name) for name in CARD_FIELDS}, 'user')
    finally:
        store.close()
    return {'message': '含义卡已保存，以后不会被自动覆盖。'}


def post_sticker_remove(qq, body):
    from sticker_library import remove
    directory = _directory(qq)
    store = FriendStore(directory)
    client = None
    try:
        if body.get('key') not in {item for item, _ in _sticker_keys(store)}:
            raise ValueError('没有这张表情')
        if body.get('qq') is True:
            from qq_link import QQClient
            client = QQClient()
        remove(store, directory, body['key'], client)
    finally:
        store.close()
        if client is not None:
            client.close()
    return {'message': 'Delete' + ('（QQ 收藏里也删了）' if client is not None else '') + '. '}


def template(qq, _query):
    """Only the template this friend uses: the others hold other people's chats (see settings())."""
    from persona_template import library_size, list_templates, load
    profile = load_profile(_directory(qq) / 'friend.json')
    chosen = next((item for item in list_templates() if item['id'] == profile.get('persona_template')), None)
    if chosen is None:
        return {'template': None}
    full = load(chosen['id'])
    return {'template': {'name': chosen['name'], 'speaker': chosen['source'].get('speaker', ''),
                         'status': chosen['status'], 'size': library_size(chosen['id']), 'model': full.get('model', ''),
                         'summary': full.get('summary', ''), 'traits': [item.get('trait', '') for item in full.get('traits', [])],
                         'style': full.get('style', {}), 'examples': len(full.get('examples', []))}}


def model(qq, _query):
    from api_backend import DEFAULT_BASE_URL, EFFORTS, MODELS, PROTOCOLS, key_path
    from api_profiles import load_connections
    from claude_backend import EFFORTS as CLAUDE_EFFORTS, MODELS as CLAUDE_MODELS
    from antigravity_backend import EFFORTS as AG_EFFORTS, MODELS as AG_MODELS
    from codex_settings import COMPACT_TOKENS, CONTEXT_TOKENS, model_options
    import provider_context
    import dsh_backend
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    api = profile.get('model_api') or {}
    named_connections, active_api_id = load_connections(profile)
    dsh_cfg = (api.get('dsh_settings') or {})
    # This is a settings snapshot, not account discovery: an unrelated CLI must
    # not block opening the page or spawn a process on every refresh.
    dsh_model = dsh_cfg.get('model') or (api.get('model') if api.get('provider') == 'dsh' else '') or dsh_backend.DEFAULT_MODELS[0]
    dsh_models = list(dict.fromkeys([dsh_model, *dsh_backend.DEFAULT_MODELS]))
    return {'provider': ('dsh' if api.get('provider') in ('dsh', 'deepseek_harness') else
                         'custom' if api.get('provider') in ('custom', 'deepseek') else
                         'antigravity' if api.get('provider') == 'antigravity' else
                         'claude' if api.get('provider') == 'claude' else 'codex'),
            'dsh_model': dsh_model,
            'dsh_effort': dsh_cfg.get('effort') or (api.get('api_effort') if api.get('provider') == 'dsh' else '') or 'low',
            'antigravity_model': api.get('antigravity_model') or AG_MODELS[0],
            'antigravity_effort': api.get('antigravity_effort') or 'low',
            'codex_model': api.get('codex_model', 'gpt-6-luna'), 'codex_effort': api.get('codex_effort', 'low'),
            'codex_fast': api.get('codex_fast', False),
            'claude_model': api.get('claude_model') or CLAUDE_MODELS[0], 'claude_effort': api.get('claude_effort') or 'low',
            'api_protocol': api.get('api_protocol') or PROTOCOLS[0], 'api_base_url': api.get('api_base_url', ''),
            'model': api.get('model', ''), 'api_effort': api.get('api_effort') or 'low',
            'compact_tokens': api.get('compact_tokens') or provider_context.info(api).window,
            'recent_chat_char_budget': api.get('recent_chat_char_budget') or '',
            'key_saved': key_path(directory, active_api_id).exists(),
            'active_api_key_id': active_api_id,
            'api_profiles': [{**value, 'key_saved': key_path(directory, identifier).exists()}
                             for identifier, value in named_connections.items()],
            'research_effort': profile.get('research_effort', 'low'), 'research_fast': profile.get('research_fast', True),
            'options': {'codex': model_options(), 'antigravity_models': list(AG_MODELS),
                        'antigravity_efforts': list(AG_EFFORTS), 'claude_models': list(CLAUDE_MODELS),
                        'claude_efforts': list(CLAUDE_EFFORTS), 'protocols': list(PROTOCOLS), 'api_efforts': list(EFFORTS),
                        'dsh_models': dsh_models, 'dsh_models_source': 'saved_and_defaults',
                        'dsh_efforts': list(dsh_backend.EFFORTS),
                        'compact_default': provider_context.info(api).window, 'base_url': DEFAULT_BASE_URL, 'api_model': MODELS[0]}}


def post_model(qq, body):
    """The panel'sSavemodelSettingsandSavemodelSettings(ModelPage.save / save_research_model), without the key."""
    from api_backend import key_path, settings as api_settings, EFFORTS as API_EFFORTS
    from api_profiles import load_connections, clean_connection
    from claude_backend import settings as claude_settings
    from antigravity_backend import settings as ag_settings
    from codex_settings import MIN_COMPACT_TOKENS, generation_settings
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    connections, active_api_id = load_connections(profile)
    saved_api = profile.get('model_api') or {}
    provider = body.get('provider')
    if provider not in ('dsh', 'codex', 'custom', 'claude', 'antigravity'):
        raise ValueError('模型来源无效')
    claude = {key: _text(body, key) if key in body else saved_api.get(key, '')
              for key in ('claude_model', 'claude_effort')}
    if provider == 'claude':
        claude_settings(claude)
    ag = {key: _text(body, key) if key in body else saved_api.get(key, '')
          for key in ('antigravity_model', 'antigravity_effort')}
    if provider == 'antigravity':
        ag_settings(ag)
    custom = {'api_protocol': _text(body, 'api_protocol'), 'api_base_url': _text(body, 'api_base_url'),
              'model': _text(body, 'model'), 'api_effort': _text(body, 'api_effort')}
    compact = _text(body, 'compact_tokens')
    recent_budget = _text(body, 'recent_chat_char_budget')
    if recent_budget and (not recent_budget.isdigit() or
                          not 4000 <= int(recent_budget) <= 128000):
        raise ValueError('自定义 API 近期聊天预算必须是 4000–128000 字符的整数，或留空用默认 24000')
    if compact and (not compact.isdigit() or int(compact) < MIN_COMPACT_TOKENS):
        raise ValueError(f'自动压缩请填不小于 {MIN_COMPACT_TOKENS} 的整数（token 数），或留空用默认')
    if provider == 'custom':
        # A remote friend session must never repoint a saved API key to an
        # arbitrary host. Creating/editing connection endpoints is desktop-only.
        requested_id = str(body.get('api_key_id') or active_api_id)
        if requested_id not in connections:
            raise ValueError('未知 API 连接，请先在 Windows 桌面端配置。')
        selected = connections[requested_id]
        for field in ('api_protocol', 'api_base_url'):
            if body.get(field) and str(body[field]).strip().rstrip('/') != selected[field]:
                raise ValueError('为了保护 API Key，请在 Windows 桌面端修改 API 协议和地址。')
        custom = {**selected, 'model': custom.get('model') or selected['model'],
                  'api_effort': custom.get('api_effort') or selected['api_effort']}
        custom = clean_connection(custom, identifier=requested_id)
        api_settings(custom)
        if not key_path(directory, requested_id).exists():
            raise ValueError('当前 API 连接尚未保存加密密钥，请在 Windows 电脑的桌面端配置。')
        connections[requested_id] = custom
        profile['api_profiles'] = connections
    api = dict(saved_api)
    api['provider'] = provider
    for key in ('codex_model', 'codex_effort'):
        if key in body:
            api[key] = _text(body, key)
    if 'codex_fast' in body:
        api['codex_fast'] = body['codex_fast'] is True
    if provider == 'codex':
        generation_settings(api)
    if provider == 'custom':
        api.update(custom)
    elif provider == 'dsh':
        config = dict(api.get('dsh_settings') or {})
        dsh_model = _text(body, 'dsh_model') or config.get('model') or 'deepseek-flash'
        dsh_effort = _text(body, 'dsh_effort') or config.get('effort') or 'low'
        config.update(model=dsh_model, effort=dsh_effort)
        api['dsh_settings'] = config
        api.update(model=dsh_model, api_effort=dsh_effort)
    api.update(claude)
    if provider == 'antigravity':
        api.update(ag)
    if compact:
        api['compact_tokens'] = int(compact)
    else:
        api.pop('compact_tokens', None)
    # Older clients may not include this newly introduced field. In that
    # case retain the user's explicit choice instead of silently resetting it.
    if 'recent_chat_char_budget' in body:
        if recent_budget:
            api['recent_chat_char_budget'] = int(recent_budget)
        else:
            api.pop('recent_chat_char_budget', None)
    research = {'research_effort': (_text(body, 'research_effort') if 'research_effort' in body
                                  else profile.get('research_effort', 'low')),
                'research_fast': (body['research_fast'] is True if 'research_fast' in body
                                  else profile.get('research_fast', True))}
    if provider == 'codex' and 'research_effort' in body:
        generation_settings({**api, 'codex_effort': research['research_effort'], 'codex_fast': research['research_fast']})
    elif research['research_effort'] not in API_EFFORTS:
        raise ValueError('话题整理思考强度无效')
    profile['model_api'] = api
    profile.update(research)
    write_profile(directory / 'friend.json', profile)
    return {'message': '模型设置已保存；下次生成消息时生效。'}


def background_settings(qq, _query):
    import mobile_settings
    return mobile_settings.snapshot(house_status.ROOT, qq)


def post_background_settings(qq, body):
    import mobile_settings
    if 'owner' in body and str(body['owner']) != qq:
        raise PermissionError('不能修改其他好友的后台设置')
    return {**mobile_settings.update(house_status.ROOT, qq, {**body, 'owner': qq}),
            'message': '后台模型与额度回复已保存，仅对当前好友生效。'}


def collaboration_rooms(qq, query):
    import mobile_collaboration
    room_id = query.get('room_id', [''])[0]
    return mobile_collaboration.action(house_status.ROOT, qq,
        {'action': 'get', 'room_id': room_id} if room_id else {'action': 'list'})


def post_collaboration_rooms(qq, body):
    import mobile_collaboration
    if 'owner' in body and str(body['owner']) != qq:
        raise PermissionError('不能操作其他好友的协作房间')
    return mobile_collaboration.action(house_status.ROOT, qq, body)


def emotion(qq, query):
    limit = min(int(query.get('limit', ['100'])[0]), 1000)
    store = FriendStore(_directory(qq))
    try:
        raw, rows = store.get('emotion_state'), store.recent_emotions(limit)
    finally:
        store.close()
    state = json.loads(raw) if raw else {}
    updated = float(state.get('updated_at', 0))
    return {'state': state, 'stale': bool(updated) and time.time() - updated >= 5 * 3600, 'rows': rows,
            'mood': followup_values(load_profile(_directory(qq) / 'friend.json'))}


def post_mood(qq, body):
    save_ignored_mood(_directory(qq) / 'friend.json', {key: body.get(key) for key in ('ignored_mood_enabled', 'ignored_mood_after')})
    return {'message': '已保存；下次续话或主动判断时生效。'}


def _input_view(full, sent):
    from prompt_context import composition, prompt_conflicts
    return {'chars': len(full), 'sent_chars': len(sent or full), 'sent': sent or full,
            'blocks': composition(full) if full else [], 'conflicts': prompt_conflicts(full) if full else []}


def context(qq, _query):
    """Current Input Context: the last generation input block by block, and the generation records."""
    import generation_log
    store = FriendStore(_directory(qq))
    try:
        data, records = json.loads(store.get('last_generation_input') or '{}'), generation_log.recent(store, 100)
    finally:
        store.close()
    full = data.get('full') or data.get('prompt', '')
    return {'mode': data.get('mode', ''), 'time': data.get('time', ''), **_input_view(full, data.get('prompt', '')),
            'records': records}


def record(qq, query):
    """One generation record: its input, the model output and the retrieval hits (ContextPage)."""
    import generation_log
    store = FriendStore(_directory(qq))
    try:
        row = generation_log.get(store, int(query.get('id', ['0'])[0]))
    finally:
        store.close()
    if row is None:
        raise LookupError('没有这条生成记录')
    return {'id': row['id'], 'mode': row['mode'], 'time': row['time'], **_input_view(row['full'], row['sent']),
            'output': (row['response'] or '') + (('\n\n出错：' + row['error']) if row['error'] else ''),
            'retrieval': json.loads(row['retrieval'] or '[]')}


def phone_register(qq, body):
    """The paired phone app tells the computer where its own MCP server is, so the model can ask the phone.

    Called by the app right after pairing (FriendHouseLink.register). The answer is deliberately small: the app
    restarts its server next, and this route may run before the phone's new endpoint is reachable.
    """
    import phone_client
    mcp_url, token = str(body.get('mcp_url') or '').strip(), str(body.get('token') or '').strip()
    if body.get('transport') == 'relay':
        if not re.fullmatch(r'[A-Za-z0-9_\-\.]{16,256}', token):
            raise ValueError('手机访问令牌无效')
        path = _directory(qq) / 'friend.json'
        profile = load_profile(path)
        profile.update(phone_transport='relay', phone_mcp_url='', phone_mcp_token=token,
                       phone_pc_url=f'http://127.0.0.1:{PORT}/', phone_friend_id=qq,
                       phone_pc_token=friend_token(qq))
        phone_relay.cancel(qq)
        write_profile(path, profile)
        return {'message': '手机已与这位好友配对，手机会主动连接电脑接收查询。', 'friends': [qq]}
    if not phone_client.usable(mcp_url):
        raise ValueError('手机地址无效：只接受 Tailscale 虚拟网段或本机地址，形如 http://100.64.0.2:8080/mcp')
    if not re.fullmatch(r'[A-Za-z0-9_\-\.]{16,256}', token):
        raise ValueError('手机访问令牌无效')
    return save_phone_address(qq, mcp_url, token)


def save_phone_address(qq, mcp_url, token):
    """Only the friend identified by the QR token receives this phone endpoint."""
    path = _directory(qq) / 'friend.json'
    profile = load_profile(path)
    profile['phone_mcp_url'], profile['phone_mcp_token'] = mcp_url, token
    profile.pop('phone_transport', None)
    phone_relay.cancel(qq)
    write_profile(path, profile)
    return {'message': '手机已与这位好友配对，模型可以按需向手机查询。', 'friends': [qq]}


# ---------- :  (Mobile Agent Console) ----------
import agent_service
import session_manager


def agent_workspaces(qq, _query):
    return {
        'workspaces': session_manager.list_workspaces(qq),
        'active_id': session_manager.get_active_workspace_id(qq),
    }


def post_agent_workspace(qq, body):
    action = body.get('action', 'switch')
    if action == 'switch':
        ws_id = body.get('ws_id', 'default')
        session_manager.set_active_workspace_id(qq, ws_id)
        return {'message': '已切换工作区'}
    elif action == 'add':
        path = str(body.get('path', '')).strip()
        title = str(body.get('title', '')).strip()
        from workspace_tools import workspace, enabled
        from tool_registry import validate_path_in_bounds
        directory = _directory(qq)
        validate_path_in_bounds(Path(path), workspace(directory), enabled(directory))
        item = session_manager.add_workspace(qq, path, title=title or None)
        return {'message': f'已添加工作区：{item["title"]}', 'workspace': item}
    raise ValueError('无效的操作')


def agent_sessions(qq, query):
    ws_id = query.get('ws_id', ['default'])[0]
    sessions = session_manager.list_sessions(qq, ws_id)
    active_id = session_manager.get_active_session_id(qq, ws_id)
    return {'sessions': sessions, 'active_id': active_id}


def post_agent_session_create(qq, body):
    ws_id = body.get('ws_id', 'default')
    title = str(body.get('title', '')).strip() or '新对话'
    item = session_manager.create_session(qq, ws_id, title)
    return {'message': '已创建新会话', 'session': item}


def post_agent_session_delete(qq, body):
    ws_id = body.get('ws_id', 'default')
    sid = body.get('sid')
    if not sid:
        raise ValueError('缺少会话ID')
    session_manager.delete_session(qq, sid, ws_id)
    return {'message': 'Delete'}


def post_agent_session_clear(qq, body):
    ws_id = body.get('ws_id', 'default')
    sid = body.get('sid')
    if not sid:
        raise ValueError('缺少会话ID')
    session_manager.clear_session(qq, sid, ws_id)
    return {'message': '会话记录已清空'}


def agent_session(qq, query):
    ws_id = query.get('ws_id', ['default'])[0]
    sid = query.get('sid', ['session_main'])[0]
    if sid == '__qq_stream__':
        return agent_service.qq_stream(qq)
    data = session_manager.load_session(qq, sid, ws_id)
    session_manager.set_active_session_id(qq, sid, ws_id)
    return data


def agent_config(qq, _query):
    from agent_tool_adapters import AGENT_BACKENDS, BUILTINS
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    api = profile.get('model_api') or {}
    provider = api.get('provider') or 'codex'
    header_title, main_models, overflow_models = agent_service.get_provider_model_structure(provider)

    if provider == 'codex':
        cur_model = api.get('codex_model', 'gpt-6-luna')
        cur_effort = api.get('codex_effort', 'low')
    elif provider == 'claude':
        cur_model = api.get('claude_model', 'claude-sonnet-5-5')
        cur_effort = api.get('claude_effort', 'low')
    elif provider == 'antigravity':
        cur_model = api.get('antigravity_model', 'gemini-3.8-flash-high')
        cur_effort = api.get('antigravity_effort', 'low')
    elif provider in ('dsh', 'deepseek_harness'):
        cur_model = (api.get('dsh_settings') or {}).get('model', 'DeepSeek-V41-Flash')
        cur_effort = (api.get('dsh_settings') or {}).get('effort', 'low')
    elif provider in AGENT_BACKENDS:
        cur_model = cur_effort = '沿用工具配置'
    else:
        cur_model = api.get('model', 'deepseek-chat')
        cur_effort = api.get('api_effort', 'low')

    efforts = agent_service.get_model_efforts(provider, cur_model)
    return {
        'provider': provider,
        'model': cur_model,
        'effort': cur_effort,
        'header_title': header_title,
        'main_models': [{'id': m[0], 'name': m[1], 'badge': m[2]} for m in main_models],
        'overflow_models': [{'id': m[0], 'name': m[1]} for m in overflow_models],
        'efforts': [{'id': e[0], 'name': e[1]} for e in efforts],
        'providers': [
            {'id': 'dsh', 'name': 'DeepSeek Harness (免Key客户端)'},
            {'id': 'claude', 'name': 'Claude 账号'},
            {'id': 'codex', 'name': 'Codex (当前默认)'},
            {'id': 'antigravity', 'name': 'Antigravity CLI (Gemini)'},
            {'id': 'custom', 'name': '自定义 API'},
        ] + [{'id': key, 'name': BUILTINS[key].name} for key in AGENT_BACKENDS]
    }


def post_agent_config(qq, body):
    from agent_tool_adapters import AGENT_BACKENDS
    provider = body.get('provider')
    model = body.get('model')
    effort = body.get('effort')
    directory = _directory(qq)
    profile = load_profile(directory / 'friend.json')
    api = dict(profile.get('model_api') or {})
    if provider:
        if provider not in ('codex', 'claude', 'antigravity', 'dsh', 'custom', *AGENT_BACKENDS):
            raise ValueError('未知的智能体来源')
        api['provider'] = provider
        api['multi_agent_enabled'] = False
        api['multi_agent'] = {**(api.get('multi_agent') or {}), 'enabled': False}
    actual_provider = provider or api.get('provider') or 'codex'
    if actual_provider == 'codex':
        if model:
            api['codex_model'] = model
            api['model'] = model
        if effort:
            api['codex_effort'] = effort
    elif actual_provider == 'claude':
        if model:
            api['claude_model'] = model
        if effort:
            api['claude_effort'] = effort
    elif actual_provider == 'antigravity':
        if model:
            api['antigravity_model'] = model
        if effort:
            api['antigravity_effort'] = effort
    elif actual_provider in ('dsh', 'deepseek_harness'):
        if model:
            api['model'] = model
            api.setdefault('dsh_settings', {})['model'] = model
        if effort:
            api['api_effort'] = effort
            api.setdefault('dsh_settings', {})['effort'] = effort
    elif actual_provider in ('custom', 'deepseek', 'api'):
        if model:
            api['model'] = model
        if effort:
            api['api_effort'] = effort
    profile['model_api'] = api
    write_profile(directory / 'friend.json', profile)
    return {'message': 'modelSettings'}


def post_agent_send(qq, body):
    ws_id = body.get('ws_id', 'default')
    sid = body.get('sid') or 'session_main'
    text = str(body.get('text', '')).strip()
    if not text:
        raise ValueError('消息内容不能为空')

    if sid == '__qq_stream__':
        receipt = agent_service.send_friend_message(qq, text)
        return {'reply': text, 'thinking': '', 'is_qq': True, **receipt}

    if sid == '__model__':
        # The phone may speak either as the QQ account (above) or as the model.
        # This generates one normal agent turn, then delivers that reply to QQ.
        workspaces = session_manager.list_workspaces(qq)
        sender_ws = ws_id if any(item.get('id') == ws_id for item in workspaces) else (
            workspaces[0]['id'] if workspaces else 'default')
        result = agent_service.execute_agent_turn(
            qq, sender_ws, 'session_main', text,
            model=body.get('model'), effort=body.get('effort'),
            provider=body.get('provider'), mode='chat')
        reply = str(result.get('reply') or '').strip()
        if not reply:
            raise ValueError('模型没有生成可发送的回复')
        receipt = agent_service.send_friend_message(qq, reply, identity='model')
        return {**result, 'reply': reply, 'is_qq': True, 'identity': 'model', **receipt}

    provider = body.get('provider')
    model = body.get('model')
    effort = body.get('effort')
    return agent_service.execute_agent_turn(qq, ws_id, sid, text, model=model, effort=effort,
                                             provider=provider, mode=body.get('mode', 'task'))


def agent_goals(qq, query):
    from task_manager import TaskManager
    return {'goals': TaskManager.get_instance().list_goals(qq)}


def post_agent_goal_action(qq, body):
    from task_manager import TaskManager
    manager = TaskManager.get_instance()
    if body.get('action') == 'propose':
        tid = manager.propose_goal(body.get('description'), body.get('scope'),
            body.get('completion_criteria'), qq, token_budget=body.get('token_budget', 0),
            max_calls=body.get('max_calls', 100), auto_start=body.get('auto_start') is True)
        return {'task_id': tid, 'message': '规划任务已入队；按所选授权方式开始执行'}
    if body.get('action') == 'accept_plan':
        return {'goal_id': manager.accept_goal_plan(body.get('task_id'), qq), 'message': '已按确认的阶段计划创建目标'}
    if body.get('action') == 'create':
        gid = manager.create_goal(
            body.get('description'), body.get('scope'), body.get('completion_criteria'),
            body.get('milestones'), qq, priority=body.get('priority', 50),
            token_budget=body.get('token_budget', 0), max_calls=body.get('max_calls', 100),
            deadline=body.get('deadline', 0))
        return {'goal_id': gid, 'message': '阶段任务已创建'}
    changed = manager.goal_action(body.get('goal_id'), qq, body.get('action'), body.get('review', ''))
    return {'changed': changed, 'message': '目标已更新' if changed else '当前状态不能执行此操作'}


def agent_tasks(qq, query):
    from task_manager import TaskManager
    manager = TaskManager.get_instance()
    status = query.get('status', [None])[0]
    tasks = manager.list_tasks(status=status, owner_id=qq)
    return {'tasks': [t.to_dict() for t in tasks]}


def agent_task_detail(qq, query):
    from task_manager import TaskManager
    manager = TaskManager.get_instance()
    tid = query.get('task_id', [''])[0]
    if not tid:
        raise ValueError('缺少 task_id')
    task = manager.get_task(tid)
    if not task:
        raise LookupError('任务不存在')
    if task.owner_id != qq:
        raise PermissionError('无权查看该任务详情')
    logs = manager.get_task_logs(tid)
    return {'task': task.to_dict(), 'logs': logs}


def post_agent_task_action(qq, body):
    from task_manager import TaskManager
    manager = TaskManager.get_instance()
    action = body.get('action')
    tid = body.get('task_id')

    if action == 'create':
        title = str(body.get('title', '')).strip()
        desc = str(body.get('description', '')).strip()
        cat = str(body.get('category', 'general')).strip()
        pri = int(body.get('priority', 50))
        deps = body.get('depends_on', [])
        max_dur = int(body.get('max_duration_seconds', 1800))
        tb = int(body.get('token_budget', 0))
        for dependency in deps:
            dep = manager.get_task(dependency)
            if not dep or dep.owner_id != qq:
                raise PermissionError('无权引用该依赖任务')
        import task_spec
        checkpoint = task_spec.build_task_spec(desc, cat)
        if cat == 'browser':
            checkpoint.update({key: body[key] for key in ('url', 'browser_steps', 'browser_autoplan', 'expected_text') if key in body})
        new_id = manager.create_task(
            title=title, description=desc, category=cat, priority=pri,
            depends_on=deps, max_duration_seconds=max_dur, token_budget=tb,
            owner_id=qq, cost_budget=float(body.get('cost_budget', 0)), max_calls=int(body.get('max_calls', 100)),
            max_retries=0 if cat == 'browser' else 3,
            initial_checkpoint=checkpoint,
        )
        return {'message': '任务创建成功', 'task_id': new_id}

    if not tid:
        raise ValueError('缺少 task_id')
    task = manager.get_task(tid)
    if not task:
        raise LookupError('任务不存在')
    if task.owner_id != qq:
        raise PermissionError('无权操作该任务')

    # Friend pairing credentials can act only for this owner, regardless of the QQ account's desktop role.
    owner = task.owner_id
    if action == 'pause':
        ok = manager.pause_task(tid, owner_id=owner)
        return {'message': '任务已暂停' if ok else '任务当前状态无法暂停'}
    elif action == 'resume':
        ok = manager.resume_task(tid, owner_id=owner)
        return {'message': '任务已恢复就绪' if ok else '任务当前状态无法继续'}
    elif action == 'cancel':
        ok = manager.cancel_task(tid, owner_id=owner)
        return {'message': '任务已取消' if ok else '任务当前状态无法取消'}
    elif action == 'approve':
        ok = manager.approve_task(tid, owner_id=owner)
        return {'message': '已确认任务结果' if ok else '任务当前未等待结果验收'}
    elif action == 'reject':
        ok = manager.reject_task(tid, owner_id=owner, reason=str(body.get('reason', '')))
        return {'message': '已拒绝任务结果' if ok else '任务当前未等待结果验收'}
    raise ValueError('任务操作无效')


def agent_approvals(qq, _query):
    from approval_manager import ApprovalManager
    mgr = ApprovalManager.get_instance()
    reqs = mgr.list_pending_requests(owner_id=qq)
    return {'approvals': [r.to_dict() for r in reqs]}


def post_agent_approval_decision(qq, body):
    from approval_manager import ApprovalManager
    mgr = ApprovalManager.get_instance()
    req_id = body.get('req_id')
    if not req_id:
        raise ValueError('缺少审批请求编号')
    req = mgr.get_request(req_id)
    if not req:
        raise LookupError('审批请求不存在')
    if req.owner_id != qq:
        raise PermissionError('无权处理该审批请求')

    decision = body.get('decision')  # 'approve' or 'reject'
    scope = body.get('allowed_scope', 'once')
    reason = body.get('reason', '')

    from approval_manager import create_trusted_identity
    eff_approver = create_trusted_identity(str(qq), is_admin=False, session_token=_tokens().get(str(qq)))
    if decision == 'approve':
        ok = mgr.approve_request(req_id, allowed_scope=scope, reason=reason, approver_id=eff_approver)
        return {'message': '操作已批准' if ok else '无法批准，或该请求已处理'}
    elif decision == 'reject':
        ok = mgr.reject_request(req_id, reason=reason or '用户手动拒绝', approver_id=eff_approver)
        return {'message': '操作已拒绝' if ok else '无法拒绝，或该请求已处理'}
    raise ValueError('审批操作无效')


def agent_notifications(qq, query):
    from notification_manager import NotificationManager
    mgr = NotificationManager.get_instance()
    unread_only = query.get('unread_only', ['0'])[0] in ('1', 'true', 'True')
    items = mgr.list_recent(limit=30, unread_only=unread_only, recipient_id=qq)
    return {'notifications': items}


def post_agent_notification_action(qq, body):
    from notification_manager import NotificationManager
    mgr = NotificationManager.get_instance()
    action = body.get('action')
    notif_id = str(body.get('id', ''))

    if action == 'mark_read' and notif_id:
        success = mgr.mark_as_read(notif_id, recipient_id=str(qq), is_admin=False)
        if success:
            return {'status': 'ok', 'read': True, 'id': notif_id}
        return {'status': 'forbidden', 'read': False, 'id': notif_id, 'error': '无权操作此通知或通知不存在'}
    elif action == 'mark_all_read':
        count = mgr.mark_all_as_read(recipient_id=str(qq))
        return {'status': 'ok', 'updated_count': count}
    elif action == 'delete' and notif_id:
        deleted = mgr.delete_notification(notif_id, recipient_id=str(qq), is_admin=False)
        if deleted:
            return {'status': 'ok', 'deleted': True, 'id': notif_id}
        return {'status': 'forbidden', 'deleted': False, 'id': notif_id, 'error': '无权删除此通知或通知不存在'}
    return {'status': 'ignored'}


def post_agent_research_action(qq, body):
    from research_tracker import ResearchTracker
    from task_manager import TaskManager
    tracker=ResearchTracker.get_instance()
    manager=TaskManager.get_instance()
    project=tracker.get_project(str(body.get('project_id','')))
    task=manager.get_task(project.task_id) if project else None
    if not task or task.owner_id!=qq:
        raise PermissionError('无权操作该研究项目')
    action=body.get('action')
    if action=='stop':
        project.status='PAUSED';tracker._save_project(project)
        manager.pause_task(task.task_id, owner_id=task.owner_id)
        with manager._get_connection() as db:
            children=db.execute("SELECT task_id FROM tasks WHERE json_extract(checkpoint,'$.research_project_id')=? AND owner_id=?",(project.project_id,task.owner_id)).fetchall()
        for child in children:
            manager.pause_task(child['task_id'], owner_id=task.owner_id)
        return {'message':'研究已停止，检查点和历史记录保留'}
    if action=='verify_stage':
        findings=body.get('findings',[])
        if not isinstance(findings,list) or not findings or not all(isinstance(f,str) and f.strip() for f in findings):
            raise ValueError('需要明确的待审核结论列表')
        notify,summary=tracker.record_stage_progress(project.project_id,int(body.get('stage_index',project.current_stage)),findings,[],
            stage_achieved=True,verification_status='verified',sources=body.get('sources'),evidence=body.get('evidence'),reviewed_by=qq)
        if notify:
            from notification_manager import NotificationManager
            NotificationManager.get_instance().publish('研究阶段目标完成',summary,category='research',recipient_id=task.owner_id,
                metadata={'dedup_key':project.project_id+':stage:'+str(body.get('stage_index',project.current_stage))})
        return {'message':summary}
    if action=='continue':
        if project.status=='PAUSED':
            project.status='ACTIVE';tracker._save_project(project)
        tid=tracker.schedule_next_stage(manager,project.project_id,body.get('delay_seconds',0))
        return {'message':'下一阶段已排入本机任务队列','task_id':tid}
    raise ValueError('研究操作无效')


def agent_tools(qq, _query):
    from tool_registry import ToolRegistry
    reg = ToolRegistry.get_instance()
    return {'tools': reg.list_tools()}


def workflows(qq, _query):
    from task_manager import TaskManager
    from task_workflows import WorkflowStore
    return {'workflows':WorkflowStore(TaskManager.get_instance()).list(qq)}


def post_workflow(qq, body):
    from task_manager import TaskManager
    from task_workflows import WorkflowStore
    store=WorkflowStore(TaskManager.get_instance())
    action=body.get('action')
    identifier=body.get('workflow_id')
    if action=='learn':
        identifier=store.learn(qq,body.get('kind','task'),body.get('source_id'))
        return {'workflow_id':identifier,'message':'已学习为流程草稿，尚未自动执行'}
    if action=='save':
        identifier=store.save(qq,body.get('title'),body.get('definition'),identifier=identifier or '',
                              version=body.get('version',0),correction=body.get('correction',''))
        return {'workflow_id':identifier,'message':'流程版本已保存，定时执行需重新开启'}
    if action=='run':
        goal=store.run(identifier,qq)
        if not goal:
            raise ValueError('上一轮仍在运行、暂停或等待验收，请先处理')
        store.manager.start_worker()
        return {'goal_id':goal,'message':'流程已进入任务队列'}
    if action=='schedule':
        store.schedule(identifier,qq,enabled=body.get('enabled'),interval_seconds=body.get('interval_seconds',86400),
                       next_run=body.get('next_run'),remaining_runs=body.get('remaining_runs',1))
        if body.get('enabled') is True:
            store.manager.start_worker()
        return {'message':'后续定时执行已开启' if body.get('enabled') else '后续定时执行已停止'}
    raise ValueError('工作流操作无效')


def study_library(qq, query):
    from topic_library import call_tool
    args = {key: query[key][0] for key in ('operation', 'query', 'id') if key in query}
    args.setdefault('operation', 'list')
    return call_tool(_directory(qq), 'study_read', args)


def post_study_action(qq, body):
    from topic_library import call_tool
    return call_tool(_directory(qq), 'study_write', body)


GET = {'voice_audio': voice_audio, 'study_library': study_library, 'collaboration': collaboration, 'friend': friend, 'history': history, 'proactive': proactive, 'memory': memory, 'usage': usage,
       'background_settings': background_settings,
       'collaboration_rooms': collaboration_rooms,
       'settings': settings, 'voice': voice_settings, 'topics': topics, 'stickers': stickers, 'template': template, 'model': model,
       'emotion': emotion, 'context': context, 'record': record,
       'agent_workspaces': agent_workspaces, 'agent_sessions': agent_sessions,
       'agent_session': agent_session, 'agent_config': agent_config,
       'agent_goals': agent_goals, 'agent_tasks': agent_tasks, 'agent_task_detail': agent_task_detail,
       'agent_approvals': agent_approvals, 'agent_notifications': agent_notifications,
       'agent_tools': agent_tools, 'workflows':workflows}
POST = {'study_action': post_study_action, 'switch': post_switch, 'toggle': post_toggle, 'retry': post_retry, 'memory': post_memory,
        'background_settings': post_background_settings,
        'collaboration_rooms': post_collaboration_rooms,
        'preview': post_preview, 'basic': post_basic, 'style': post_style, 'voice': post_voice_settings, 'voice_input': post_voice_input, 'page_settings': post_page_settings,
        'research': post_research, 'workflow':post_workflow, 'topic_remove': post_topic_remove, 'sticker': post_sticker,
        'sticker_remove': post_sticker_remove, 'model': post_model, 'mood': post_mood,
        'agent_workspace': post_agent_workspace, 'agent_session_create': post_agent_session_create,
        'agent_session_delete': post_agent_session_delete, 'agent_session_clear': post_agent_session_clear,
        'agent_config': post_agent_config, 'agent_send': post_agent_send,
        'agent_goal_action': post_agent_goal_action, 'agent_task_action': post_agent_task_action,
        'agent_approval_decision': post_agent_approval_decision, 'agent_research_action': post_agent_research_action, 'agent_notification_action': post_agent_notification_action}


class Handler(BaseHTTPRequestHandler):
    server_version = 'FriendHouse'

    def log_message(self, *args):  # no request log (it would carry message ids)
        pass

    def _send(self, status, payload, kind='application/json; charset=utf-8'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if (self.headers.get('X-Forwarded-Proto', '').lower() == 'https'
                or funnel_request(self.headers)):
            self.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        url = urlparse(self.path)
        # Funnel arrives from tailscaled on loopback, so client_address cannot
        # distinguish it from a local browser. Refuse the whole public surface
        # unless the operator explicitly opted in.
        if funnel_request(self.headers) and not funnel_allowed():
            return self._send(403, {'error': 'Tailscale Funnel 未启用；请在电脑的移动设备与网页访问中点击一键配置'})
        if method == 'GET' and url.path in ('/', '/index.html'):
            return self._send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
        if method == 'GET' and url.path == '/app-icon.png':
            return self._send(200, (PAGE.parent / 'assets' / 'app-icon.png').read_bytes(), 'image/png')
        if method == 'GET' and url.path in ('/favicon.ico', '/app-icon.ico'):
            ico_file = PAGE.parent / 'assets' / 'app-icon.ico'
            if not ico_file.is_file():
                ico_file = PAGE.parent / 'friend-house.ico'
            return self._send(200, ico_file.read_bytes(), 'image/x-icon')
        if not url.path.startswith('/api/'):
            return self._send(404, {'error': '没有这个页面'})
        given = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        qq = friend_for_token(given)
        if qq is None:
            return self._send(401, {'error': '令牌无效，请在电脑小屋重新扫码'})
        parts = url.path.split('/')[2:]
        # The QR token delegates one friend's house, not that QQ account's administrator role.
        if (parts and parts[0] in ('overview', 'bridge') or
                parts and parts[0] == 'friend' and (len(parts) < 2 or parts[1] != qq)):
            return self._send(403, {'error': '无权访问该资源，此令牌仅限访问自身好友小屋'})
        try:
            if method == 'POST':
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_BODY:
                    raise ValueError('内容太长')
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('请求内容必须是对象')
                if parts == ['phone', 'register']:
                    if body.get('qq', qq) != qq:
                        return self._send(403, {'error': '配对链接与好友不匹配，请重新扫码'})
                    return self._send(200, phone_register(qq, body))
                if len(parts) == 2 and parts[0] == 'phone' and parts[1] in ('poll', 'result', 'query'):
                    profile = load_profile(_directory(qq) / 'friend.json')
                    phone_token = self.headers.get('X-Phone-Token', '')
                    expected = str(profile.get('phone_mcp_token') or '')
                    if (profile.get('phone_transport') != 'relay' or not expected or not phone_token
                            or not hmac.compare_digest(expected.encode(), phone_token.encode())):
                        return self._send(403, {'error': '手机配对已失效，请重新扫码'})
                    if not profile.get('bridge_enabled', True):
                        phone_relay.cancel(qq)
                        return self._send(403, {'error': '当前好友已暂停，未查询手机'})
                    if parts[1] == 'poll':
                        phone_client.mark_seen(_directory(qq))
                        task = phone_relay.poll(qq, phone_token)
                        # A re-pair or pause may happen during the long poll.
                        current = load_profile(_directory(qq) / 'friend.json')
                        if current.get('phone_mcp_token') != phone_token or not current.get('bridge_enabled', True):
                            phone_relay.cancel(qq)
                            return self._send(403, {'error': '手机配对已变更或好友已暂停'})
                        return self._send(200, {'task': task})
                    if parts[1] == 'result':
                        return self._send(200, phone_relay.result(qq, phone_token, body))
                    # Funnel requests also arrive on loopback, from tailscaled, which adds forwarding headers.
                    if (self.client_address[0] not in ('127.0.0.1', '::1')
                            or any(name in self.headers for name in FORWARDED)):
                        return self._send(403, {'error': '查询只能由电脑本机发起'})
                    import phone_tools
                    name, arguments = body.get('name'), body.get('arguments')
                    if name not in phone_tools.NAMES or not isinstance(arguments, dict) or not phone_tools.valid_arguments(name, arguments):
                        raise ValueError('手机工具参数无效')
                    answer = phone_relay.query(qq, phone_token, name, arguments)
                    current = load_profile(_directory(qq) / 'friend.json')
                    if current.get('phone_mcp_token') != phone_token or not current.get('bridge_enabled', True):
                        return self._send(403, {'error': '手机配对已变更或好友已暂停'})
                    return self._send(200, {'result': answer})
                if len(parts) == 3 and parts[0] == 'friend' and re.fullmatch(r'\d{5,12}', parts[1]) and parts[2] in POST:
                    return self._send(200, POST[parts[2]](parts[1], body))
            else:
                query = parse_qs(url.query)
                if parts == ['session']:
                    return self._send(200, {'qq': qq, 'name': _friends()[qq].get('display_name', qq)})
                if len(parts) == 2 and parts[0] == 'friend' and re.fullmatch(r'\d{5,12}', parts[1]):
                    return self._send(200, friend(parts[1], query))
                if len(parts) == 3 and parts[0] == 'friend' and parts[1] == qq and parts[2] == 'sticker_image':
                    return self._send(200, sticker_image(qq, query), 'image/png')
                if len(parts) == 3 and parts[0] == 'friend' and parts[1] == qq and parts[2] == 'voice_audio':
                    return self._send(200, voice_audio(qq, query), 'audio/wav')
                if len(parts) == 3 and parts[0] == 'friend' and re.fullmatch(r'\d{5,12}', parts[1]) and parts[2] in GET:
                    return self._send(200, GET[parts[2]](parts[1], query))
            return self._send(404, {'error': '没有这个接口'})
        except PermissionError as exc:
            return self._send(403, {'error': str(exc)})
        except LookupError as exc:
            return self._send(404, {'error': str(exc)})
        except TimeoutError as exc:
            return self._send(504, {'error': str(exc)})
        except (ValueError, TypeError) as exc:
            return self._send(400, {'error': str(exc)})
        except Exception:
            return self._send(500, {'error': '电脑暂时未能完成请求，请稍后重试或在电脑端检查状态'})

    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')


def start(allow_remote: bool | None = None):
    """Serves the friend-scoped pages.

    Loopback is always bound.  A detected Tailscale address is also bound by
    default so the phone/Tailscale entry appears automatically; set
    QQ_ALLOW_TAILSCALE=0 to keep the old local-only behavior.  LAN/Radmin/public
    endpoints still require QQ_ALLOW_REMOTE=1. Funnel only forwards loopback
    and uses its separate explicit desktop setting.
    """
    if allow_remote is None:
        import os
        allow_remote = os.environ.get('QQ_ALLOW_REMOTE', '0') in ('1', 'true', 'True')
    import os
    allow_tailscale = os.environ.get('QQ_ALLOW_TAILSCALE', '1') not in ('0', 'false', 'False')

    radmin_ip = network.radmin_address() if allow_remote else None
    lan_ip = network.lan_address() if allow_remote else None
    address = tailnet_address() if (allow_remote or allow_tailscale) else None
    info = {
        'local': f'http://127.0.0.1:{PORT}/',
        'radmin': f'http://{radmin_ip}:{PORT}/' if radmin_ip else None,
        'lan': f'http://{lan_ip}:{PORT}/' if lan_ip else None,
        'tailnet': f'http://{address}:{PORT}/' if address else None,
        'public': network.public_url(PORT) if funnel_allowed() else None,
    }

    if allow_remote:
        # Try binding 0.0.0.0 to handle all networks cleanly
        bound_all = False
        try:
            server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, name='house-web-all', daemon=True).start()
            bound_all = True
        except OSError:
            pass

        if not bound_all:
            hosts = ['127.0.0.1']
            if radmin_ip:
                hosts.append(radmin_ip)
            if lan_ip:
                hosts.append(lan_ip)
            if address:
                hosts.append(address)

            for host in hosts:
                try:
                    server = ThreadingHTTPServer((host, PORT), Handler)
                    server.daemon_threads = True
                    threading.Thread(target=server.serve_forever, name=f'house-web-{host}', daemon=True).start()
                except OSError:
                    if host == address:
                        info['tailnet'] = None
                    elif host == radmin_ip:
                        info['radmin'] = None
                    elif host == lan_ip:
                        info['lan'] = None
                    continue
    else:
        # Local-only mode, plus the Tailscale interface when it exists.
        try:
            server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, name='house-web-local', daemon=True).start()
        except OSError:
            pass
        if address:
            try:
                tail_server = ThreadingHTTPServer((address, PORT), Handler)
                tail_server.daemon_threads = True
                threading.Thread(target=tail_server.serve_forever, name='house-web-tailnet', daemon=True).start()
            except OSError:
                info['tailnet'] = None

    try:
        from task_manager import TaskManager
        TaskManager.get_instance().start_worker()
    except Exception:
        pass

    return info

