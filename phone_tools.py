"""The phone's friend-house tools as a local stdio MCP server, so the model can ask the phone when it needs to.

2026-10-11 user:  — the computer's model calls the phone on demand (front app, today's usage,
the chosen apps' history, the words on the screen) instead of the phone streaming events. The tools themselves live
on the phone (McpServerService / PhoneContextTools); this process only carries the call over Tailscale with the
token the phone registered, and offers nothing the phone's friend-house set does not have.

Started by the Codex app-server per friend:  python -X utf8 -u phone_tools.py <friend directory>
"""
import json
import re
import sys
from pathlib import Path

import phone_client

TOOLS = [
    {"name": "phone_now", "description": "前台App、使用时长、亮屏/锁屏；不确定时看一次。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "phone_usage_today", "description": "今天各App用时前几名；不必每轮查。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "phone_screen_text", "description": "当前屏幕可见文字；锁屏/息屏/隐私页为空。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "app_history", "description": "最近1-2个App的话题/开头文字，手机保留最多3天。",
     "inputSchema": {"type": "object", "properties": {"package_name": {"type": "string", "description": "其中一个已选 App；留空表示全部已选 App。"},
                                                     "hours": {"type": "integer", "description": "回看多少小时（1–72，默认24）。"}},
                     "additionalProperties": False}},
    {"name": "set_history_apps", "description": "设置最多2个保留浏览文字的App；传空数组清除。",
     "inputSchema": {"type": "object", "properties": {"packages": {"type": "array", "items": {"type": "string"}}},
                     "required": ["packages"], "additionalProperties": False}},
]
NAMES = {tool['name'] for tool in TOOLS}
NO_ARGUMENTS = {'phone_now', 'phone_usage_today', 'phone_screen_text'}
HISTORY_ARGUMENTS = {'package_name', 'hours'}


def valid_arguments(name, arguments):
    """Shape only: the phone validates values (hours range, package names) and reports its own error."""
    if name in NO_ARGUMENTS:
        return not arguments
    if name == 'app_history':
        return all(key in HISTORY_ARGUMENTS and isinstance(value, (str, int)) for key, value in arguments.items())
    return (isinstance(arguments.get('packages'), list)
            and all(isinstance(package, str) for package in arguments['packages']))


# 2026-10-13 user (, , ): the phone keeps every visible label of a screen; the model only gets,
# per app, the #topics of the videos seen (most frequent first) and a short start of captions that have no #topic.
# Comments, account names, buttons and counts never reach it.
TAG = re.compile(r'#([^\s#>…]+)')
CAPTION = re.compile(r'(展开>?|>)$')  # a video caption line ends with the "" (expand) control
MAX_TAGS = 10
MAX_UNTAGGED = 3
UNTAGGED_CHARS = 12
MAX_RESULT_CHARS = 1600


def clean_history(text):
    """app_history JSON → a few lines per app of what was watched. Anything not in the expected shape is unchanged."""
    try:
        value = json.loads(text)
        entries, apps = value['entries'], value['apps']
    except (ValueError, KeyError, TypeError):
        return text
    found = {app: {'captions': set(), 'tags': {}, 'untagged': []} for app in apps}
    for entry in entries:
        item = found.setdefault(entry.get('app', ''), {'captions': set(), 'tags': {}, 'untagged': []})
        for line in str(entry.get('text', '')).split('\n'):
            line = line.strip()
            # phones from 2026-10-13 store only "#a #b" / "~start" per caption (AppHistory.topics); older ones raw text
            stored = line.startswith(('#', '~')) and not CAPTION.search(line)
            if not (stored or CAPTION.search(line)) or line in item['captions']:
                continue
            item['captions'].add(line)
            line = re.sub(r'\S*(?:\.\.\.|…)', '', line)  # the app cut the caption: its last word / #topic is partial
            tags = TAG.findall(line)
            for tag in tags:
                item['tags'][tag] = item['tags'].get(tag, 0) + 1
            start = CAPTION.sub('', line).strip(' .…~')[:UNTAGGED_CHARS]
            if not tags and start and len(item['untagged']) < MAX_UNTAGGED:
                item['untagged'].append(start)
    lines = []
    for app, item in found.items():
        if not item['captions']:
            lines.append(f'{app}：没有记录')
            continue
        tags = sorted(item['tags'], key=lambda tag: -item['tags'][tag])[:MAX_TAGS]
        lines.append(f"{app}（约 {len(item['captions'])} 个视频）：" + ' '.join('#' + tag for tag in tags)
                     + ('；' if tags and item['untagged'] else '') + '、'.join(item['untagged']))
    return '\n'.join(lines)


