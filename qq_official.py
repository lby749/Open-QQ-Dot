"""Official QQ bots, isolated from the optional personal-account OneBot transport."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def app_id(value):
    value = str(value).strip()
    if not re.fullmatch(r'\d{5,30}', value):
        raise ValueError('AppID 须为官方后台提供的数字标识')
    return value


def bots(home):
    path = Path(home) / 'data/qq_official_bots.json'
    if not path.is_file():
        return []
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, list):
        raise ValueError('机器人配置损坏，请从备份恢复')
    return value


def secret_dir(home):
    path = Path(home) / 'data/qq_bot_secrets'
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_secret(home, identifier):
    from api_backend import load_key
    identifier = app_id(identifier)
    value = os.environ.get('QQDOT_BOT_SECRET_' + identifier, '').strip()
    return value or load_key(secret_dir(home), purpose='qqbot_' + identifier)


def save_bot(home, identifier, name, secret='', *, enabled=True):
    from api_backend import save_key
    from house_settings import replace_file
    identifier = app_id(identifier)
    rows = [row for row in bots(home) if row['app_id'] != identifier]
    if enabled and sum(bool(row.get('enabled', True)) for row in rows) >= 5:
        raise ValueError('最多同时启用 5 个官方机器人，请先停用其他账号')
    if not isinstance(secret, str) or len(secret) > 4096:
        raise ValueError('AppSecret 格式无效')
    if secret.strip():
        save_key(secret_dir(home), secret, purpose='qqbot_' + identifier)
    else:
        load_secret(home, identifier)
    rows.append({'app_id': identifier, 'name': str(name).strip()[:60] or 'QQ 官方机器人', 'enabled': bool(enabled)})
    path = Path(home) / 'data/qq_official_bots.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), 'utf-8')
    replace_file(temporary, path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _message_id(value):
    # Official message IDs are opaque strings (including ROBOT1.0_...), used only in JSON.
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('官方消息编号格式无效')
    return value


class OfficialGroupSender:
    """Use official REST replies; the application's existing gateway remains the only event connection."""
    def __init__(self, home, identifier):
        self.home, self.app_id = Path(home), app_id(identifier)
        self.secret, self.token, self.expires = '', '', 0
        self.lock = threading.Lock()

    def _request(self, url, body, headers=None):
        request = Request(url, json.dumps(body).encode('utf-8'),
                          headers={'Content-Type': 'application/json', **(headers or {})}, method='POST')
        try:
            with build_opener(_NoRedirect).open(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('官方接口响应过大')
                result = json.loads(raw)
        except HTTPError as error:
            raise RuntimeError(f'官方机器人接口未确认成功（HTTP {error.code}）') from None
        if not isinstance(result, dict) or result.get('code'):
            raise RuntimeError('官方机器人接口未确认成功')
        return result

    def access_token(self):
        with self.lock:
            secret = load_secret(self.home, self.app_id)
            if secret != self.secret:
                self.secret, self.expires = secret, 0
            if time.time() >= self.expires:
                result = self._request('https://bots.qq.com/app/getAppAccessToken',
                                       {'appId': self.app_id, 'clientSecret': self.secret})
                lifetime = int(result.get('expires_in', 0))
                if not result.get('access_token') or lifetime <= 60:
                    raise RuntimeError('官方机器人未获得有效访问令牌')
                self.token, self.expires = result['access_token'], time.time() + lifetime - 60
            return self.token

    def send(self, channel, content, message_id, sequence):
        return self._reply('groups', channel, content, message_id, sequence)

    def send_private(self, channel, content, message_id, sequence):
        return self._reply('users', channel, content, message_id, sequence)

    def _reply(self, kind, channel, content, message_id, sequence):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(channel)):
            raise ValueError('需要该机器人自己的会话 OpenID')
        _message_id(message_id)
        if not 1 <= int(sequence) <= 5:
            raise ValueError('缺少有效的官方回复授权')
        result = self._request(f'https://api.sgroup.qq.com/v2/{kind}/{channel}/messages',
            {'content': str(content)[:1500], 'msg_type': 0, 'msg_id': message_id, 'msg_seq': int(sequence)},
            {'Authorization': 'QQBot ' + self.access_token(), 'X-Union-Appid': self.app_id})
        if not result.get('id'):
            raise RuntimeError('官方消息没有发送回执，不能自动重发')
        return str(result['id'])

    def invitation(self):
        """Use the authenticated official profile; AppID and the guild user ID are not bot QQ numbers."""
        request = Request('https://api.sgroup.qq.com/users/@me', headers={
            'Authorization': 'QQBot ' + self.access_token(), 'X-Union-Appid': self.app_id})
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('官方机器人资料响应过大')
        profile = json.loads(raw)
        if not isinstance(profile, dict):
            raise ValueError('官方机器人资料格式无效')
        url = str(profile.get('share_url') or '')
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        uin = query.get('robot_uin', [''])[0]
        if (parsed.scheme != 'https' or parsed.hostname != 'qun.qq.com' or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment
                or parsed.path != '/qunpro/robot/qunshare' or len(url) > 2048
                or query.get('robot_appid') != [self.app_id] or len(query.get('robot_uin', [])) != 1
                or not re.fullmatch(r'\d{5,12}', uin)):
            raise ValueError('官方资料未返回与本机器人匹配的有效入群链接')
        return {'app_id': self.app_id, 'qq_id': uin, 'name': str(profile.get('username') or '')[:60],
                'invite_url': url}


