"""Short-lived friend-scoped requests, carried by the phone's outbound connection.

Lives in the house web process; model subprocesses submit through its loopback API.
Requests and results stay in memory and are removed on completion or timeout.
"""
import secrets
import threading
import time

WAIT_SECONDS = 15
POLL_SECONDS = 10
_changed = threading.Condition()
_pending = {}


def cancel(qq):
    with _changed:
        task = _pending.pop(qq, None)
        if task:
            task['done'].set()
        _changed.notify_all()


def query(qq, token, name, arguments):
    task = {'id': secrets.token_urlsafe(18), 'token': token, 'name': name, 'arguments': arguments,
            'deadline': time.monotonic() + WAIT_SECONDS, 'delivered': False,
            'done': threading.Event(), 'result': None}
    with _changed:
        if qq in _pending:
            raise ValueError('这台手机正在处理查询，请稍后再试')
        _pending[qq] = task
        _changed.notify_all()
    try:
        task['done'].wait(WAIT_SECONDS)
        if task['result'] is None:
            raise TimeoutError('手机暂不可用：查询超时或配对已变更')
        return task['result']
    finally:
        with _changed:
            if _pending.get(qq) is task:
                del _pending[qq]


def poll(qq, token):
    until = time.monotonic() + POLL_SECONDS
    with _changed:
        while True:
            task = _pending.get(qq)
            if (task and task['token'] == token and not task['delivered']
                    and task['deadline'] > time.monotonic()):
                task['delivered'] = True
                return {'id': task['id'], 'name': task['name'], 'arguments': task['arguments']}
            remaining = until - time.monotonic()
            if remaining <= 0:
                return {}
            _changed.wait(remaining)


def result(qq, token, body):
    answer = body.get('result')
    if (not isinstance(answer, dict) or not isinstance(answer.get('content'), list)
            or not all(isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)
                       for item in answer['content'])
            or 'isError' in answer and not isinstance(answer['isError'], bool)):
        raise ValueError('手机结果格式无效')
    with _changed:
        task = _pending.get(qq)
        if (not task or task['id'] != body.get('id') or task['token'] != token
                or not task['delivered'] or task['done'].is_set() or task['deadline'] <= time.monotonic()):
            raise LookupError('查询已过期或不属于当前配对')
        task['result'] = answer
        task['done'].set()
    return {'ok': True}