PASSING_APPS = ('launcher', 'com.tencent.mobileqq', 'com.android.systemui')


def note_now(text):
    """2026-10-13 user (): while chatting with us the friend's front app is QQ or the launcher they are
    switching through; the model is told that is not what they are doing and where to look instead."""
    try:
        package = json.loads(text).get('package_name', '')
    except (ValueError, AttributeError):
        return text
    if not any(name in package for name in PASSING_APPS):
        return text
    return (text + '\nNOTE:LAUNCHER_OR_QQ = FRIEND_SWITCHING_OR_REPLYING_US, NOT_WHAT_THEY_ARE_DOING; '
            'WHAT_THEY_WERE_DOING -> app_history|phone_usage_today')


def call_tool(directory, name, arguments):
    """One phone tool; needs a paired friend whose phone registered its endpoint."""
    if name not in NAMES or not isinstance(arguments, dict) or not valid_arguments(name, arguments):
        raise ValueError('手机工具参数无效')
    session = phone_client.load_session(Path(directory) / 'friend.json')
    if not phone_client.configured(session):
        return {'status': '手机还没连上这位好友的小屋：请在该好友的「小屋状态」页重新配对手机 App'}
    if not session.get('bridge_enabled', True):
        return {'status': '当前好友已暂停（paused），未查询手机'}
    text = phone_client.trim_warning(phone_client.client_for(session).call(name, arguments))
    text = clean_history(text) if name == 'app_history' else note_now(text) if name == 'phone_now' else text
    if name in ('phone_screen_text', 'phone_usage_today') and len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS].rstrip() + '…'
    return {'result': text}


def serve(directory):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    from mcp_stdio import requests, protocol_version
    for request in requests(sys.stdin):
        method, params = request['method'], request.get('params', {})
        try:
            if method == 'initialize':
                result = {'protocolVersion': protocol_version(params), 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'qq_phone', 'version': '1.0.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{**tool, 'annotations': {'readOnlyHint': tool['name'] != 'set_history_apps',
                                                             'destructiveHint': False, 'openWorldHint': False}}
                                    for tool in TOOLS]}
            elif method == 'tools/call':
                result = {'content': [{'type': 'text', 'text': json.dumps(call_tool(directory, params.get('name'),
                                                                                    params.get('arguments')),
                                                                          ensure_ascii=False)}], 'isError': False}
            elif method in ('resources/list', 'prompts/list'):
                result = {'resources' if method == 'resources/list' else 'prompts': []}
            else:
                raise ValueError('不支持的请求')
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except phone_client.PhoneUnavailable as exc:
            # A phone that is off, locked away or off the tailnet must not break the turn: report and move on.
            reply = {'jsonrpc': '2.0', 'id': request['id'],
                     'result': {'content': [{'type': 'text', 'text': json.dumps({'status': str(exc)}, ensure_ascii=False)}],
                                'isError': False}}
        except Exception as exc:
            message = str(exc) if isinstance(exc, (phone_client.PhoneToolError, ValueError)) else '手机工具调用失败'
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32602, 'message': message}}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    serve(Path(sys.argv[1]).resolve())