class ContactStore:
    """OpenIDs are never treated as QQ numbers; local numeric IDs stay inside this adapter."""
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / 'data/qq_official.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS contacts(
                local_id TEXT PRIMARY KEY,app_id TEXT NOT NULL,kind TEXT NOT NULL,
                channel TEXT NOT NULL,sender TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
                msg_id TEXT NOT NULL DEFAULT '',received REAL NOT NULL DEFAULT 0,sequence INTEGER NOT NULL DEFAULT 0,
                UNIQUE(app_id,kind,channel,sender));
                CREATE TABLE IF NOT EXISTS outbox(
                local_id TEXT NOT NULL,msg_id TEXT NOT NULL,body_hash TEXT NOT NULL,sequence INTEGER NOT NULL,
                state TEXT NOT NULL,receipt TEXT NOT NULL DEFAULT '',PRIMARY KEY(local_id,msg_id,body_hash));''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def incoming(self, identifier, kind, channel, sender, message_id):
        identifier = app_id(identifier)
        if kind not in ('c2c', 'group') or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(v))
                for v in (channel, sender)):
            raise ValueError('官方事件标识无效')
        _message_id(message_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT * FROM contacts WHERE app_id=? AND kind=? AND channel=? AND sender=?',
                               (identifier, kind, channel, sender)).fetchone()
            if not found:
                from main import load_registry
                existing = load_registry().get('friends', {})
                while True:
                    local_id = str(900000000000 + secrets.randbelow(99999999999))
                    if local_id not in existing and not db.execute('SELECT 1 FROM contacts WHERE local_id=?', (local_id,)).fetchone():
                        break
                db.execute('INSERT INTO contacts(local_id,app_id,kind,channel,sender) VALUES(?,?,?,?,?)',
                           (local_id, identifier, kind, channel, sender))
                found = db.execute('SELECT * FROM contacts WHERE local_id=?', (local_id,)).fetchone()
            # Duplicate gateway delivery cannot renew an expired reply grant or reset its sequence.
            if found['msg_id'] != message_id:
                db.execute('UPDATE contacts SET msg_id=?,received=?,sequence=0 WHERE local_id=?',
                           (message_id, time.time(), found['local_id']))
            return self.get(found['local_id'], db=db)

    def get(self, local_id, *, db=None):
        if db is not None:
            value = db.execute('SELECT * FROM contacts WHERE local_id=?', (str(local_id),)).fetchone()
            return dict(value) if value else None
        with self.db() as db:
            return self.get(local_id, db=db)

    def list(self, identifier):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM contacts WHERE app_id=? ORDER BY received DESC LIMIT 100', (app_id(identifier),))]

    def approve(self, identifier, local_id, name):
        from main import default_profile, dump_json, load_registry, save_registry
        contact = self.get(local_id)
        if not contact or contact['app_id'] != app_id(identifier):
            raise ValueError('会话不属于所选机器人')
        registry = load_registry()
        old = registry.setdefault('friends', {}).get(str(local_id))
        if old and (old.get('platform') != 'qq_official' or old.get('self_id') != identifier):
            raise ValueError('本地会话标识冲突，未覆盖原好友数据')
        directory = self.home / 'friends' / str(local_id)
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / 'friend.json').exists():
            profile = default_profile(str(local_id), str(name)[:60], False)
            profile.update(platform='qq_official', computer_tools={'enabled': False}, sticker_frequency=0,
                           media_reply={'mode': 'never'}, reply_bubbles_max=1)
            dump_json(directory / 'friend.json', profile)
        registry['friends'][str(local_id)] = dict(qq_id=str(local_id), directory=str(directory),
            display_name=str(name).strip()[:60] or '机器人会话', self_id=identifier, platform='qq_official')
        save_registry(registry)
        with self.db() as db:
            db.execute('UPDATE contacts SET enabled=1 WHERE local_id=? AND app_id=?', (str(local_id), identifier))

    def reserve(self, identifier, local_id, text):
        from notification_manager import NotificationDeferred
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            contact = self.get(local_id, db=db)
            if not contact or not contact['enabled'] or contact['app_id'] != identifier:
                raise PermissionError('机器人会话尚未授权或不属于此机器人')
            prior = db.execute('SELECT * FROM outbox WHERE local_id=? AND msg_id=? AND body_hash=?',
                               (str(local_id), contact['msg_id'], digest)).fetchone()
            if prior:
                if prior['state'] == 'SENT':
                    return contact, digest, prior['receipt']
                raise RuntimeError('此条消息的发送结果未确认，请核对 QQ；不会自动重复发送')
            if not contact['msg_id'] or time.time() - contact['received'] >= 3600 or contact['sequence'] >= 5:
                raise NotificationDeferred('官方回复窗口已结束或次数已用完，请在 QQ 再发一条消息后继续；结果已保留在本地')
            contact['sequence'] += 1
            db.execute('UPDATE contacts SET sequence=? WHERE local_id=?', (contact['sequence'], str(local_id)))
            db.execute('INSERT INTO outbox(local_id,msg_id,body_hash,sequence,state) VALUES(?,?,?,?,?)',
                       (str(local_id), contact['msg_id'], digest, contact['sequence'], 'SENDING'))
            return contact, digest, ''

    def receipt(self, contact, digest, receipt):
        with self.db() as db:
            db.execute('UPDATE outbox SET state=?,receipt=? WHERE local_id=? AND msg_id=? AND body_hash=?',
                       ('SENT' if receipt else 'UNCERTAIN', receipt, contact['local_id'], contact['msg_id'], digest))


