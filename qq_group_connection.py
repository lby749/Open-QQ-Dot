"""Authenticated group setup; QQ login and bot secrets stay in the host connector."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.request import Request, build_opener

from qq_official import OfficialGroupSender, _NoRedirect, app_id


def _number(value):
    value = str(value).strip()
    if not re.fullmatch(r'\d{5,12}', value):
        raise ValueError('QQ 账号和群号必须是有效数字')
    return value


def _varint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('协议整数越界')
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def _message(*pairs):
    out = bytearray()
    for field, value in pairs:
        if not 0 < field < 2**29:
            raise ValueError('协议字段无效')
        if type(value) is int:
            out.extend(_varint(field << 3) + _varint(value))
        else:
            value = value.encode('utf-8') if isinstance(value, str) else value
            if not isinstance(value, bytes):
                raise ValueError('协议字段类型无效')
            out.extend(_varint((field << 3) | 2) + _varint(len(value)) + value)
    return bytes(out)


def _fields(data):
    if not isinstance(data, bytes) or len(data) > 1048576:
        raise ValueError('群操作响应过大或格式无效')
    cursor = 0
    def read():
        nonlocal cursor
        value = 0
        for shift in range(0, 70, 7):
            if cursor >= len(data):
                raise ValueError('群操作响应不完整')
            byte = data[cursor]
            cursor += 1
            value |= (byte & 127) << shift
            if byte < 128 and value < 2**64:
                return value
        raise ValueError('群操作响应整数越界')
    result = {}
    while cursor < len(data):
        key = read()
        field, kind = key >> 3, key & 7
        if not field:
            raise ValueError('群操作响应字段无效')
        if kind == 0:
            value = read()
        else:
            length = read() if kind == 2 else {1: 8, 5: 4}.get(kind)
            if length is None or cursor + length > len(data):
                raise ValueError('群操作响应字段无效')
            value = data[cursor:cursor + length]
            cursor += length
        result.setdefault(field, []).append(value)
    return result


class GroupConnection:
    def __init__(self, home, owner, self_id, client):
        self.home, self.owner = Path(home), str(owner)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.owner) or not (self.home / 'friends' / self.owner / 'friend.json').is_file():
            raise PermissionError('群操作必须绑定已有好友')
        self.self_id, self.client = _number(self_id), client
        folder = self.home / 'data'
        folder.mkdir(exist_ok=True)
        self.path = folder / 'qq_group_connections.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS operations (
                owner TEXT, self_id TEXT, operation TEXT, payload TEXT, state TEXT,
                receipt TEXT DEFAULT '', updated REAL, PRIMARY KEY(owner,self_id,operation))''')
            db.execute('''CREATE TABLE IF NOT EXISTS links (
                nonce TEXT, owner TEXT, self_id TEXT, group_id TEXT, app TEXT, bot TEXT,
                channel TEXT DEFAULT '', state TEXT, expires REAL, PRIMARY KEY(nonce,app))''')
            if 'error' not in {row[1] for row in db.execute('PRAGMA table_info(links)')}:
                db.execute("ALTER TABLE links ADD COLUMN error TEXT DEFAULT ''")
            if 'kind' not in {row[1] for row in db.execute('PRAGMA table_info(links)')}:
                db.execute("ALTER TABLE links ADD COLUMN kind TEXT NOT NULL DEFAULT 'group'")

    def _online(self):
        state = self.client.call('get_status') or {}
        if state.get('online') is not True or state.get('good') is False:
            raise ConnectionError('主 QQ 尚未在线，未执行群操作')
        if str((self.client.call('get_login_info') or {}).get('user_id')) != self.self_id:
            raise PermissionError('当前主 QQ 与群操作绑定不一致')

    def _manage(self, group):
        self._online()
        group = _number(group)
        info = self.client.call('get_group_member_info', {
            'group_id': int(group), 'user_id': int(self.self_id), 'no_cache': True}) or {}
        if info.get('role') not in ('owner', 'admin'):
            raise PermissionError('当前主 QQ 不是目标群的群主或管理员')
        return group

    def group_bots(self, group):
        group = self._manage(group)
        cookies = (self.client.call('get_cookies', {'domain': 'q.qq.com'}) or {}).get('cookies') or ''
        values = dict(part.strip().split('=', 1) for part in cookies.split(';') if '=' in part)
        if not values.get('skey'):
            raise ConnectionError('当前 QQ 的群管理会话尚未就绪')
        token = 5381
        for char in values['skey']:
            token = (token * 33 + ord(char)) & 0x7fffffff
        request = Request('https://q.qq.com/lite/query_group_botlist?bkn=' + str(token),
            data=json.dumps({'group_id': int(group)}).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Cookie': cookies,
                     'Origin': 'https://q.qq.com', 'Referer': 'https://q.qq.com/qqbot/openclaw/'})
        with build_opener(_NoRedirect()).open(request, timeout=12) as response:
            raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('群机器人列表响应过大')
        data = json.loads(raw)
        if data.get('retcode') != 0:
            raise ConnectionError('未取得目标群的官方机器人列表')
        return [{'qq_id': _number(row['bot_uin']), 'name': str(row.get('bot_name') or '')[:80]}
                for row in (data.get('data') or {}).get('bot_infos', [])]

    def _claim(self, operation, payload):
        if not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', str(operation)):
            raise ValueError('群操作需要稳定的操作编号')
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload,state,receipt FROM operations WHERE owner=? AND self_id=? AND operation=?',
                             (self.owner, self.self_id, operation)).fetchone()
            if old:
                if old[0] != encoded:
                    raise ValueError('操作编号已用于其他群操作')
                return {'state': old[1], 'receipt': old[2]}
            prior = db.execute('''SELECT state,receipt FROM operations WHERE owner=? AND self_id=?
                AND payload=? AND state!='REJECTED' ORDER BY updated DESC LIMIT 1''',
                (self.owner, self.self_id, encoded)).fetchone()
            if prior:
                return {'state': prior[0], 'receipt': prior[1]}
            db.execute('INSERT INTO operations(owner,self_id,operation,payload,state,updated) VALUES(?,?,?,?,?,?)',
                       (self.owner, self.self_id, operation, encoded, 'SENDING', time.time()))
        return None

    def _record(self, operation, state, receipt=''):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('UPDATE operations SET state=?,receipt=?,updated=? WHERE owner=? AND self_id=? AND operation=?',
                       (state, receipt, time.time(), self.owner, self.self_id, operation))
        return {'state': state, 'receipt': receipt}

    def _oidb(self, command, body, subcommand=1):
        request = _message((1, command), (2, subcommand), (4, body), (12, 1))
        raw = self.client.call('send_packet', {'cmd': f'OidbSvcTrpcTcp.0x{command:x}_{subcommand}',
                                             'data': request.hex(), 'rsp': True}, timeout=15)
        if not isinstance(raw, str) or len(raw) > 2097152:
            raise ValueError('群操作未取得有效回执，不能直接重试')
        response = _fields(bytes.fromhex(raw))
        if not response:
            raise ValueError('连接层未返回群操作回执，未确认成功；不会自动重试')
        if response.get(1) != [command] or response.get(2) != [subcommand]:
            raise ValueError('群操作回执与请求不匹配：' + str({key: response.get(key) for key in (1, 2, 3)}))
        if response.get(3, [0])[0] != 0:
            reason = response.get(5, [b''])[0]
            reason = reason.decode('utf-8', errors='replace')[:160] if isinstance(reason, bytes) else ''
            raise PermissionError('QQ 拒绝了群操作，返回码：' + str(response[3][0]) + ('；' + reason if reason else ''))
        return response.get(4, [b''])[0]

    def create_group(self, name, operation):
        self._online()
        name = str(name).strip()
        if not name or len(name.encode('utf-8')) > 60 or any(ord(char) < 32 for char in name):
            raise ValueError('群名称须为1至60字节，不含控制字符')
        old = self._claim(operation, {'action': 'create_group', 'name': name, 'protocol': 'nt-uid-lowercase'})
        if old:
            return {**old, 'group_id': old['receipt']}
        identity = self.client.call('get_stranger_info', {'user_id': int(self.self_id)}) or {}
        creator = identity.get('uid')
        if not isinstance(creator, str) or not re.fullmatch(r'u_[A-Za-z0-9_-]{8,64}', creator):
            self._record(operation, 'REJECTED')
            raise ValueError('未取得当前 QQ 的有效身份标识，未建群')
        # NT encoder fields; creation is confirmed only by a returned group ID and live owner check.
        attributes = _message((1, 0), (2, creator), (3, 1), (4, 0), (5, 0), (6, 0),
                              (7, 200000007), (8, 1), (9, 16), (10, name), (25, 1))
        try:
            body = self._oidb(0x8a1, _message((1, 1), (2, attributes)), subcommand=7)
            group = _number(_fields(body).get(2, [''])[0])
        except PermissionError:
            self._record(operation, 'REJECTED')
            raise
        except Exception:
            self._record(operation, 'UNCERTAIN')
            raise
        self._record(operation, 'ACKNOWLEDGED', group)
        # A positive protocol receipt is retained even if list synchronization is still pending.
        self._manage(group)
        result = self._record(operation, 'VERIFIED', group)
        return {**result, 'group_id': group, 'group_name': name}

    def add_bot(self, group, identifier, operation):
        group, identifier = self._manage(group), app_id(identifier)
        profile = OfficialGroupSender(self.home, identifier).invitation()
        bot = profile['qq_id']
        if any(row['qq_id'] == bot for row in self.group_bots(group)):
            return {'state': 'VERIFIED', 'group_id': group, 'app_id': identifier, 'qq_id': bot}
        old = self._claim(operation, {'action': 'add_bot', 'group': group, 'app': identifier, 'qq': bot})
        if old:
            return old  # Unknown or acknowledged external effects are never resent.
        try:
            self._oidb(0x9076, _message((1, int(group)), (2, int(bot))))
        except Exception:
            self._record(operation, 'UNCERTAIN')
            raise
        self._record(operation, 'ACKNOWLEDGED')
        verified = any(row['qq_id'] == bot for row in self.group_bots(group))
        result = self._record(operation, 'VERIFIED' if verified else 'ACKNOWLEDGED', bot)
        return {**result, 'group_id': group, 'app_id': identifier, 'qq_id': bot}

    def connect_room(self, room_id, operation, progress=lambda _: None):
        import agent_group
        import qq_group_team
        base = self.home / 'data'
        room = agent_group.load_room(base, self.owner, room_id)
        scope = room.get('qq_group_setup') or {}
        if scope.get('self_id') != self.self_id or not scope.get('group_id'):
            raise PermissionError('请先在这个协作群中保存主 QQ 和目标群号')
        group = scope['group_id']
        identifiers = list(scope['bots'].values())
        for identifier in identifiers:
            progress('正在核对并邀请机器人 ' + identifier)
            result = self.add_bot(group, identifier, operation + ':' + identifier)
            if result['state'] != 'VERIFIED':
                return result
        progress('正在自动确认各机器人的群连接')
        result = self.link_bots(group, identifiers, operation + ':link')
        if result['state'] != 'VERIFIED':
            return result
        latest = agent_group.load_room(base, self.owner, room_id)
        if latest.get('qq_group_setup') != scope:
            raise PermissionError('群配置已改变，未应用旧连接；原邀请记录已保留')
        qq_group_team.save_binding(base, self.owner, room_id, {
            'enabled': True, 'self_id': self.self_id, 'group_id': group, 'operators': scope['members'],
            'bots': {member: {'app_id': identifier, 'group_openid': result['channels'][identifier]}
                     for member, identifier in scope['bots'].items()}})
        return {'state': 'VERIFIED', 'group_id': group, 'connected_bots': len(identifiers)}

    def link_bots(self, group, identifiers, operation, *, timeout=25):
        return self._link('group', self._manage(group), identifiers, operation, timeout)

    def link_private(self, identifiers, operation, *, timeout=25):
        self._online()
        return self._link('c2c', '', identifiers, operation, timeout)

    def _link(self, kind, group, identifiers, operation, timeout):
        """Correlate both transports, rather than guessing OpenIDs from a group number."""
        identifiers = list(dict.fromkeys(app_id(value) for value in identifiers))
        if not 1 <= len(identifiers) <= 5:
            raise ValueError('每次连接1至5个官方机器人')
        profiles = [OfficialGroupSender(self.home, value).invitation() for value in identifiers]
        if kind == 'group':
            # The logged-in native account already exposes the actual roster. Do not
            # require a separate q.qq.com cookie/network session just to confirm membership.
            roster = self.client.call('get_group_member_list', {'group_id': int(group)})
            if not isinstance(roster, list):
                raise ValueError('未取得目标群成员列表，未发送接入请求')
            present = {str(row.get('user_id')) for row in roster if isinstance(row, dict)}
            if any(profile['qq_id'] not in present for profile in profiles):
                raise ValueError('所选机器人尚未全部加入目标群')
        old = self._claim(operation, {'action': 'link_bots', 'kind': kind, 'group': group, 'apps': sorted(identifiers)})
        if old:
            return {**old, 'channels': json.loads(old['receipt']) if old['state'] == 'VERIFIED' else {}}
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            known = {identifier: db.execute("""SELECT channel FROM links WHERE owner=? AND self_id=?
                AND group_id=? AND app=? AND kind=? AND state='VERIFIED' ORDER BY rowid DESC LIMIT 1""",
                (self.owner, self.self_id, group, identifier, kind)).fetchone() for identifier in identifiers}
        if all(known.values()):
            channels = {identifier: row[0] for identifier, row in known.items()}
            self._record(operation, 'VERIFIED', json.dumps(channels, sort_keys=True))
            return {'state': 'VERIFIED', 'receipt': json.dumps(channels, sort_keys=True), 'channels': channels}
        nonce = secrets.token_hex(16)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.executemany('INSERT INTO links(nonce,owner,self_id,group_id,app,bot,state,expires,kind) VALUES(?,?,?,?,?,?,?,?,?)',
                [(nonce, self.owner, self.self_id, group, profile['app_id'], profile['qq_id'],
                  'WAITING', time.time() + 60, kind) for profile in profiles])
        marker = '〔QQDOT:' + nonce + '〕'
        segments = [{'type': 'at', 'data': {'qq': profile['qq_id']}} for profile in profiles]
        segments.append({'type': 'text', 'data': {'text': ' 请确认接入本群的协作连接。' + marker}})
        try:
            if kind == 'group':
                self.client.send_group_segments(group, segments)
            else:
                for profile in profiles:
                    result = self.client.call('send_private_msg', {'user_id': int(profile['qq_id']),
                        'message': [{'type': 'text', 'data': {'text': '请确认与主账号的协作汇报连接。' + marker}}]})
                    if not isinstance(result, dict) or not result.get('message_id'):
                        raise RuntimeError('私聊连接请求没有发送回执，不自动重发')
        except Exception:
            self._record(operation, 'UNCERTAIN')
            raise
        deadline = time.monotonic() + max(1, min(timeout, 30))
        while time.monotonic() < deadline:
            event = self.client.next_event(timeout=0.5)
            if not isinstance(event, dict) or event.get('message_type') != ('group' if kind == 'group' else 'private'):
                continue
            if str(event.get('self_id')) != self.self_id or (kind == 'group' and str(event.get('group_id')) != group):
                continue
            text = ''.join(str(item.get('data', {}).get('text') or '') for item in event.get('message', [])
                           if isinstance(item, dict) and item.get('type') == 'text')
            sender = str(event.get('user_id') or (event.get('sender') or {}).get('user_id') or '')
            if text.strip() != ('我已接入本群，可以开始协作了。' if kind == 'group' else '已连接主账号，后续可以在这里汇报。') + marker:
                continue
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("UPDATE links SET state='VERIFIED' WHERE nonce=? AND bot=? AND owner=? AND self_id=? AND group_id=? AND kind=? AND channel!='' AND state IN ('SENDING','BOT_ACKNOWLEDGED')",
                           (nonce, sender, self.owner, self.self_id, group, kind))
                rows = db.execute("SELECT app,channel FROM links WHERE nonce=? AND state='VERIFIED'", (nonce,)).fetchall()
            if len(rows) == len(profiles):
                channels = {identifier: channel for identifier, channel in rows}
                result = self._record(operation, 'VERIFIED', json.dumps(channels, sort_keys=True))
                return {**result, 'channels': channels}
        self._record(operation, 'UNCERTAIN')
        raise TimeoutError('尚未收到所有机器人的接入确认；已保留记录，不会自动重试')


def capture_link(home, identifier, channel, message_id, content, *, kind='group', timestamp=None):
    """Consume only an explicit, live setup challenge; never grant chat/tool permissions."""
    path = Path(home) / 'data/qq_group_connections.sqlite3'
    match = re.search(r'〔QQDOT:([a-f0-9]{32})〕', str(content or ''))
    if not match or not path.is_file():
        return False
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if 'kind' not in {row[1] for row in db.execute('PRAGMA table_info(links)')}:
            db.execute("ALTER TABLE links ADD COLUMN kind TEXT NOT NULL DEFAULT 'group'")
        row = db.execute('SELECT state FROM links WHERE nonce=? AND app=? AND kind=? AND expires>?',
                         (match[1], str(identifier), kind, time.time())).fetchone()
        if not row:
            return False
        if row[0] != 'WAITING':
            return True
        db.execute("UPDATE links SET channel=?,state='SENDING' WHERE nonce=? AND app=?",
                   (str(channel), match[1], str(identifier)))
    try:
        sender = OfficialGroupSender(home, identifier)
        reply = sender.send if kind == 'group' else sender.send_private
        receipt = reply(channel, ('我已接入本群，可以开始协作了。' if kind == 'group' else
                       '已连接主账号，后续可以在这里汇报。') + '〔QQDOT:' + match[1] + '〕', message_id, 1)
        if not receipt:
            return True  # Do not replay an uncertain external send.
    except Exception as error:
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            # Opaque IDs and remote error bodies must not appear in UI/log diagnostics.
            db.execute("UPDATE links SET error=? WHERE nonce=? AND app=?",
                       (type(error).__name__, match[1], str(identifier)))
        return True
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("UPDATE links SET state='BOT_ACKNOWLEDGED' WHERE nonce=? AND app=? AND state='SENDING'",
                   (match[1], str(identifier)))
    if kind == 'c2c':
        from qq_group_team import capture_grant
        capture_grant(home, identifier, channel, message_id, timestamp, kind=kind, used=1)
    return True


def save_setup(base, owner, room_id, config):
    """Host UI grants a room-specific scope; model arguments cannot expand it."""
    import agent_group
    import qq_official
    room = agent_group.load_room(base, owner, room_id)
    workers = {m['id'] for m in agent_group.active_bot_members(room)}
    if not room.get('host_id') or not 1 <= len(workers) <= agent_group.MAX_QQ_BOTS:
        raise ValueError('先设置主持人及1至5名机器人工作成员')
    bots = {str(member): app_id(identifier) for member, identifier in config.get('bots', {}).items() if str(member) in workers}
    available = {r['app_id'] for r in qq_official.bots(Path(base).parent)}
    if set(bots) != workers or len(set(bots.values())) != len(workers) or not set(bots.values()) <= available:
        raise ValueError('每名工作成员须选择一只已配置的独立机器人')
    members = list(dict.fromkeys(_number(v) for v in config.get('members', [])))
    if not members:
        raise ValueError('请指定可在群内发起协作的成员')
    room['qq_group_setup'] = {'enabled': bool(config.get('enabled')), 'self_id': _number(config.get('self_id', '')),
        'group_id': _number(config['group_id']) if config.get('group_id') else '', 'members': members, 'bots': bots}
    return agent_group.save_room(base, owner, room)


def tools(directory):
    import agent_group
    directory = Path(directory)
    from house_settings import root
    home = root().resolve()
    if directory.resolve().parent != (home / 'friends').resolve():
        return []
    if not any((agent_group.load_room(home / 'data', directory.name, r['id']).get('qq_group_setup') or {}).get('enabled')
               for r in agent_group.list_rooms(home / 'data', directory.name)):
        return []
    return [{'name': 'qq_manage_group', 'description': '为当前好友已授权的协作房间建群或自动接入所选官方机器人，不主动邀请普通好友。'
             '只使用房间内保存的账号、群和机器人；不读取密钥。operation_id须在同一任务重试时保持不变；结果不确定时不得换编号重试。建群尚待真实环境验收。',
             'inputSchema': {'type':'object', 'properties': {
                 'room_id': {'type':'string', 'maxLength':64}, 'operation_id': {'type':'string','pattern':'^[A-Za-z0-9_:-]{1,96}$'},
                 'action': {'type':'string', 'enum':['create','connect']},
                 'name': {'type':'string','maxLength':60}},
                 'required':['room_id','operation_id','action'], 'additionalProperties':False}}]


def call(directory, arguments):
    import agent_group
    from house_settings import root
    from qq_link import QQClient
    from task_runtime import current_execution
    directory, home = Path(directory).resolve(), root().resolve()
    if directory.parent != (home / 'friends').resolve():
        raise PermissionError('群管理只对当前好友目录开放')
    execution = current_execution.get()
    if execution:
        execution.check()
        if str(execution.owner_id) != directory.name:
            raise PermissionError('群管理与当前任务归属不一致')
    room = agent_group.load_room(home / 'data', directory.name, arguments['room_id'])
    scope = room.get('qq_group_setup') or {}
    if not scope.get('enabled'):
        raise PermissionError('这个房间未授权群管理工具')
    action = arguments['action']
    if action not in ('create', 'connect'):
        raise ValueError('仅支持新建QQ群和接入机器人')
    client = QQClient()
    try:
        service = GroupConnection(home, directory.name, scope['self_id'], client)
        operation = hashlib.sha256((directory.name + ':' + room['id'] + ':' + arguments['operation_id']).encode()).hexdigest()
        if action == 'connect':
            return service.connect_room(room['id'], operation)
        if action != 'create' or scope.get('group_id'):
            raise PermissionError('这个房间已有目标群，不能重复建群')
        result = service.create_group(arguments.get('name', ''), operation)
        if result['state'] == 'VERIFIED':
            latest = agent_group.load_room(home / 'data', directory.name, room['id'])
            if latest.get('qq_group_setup') != scope:
                raise PermissionError('群配置已改变，已创建的群号保留在操作记录中')
            scope['group_id'] = result['group_id']
            save_setup(home / 'data', directory.name, room['id'], scope)
        return result
    finally:
        client.close()