class BotSession:
    transport_name = 'QQ 官方机器人'
    is_official_bot = True

    def __init__(self, home, record):
        self.home, self.record = Path(home), record
        self.app_id = app_id(record['app_id'])
        self.store = ContactStore(home)
        self.events = queue.Queue(maxsize=200)
        self.stopped = threading.Event()
        self.status = '尚未连接'
        self.loop = self.client = None
        self.gateway_thread = self.dispatch_thread = None
        self.notification_sink_name = 'qq_official_' + self.app_id

    def start(self):
        if self.gateway_thread and self.gateway_thread.is_alive():
            return
        if self.dispatch_thread and self.dispatch_thread.is_alive():
            self.stopped.set()
            self.dispatch_thread.join(timeout=2)
            if self.dispatch_thread.is_alive():
                raise RuntimeError('上一轮模型调用尚未结束，已阻止重复连接，请稍后再试')
        self.stopped.clear()
        self.status = '正在连接官方服务…'
        self.gateway_thread = threading.Thread(target=self._gateway, daemon=True, name=self.notification_sink_name)
        self.dispatch_thread = threading.Thread(target=self._dispatch, daemon=True, name=self.notification_sink_name + '_chat')
        self.gateway_thread.start()
        self.dispatch_thread.start()

    def close(self):
        # Bridge owns no socket here; lifecycle shutdown is explicitly stop().
        pass

    def stop(self):
        self.stopped.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: [task.cancel() for task in asyncio.all_tasks(self.loop)])
        self.status = '已断开（凭据和会话保留）'
        from notification_manager import NotificationManager
        NotificationManager.get_instance().unregister_channel_sink(self.notification_sink_name)

    def next_event(self, timeout=0):
        try:
            return self.events.get(timeout=max(0, timeout))
        except queue.Empty:
            return None

    def enqueue(self, kind, channel, sender, message_id, content, timestamp=None):
        if kind in ('group', 'c2c'):
            from qq_group_connection import capture_link
            if capture_link(self.home, self.app_id, channel, message_id, content, kind=kind, timestamp=timestamp):
                self.status = '已连接；协作接入确认已处理'
                return
            from qq_group_team import capture_grant
            if capture_grant(self.home, self.app_id, channel, message_id, timestamp, kind=kind):
                self.status = '已连接；协作回复授权已交给主账号'
                return
        contact = self.store.incoming(self.app_id, kind, channel, sender, message_id)
        if not contact['enabled']:
            self.status = '已连接；发现新会话，请在下方授权后再发消息'
            return
        text = str(content or '').strip()
        if not text:
            return
        try:
            self.events.put_nowait(dict(post_type='message', message_type='private', self_id=self.app_id,
                user_id=contact['local_id'], message_id=message_id, time=time.time(),
                message=[{'type': 'text', 'data': {'text': text}}]))
        except queue.Full:
            self.status = '消息队列已满，本条未处理，请稍后重发'

    def _gateway(self):
        async def serve():
            import botpy
            session = self
            class Client(botpy.Client):
                async def on_ready(self):
                    session.status = '已连接官方服务，等待私聊或群内 @ 机器人'
                async def on_c2c_message_create(self, message):
                    await asyncio.to_thread(session.enqueue, 'c2c', message.author.user_openid,
                        message.author.user_openid, message.id, message.content, message.timestamp)
                async def on_group_at_message_create(self, message):
                    content = re.sub(r'^\s*<@!?\d+>\s*', '', message.content or '')
                    await asyncio.to_thread(session.enqueue, 'group', message.group_openid,
                        message.author.member_openid, message.id, content, message.timestamp)
            self.loop = asyncio.get_running_loop()
            self.client = Client(intents=botpy.Intents(public_messages=True), bot_log=False, ext_handlers=False)
            try:
                await self.client.start(appid=self.app_id, secret=load_secret(self.home, self.app_id))
            finally:
                await self.client.close()
        try:
            asyncio.run(serve())
        except asyncio.CancelledError:
            pass
        except Exception:
            self.status = '连接失败：请检查 AppID、AppSecret、官方后台事件权限及网络；密钥不会写入日志'
            self.stopped.set()

    def _dispatch(self):
        from main import Bridge
        bridge = Bridge(client=self)
        try:
            bridge.connect()
            next_tick = time.monotonic() + 2
            while not self.stopped.is_set():
                event = bridge.deferred.pop(0) if bridge.deferred else self.next_event(1)
                if event:
                    bridge.receive(event)
                if time.monotonic() >= next_tick:
                    bridge.tick()
                    next_tick = time.monotonic() + 2
        except Exception as error:
            self.status = '消息处理暂停：' + type(error).__name__ + '，请查看本地任务与会话记录后重新连接'

    def call(self, action, params=None, timeout=20):
        params = params or {}
        if action == 'get_login_info':
            if self.stopped.is_set():
                raise RuntimeError('机器人已断开')
            return {'user_id': self.app_id, 'nickname': self.record['name']}
        if action == 'get_friend_msg_history':
            return {'messages': []}  # Official API does not offer OneBot history sync.
        if action != 'send_private_msg':
            raise ValueError('官方机器人暂不支持此个人账号接口：' + str(action))
        pieces = params.get('message') or []
        if isinstance(pieces, str):
            text = pieces
        else:
            if any(part.get('type') not in ('text', 'reply') for part in pieces):
                raise ValueError('官方连接当前仅支持文字回复；图片、文件与语音需官方媒体上传接口，不能借用个人 QQ 发送')
            text = ''.join(str(part.get('data', {}).get('text', '')) for part in pieces if part.get('type') == 'text')
        if not text.strip() or len(text) > 1500:
            raise ValueError('官方文字消息须为 1–1500 字，请缩短回复或在工作台查看全文')
        if not self.loop or not self.loop.is_running() or not self.client:
            raise RuntimeError('官方服务尚未连接')
        contact, digest, prior = self.store.reserve(self.app_id, params.get('user_id'), text)
        if prior:
            return {'message_id': prior}
        async def send():
            common = dict(content=text, msg_type=0, msg_id=contact['msg_id'], msg_seq=contact['sequence'])
            if contact['kind'] == 'group':
                return await self.client.api.post_group_message(group_openid=contact['channel'], **common)
            return await self.client.api.post_c2c_message(openid=contact['channel'], **common)
        try:
            result = asyncio.run_coroutine_threadsafe(send(), self.loop).result(timeout=timeout)
            receipt = str((result or {}).get('id') or '')
            if not receipt:
                raise RuntimeError('官方接口未返回发送回执')
            self.store.receipt(contact, digest, receipt)
            return {'message_id': receipt}
        except Exception:
            self.store.receipt(contact, digest, '')
            raise RuntimeError('发送未确认，已保留记录；请核对 QQ，不会自动重试这条消息') from None


_sessions = {}


def connect(home, identifier):
    identifier = app_id(identifier)
    record = next((r for r in bots(home) if r['app_id'] == identifier), None)
    if not record:
        raise ValueError('请先保存机器人 AppID 与 AppSecret')
    key = (str(Path(home).resolve()), identifier)
    session = _sessions.get(key)
    if session and session.stopped.is_set() and any(thread and thread.is_alive()
            for thread in (session.gateway_thread, session.dispatch_thread)):
        raise RuntimeError('旧连接正在结束，未启动第二个实例；请稍后再试')
    if session is None or session.stopped.is_set():
        session = _sessions[key] = BotSession(home, record)
    session.start()
    return session


def status(home, identifier):
    session = _sessions.get((str(Path(home).resolve()), str(identifier)))
    return session.status if session else '尚未连接'


def stop(home, identifier=None):
    for (directory, app), session in list(_sessions.items()):
        if directory == str(Path(home).resolve()) and (identifier is None or str(identifier) == app):
            session.stop()


def start_configured(home):
    errors = []
    try:
        records = bots(home)
    except (OSError, ValueError):
        return ['官方机器人配置无法读取，请在连接页修复；个人 QQ 连接仍可使用。']
    for record in records:
        try:
            if record.get('enabled'):
                connect(home, record['app_id'])
        except Exception:
            errors.append('有一个官方机器人未能启动，请在连接页检查其配置；其他连接继续运行。')
    return errors
